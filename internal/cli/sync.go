package cli

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/ernsoylu/tup/internal/core"
	"github.com/ernsoylu/tup/internal/telegram"
	"github.com/pterm/pterm"
	"github.com/spf13/cobra"
)

var syncCmd = &cobra.Command{
	Use:   "sync [alias]",
	Short: "Synchronize local VFS database with remote Telegram chat operations",
	Args:  cobra.ExactArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		alias := args[0]
		pterm.Info.Printf("Syncing drive '%s' with Telegram cloud log...\n", alias)

		if err := telegram.SyncDrive(cmd.Context(), alias); err != nil {
			if conflictErr, ok := err.(*telegram.ConflictError); ok {
				highlightConflictBanner(conflictErr)
				pterm.Info.Println("Run 'tup resolve " + alias + ":" + conflictErr.Path + "' to resolve this conflict.")
				os.Exit(1)
			}
			pterm.Error.Println("Sync failed:", err)
			return
		}

		pterm.Success.Printf("Drive '%s' is up to date!\n", alias)
	},
}

// AutoSync attempts to sync the drive if --no-sync is not specified.
func AutoSync(cmd *cobra.Command, alias string) {
	noSync, _ := cmd.Flags().GetBool("no-sync")
	if noSync || alias == "" {
		return
	}
	if err := telegram.SyncDrive(cmd.Context(), alias); err != nil {
		if conflictErr, ok := err.(*telegram.ConflictError); ok {
			highlightConflictBanner(conflictErr)
			pterm.Info.Println("Run 'tup resolve " + alias + ":" + conflictErr.Path + "' to resolve this conflict.")
			os.Exit(1)
		}
		pterm.Warning.Println("Auto-sync skipped:", err)
	}
}

type conflictJSON struct {
	Alias        string `json:"alias"`
	Path         string `json:"path"`
	ConflictType string `json:"conflict_type"`
	LocalMsgID   int    `json:"local_msg_id"`
	RemoteMsgID  int    `json:"remote_msg_id"`
	CreatedAt    string `json:"created_at"`
}

var conflictsCmd = &cobra.Command{
	Use:   "conflicts [alias]",
	Short: "List unresolved VFS sync conflicts",
	Args:  cobra.ExactArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		alias := args[0]
		asJSON, _ := cmd.Flags().GetBool("json")

		rows, err := core.DB.Query(`
			SELECT path, conflict_type, local_msg_id, remote_msg_id, created_at
			FROM vfs_conflicts WHERE alias = ?
		`, alias)
		if err != nil {
			pterm.Error.Println("Failed to query conflicts:", err)
			return
		}
		defer func() { _ = rows.Close() }()

		var list []conflictJSON
		tableData := pterm.TableData{{"Path", "Conflict Type", "Local Msg", "Remote Msg", "Detected At"}}

		for rows.Next() {
			var c conflictJSON
			c.Alias = alias
			if err := rows.Scan(&c.Path, &c.ConflictType, &c.LocalMsgID, &c.RemoteMsgID, &c.CreatedAt); err == nil {
				list = append(list, c)
				tableData = append(tableData, []string{
					c.Path, c.ConflictType, fmt.Sprintf("#%d", c.LocalMsgID), fmt.Sprintf("#%d", c.RemoteMsgID), c.CreatedAt,
				})
			}
		}

		if asJSON {
			enc := json.NewEncoder(os.Stdout)
			enc.SetIndent("", "  ")
			_ = enc.Encode(list)
			return
		}

		if len(list) == 0 {
			pterm.Success.Printf("No unresolved conflicts for drive '%s'.\n", alias)
			return
		}

		pterm.DefaultHeader.WithFullWidth().WithBackgroundStyle(pterm.NewStyle(pterm.BgRed)).Println("Unresolved Conflicts")
		_ = pterm.DefaultTable.WithHasHeader().WithData(tableData).Render()
		pterm.Info.Println("Resolve conflicts using: tup resolve <alias>:<path>")
	},
}

