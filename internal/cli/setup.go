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

		pterm.Info.Println("We need your Telegram API ID and API Hash from my.telegram.org to authenticate your user account.")
		
		apiIDStr, _ := pterm.DefaultInteractiveTextInput.WithDefaultText("Enter your API_ID").Show()
		apiHash, _ := pterm.DefaultInteractiveTextInput.WithDefaultText("Enter your API_HASH").Show()

		if _, err := strconv.Atoi(apiIDStr); err != nil {
			pterm.Error.Println("API_ID must be a number!")
			return
		}

		envContent := fmt.Sprintf("TELEGRAM_API_ID=%s\nTELEGRAM_API_HASH=%s\n", apiIDStr, apiHash)
		pterm.Info.Println("Configuration saved. We will ask for your phone number on first run.")

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
