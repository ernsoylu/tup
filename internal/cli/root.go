package cli

import (
	"github.com/spf13/cobra"
)

// RootCmd is the base command for tup
var RootCmd = &cobra.Command{
	Use:   "tup",
	Short: "tup is a POSIX-like CLI for managing files on Telegram",
	Long: `tup treats Telegram chats like a virtual file system (VFS).
You can copy, move, and remove files natively using standard POSIX semantics.
Example: tup cp ~/file.txt work:/docs/`,
	Run: func(cmd *cobra.Command, args []string) {
		_ = cmd.Help()
	},
}

func init() {
	// Root-level persistent flags
	RootCmd.PersistentFlags().BoolP("debug", "d", false, "enable debug logging")
	RootCmd.PersistentFlags().Bool("no-sync", false, "disable automatic VFS synchronization with Telegram cloud log")
}
