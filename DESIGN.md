# tup - Architecture & Design

`tup` has been entirely rewritten from Python to Go to provide a single, statically-compiled binary with zero external runtime dependencies. 

## Core Architecture

### 1. `cmd/tup/main.go`
The entry point of the CLI application. Initializes the `core.Config` and `core.DB` sequentially before dispatching to the `cli.RootCmd`.

### 2. `internal/cli/`
Utilizes `spf13/cobra` to define all POSIX commands.
- **`posix.go`**: Contains `cp`, `ls`, `rm`, `mkdir`, `tree` (with `--json` export), utilizing the internal `vfs` and `telegram` packages to route local paths vs remote aliases.
- **`setup.go`**: Contains `tup login` for interactive `pterm` prompts.
- **`drive.go`**: Contains `tup drive add` and `tup drive list`.
- **`backup.go`**: Contains `tup backup <alias>` for extracting VFS state to JSON (supports `--json` / `--stdout` export).
- **`ai.go`**: Contains `tup ai` for generating `.tup-skill.md`.

### 3. `internal/core/`
- **`config.go`**: Uses `viper` to read `~/.tup/.env` natively.
- **`db.go`**: Embeds `modernc.org/sqlite` (pure Go SQLite, no CGO) to maintain a fast, local index of remote Telegram files.
- **`vfs.go`**: Helper functions to traverse paths, map parent-child relationships, and abstract SQL queries from the CLI layer.

### 4. `internal/telegram/`
- **`mtproto.go`**: The core `gotd/td` engine for native 2GB uploads/downloads.
- **`bot.go`**: Fallback `tgbotapi` engine for simpler 50MB uploads.
- **`restore.go`**: Logic for parsing and injecting `.json` VFS backups back into SQLite during `tup drive add`.

## Data Flow (Uploads)
1. `tup cp local.txt work:/docs/remote.txt`
2. `vfs.ParsePath` detects a local to remote transfer.
3. `core.GetChatID` looks up the `work` alias in SQLite.
4. `telegram.UploadFileMTProto` resolves the peer and chunks the file up to Telegram servers at maximum bandwidth using `gotd/td`.
5. Once confirmed, `core.InsertEntry` inserts the message ID and VFS metadata into SQLite under the `/docs/` parent ID.

## Self-Contained Drives
To ensure drives are portable:
1. `tup backup <alias>` dumps the SQLite table to JSON.
2. The JSON is uploaded into the same Telegram chat as a hidden payload.
3. Adding the drive on a new device (`tup drive add`) fetches this JSON and restores the exact VFS tree in the local SQLite cache without re-scraping thousands of individual messages.
