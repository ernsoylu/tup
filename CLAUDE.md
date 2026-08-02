# tup - Developer Guide

`tup` is a POSIX-compatible Virtual File System (VFS) mapped over Telegram MTProto.

## Build Commands
- `go mod tidy` : Sync dependencies
- `go build ./cmd/tup` : Build the CLI binary
- `./tup help` : Run the CLI locally

## Architecture
- `internal/cli`: Cobra CLI commands (`posix.go`, `drive.go`, `setup.go`)
- `internal/core`: SQLite DB wrapper (`db.go`), VFS logic (`vfs.go`), and Viper config (`config.go`)
- `internal/telegram`: `gotd/td` MTProto client for 2GB uploads/downloads (`mtproto.go`, `restore.go`)
- `internal/vfs`: Path parsing and abstraction (`path.go`)

## Coding Conventions
- **Pure Go**: We use `modernc.org/sqlite` instead of `mattn/go-sqlite3` to ensure zero CGO dependencies and seamless cross-compilation.
- **Beautiful UI**: We use `pterm` for all terminal outputs, spinners, headers, and interactive prompts. Never use raw `fmt.Printf` for user-facing CLI output.
- **Error Handling**: Bubble errors up to the Cobra `Run` function and print them using `pterm.Error.Println`.

## Testing
- Ensure you have run `tup login` to generate the `~/.tup/.env` file before executing network tests.
