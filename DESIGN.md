# tup - Architecture & Design

`tup` has been entirely rewritten from Python to Go to provide a single, statically-compiled binary with zero external runtime dependencies. 

## Core Architecture

### 1. `cmd/tup/main.go`
The entry point of the CLI application. Initializes the `core.Config` and `core.DB` sequentially before dispatching to the `cli.RootCmd`.

### 2. `internal/cli/`
Utilizes `spf13/cobra` to define all POSIX commands.
- **`posix.go`**: Contains `cp`, `ls`, `rm`, `mkdir`, `tree` (with `--json` export), utilizing the internal `vfs` and `telegram` packages to route local paths vs remote aliases.
- **`sync.go`**: Contains `tup sync`, `tup conflicts`, `tup resolve` (with `--ours`, `--theirs`, `--keep-both`), and `AutoSync` helper.
- **`setup.go`**: Contains `tup login` for interactive `pterm` prompts.
- **`drive.go`**: Contains `tup drive add`, `tup drive list`, and `tup drive format`.
- **`backup.go`**: Contains `tup backup <alias>` for creating VFS snapshot payloads (`OpSNAPSHOT`) and JSON exports.
- **`ai.go`**: Contains `tup ai` for generating `.tup-skill.md`.

### 3. `internal/core/`
- **`config.go`**: Uses `viper` to read `~/.tup/.env` natively.
- **`db.go`**: Embeds `modernc.org/sqlite` (pure Go SQLite, no CGO) maintaining `vfs_entries`, `sync_state`, `vfs_operations_log`, and `vfs_conflicts`.
- **`vfs.go`**: Helper functions to traverse paths, map parent-child relationships, and abstract SQL queries.

### 4. `internal/telegram/`
- **`mtproto.go`**: The core `gotd/td` engine for native 2GB uploads/downloads and document captions.
- **`operation.go`**: Defines `Operation` struct (`CP`, `MKDIR`, `RM`, `MV`, `SNAPSHOT`, `RESOLVE`, `FORMAT`) and SHA-256 commit hashing.
- **`sync.go`**: Core event-sourced incremental sync engine (`SyncDrive` & `ApplyOperation`).
- **`format.go`**: Chat formatting logic with rate-limit handling and `OpFORMAT` sentinel emission.

## Data Flow (Event-Sourced Uploads & Auto-Sync)
1. `tup cp local.txt work:/docs/remote.txt`
2. `vfs.ParsePath` detects a local to remote transfer.
3. `core.GetChatID` looks up the `work` alias in SQLite.
4. `telegram.UploadFileMTProtoWithCaption` uploads the file to Telegram accompanied by an operation payload (`TUP_OP:{"v":1,"op":"CP",...}`).
5. `SyncDrive` automatically pulls operations posted after `last_synced_msg_id` on other devices and updates `vfs.db` in single-transaction steps.

## Conflict Resolution & Snapshots
1. **Conflict Detection**: Occurs when an operation payload target path was modified concurrently on another device (`prev_hash` mismatch). `tup` highlights conflicts via `pterm` banners and halts until resolved via `tup resolve`.
2. **Snapshot Compaction**: Every $N$ operations or explicit `tup backup <alias>`, `tup` uploads `tup_snapshot_<hash>.json` with an `OpSNAPSHOT` commit marker so new devices fast-forward initial sync in 1 second.
