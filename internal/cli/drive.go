package cli

import (
	"context"
	"strings"

	"github.com/ernsoylu/tup/internal/core"
	"github.com/ernsoylu/tup/internal/telegram"
	"github.com/pterm/pterm"
	"github.com/spf13/cobra"
)

var driveCmd = &cobra.Command{
	Use:   "drive",
	Short: "Manage remote Telegram drives (chats/channels)",
}

var driveAddCmd = &cobra.Command{
	Use:   "add [alias] [chat_id]",
	Short: "Register a Telegram chat as a remote drive",
	Args:  cobra.ExactArgs(2),
	Run: func(cmd *cobra.Command, args []string) {
		alias := args[0]
		chatID := args[1]

		_, err := core.DB.Exec("INSERT OR REPLACE INTO drive_aliases (alias, chat_id) VALUES (?, ?)", alias, chatID)
		if err != nil {
			pterm.Error.Println("Failed to register drive:", err)
			return
		}

		pterm.Success.Printf("Registered drive '%s' -> %s\n", alias, chatID)

		pterm.Info.Println("Scanning chat for existing VFS backup...")
		err = telegram.SearchAndRestoreBackup(cmd.Context(), alias, chatID)
		if err != nil {
			pterm.Error.Println("Restore failed:", err)
		}
		pterm.Info.Println("If a backup existed, it has been restored automatically.")
	},
}

var driveListCmd = &cobra.Command{
	Use:   "list",
	Short: "List all registered remote drives",
	Run: func(cmd *cobra.Command, args []string) {
		rows, err := core.DB.Query("SELECT alias, chat_id FROM drive_aliases")
		if err != nil {
			pterm.Error.Println("Failed to fetch drives:", err)
			return
		}
		defer func() { _ = rows.Close() }()

		pterm.DefaultHeader.WithFullWidth().WithBackgroundStyle(pterm.NewStyle(pterm.BgCyan)).Println("Registered Drives")

		tableData := pterm.TableData{{"Alias", "Chat ID"}}
		for rows.Next() {
			var alias, chatID string
			if err := rows.Scan(&alias, &chatID); err == nil {
				tableData = append(tableData, []string{alias, chatID})
			}
		}

		if len(tableData) == 1 {
			pterm.Warning.Println("No drives registered. Use 'tup drive add <alias> <chat_id>'")
			return
		}

		_ = pterm.DefaultTable.WithHasHeader().WithData(tableData).Render()
	},
}

var driveChatsCmd = &cobra.Command{
	Use:   "chats",
	Short: "List your Telegram chats with their IDs (for 'drive add')",
	Run: func(cmd *cobra.Command, args []string) {
		filter, _ := cmd.Flags().GetString("filter")

		var dialogs []telegram.DialogInfo
		err := telegram.Run(cmd.Context(), func(ctx context.Context) error {
			// Auth is already handled by Run() before we get here,
			// so it's safe to start the spinner now.
			spinner, _ := pterm.DefaultSpinner.Start("Fetching chats from Telegram...")
			var fetchErr error
			dialogs, fetchErr = telegram.FetchDialogs(ctx)
			if fetchErr != nil {
				spinner.Fail("Failed to fetch chats: ", fetchErr)
				return fetchErr
			}
			_ = spinner.Stop()
			return nil
		})
		if err != nil {
			pterm.Error.Println("Failed to fetch chats:", err)
			return
		}

		tableData := pterm.TableData{{"Name", "Type", "Username", "Chat ID"}}
		for _, d := range dialogs {
			if filter != "" && !strings.Contains(strings.ToLower(d.Title), strings.ToLower(filter)) {
				continue
			}
			username := d.Username
			if username != "" {
				username = "@" + username
			}
			tableData = append(tableData, []string{d.Title, d.Type, username, d.ChatID})
		}

		if len(tableData) == 1 {
			pterm.Warning.Println("No chats matched.")
			return
		}

		pterm.DefaultHeader.WithFullWidth().WithBackgroundStyle(pterm.NewStyle(pterm.BgCyan)).Println("Telegram Chats")
		_ = pterm.DefaultTable.WithHasHeader().WithData(tableData).Render()
		pterm.Info.Println("Register one with: tup drive add <alias> <chat_id>")
	},
}

func init() {
	driveChatsCmd.Flags().StringP("filter", "f", "", "filter chats by name (case-insensitive)")
	driveCmd.AddCommand(driveAddCmd, driveListCmd, driveChatsCmd)
	RootCmd.AddCommand(driveCmd)
}
