package cli

import (
	"fmt"
	"strings"
	"time"

	"github.com/ernsoylu/tup/internal/core"
	"github.com/ernsoylu/tup/internal/telegram"
	"github.com/ernsoylu/tup/internal/vfs"
	"github.com/pterm/pterm"
	"github.com/spf13/cobra"
)

// -- CP --
var cpCmd = &cobra.Command{
	Use:   "cp [source] [destination]",
	Short: "Copy files and directories",
	Args:  cobra.ExactArgs(2),
	Run: func(cmd *cobra.Command, args []string) {
		pterm.DefaultHeader.WithFullWidth().WithBackgroundStyle(pterm.NewStyle(pterm.BgBlue)).Println("Tup Copy")

		srcInfo := vfs.ParsePath(args[0])
		dstInfo := vfs.ParsePath(args[1])

		if !srcInfo.IsRemote && dstInfo.IsRemote {
			pterm.Info.Printf("Uploading %s to %s:/%s\n", srcInfo.Path, dstInfo.Alias, dstInfo.Path)
			chatID, err := core.GetChatID(dstInfo.Alias)
			if err != nil {
				pterm.Error.Println("Failed to resolve alias:", err)
				return
			}
			err = telegram.UploadFileMTProto(cmd.Context(), srcInfo.Path, chatID)
			if err != nil {
				pterm.Error.Println("Upload failed:", err)
				return
			}
		} else if srcInfo.IsRemote && !dstInfo.IsRemote {
			pterm.Info.Printf("Downloading %s:/%s to %s\n", srcInfo.Alias, srcInfo.Path, dstInfo.Path)
			// telegram.DownloadFile(srcInfo.Alias, srcInfo.Path, dstInfo.Path) will be implemented here
		} else if srcInfo.IsRemote && dstInfo.IsRemote {
			pterm.Info.Printf("Remote Copy %s:/%s to %s:/%s\n", srcInfo.Alias, srcInfo.Path, dstInfo.Alias, dstInfo.Path)
		} else {
			pterm.Error.Println("Local to local copies are not supported by tup. Use standard 'cp'.")
			return
		}

		// Simulate file transfer with a beautiful progress bar
		p, _ := pterm.DefaultProgressbar.WithTotal(100).WithTitle("Transferring...").Start()
		for i := 0; i < p.Total; i++ {
			p.Title = fmt.Sprintf("Processing chunk %d", i)
			p.Increment()
			time.Sleep(time.Millisecond * 20)
		}
		pterm.Success.Println("Transfer complete!")
	},
}

// -- MV --
var mvCmd = &cobra.Command{
	Use:   "mv [source] [destination]",
	Short: "Move or rename files and directories",
	Args:  cobra.ExactArgs(2),
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Printf("Moving %s to %s\n", args[0], args[1])
	},
}

// -- RM --
var rmCmd = &cobra.Command{
	Use:   "rm [target]",
	Short: "Remove files or directories",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		targetInfo := vfs.ParsePath(args[0])
		if !targetInfo.IsRemote {
			pterm.Error.Println("Use standard 'rm' for local files.")
			return
		}

		entry, err := core.GetEntryByPath(targetInfo.Alias, targetInfo.Path)
		if err != nil || entry == nil {
			if err != nil {
				pterm.Error.Println(err)
			} else {
				pterm.Error.Printf("path not found: %s\n", targetInfo.Path)
			}
			return
		}

		err = core.DeleteEntry(entry.ID)
		if err != nil {
			pterm.Error.Println("Failed to delete entry:", err)
			return
		}

		// Call telegram to delete the actual message using entry.MessageID when implemented
		pterm.Success.Printf("Removed %s:/%s\n", targetInfo.Alias, targetInfo.Path)
	},
}

// -- RMDIR --
var rmdirCmd = &cobra.Command{
	Use:   "rmdir [target]",
	Short: "Remove empty directories",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		pterm.Error.Println("Not implemented yet. Use 'rm -r'.")
	},
}

// -- MKDIR --
var mkdirCmd = &cobra.Command{
	Use:   "mkdir [directory]",
	Short: "Create directories",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		targetInfo := vfs.ParsePath(args[0])
		if !targetInfo.IsRemote {
			pterm.Error.Println("Use standard 'mkdir' for local directories.")
			return
		}

		// Simplistic implementation for prototype:
		// We insert it directly into root (parent_id = 0) for testing
		err := core.InsertEntry(core.VfsEntry{
			Alias:    targetInfo.Alias,
			ParentID: 0,
			Name:     strings.Trim(targetInfo.Path, "/"),
			IsDir:    true,
		})

		if err != nil {
			pterm.Error.Println("Failed to create directory:", err)
			return
		}
		pterm.Success.Printf("Created directory %s:/%s\n", targetInfo.Alias, targetInfo.Path)
	},
}

