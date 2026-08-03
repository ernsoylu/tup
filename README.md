# tup

`tup` treats Telegram chats and channels like a POSIX-compatible Virtual File System (VFS). With `tup`, you can use standard terminal commands (`cp`, `mv`, `rm`, `ls`) to manage remote files with native 2GB MTProto streaming bandwidth.

## Features

- **POSIX Interface**: Use `tup cp`, `tup ls`, `tup rm`, `tup mkdir` exactly as you would natively.
- **Git-Like Auto-Sync**: Seamless multi-device synchronization over Telegram event log. Changes on one device auto-sync to all other devices on the same account.
- **Conflict Resolution**: Built-in conflict detection (`tup conflicts`) and interactive resolver (`tup resolve`).
- **Chat Discovery**: Run `tup drive chats` to list your Telegram chats with their IDs — no external bots needed to find a Chat ID.
- **Native 2GB Support**: Utilizes the `gotd/td` MTProto client to unlock Telegram's native 2GB file upload limits.
- **VFS Snapshots**: Fast-forward new devices via `tup backup <alias>` compaction snapshots or export metadata via `tup backup <alias> --json`.
- **JSON Export**: Machine-readable `--json` output support for `tup tree`, `tup backup`, and `tup conflicts` for AI tools and scripts.
- **AI Ready**: Run `tup ai` to generate instructions that teach LLMs (Cursor, Claude, Copilot, Antigravity) how to interact with your cloud files natively.

## Installation

Install via curl:

```bash
curl -fsSL https://raw.githubusercontent.com/ernsoylu/tup/main/install.sh | sh
```

Or download the binary directly from [Releases](https://github.com/ernsoylu/tup/releases).

## Getting Started

### 1. Login

Run the interactive setup to configure your Telegram API credentials:
```bash
tup login
```
*Note: We highly recommend MTProto (2GB) mode. You will need your `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org).*

### 2. Find and Register a Drive

List your Telegram chats to find the one you want to use as a drive:
```bash
tup drive chats                # all chats with name, type, username and Chat ID
tup drive chats --filter work  # filter by name (case-insensitive)
```

Then register a Chat ID as an alias:
```bash
tup drive add work <chat_id>
tup drive list                 # show registered drives
```
*(When registered on a new machine, `tup` will automatically sync the virtual file system from Telegram chat history!)*

### 3. POSIX Commands & Auto-Sync

Now, just use standard POSIX syntax. `tup` automatically keeps your local VFS in sync before commands:
```bash
# Upload a file to Telegram
tup cp ./local-report.pdf work:/docs/report.pdf

# List remote files (auto-syncs changes from other devices)
tup ls work:/docs

# Explicitly trigger drive sync
tup sync work

# List and resolve sync conflicts (if concurrent edits occurred)
tup conflicts work
tup resolve work:/docs/report.pdf --ours   # or --theirs / --keep-both

# Run offline without network auto-sync
tup --no-sync ls work:/docs

# Download a file
tup cp work:/docs/report.pdf ./downloaded.pdf

# Delete a remote file
tup rm work:/docs/report.pdf

# Machine-readable JSON tree (for AI agents & scripts)
tup tree work:/ --json

# Export drive VFS index snapshot as JSON
tup backup work --json
```

Also available: `mv`, `mkdir`, `rmdir`, `cat`, `find`, `stat`, `tree`, `touch`, `du` — plus `tup backup <alias>` to push the VFS index into the chat itself.

## Updates

`tup` comes with a built-in self-updater. Just run:
```bash
tup update
```
It will automatically fetch and install the latest binary from GitHub!

## Development

Built natively in Go using `cobra`, `gotd/td`, `pterm`, and a pure-Go SQLite (`modernc.org/sqlite`) — zero CGO, so it cross-compiles everywhere.

```bash
git clone https://github.com/ernsoylu/tup
cd tup
go build ./cmd/tup
```

Releases are built by GoReleaser for macOS (Intel & Apple Silicon), Linux (amd64 & arm64), and Windows: pushing a `v*` tag triggers the release workflow. CI runs `go vet`, tests, and `golangci-lint` on every PR.
