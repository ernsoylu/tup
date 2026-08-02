package cli

import (
	"os"
	"path/filepath"

	"github.com/pterm/pterm"
	"github.com/spf13/cobra"
)

const aiSkillTemplate = `
# Tup CLI Skill for AI Agents

You are interacting with a project that uses 'tup' to manage remote files on Telegram.
Treat 'tup' exactly like standard POSIX tools (cp, mv, rm, ls) but for remote drives.

## Remote Path Syntax
Remote paths are prefixed with a drive alias followed by a colon: 'drive:/path/to/file'
Example: 'work:/docs/report.pdf'

## Available Commands
- tup cp <src> <dst>   : Upload, download, or copy (supports -r for directories).
- tup mv <src> <dst>   : Move or rename files.
- tup rm <target>      : Delete a file (supports -r and -f).
- tup rmdir <target>   : Delete an empty directory.
- tup mkdir <target>   : Create a remote directory (supports -p).
- tup ls <target>      : List contents (supports -l, -R, -a, -H).
- tup cat <target>     : Stream remote file to stdout.
- tup tree <target>    : Show directory tree (use --json for machine-readable output).
- tup find <path> -name <glob> : Search by basename.
- tup stat <path>      : Show entry metadata.
- tup du <path>        : Disk usage (-s summarize, -H human sizes).

## Machine-readable tree (preferred for agents)
tup tree mydrive:/ --json
tup tree mydrive:/docs --json

JSON shape:
{
  "path": "mydrive:/docs",
  "alias": "mydrive",
  "tree": { "name": "docs", "path": "mydrive:/docs", "type": "directory", "children": [...] },
  "summary": { "directories": 3, "files": 10 }
}
File nodes include "size" and "message_id". Prefer --json over scraping text trees.

## Examples
Upload a file:  tup cp ./local.txt mydrive:/remote.txt
Download a file: tup cp mydrive:/remote.txt ./local.txt
Server-side move: tup mv mydrive:/old.txt mydrive:/new.txt
List for agents:  tup tree mydrive:/ --json

Do NOT use custom python scripts to interact with Telegram. Always use the 'tup' CLI.
`

var aiCmd = &cobra.Command{
	Use:   "ai",
	Short: "Inject a tup skill for AI coding assistants (Claude, AGY, Cursor, etc.)",
	Run: func(cmd *cobra.Command, args []string) {
		cwd, err := os.Getwd()
		if err != nil {
			pterm.Error.Println("Could not get current directory:", err)
			return
		}

		skillPath := filepath.Join(cwd, "tup-skill.md")
		err = os.WriteFile(skillPath, []byte(aiSkillTemplate), 0644)
		if err != nil {
			pterm.Error.Println("Failed to create skill file:", err)
			return
		}

		pterm.Success.Printf("Generated AI skill instructions at %s\n", skillPath)
		pterm.Info.Println("Include this file in your .cursorrules, CLAUDE.md, or Antigravity skills to teach your LLM how to use tup!")
	},
}

func init() {
	RootCmd.AddCommand(aiCmd)
}