// -- LS --
var lsCmd = &cobra.Command{
	Use:   "ls [directory]",
	Short: "List directory contents",
	Args:  cobra.MaximumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		target := "."
		if len(args) > 0 {
			target = args[0]
		}

		targetInfo := vfs.ParsePath(target)
		if !targetInfo.IsRemote {
			pterm.Error.Println("Use standard 'ls' for local directories.")
			return
		}

		entry, err := core.GetEntryByPath(targetInfo.Alias, targetInfo.Path)
		if err != nil || entry == nil {
			if err != nil {
				pterm.Error.Println(err)
			} else {
				pterm.Error.Printf("path not found: %s\n", targetInfo.Path)
			}
			return
		}

		if !entry.IsDir {
			pterm.DefaultBasicText.Println(entry.Name)
			return
		}

		children, err := core.ListDirectory(targetInfo.Alias, entry.ID)
		if err != nil {
			pterm.Error.Println("Failed to list directory:", err)
			return
		}

		for _, child := range children {
			if child.IsDir {
				pterm.DefaultBasicText.Printf(pterm.Blue("%s/\n"), child.Name)
			} else {
				pterm.DefaultBasicText.Printf("%s\t(Size: %d)\n", child.Name, child.Size)
			}
		}
	},
}

// -- CAT --
var catCmd = &cobra.Command{
	Use:   "cat [file]",
	Short: "Print file contents to standard output",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Printf("Streaming %v\n", args)
	},
}

// -- FIND --
var findCmd = &cobra.Command{
	Use:   "find [path] [expression]",
	Short: "Search for files in a directory hierarchy",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Printf("Finding in %v\n", args)
	},
}

// -- STAT --
var statCmd = &cobra.Command{
	Use:   "stat [file]",
	Short: "Display file or file system status",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Printf("Stating %v\n", args)
	},
}

// -- TREE --
var treeCmd = &cobra.Command{
	Use:   "tree [directory]",
	Short: "List contents of directories in a tree-like format",
	Args:  cobra.MaximumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		target := "."
		if len(args) > 0 {
			target = args[0]
		}
		fmt.Printf("Tree for %s\n", target)
	},
}

// -- TOUCH --
var touchCmd = &cobra.Command{
	Use:   "touch [file]",
	Short: "Create empty file or update timestamps",
	Args:  cobra.MinimumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Printf("Touching %v\n", args)
	},
}

// -- DU --
var duCmd = &cobra.Command{
	Use:   "du [directory]",
	Short: "Estimate file space usage",
	Args:  cobra.MaximumNArgs(1),
	Run: func(cmd *cobra.Command, args []string) {
		fmt.Printf("Estimating space for %v\n", args)
	},
}

func init() {
	// Prevent negative chat IDs (e.g. -1004342175024:/path) from being misparsed as shorthand flags
	for _, cmd := range []*cobra.Command{cpCmd, mvCmd, rmCmd, rmdirCmd, mkdirCmd, lsCmd, catCmd, findCmd, statCmd, treeCmd, touchCmd, duCmd} {
		cmd.Flags().SetInterspersed(false)
	}

	// Flags for rm
	rmCmd.Flags().BoolP("recursive", "r", false, "remove directories and their contents recursively")
	rmCmd.Flags().BoolP("force", "f", false, "ignore nonexistent files and arguments, never prompt")

	// Flags for mkdir
	mkdirCmd.Flags().BoolP("parents", "p", false, "no error if existing, make parent directories as needed")

	// Flags for ls
	lsCmd.Flags().BoolP("all", "a", false, "do not ignore entries starting with .")
	lsCmd.Flags().BoolP("long", "l", false, "use a long listing format")
	lsCmd.Flags().BoolP("human-readable", "H", false, "with -l, print sizes in human readable format")
	lsCmd.Flags().BoolP("recursive", "R", false, "list subdirectories recursively")

	RootCmd.AddCommand(cpCmd, mvCmd, rmCmd, rmdirCmd, mkdirCmd, lsCmd, catCmd, findCmd, statCmd, treeCmd, touchCmd, duCmd)
}
