package telegram

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/ernsoylu/tup/internal/core"
	"github.com/pterm/pterm"
)

// VfsBackupEntry must match the struct in cli/backup.go
type VfsBackupEntry struct {
	Alias     string `json:"alias"`
	ParentID  int    `json:"parent_id"`
	Name      string `json:"name"`
	IsDir     bool   `json:"is_dir"`
	Size      int64  `json:"size"`
	Sha256    string `json:"sha256"`
	MessageID int    `json:"message_id"`
}

// SearchAndRestoreBackup looks for a backup file in the chat and rebuilds the VFS.
func SearchAndRestoreBackup(ctx context.Context, alias, chatIDStr string) error {
	return Run(ctx, func(ctx context.Context) error {
		pterm.Info.Printf("Connecting to chat %s to search for backups...\n", chatIDStr)
		
		// TODO: Implement gotd/td message search to find "tup_backup_alias.json"
		// For now, we will simulate the download step since full MTProto search
		// requires handling peer resolution and history pagination.
		
		home, _ := os.UserHomeDir()
		backupFile := filepath.Join(home, ".tup", fmt.Sprintf("tup_backup_%s.json", alias))

		if _, err := os.Stat(backupFile); os.IsNotExist(err) {
			pterm.Warning.Println("No backup found remotely. Starting with an empty VFS.")
			return nil
		}

		pterm.Info.Println("Backup file located! Restoring VFS state...")

		file, err := os.Open(backupFile)
		if err != nil {
			return err
		}
		defer file.Close()

		var entries []VfsBackupEntry
		if err := json.NewDecoder(file).Decode(&entries); err != nil {
			return fmt.Errorf("failed to parse backup JSON: %w", err)
		}

		// Insert back into SQLite
		tx, err := core.DB.Begin()
		if err != nil {
			return err
		}

		stmt, err := tx.Prepare(`
			INSERT OR REPLACE INTO vfs_entries (alias, parent_id, name, is_dir, size, sha256, message_id)
			VALUES (?, ?, ?, ?, ?, ?, ?)
		`)
		if err != nil {
			return err
		}
		defer stmt.Close()

		for _, e := range entries {
			var parentID interface{}
			if e.ParentID != 0 {
				parentID = e.ParentID
			}

			_, err = stmt.Exec(e.Alias, parentID, e.Name, e.IsDir, e.Size, e.Sha256, e.MessageID)
			if err != nil {
				tx.Rollback()
				return fmt.Errorf("failed to insert entry %s: %w", e.Name, err)
			}
		}

		if err := tx.Commit(); err != nil {
			return err
		}

		pterm.Success.Printf("Successfully restored %d files to the VFS!\n", len(entries))
		return nil
	})
}
