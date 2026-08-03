package telegram

import (
	"context"
	"fmt"

	"github.com/ernsoylu/tup/internal/core"
	"github.com/pterm/pterm"
)

// VfsBackupEntry must match the struct in cli/backup.go
type VfsBackupEntry struct {
	ID        int    `json:"id,omitempty"`
	Alias     string `json:"alias"`
	ParentID  int    `json:"parent_id"`
	Name      string `json:"name"`
	IsDir     bool   `json:"is_dir"`
	Size      int64  `json:"size"`
	Sha256    string `json:"sha256"`
	MessageID int    `json:"message_id"`
}

// SearchAndRestoreBackup synchronizes the drive automatically over Telegram history and snapshots.
func SearchAndRestoreBackup(ctx context.Context, alias, chatIDStr string) error {
	pterm.Info.Printf("Synchronizing drive '%s' with Telegram operations log...\n", alias)
	return SyncDrive(ctx, alias)
}

func restoreToDB(entries []VfsBackupEntry) error {
	tx, err := core.DB.Begin()
	if err != nil {
		return err
	}

	stmtWithID, err := tx.Prepare(`
		INSERT OR REPLACE INTO vfs_entries (id, alias, parent_id, name, is_dir, size, sha256, message_id)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)
	`)
	if err != nil {
		_ = tx.Rollback()
		return err
	}
	defer func() { _ = stmtWithID.Close() }()

	stmtWithoutID, err := tx.Prepare(`
		INSERT OR REPLACE INTO vfs_entries (alias, parent_id, name, is_dir, size, sha256, message_id)
		VALUES (?, ?, ?, ?, ?, ?, ?)
	`)
	if err != nil {
		_ = tx.Rollback()
		return err
	}
	defer func() { _ = stmtWithoutID.Close() }()

	for _, e := range entries {
		if e.ID != 0 {
			_, err = stmtWithID.Exec(e.ID, e.Alias, e.ParentID, e.Name, e.IsDir, e.Size, e.Sha256, e.MessageID)
		} else {
			_, err = stmtWithoutID.Exec(e.Alias, e.ParentID, e.Name, e.IsDir, e.Size, e.Sha256, e.MessageID)
		}
		if err != nil {
			_ = tx.Rollback()
			return fmt.Errorf("failed to insert entry %s: %w", e.Name, err)
		}
	}

	return tx.Commit()
}
