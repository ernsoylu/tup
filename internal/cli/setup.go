package cli

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"

	"github.com/pterm/pterm"
	"github.com/spf13/cobra"
)

var loginCmd = &cobra.Command{
	Use:   "login",
	Short: "Interactive setup for Telegram credentials",
	Run: func(cmd *cobra.Command, args []string) {
		pterm.DefaultHeader.WithFullWidth().WithBackgroundStyle(pterm.NewStyle(pterm.BgMagenta)).Println("Tup Setup")

		options := []string{
			"1. MTProto (2GB File Uploads) [Recommended]",
			"2. Bot API (50MB File Uploads) [Basic]",
		}

		selected, _ := pterm.DefaultInteractiveSelect.
			WithOptions(options).
			WithDefaultText("Select upload engine").
			Show()

		botToken, _ := pterm.DefaultInteractiveTextInput.WithDefaultText("Enter your Telegram Bot Token").Show()

		envContent := fmt.Sprintf("TELEGRAM_BOT_TOKEN=%s\n", botToken)

		// If they chose MTProto (Option 1)
		if selected == options[0] {
			pterm.Info.Println("MTProto requires your Telegram API ID and API Hash from my.telegram.org")
			apiIDStr, _ := pterm.DefaultInteractiveTextInput.WithDefaultText("Enter your API_ID").Show()
			apiHash, _ := pterm.DefaultInteractiveTextInput.WithDefaultText("Enter your API_HASH").Show()

			// Validate API ID is an integer
			if _, err := strconv.Atoi(apiIDStr); err != nil {
				pterm.Error.Println("API_ID must be a number!")
				return
			}

			envContent += fmt.Sprintf("TELEGRAM_API_ID=%s\nTELEGRAM_API_HASH=%s\n", apiIDStr, apiHash)
		} else {
			pterm.Warning.Println("You selected Bot API mode. Files larger than 50MB will fail to upload.")
		}

		home, err := os.UserHomeDir()
		if err != nil {
			pterm.Error.Println("Could not find home directory:", err)
			return
		}

		tupDir := filepath.Join(home, ".tup")
		if err := os.MkdirAll(tupDir, 0700); err != nil {
			pterm.Error.Println("Failed to create config directory:", err)
			return
		}
		envPath := filepath.Join(tupDir, ".env")

		err = os.WriteFile(envPath, []byte(envContent), 0600)
		if err != nil {
			pterm.Error.Println("Failed to save configuration:", err)
			return
		}

		pterm.Success.Printf("Configuration saved successfully to %s\n", envPath)
	},
}

func init() {
	RootCmd.AddCommand(loginCmd)
}
