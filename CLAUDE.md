# tup — AI Architecture & Implementation Specification (`CLAUDE.md`)

> **Convention Note**: This file is named `CLAUDE.md` (uppercase) following standard Claude Code project conventions. It serves as the authoritative, imperative architectural blueprint for AI coding assistants.

## 1. Project Overview & Tech Stack
Build `tup`, a resilient Python CLI that transforms Telegram chats, groups, and channels into S3-style cloud storage drives and POSIX-like Virtual Filesystems (VFS).
- **Language**: Python 3.12+
- **Package Manager**: `uv` (strictly via `pyproject.toml` and `uv.lock`)
- **Telegram Client (metadata)**: `python-telegram-bot>=20.8` (async/await native)
- **Upload Transport**: `telethon>=1.36` (MTProto, bot-token login, 2 GB uploads)
- **Database Driver**: `aiosqlite>=0.19.0` (non-blocking async SQLite)
- **CLI Framework**: `typer>=0.12,<1.0` (wrapped with `asyncio.run()`)
- **Terminal UI**: `rich>=13.0.0` (progress bars, tables, trees, logging handler)
- **Interactive TUI**: `questionary>=2.0.0`
- **Config & Validation**: `pydantic-settings>=2.0.0` & `python-dotenv>=1.0.0`
- **MIME Detection**: `filetype>=1.2.0` (magic-byte header inspection, with fallback to standard library `mimetypes`)

## 2. Project Structure
```text
tup/
├── pyproject.toml
├── uv.lock
├── .env                 # Gitignored, generated with 0600 permissions
├── README.md
├── CONTRIBUTING.md
├── LICENSE
├── CLAUDE.md            # Uppercase per Claude Code convention
└── src/
    └── tup/
        ├── __init__.py
        ├── __main__.py  # Enables `python -m tup`
        ├── cli.py       # Typer commands & async execution bridge
        ├── config.py    # Pydantic Settings & schema validation
        ├── database.py  # Async SQLite pool, migrations & indexed queries
        ├── setup.py     # Questionary TUI wizard (safe: src layout, no root setup.py)
        ├── uploader.py  # PTB Bot lifecycle, routing & VFS captions
        ├── progress.py  # Rich UI & ProgressFileReader wrapper
        └── utils.py     # MIME detection, SHA-256 hashing & secret scrubber
```

## 3. Environment & Security Model (`config.py`)

* **Execution Gate**: On startup, call `Settings.load()`. If `.env` is missing or fails validation, trigger `setup.py` TUI wizard, validate Bot Token live via `bot.get_me()`, write `.env`, and immediately apply `0600` filesystem permissions (`os.chmod(".env", 0o600)`).
* **Pydantic Settings Schema**:
```python
from pathlib import Path
from typing import Optional
from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    telegram_bot_token: SecretStr = Field(..., description="Telegram Bot API Token")
    default_chat_id: Optional[str] = Field(None, description="Default numeric ID or alias")
    default_chat_type: str = Field("group", pattern="^(group|user|channel)$")
    telegram_api_base_url: Optional[HttpUrl] = Field(None, description="Local Bot API URL for 2GB limits")
    max_retries: int = Field(3, ge=1, le=10)
    request_timeout: int = Field(120, ge=10, le=3600)
    database_path: Path = Field(default_factory=lambda: Path("~/.tui/registry.db").expanduser())
    log_level: str = Field("INFO", pattern="^(DEBUG|INFO|WARNING|ERROR)$")
```

* **Security & Secret Scrubbing**:
* Tokens MUST NEVER be printed in raw form. In CLI outputs, mask using `token.get_secret_value()[:4] + "..." + token.get_secret_value()[-4:]`.
* Implement a `SecretScrubberFormatter` for Python `logging` that intercepts all log strings and regex-replaces any string matching `TELEGRAM_BOT_TOKEN` or `bot<token>` URLs with `[SCRUBBED_TOKEN]`.
* Ensure `~/.tui/registry.db` also receives `0600` permissions upon initial creation.
* **Home Directory (`~/.tui`)**: `.env`, `registry.db`, `tup.log`, the MTProto session file, and the GUI's per-drive download cache (`~/.tui/<chat_id>/<vfs folders>/<file>`) all live in the hidden `~/.tui` directory (override: `TUP_CONFIG_DIR`; cache-only override: `TUP_CACHE_DIR`). On startup, `migrate_legacy_config()` moves any pre-0.3 files from `~/.config/tup` into `~/.tui` exactly once, never overwriting existing destinations.

