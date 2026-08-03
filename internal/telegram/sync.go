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

// SyncDrive performs incremental auto-sync for a drive alias by reading new operations from Telegram
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

		// Fetch history since lastMsgID
		res, err := api.MessagesGetHistory(ctx, &tg.MessagesGetHistoryRequest{
			Peer:  peer,
			MinID: lastMsgID,
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
			return nil
		}

		pterm.Info.Printf("Syncing drive '%s' (%d new messages)...\n", alias, len(messages))

		// Process messages in chronological order (oldest to newest)
		for i := len(messages) - 1; i >= 0; i-- {
			msg, ok := messages[i].(*tg.Message)
			if !ok {
				continue
			}

			text := msg.Message
			if text == "" {
				continue
			}

			op, err := DecodeOperation(text)
			if err != nil {
				// Message does not contain a tup operation payload
				continue
			}

			op.MessageID = msg.ID

			// Check for conflicts
			if op.PrevHash != "" && headHash != "" && op.PrevHash != headHash {
				// Check if the file at op.Path was locally modified
				var localMsgID int
				err := core.DB.QueryRow("SELECT message_id FROM vfs_entries WHERE alias = ? AND name = ?", alias, path.Base(op.Path)).Scan(&localMsgID)
				if err == nil && localMsgID != 0 && localMsgID != msg.ID {
					conflictType := "MODIFY_MODIFY"
					if op.Op == OpRM {
						conflictType = "MODIFY_DELETE"
					}
					// Record conflict in SQLite table
					_, _ = core.DB.Exec(`
						INSERT OR REPLACE INTO vfs_conflicts (alias, path, conflict_type, local_msg_id, local_hash, remote_msg_id, remote_hash)
						VALUES (?, ?, ?, ?, ?, ?, ?)
					`, alias, op.Path, conflictType, localMsgID, headHash, msg.ID, op.Hash)

					return &ConflictError{
						Alias:       alias,
						Path:        op.Path,
						Type:        conflictType,
						LocalMsgID:  localMsgID,
						RemoteMsgID: msg.ID,
					}
				}
			}

			// Apply operation to local VFS
			if err := ApplyOperation(alias, op); err != nil {
				return fmt.Errorf("failed to apply operation %s: %w", op.Hash, err)
			}

			headHash = op.Hash
			lastMsgID = msg.ID
			_ = UpdateSyncState(alias, lastMsgID, headHash)
		}

		return nil
	})
}

// ApplyOperation updates the SQLite vfs_entries according to the operation type
func ApplyOperation(alias string, op *Operation) error {
	tx, err := core.DB.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	dir, fileName := path.Split(path.Clean(op.Path))
	dir = path.Clean(dir)

	// Resolve parent ID
	parentID := 0
	if dir != "." && dir != "/" {
		_ = tx.QueryRow("SELECT id FROM vfs_entries WHERE alias = ? AND name = ? AND is_dir = 1", alias, path.Base(dir)).Scan(&parentID)
	}

	switch op.Op {
	case OpCP:
		_, err = tx.Exec(`
			INSERT INTO vfs_entries (alias, parent_id, name, is_dir, size, sha256, message_id)
			VALUES (?, ?, ?, 0, ?, ?, ?)
			ON CONFLICT(alias, parent_id, name) DO UPDATE SET
				size = excluded.size,
				sha256 = excluded.sha256,
				message_id = excluded.message_id
		`, alias, parentID, fileName, op.Size, op.Sha256, op.MessageID)

	case OpMKDIR:
		_, err = tx.Exec(`
			INSERT INTO vfs_entries (alias, parent_id, name, is_dir, size, sha256, message_id)
			VALUES (?, ?, ?, 1, 0, '', ?)
			ON CONFLICT(alias, parent_id, name) DO NOTHING
		`, alias, parentID, fileName, op.MessageID)

	case OpRM:
		_, err = tx.Exec("DELETE FROM vfs_entries WHERE alias = ? AND name = ?", alias, fileName)

	case OpMV:
		if op.TargetPath != "" {
			_, dstName := path.Split(path.Clean(op.TargetPath))
			_, err = tx.Exec("UPDATE vfs_entries SET name = ? WHERE alias = ? AND name = ?", dstName, alias, fileName)
		}

	case OpRESOLVE:
		// Clear conflict for path
		_, _ = tx.Exec("DELETE FROM vfs_conflicts WHERE alias = ? AND path = ?", alias, op.Path)
		if op.Size > 0 || op.MessageID > 0 {
			_, err = tx.Exec(`
				INSERT INTO vfs_entries (alias, parent_id, name, is_dir, size, sha256, message_id)
				VALUES (?, ?, ?, 0, ?, ?, ?)
				ON CONFLICT(alias, parent_id, name) DO UPDATE SET
					size = excluded.size,
					sha256 = excluded.sha256,
					message_id = excluded.message_id
			`, alias, parentID, fileName, op.Size, op.Sha256, op.MessageID)
		}
	}

	if err != nil {
		return err
	}

	// Record in operations log
	_, err = tx.Exec(`
		INSERT OR IGNORE INTO vfs_operations_log (alias, msg_id, hash, prev_hash, op_type, path, target_path, size, sha256, timestamp)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`, alias, op.MessageID, op.Hash, op.PrevHash, string(op.Op), op.Path, op.TargetPath, op.Size, op.Sha256, op.Timestamp)
	if err != nil {
		return err
	}

	return tx.Commit()
}
