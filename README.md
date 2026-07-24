# tup

Turn Telegram chats, groups, and channels into S3-style cloud storage drives with a
POSIX-like virtual filesystem (VFS).

## Install

**From a release binary** (no Python required) — download the archive for your OS from
the [releases page](../../releases), extract it, and run the bundled installer:

```bash
tar -xzf tup-v0.0.1-<os-arch>.tar.gz && cd tup-<os-arch>
./install.sh              # installs to ~/.tup/bin and symlinks `tup` onto your PATH
```

On Windows, extract the `.zip` and run `install.ps1` instead.

**From source**:

```bash
uv sync --all-extras
uv run tup setup          # interactive wizard: validates your bot token live
```

First run without configuration opens the setup wizard (`tup setup` in the terminal, or
a graphical wizard when launching `tup gui`). It walks you through the @BotFather bot
token and the my.telegram.org API ID/hash needed for 2 GB uploads. Configuration lands
in `~/.tup/.env` with `0600` permissions; the SQLite index lives at `~/.tup/registry.db`.

## Usage

```bash
# Drives (chat aliases)
tup chat add work -1001234567890
tup chat list
tup chat remove work

# Uploads
tup up report.pdf --to work --dest /docs
tup report.pdf                  # shortcut: unknown tokens fall through to `up`
tup up ./photos --to work      # a folder mounts under its own name: /photos/

# Browse & manage the VFS (local index, instant)
tup tree work
tup ls work /docs -R
tup mkdir work /inbox
tup cp work /docs/report.pdf /archive/     # server-side copy, no re-upload
tup mv work /docs/report.pdf /archive/     # path move only (no renames)
tup rm work /archive/report.pdf            # → Recycle Bin (/.Trash/); --force purges
tup trash list work                        # list / restore / empty the bin
tup rmdir work /inbox

# Captions, tags, editing & versions
tup caption work /docs/report.pdf "Q3 numbers #finance #q3"
tup tag work /docs/report.pdf urgent       # append a hashtag
tup ls work --tag finance                  # drive-wide tag filter
tup edit work /notes/todo.md               # $EDITOR round-trip; old revision kept
tup versions work /notes/todo.md           # history; --restore <id> brings one back

# Backups (gzipped-JSON registry dump stored on the drive itself)
tup backup work                            # → /Backups/tup-backup-<stamp>.json.gz
tup backup work --restore /Backups/tup-backup-20260721-120000.json.gz

# Sync & reconcile
tup sync ./backup work /backup             # skips files whose SHA-256 already matches
tup index work --reconstruct

# Failures & audit
tup failed
tup retry
tup logs --limit 50 --chat work
```

## Behavior notes

- **One upload transport: MTProto.** All uploads and server-side copies go through
  Telethon (MTProto) using your bot token plus `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`
  from https://my.telegram.org — uniform 2 GB cap, no local Bot API server, no Docker.
  The Bot API is still used internally for metadata (chat validation, caption edits,
  deletes, update draining).
- **Media stays browsable.** Images, videos, and audio are detected by magic bytes and
  sent as native media (videos with streaming support), so they appear in Telegram's
  media gallery. Use `--as-doc` to force original-quality document uploads
  (note: Telegram recompresses photos sent as photos).
- **`tup <path>` fallback.** Unknown first tokens are routed to `tup up`. Known command
  names always win — to upload a file literally named `tree`, use `tup up tree`.
- **`mv` is path-only.** Telegram fixes a file's download name at upload time; `mv`
  updates the virtual path (DB + remote caption) but cannot rename the file.
- **`tup index` limitation.** The Bot API has no message-history endpoint, so `index`
  can only drain pending updates (`getUpdates`) — it applies native caption edits and,
  with `--reconstruct`, indexes tup-captioned messages it hasn't seen. It cannot scan
  arbitrary old history.

## Development

```bash
uv run pytest             # full suite (Telegram API fully mocked — never hits the network)
uv run ruff check .
uv run ruff format .
uv run mypy src tests
```

See `CLAUDE.md` for the full architecture specification.