## 4. PTB Lifecycle & Telegram Edge Cases

* **PTB Application Lifecycle**: Do NOT instantiate a long-lived polling `Application` event loop. Since `tup` is a CLI tool, construct a short-lived `Application` per command using the async context manager to ensure clean connection teardown. Note: `telegram.Bot` does not support `async with` directly in v20+.
```python
app = Application.builder().token(token).base_url(base_url).build()
async with app:
    await app.bot.send_document(...)
```
* **Bot Not in Chat / Kicked**: Catch `telegram.error.Forbidden` and `telegram.error.BadRequest`. Return a clean `Rich` panel: `❌ Error: Bot lacks access to chat [ID]. Ensure it is added as a member (Groups) or Administrator (Channels).`
* **Channels vs. Groups**: Channels require the bot to be an **Administrator** with `Post Messages` and `Edit Messages` rights. Groups require standard member access. Check permissions dynamically before batch operations.
* **Media Groups (Albums)**: When multiple photos/videos are sent simultaneously as an album, Telegram assigns each asset a unique `file_id` and `message_id`. Treat each item in an album as an independent file record in `vfs_index`.
* **Edited Messages**: If a VFS caption is modified natively in Telegram, `tup index` must detect the discrepancy via `message.edit_date` and update the SQLite row without re-downloading the binary.
* **History Limitation (Bot API)**: The Telegram Bot API has **no message-history-fetch method** (that is MTProto territory). A bot can only observe (a) pending updates via `getUpdates` and (b) messages it sent itself. Therefore `tup index` operates by draining pending `getUpdates` (bounded by `sync_state.last_scanned_message_id`) and reconciling from parsed VFS captions plus `uploads_log`. This limitation MUST be stated in the `index` command's help text.

## 5. Core VFS Invariants & Media Routing

* **Drive Model**: Every Telegram `chat_id` represents an isolated drive bucket.
* **VFS Path Rule**: All paths MUST be root-relative POSIX paths starting with `/` (e.g., `/docs/file.pdf`). Uploading a local folder `/home/user/code` mounts at root: `/code/`.
* **Media Routing & MIME Fallback**: Use `filetype.guess()` to inspect magic bytes and route via `send_photo`, `send_video`, `send_audio`, or `send_document`. Because `filetype` is unmaintained (last release 2023) and may miss newer formats or extension-only definitions, implement a mandatory fallback to Python's built-in `mimetypes.guess_type(file_path)` when `filetype.guess()` returns `None`. Allow CLI override flags (`--as-doc`, `--as-video`, `--as-audio`).
* **Upload Transport (MTProto)**: All uploads and server-side copies run over MTProto via `telethon` (bot-token login; requires `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` from my.telegram.org). Uniform 2 GB cap — abort with a `Rich` error above 2 GB. The Bot API (PTB) is retained only for metadata operations: chat validation, `edit_message_caption`, `delete_message`, and `getUpdates` draining. MTProto message IDs are chat-scoped and shared with the Bot API, so mv/rm interoperate; MTProto responses carry no Bot API `file_id` (store `""` in `vfs_index`), and `cp` re-sends the source message's media object instead.
* **Same-Folder Deduplication**: Two files with the same SHA-256 MUST NOT coexist in one virtual directory. `up` and `cp` check `vfs_index` by hash before any network work and raise `DuplicateFileError` (a `TupError` subclass) — it is a *skip*, not a failure: nothing is written to `failed_registry`, `sync` counts it as unchanged, and the GUI transfer queue marks the item "skipped".
* **VFS Caption Protocol**: Every uploaded file MUST append this exact metadata block:
```text
📁 `/virtual/path/file_name.ext`
🔗 SHA256: <64-char-hex-hash>

<Optional Caption User>

#vfs #folder_name
```

## 6. SQLite Schemas, Indexes & Migrations (`database.py`)

Initialize on startup using `aiosqlite`. Include a schema versioning table and performance indexes for hot query paths.

* **Timestamp Convention**: All timestamp columns (`upload_timestamp`, `created_at`, etc.) MUST store UTC times formatted as strict ISO 8601 strings (`YYYY-MM-DDTHH:MM:SSZ`), generated via `datetime.now(timezone.utc).isoformat()`.

