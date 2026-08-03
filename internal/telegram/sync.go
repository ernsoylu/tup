package telegram

import (
	"context"
	"database/sql"
	"fmt"
	"path"

	"github.com/ernsoylu/tup/internal/core"
	"github.com/gotd/td/tg"
	"github.com/pterm/pterm"
)

type ConflictError struct {
	Alias       string
	Path        string
	Type        string
	LocalMsgID  int
	RemoteMsgID int
}

func (e *ConflictError) Error() string {
	return fmt.Sprintf("sync conflict detected in drive '%s' on path '%s' (%s: local #%d vs remote #%d)",
		e.Alias, e.Path, e.Type, e.LocalMsgID, e.RemoteMsgID)
}

// GetSyncState retrieves the last synced message ID and head commit hash for an alias
func GetSyncState(alias string) (int, string, error) {
	var lastMsgID int
	var headHash sql.NullString
	err := core.DB.QueryRow("SELECT last_synced_msg_id, head_hash FROM sync_state WHERE alias = ?", alias).
		Scan(&lastMsgID, &headHash)
	if err != nil {
		if err == sql.ErrNoRows {
			return 0, "", nil
		}
		return 0, "", err
	}
	return lastMsgID, headHash.String, nil
}

// UpdateSyncState records the latest synced message ID and head commit hash
func UpdateSyncState(alias string, lastMsgID int, headHash string) error {
	_, err := core.DB.Exec(`
		INSERT INTO sync_state (alias, last_synced_msg_id, head_hash, last_synced_at)
		VALUES (?, ?, ?, CURRENT_TIMESTAMP)
		ON CONFLICT(alias) DO UPDATE SET
			last_synced_msg_id = excluded.last_synced_msg_id,
			head_hash = excluded.head_hash,
			last_synced_at = CURRENT_TIMESTAMP
	`, alias, lastMsgID, headHash)
	return err
}

// SyncDrive performs bulk incremental auto-sync by fetching all missing operations and applying them in a single batch transaction.
func SyncDrive(ctx context.Context, alias string) error {
	chatID, err := core.GetChatID(alias)
	if err != nil {
		return err
	}

	lastMsgID, headHash, err := GetSyncState(alias)
	if err != nil {
		return fmt.Errorf("failed to read sync state: %w", err)
	}

	return Run(ctx, func(ctx context.Context) error {
		api := Client.API()
		peer, err := resolvePeer(ctx, api, chatID)
		if err != nil {
			return fmt.Errorf("peer resolution failed: %w", err)
		}

		// Safety check: verify if top message ID in chat is smaller than local lastMsgID (chat was cleared/recreated)
		if lastMsgID > 0 {
			latestRes, err := api.MessagesGetHistory(ctx, &tg.MessagesGetHistoryRequest{
				Peer:  peer,
				Limit: 1,
			})
			if err == nil {
				var topMsgID int
				switch m := latestRes.(type) {
				case *tg.MessagesMessages:
					if len(m.Messages) > 0 {
						topMsgID = m.Messages[0].GetID()
					}
				case *tg.MessagesMessagesSlice:
					if len(m.Messages) > 0 {
						topMsgID = m.Messages[0].GetID()
					}
				case *tg.MessagesChannelMessages:
					if len(m.Messages) > 0 {
						topMsgID = m.Messages[0].GetID()
					}
				}
				if topMsgID > 0 && topMsgID < lastMsgID {
					pterm.Warning.Printf("Chat history reset detected (top msg #%d < local #%d). Clearing local VFS cache...\n", topMsgID, lastMsgID)
					_, _ = core.DB.Exec("DELETE FROM vfs_entries WHERE alias = ?", alias)
					_, _ = core.DB.Exec("DELETE FROM vfs_conflicts WHERE alias = ?", alias)
					_, _ = core.DB.Exec("DELETE FROM vfs_operations_log WHERE alias = ?", alias)
					lastMsgID = 0
					headHash = ""
					_ = UpdateSyncState(alias, 0, "")
				}
			}
		}

		// 1. Bulk Download: Collect all missing operation messages from Telegram history into memory
		var collectedOps []*Operation
		currentMinID := lastMsgID

		for {
			res, err := api.MessagesGetHistory(ctx, &tg.MessagesGetHistoryRequest{
				Peer:  peer,
				MinID: currentMinID,
				Limit: 100,
			})
			if err != nil {
				return fmt.Errorf("failed to fetch chat history: %w", err)
			}

			var messages []tg.MessageClass
			switch m := res.(type) {
			case *tg.MessagesMessages:
				messages = m.Messages
			case *tg.MessagesMessagesSlice:
				messages = m.Messages
			case *tg.MessagesChannelMessages:
				messages = m.Messages
			}

			if len(messages) == 0 {
				break
			}

			foundNewInBatch := false
			// Process batch in chronological order (oldest to newest)
			for i := len(messages) - 1; i >= 0; i-- {
				msg, ok := messages[i].(*tg.Message)
				if !ok || msg.ID <= currentMinID {
					continue
				}

				text := msg.Message
				if text == "" {
					continue
				}

				op, err := DecodeOperation(text)
				if err != nil {
					continue
				}

				op.MessageID = msg.ID
				collectedOps = append(collectedOps, op)
				currentMinID = msg.ID
				foundNewInBatch = true
			}

			if !foundNewInBatch || len(messages) < 100 {
				break
			}
		}

		if len(collectedOps) == 0 {
			return nil
		}

		pterm.Info.Printf("Syncing drive '%s' (%d new operations)...\n", alias, len(collectedOps))

		// 2. Conflict Checking
		for _, op := range collectedOps {
			if op.PrevHash != "" && headHash != "" && op.PrevHash != headHash {
				var localMsgID int
				err := core.DB.QueryRow("SELECT message_id FROM vfs_entries WHERE alias = ? AND name = ?", alias, path.Base(op.Path)).Scan(&localMsgID)
				if err == nil && localMsgID != 0 && localMsgID != op.MessageID {
					conflictType := "MODIFY_MODIFY"
					if op.Op == OpRM {
						conflictType = "MODIFY_DELETE"
					}
					_, _ = core.DB.Exec(`
						INSERT OR REPLACE INTO vfs_conflicts (alias, path, conflict_type, local_msg_id, local_hash, remote_msg_id, remote_hash)
						VALUES (?, ?, ?, ?, ?, ?, ?)
					`, alias, op.Path, conflictType, localMsgID, headHash, op.MessageID, op.Hash)

					return &ConflictError{
						Alias:       alias,
						Path:        op.Path,
						Type:        conflictType,
						LocalMsgID:  localMsgID,
						RemoteMsgID: op.MessageID,
					}
				}
			}
			headHash = op.Hash
		}

		// 3. Bulk Batch Database Execution: Apply all operations in ONE single SQLite transaction
		if err := ApplyBatchOperations(alias, collectedOps); err != nil {
			return fmt.Errorf("failed to apply operation batch: %w", err)
		}

		lastMsgID = collectedOps[len(collectedOps)-1].MessageID
		headHash = collectedOps[len(collectedOps)-1].Hash
		_ = UpdateSyncState(alias, lastMsgID, headHash)

		return nil
	})
}