var resolveCmd = &cobra.Command{
	Use:   "resolve [alias]:[path]",
	Short: "Resolve a sync conflict for a file or directory",
	Args:  cobra.ExactArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		info := resolveRemotePath(args[0])
		if !info.IsRemote || info.Alias == "" || info.Path == "" {
			pterm.Error.Println("Invalid resolution target. Usage: tup resolve <alias>:<path>")
			return
		}

		ours, _ := cmd.Flags().GetBool("ours")
		theirs, _ := cmd.Flags().GetBool("theirs")
		keepBoth, _ := cmd.Flags().GetBool("keep-both")

		var conflictType string
		var localMsgID, remoteMsgID int
		err := core.DB.QueryRow(`
			SELECT conflict_type, local_msg_id, remote_msg_id
			FROM vfs_conflicts WHERE alias = ? AND path = ?
		`, info.Alias, info.Path).Scan(&conflictType, &localMsgID, &remoteMsgID)

		if err != nil {
			pterm.Error.Printf("No conflict record found for %s\n", info.Format())
			return
		}

		choice := ""
		if ours {
			choice = "ours"
		} else if theirs {
			choice = "theirs"
		} else if keepBoth {
			choice = "both"
		} else {
			// Interactive prompt
			pterm.Println()
			pterm.Warning.Printf("Resolving Conflict for %s (%s)\n", info.Format(), conflictType)
			pterm.Println()

			selected, _ := pterm.DefaultInteractiveSelect.
				WithOptions([]string{
					"Keep Local Version (--ours)",
					"Keep Remote Version (--theirs)",
					"Keep Both (Auto-Rename)",
					"Cancel",
				}).
				WithDefaultOption("Keep Remote Version (--theirs)").
				Show()

			switch selected {
			case "Keep Local Version (--ours)":
				choice = "ours"
			case "Keep Remote Version (--theirs)":
				choice = "theirs"
			case "Keep Both (Auto-Rename)":
				choice = "both"
			default:
				pterm.Info.Println("Resolution cancelled.")
				return
			}
		}

		var op *telegram.Operation
		switch choice {
		case "ours":
			op = &telegram.Operation{
				Op:        telegram.OpRESOLVE,
				Path:      info.Path,
				MessageID: localMsgID,
			}
			pterm.Success.Printf("Resolving %s using Local Version (#%d)\n", info.Format(), localMsgID)

		case "theirs":
			op = &telegram.Operation{
				Op:        telegram.OpRESOLVE,
				Path:      info.Path,
				MessageID: remoteMsgID,
			}
			pterm.Success.Printf("Resolving %s using Remote Version (#%d)\n", info.Format(), remoteMsgID)

		case "both":
			renamedPath := info.Path + ".conflict-pc1"
			op = &telegram.Operation{
				Op:         telegram.OpMV,
				Path:       info.Path,
				TargetPath: renamedPath,
			}
			pterm.Success.Printf("Resolving %s by keeping both (renamed local to %s)\n", info.Format(), renamedPath)
		}

		if err := telegram.ApplyOperation(info.Alias, op); err != nil {
			pterm.Error.Println("Failed to apply resolution:", err)
			return
		}

		_, _ = core.DB.Exec("DELETE FROM vfs_conflicts WHERE alias = ? AND path = ?", info.Alias, info.Path)
		pterm.Success.Printf("Conflict resolved successfully for %s!\n", info.Format())
	},
}

func highlightConflictBanner(e *telegram.ConflictError) {
	pterm.Println()
	pterm.DefaultHeader.WithFullWidth().
		WithBackgroundStyle(pterm.NewStyle(pterm.BgRed)).
		WithTextStyle(pterm.NewStyle(pterm.FgWhite, pterm.Bold)).
		Println("⚠  SYNC CONFLICT DETECTED  ⚠")

	pterm.Println()
	pterm.Warning.Printf("Drive:         %s\n", e.Alias)
	pterm.Warning.Printf("File Path:     %s\n", e.Path)
	pterm.Warning.Printf("Conflict Type: %s\n", e.Type)
	pterm.Warning.Printf("Local Msg:     #%d\n", e.LocalMsgID)
	pterm.Warning.Printf("Remote Msg:    #%d\n", e.RemoteMsgID)
	pterm.Println()
}

func init() {
	conflictsCmd.Flags().Bool("json", false, "emit conflicts in JSON format")

	resolveCmd.Flags().Bool("ours", false, "resolve by keeping local version")
	resolveCmd.Flags().Bool("theirs", false, "resolve by keeping remote version")
	resolveCmd.Flags().Bool("keep-both", false, "resolve by keeping both versions (auto-rename)")

	RootCmd.AddCommand(syncCmd, conflictsCmd, resolveCmd)
}