```sql
-- Migration Tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Core Tables
CREATE TABLE IF NOT EXISTS chat_aliases (
    alias TEXT PRIMARY KEY,
    chat_id TEXT UNIQUE NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vfs_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    virtual_path TEXT NOT NULL,         -- MUST end with trailing slash '/'
    file_name TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    file_hash TEXT NOT NULL,            -- SHA-256
    telegram_file_id TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    upload_timestamp TEXT NOT NULL,
    -- v2 file attributes (added by migrate_v1_to_v2 via ALTER TABLE; defaults keep v1 rows valid)
    mime_type TEXT NOT NULL DEFAULT '',
    media_kind TEXT NOT NULL DEFAULT '',  -- document | photo | video | audio ('' for .keep rows)
    width INTEGER,                        -- pixels (photo/video)
    height INTEGER,
    duration INTEGER,                     -- seconds (video/audio)
    source_mtime TEXT NOT NULL DEFAULT '',-- ISO 8601 mtime of the uploaded local file
    UNIQUE(chat_id, virtual_path, file_name)
);

CREATE TABLE IF NOT EXISTS uploads_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    upload_type TEXT NOT NULL,
    status TEXT NOT NULL,               -- 'success' | 'failed'
    error_message TEXT,
    telegram_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS failed_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    file_path TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    caption TEXT,
    upload_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    retry_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending'       -- 'pending' | 'resolved' | 'abandoned'
);

CREATE TABLE IF NOT EXISTS sync_state (
    chat_id TEXT PRIMARY KEY,
    last_scanned_message_id INTEGER DEFAULT 0,
    last_sync_timestamp TEXT NOT NULL
);

-- Hot Query Path Indexes
CREATE INDEX IF NOT EXISTS idx_vfs_path ON vfs_index(chat_id, virtual_path);
CREATE INDEX IF NOT EXISTS idx_vfs_hash ON vfs_index(chat_id, file_hash);
CREATE INDEX IF NOT EXISTS idx_failed_status ON failed_registry(status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_logs_chat_time ON uploads_log(chat_id, timestamp DESC);
```

* **Schema Versioning / Migrations**: On connection, check `MAX(version)` in `schema_version`. If empty, run baseline creation and insert `version = 1`. For future schema changes, sequentially execute migration functions (`migrate_v1_to_v2()`, etc.) within an atomic transaction.

## 7. CLI Command Signatures (`cli.py`)

Resolve `<drive>` arguments via `chat_aliases` table. If match found, use integer `chat_id`; otherwise treat input as raw `chat_id`. Define Typer handlers synchronously and wrap core async logic:

```python
@app.command()
def tree(drive: str, path: str = "/", level: Optional[int] = typer.Option(None, "-L", "--level")):
    asyncio.run(async_tree_handler(drive, path, level))
```

* **Upload Invocation (`tup up`)**: A top-level positional argument cannot coexist with Click subcommands, so uploads use an explicit `tup up <path>` command. For ergonomics, the Typer app uses a custom `TyperGroup` subclass whose `resolve_command` catches `click.UsageError` (unknown command token) and falls back to the `up` command without consuming the token — so `tup somefile.pdf` still works. **Known command names always win**: a local file literally named `tree` must be uploaded via `tup up tree`.

```bash
# Drive & Alias Management
tup chat add <alias> <chat_id>          # Validate chat, fetch title, save to DB
tup chat list                           # Rich table: Alias | ID | Title | Date
tup chat remove <alias>

# Standard Uploads
tup up <path> [--to <drive>] [--dest <vfs_path>] [--as-doc|--as-video] [--silent]
tup <path> ...                          # Fallback alias for `tup up` (unknown tokens only)

# POSIX VFS Operations (Operate on local SQLite vfs_index + remote Telegram state)
tup tree <drive> [path] [-L|--level <int>]  # -L 0 = root leaves only, -L 1 = 1 level down
tup ls <drive> [path] [-R|--recursive]      # POSIX ls -lh style Rich table output
tup mkdir <drive> <path>                    # Create hidden '.keep' index entry
tup cp <drive> <src> <dest>                 # Duplicate via send_document(file_id=...), no re-upload
tup mv <drive> <src> <dest>                 # Update DB + remote edit_message_caption.
                                            # NOTE: Cannot rename file_name (Telegram API limit); path changes only.
tup rm <drive> <path>                       # Delete DB row + remote delete_message
tup rmdir <drive> <path>                    # Remove '.keep' row. Error if folder not empty

# Sync & Reconciliation
tup sync <local_dir> <drive> <remote_path>  # S3-style upload. Skip if local SHA256 matches DB
tup index <drive> [--reconstruct]           # Reconcile DB via getUpdates drain (see §4 History Limitation)

# Admin & Retries
tup failed                              # Rich table of pending failed uploads
tup retry [--id <int>]                  # Re-attempt failed_registry items
tup logs                                # Show last 20 audit log entries
```