// ApplyOperation calls ApplyBatchOperations for a single operation.
func ApplyOperation(alias string, op *Operation) error {
	return ApplyBatchOperations(alias, []*Operation{op})
}

// ApplyBatchOperations executes a slice of operations sequentially inside a SINGLE SQLite transaction for maximum speed.
func ApplyBatchOperations(alias string, ops []*Operation) error {
	if len(ops) == 0 {
		return nil
	}

	tx, err := core.DB.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	stmtCP, err := tx.Prepare(`
		INSERT INTO vfs_entries (alias, parent_id, name, is_dir, size, sha256, message_id)
		VALUES (?, ?, ?, 0, ?, ?, ?)
		ON CONFLICT(alias, parent_id, name) DO UPDATE SET
			size = excluded.size,
			sha256 = excluded.sha256,
			message_id = excluded.message_id
	`)
	if err != nil {
		return err
	}
	defer func() { _ = stmtCP.Close() }()

	stmtMKDIR, err := tx.Prepare(`
		INSERT INTO vfs_entries (alias, parent_id, name, is_dir, size, sha256, message_id)
		VALUES (?, ?, ?, 1, 0, '', ?)
		ON CONFLICT(alias, parent_id, name) DO NOTHING
	`)
	if err != nil {
		return err
	}
	defer func() { _ = stmtMKDIR.Close() }()

	stmtRM, err := tx.Prepare("DELETE FROM vfs_entries WHERE alias = ? AND name = ?")
	if err != nil {
		return err
	}
	defer func() { _ = stmtRM.Close() }()

	stmtMV, err := tx.Prepare("UPDATE vfs_entries SET name = ? WHERE alias = ? AND name = ?")
	if err != nil {
		return err
	}
	defer func() { _ = stmtMV.Close() }()

	stmtLog, err := tx.Prepare(`
		INSERT OR IGNORE INTO vfs_operations_log (alias, msg_id, hash, prev_hash, op_type, path, target_path, size, sha256, timestamp)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`)
	if err != nil {
		return err
	}
	defer func() { _ = stmtLog.Close() }()

	for _, op := range ops {
		dir, fileName := path.Split(path.Clean(op.Path))
		dir = path.Clean(dir)

		parentID := 0
		if dir != "." && dir != "/" {
			_ = tx.QueryRow("SELECT id FROM vfs_entries WHERE alias = ? AND name = ? AND is_dir = 1", alias, path.Base(dir)).Scan(&parentID)
		}

		switch op.Op {
		case OpCP:
			if _, err := stmtCP.Exec(alias, parentID, fileName, op.Size, op.Sha256, op.MessageID); err != nil {
				return err
			}

		case OpMKDIR:
			if _, err := stmtMKDIR.Exec(alias, parentID, fileName, op.MessageID); err != nil {
				return err
			}

		case OpRM:
			if _, err := stmtRM.Exec(alias, fileName); err != nil {
				return err
			}

		case OpMV:
			if op.TargetPath != "" {
				_, dstName := path.Split(path.Clean(op.TargetPath))
				if _, err := stmtMV.Exec(dstName, alias, fileName); err != nil {
					return err
				}
			}

		case OpRESOLVE:
			_, _ = tx.Exec("DELETE FROM vfs_conflicts WHERE alias = ? AND path = ?", alias, op.Path)
			if op.Size > 0 || op.MessageID > 0 {
				if _, err := stmtCP.Exec(alias, parentID, fileName, op.Size, op.Sha256, op.MessageID); err != nil {
					return err
				}
			}

		case OpFORMAT:
			_, _ = tx.Exec("DELETE FROM vfs_entries WHERE alias = ?", alias)
			_, _ = tx.Exec("DELETE FROM vfs_conflicts WHERE alias = ?", alias)
			_, _ = tx.Exec("DELETE FROM vfs_operations_log WHERE alias = ?", alias)
		}

		if _, err := stmtLog.Exec(alias, op.MessageID, op.Hash, op.PrevHash, string(op.Op), op.Path, op.TargetPath, op.Size, op.Sha256, op.Timestamp); err != nil {
			return err
		}
	}

	return tx.Commit()
}
