package cli

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/ernsoylu/tup/internal/core"
	"github.com/ernsoylu/tup/internal/telegram"
	"github.com/pterm/pterm"
	"github.com/spf13/cobra"
)

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

var backupCmd = &cobra.Command{
	Use:   "backup [alias]",
	Short: "Backup the VFS index of a drive to its Telegram chat",
	Args:  cobra.ExactArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		alias := args[0]
		pterm.Info.Printf("Generating VFS backup for drive '%s'...\n", alias)

		chatID, err := core.GetChatID(alias)
		if err != nil {
			pterm.Error.Println("Drive not found:", err)
			return
		}

		rows, err := core.DB.Query("SELECT id, alias, parent_id, name, is_dir, size, sha256, message_id FROM vfs_entries WHERE alias = ?", alias)
		if err != nil {
			pterm.Error.Println("Failed to query database:", err)
			return
		}
		defer func() { _ = rows.Close() }()

		var entries []VfsBackupEntry
		for rows.Next() {
			var e VfsBackupEntry
			var parentID, size, msgID interface{}
			var sha256 interface{}

			err = rows.Scan(&e.ID, &e.Alias, &parentID, &e.Name, &e.IsDir, &size, &sha256, &msgID)
			if err != nil {
				pterm.Error.Println("Row scan error:", err)
				return
			}

			// Handle nullable fields
			if p, ok := parentID.(int64); ok {
				e.ParentID = int(p)
			}
			if s, ok := size.(int64); ok {
				e.Size = s
			}
			if sh, ok := sha256.(string); ok {
				e.Sha256 = sh
			}
			if m, ok := msgID.(int64); ok {
				e.MessageID = int(m)
			}

			entries = append(entries, e)
		}

		if len(entries) == 0 {
			pterm.Warning.Println("No files found in this drive. Nothing to backup.")
			return
		}

		asJSON, _ := cmd.Flags().GetBool("json")
		toStdout, _ := cmd.Flags().GetBool("stdout")
		if asJSON || toStdout {
			encoder := json.NewEncoder(os.Stdout)
			encoder.SetIndent("", "  ")
			if err := encoder.Encode(entries); err != nil {
				pterm.Error.Println("JSON encoding failed:", err)
			}
			return
		}

		// Save to temp JSON file
		home, _ := os.UserHomeDir()
		backupFile := filepath.Join(home, ".tup", fmt.Sprintf("tup_backup_%s.json", alias))

		file, err := os.Create(backupFile)
		if err != nil {
			pterm.Error.Println("Failed to create backup file:", err)
			return
		}

		encoder := json.NewEncoder(file)
		encoder.SetIndent("", "  ")
		if err := encoder.Encode(entries); err != nil {
			_ = file.Close()
			pterm.Error.Println("JSON encoding failed:", err)
			return
		}
		if err := file.Close(); err != nil {
			pterm.Error.Println("Failed to finalize backup file:", err)
			return
		}

		// Encode OpSNAPSHOT operation payload
		op := &telegram.Operation{
			Op:   telegram.OpSNAPSHOT,
			Path: "/",
		}
		caption, _ := op.Encode()

		pterm.Success.Printf("Backup snapshot generated locally (%d entries). Uploading to Telegram...\n", len(entries))

		// Upload back to the drive's chat with OpSNAPSHOT payload caption
		msgID, err := telegram.UploadFileMTProtoWithCaption(cmd.Context(), backupFile, chatID, caption)
		if err != nil {
			pterm.Error.Println("Upload failed:", err)
			return
		}

		_ = telegram.UpdateSyncState(alias, msgID, op.Hash)
		pterm.Success.Println("VFS snapshot successfully backed up to Telegram!")
	},
}

func init() {
	backupCmd.Flags().Bool("stdout", false, "print backup JSON to stdout instead of uploading to Telegram")
	backupCmd.Flags().Bool("json", false, "emit backup JSON to stdout (for export / AI tools)")
	RootCmd.AddCommand(backupCmd)
}