## 8. Error Handling & Structured Logging

* **Philosophy**: Fail fast on validation/configuration faults. Be resilient and self-healing on network boundaries. Never print raw tracebacks to the console unless `--debug` is explicitly passed.
* **Transient Errors**: Catch `telegram.error.RetryAfter`. Extract `retry_after`, log a yellow warning, execute `asyncio.sleep(retry_after)`, and retry up to `MAX_RETRIES`. For `TimedOut` or `NetworkError`, apply exponential backoff ($2^n$ seconds).
* **Terminal Failures**: If retries are exhausted, write the payload to `failed_registry` with `status='pending'`, record an entry in `uploads_log` with `status='failed'`, and display a clean `Rich` error panel explaining how to re-run via `tup retry`.
* **Structured Logging**: Configure `logging` using `rich.logging.RichHandler` for stdout and a file handler writing JSON-lines to `~/.config/tup/tup.log`. All file log outputs MUST pass through the `SecretScrubberFormatter`.

## 9. Testing Strategy & Mocking

* **Unit Testing (`tests/unit/`)**:
* Test VFS path normalization (`/`, leading/trailing slashes, parent directory traversal execution).
* Validate SHA-256 calculation accuracy against known file fixtures.
* Verify `filetype` magic-byte MIME routing logic against synthetic header bytes, ensuring fallback to `mimetypes` triggers correctly on unknown headers.
* Test `SecretScrubberFormatter` to guarantee token strings are completely stripped.

* **Integration Testing (`tests/integration/`)**:
* Execute SQLite migrations against an in-memory database (single long-lived `aiosqlite` connection with `":memory:"` — the `Database` class owns one connection, so shared-cache URIs are unnecessary).
* Verify CRUD operations, unique constraints, and cascade deletions across `vfs_index` and `failed_registry`.

* **Mocking Telegram (`tests/mocks/`)**:
* **NEVER** hit the live Telegram Bot API during test runs.
* Use `pytest-asyncio` paired with `respx` to intercept HTTP requests to `https://api.telegram.org/bot*` and return standardized JSON fixtures (`{"ok": true, "result": {"message_id": 101, ...}}`).
* Provide an `mock_bot` Pytest fixture that yields an `AsyncMock` instance of `telegram.Bot`.

## 10. Development Workflow & Tooling (`pyproject.toml`)

* **Commands**:
```bash
uv sync --all-extras              # Install dependencies with dev and test extras
uv run pytest                     # Execute full test suite
uv run ruff check .               # Lint codebase
uv run ruff format .              # Format codebase
uv run mypy src                   # Strict static type checking
```

* **Tooling Configuration**:
```toml
[project]
name = "tup"
version = "0.2.0"
description = "Telegram S3-style Virtual Filesystem and Uploader CLI"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "python-telegram-bot>=20.8",
    "typer>=0.12,<1.0",
    "rich>=13.0.0",
    "python-dotenv>=1.0.0",
    "pydantic-settings>=2.0.0",
    "questionary>=2.0.0",
    "filetype>=1.2.0",
    "aiosqlite>=0.19.0",
]

[project.optional-dependencies]
dev = [
    "ruff>=0.4.0",
    "mypy>=1.10.0",
]
test = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "respx>=0.21.0",
]

[project.scripts]
tup = "tup.cli:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
# NOTE: security rules use the "S" prefix (flake8-bandit); "SEC" does not exist in ruff.
select = ["E", "F", "I", "N", "W", "UP", "ASYNC", "B", "C4", "S"]
ignore = ["E501"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101"]  # assert is expected in tests

[tool.mypy]
python_version = "3.12"
strict = true
disallow_untyped_defs = true
warn_return_any = true
warn_unused_configs = true

# questionary and filetype ship no type stubs
[[tool.mypy.overrides]]
module = ["questionary.*", "filetype.*"]
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```
