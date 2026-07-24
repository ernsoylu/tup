"""Async SQLite layer: single-connection Database class, migrations, typed CRUD."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

import aiosqlite

from tup.utils import utc_now_iso

_BASELINE_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_aliases (
    alias TEXT PRIMARY KEY,
    chat_id TEXT UNIQUE NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vfs_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    virtual_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    file_hash TEXT NOT NULL,
    telegram_file_id TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    upload_timestamp TEXT NOT NULL,
    UNIQUE(chat_id, virtual_path, file_name)
);

CREATE TABLE IF NOT EXISTS uploads_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    upload_type TEXT NOT NULL,
    status TEXT NOT NULL,
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
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'resolved', 'abandoned'))
);

CREATE TABLE IF NOT EXISTS sync_state (
    chat_id TEXT PRIMARY KEY,
    last_scanned_message_id INTEGER DEFAULT 0,
    last_sync_timestamp TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vfs_path ON vfs_index(chat_id, virtual_path);
CREATE INDEX IF NOT EXISTS idx_vfs_hash ON vfs_index(chat_id, file_hash);
CREATE INDEX IF NOT EXISTS idx_failed_status ON failed_registry(status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_logs_chat_time ON uploads_log(chat_id, timestamp DESC);
"""

# v2: file attributes for the GUI — MIME, media kind, dimensions, duration,
# and the source file's modification time. Nullable/defaulted so v1 rows and
# .keep entries stay valid.
_MIGRATION_V2_SQL = """
ALTER TABLE vfs_index ADD COLUMN mime_type TEXT NOT NULL DEFAULT '';
ALTER TABLE vfs_index ADD COLUMN media_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE vfs_index ADD COLUMN width INTEGER;
ALTER TABLE vfs_index ADD COLUMN height INTEGER;
ALTER TABLE vfs_index ADD COLUMN duration INTEGER;
ALTER TABLE vfs_index ADD COLUMN source_mtime TEXT NOT NULL DEFAULT '';
"""

# v3: cloud parity — user captions & tags (parsed from caption hashtags),
# message ownership (origin: 'upload' | 'observed'), and file version history
# (superseded Telegram messages of edited files, capped by the ops layer).
# Conventions match tup-cloud so all frontends interoperate on one chat.
_MIGRATION_V3_SQL = """
ALTER TABLE vfs_index ADD COLUMN user_caption TEXT NOT NULL DEFAULT '';
ALTER TABLE vfs_index ADD COLUMN tags TEXT NOT NULL DEFAULT '';
ALTER TABLE vfs_index ADD COLUMN origin TEXT NOT NULL DEFAULT 'upload';

CREATE TABLE IF NOT EXISTS file_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES vfs_index(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    file_hash TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    saved_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_versions_entry ON file_versions(entry_id);
"""

SCHEMA_VERSION = 3


class DatabaseError(RuntimeError):
    """Domain error for constraint violations and invalid database operations."""


@dataclass(frozen=True)
class ChatAlias:
    alias: str
    chat_id: str
    title: str | None
    created_at: str


@dataclass(frozen=True)
class VfsEntry:
    id: int
    chat_id: str
    virtual_path: str
    file_name: str
    file_size: int
    file_hash: str
    telegram_file_id: str
    telegram_message_id: int
    upload_timestamp: str
    mime_type: str = ""
    media_kind: str = ""  # document | photo | video | audio ('' for .keep rows)
    width: int | None = None
    height: int | None = None
    duration: int | None = None  # seconds
    source_mtime: str = ""  # ISO 8601 mtime of the uploaded local file
    user_caption: str = ""  # free text shown under the protocol block
    tags: str = ""  # space-separated lowercase hashtags, no '#'
    origin: str = "upload"  # 'upload' (tup owns the message) | 'observed'


@dataclass(frozen=True)
class FileVersion:
    """A superseded Telegram message of an edited file (the old revision)."""

    id: int
    entry_id: int
    chat_id: str
    telegram_message_id: int
    file_hash: str
    file_size: int
    saved_by: str
    created_at: str


@dataclass(frozen=True)
class FailedUpload:
    id: int
    timestamp: str
    file_path: str
    chat_id: str
    caption: str | None
    upload_type: str
    error_message: str
    retry_count: int
    status: str


@dataclass(frozen=True)
class UploadLogEntry:
    id: int
    timestamp: str
    file_path: str
    file_size: int
    chat_id: str
    upload_type: str
    status: str
    error_message: str | None
    telegram_message_id: int | None


def _prepare_db_path(path: Path | str) -> bool:
    """Ensure the parent directory exists; return True if the DB file is new."""
    if str(path) == ":memory:":
        return False
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return not db_path.exists()


class Database:
    """Owns a single long-lived aiosqlite connection; use as an async context manager."""

    def __init__(self, path: Path | str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> Database:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise DatabaseError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        created = _prepare_db_path(self._path)
        self._conn = await aiosqlite.connect(str(self._path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._migrate()
        if created:
            os.chmod(self._path, 0o600)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _migrate(self) -> None:
        conn = self.conn
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        async with conn.execute("SELECT MAX(version) AS v FROM schema_version") as cur:
            row = await cur.fetchone()
        current = row["v"] if row is not None and row["v"] is not None else 0
        if current < 1:
            await conn.executescript(_BASELINE_SQL)
            await conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (1, utc_now_iso()),
            )
        if current < 2:
            await conn.executescript(_MIGRATION_V2_SQL)
            await conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (2, utc_now_iso()),
            )
        if current < 3:
            await conn.executescript(_MIGRATION_V3_SQL)
            await conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (3, utc_now_iso()),
            )
        await conn.commit()

    # --- chat aliases ---------------------------------------------------------

    async def alias_add(self, alias: str, chat_id: str, title: str | None) -> None:
        try:
            await self.conn.execute(
                "INSERT INTO chat_aliases (alias, chat_id, title, created_at) VALUES (?, ?, ?, ?)",
                (alias, chat_id, title, utc_now_iso()),
            )
            await self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DatabaseError(f"Alias or chat_id already registered: {alias!r}") from exc

    async def alias_get(self, alias: str) -> ChatAlias | None:
        async with self.conn.execute("SELECT * FROM chat_aliases WHERE alias = ?", (alias,)) as cur:
            row = await cur.fetchone()
        return ChatAlias(**dict(row)) if row else None

    async def alias_list(self) -> list[ChatAlias]:
        async with self.conn.execute("SELECT * FROM chat_aliases ORDER BY alias") as cur:
            rows = await cur.fetchall()
        return [ChatAlias(**dict(r)) for r in rows]

    async def alias_remove(self, alias: str) -> bool:
        cur = await self.conn.execute("DELETE FROM chat_aliases WHERE alias = ?", (alias,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def resolve_drive(self, drive: str) -> str:
        """Resolve an alias to its chat_id; unknown aliases pass through as raw IDs."""
        found = await self.alias_get(drive)
        return found.chat_id if found else drive

    # --- vfs index ------------------------------------------------------------

    async def vfs_upsert(
        self,
        chat_id: str,
        virtual_path: str,
        file_name: str,
        file_size: int,
        file_hash: str,
        telegram_file_id: str,
        telegram_message_id: int,
        *,
        mime_type: str = "",
        media_kind: str = "",
        width: int | None = None,
        height: int | None = None,
        duration: int | None = None,
        source_mtime: str = "",
        user_caption: str = "",
        tags: str = "",
        origin: str = "upload",
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO vfs_index
                (chat_id, virtual_path, file_name, file_size, file_hash,
                 telegram_file_id, telegram_message_id, upload_timestamp,
                 mime_type, media_kind, width, height, duration, source_mtime,
                 user_caption, tags, origin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, virtual_path, file_name) DO UPDATE SET
                file_size = excluded.file_size,
                file_hash = excluded.file_hash,
                telegram_file_id = excluded.telegram_file_id,
                telegram_message_id = excluded.telegram_message_id,
                upload_timestamp = excluded.upload_timestamp,
                mime_type = excluded.mime_type,
                media_kind = excluded.media_kind,
                width = excluded.width,
                height = excluded.height,
                duration = excluded.duration,
                source_mtime = excluded.source_mtime,
                user_caption = excluded.user_caption,
                tags = excluded.tags,
                origin = excluded.origin
            """,
            (
                chat_id,
                virtual_path,
                file_name,
                file_size,
                file_hash,
                telegram_file_id,
                telegram_message_id,
                utc_now_iso(),
                mime_type,
                media_kind,
                width,
                height,
                duration,
                source_mtime,
                user_caption,
                tags,
                origin,
            ),
        )
        await self.conn.commit()

    async def vfs_get(self, chat_id: str, virtual_path: str, file_name: str) -> VfsEntry | None:
        async with self.conn.execute(
            "SELECT * FROM vfs_index WHERE chat_id = ? AND virtual_path = ? AND file_name = ?",
            (chat_id, virtual_path, file_name),
        ) as cur:
            row = await cur.fetchone()
        return VfsEntry(**dict(row)) if row else None

    async def vfs_get_by_message(self, chat_id: str, message_id: int) -> VfsEntry | None:
        async with self.conn.execute(
            "SELECT * FROM vfs_index WHERE chat_id = ? AND telegram_message_id = ?",
            (chat_id, message_id),
        ) as cur:
            row = await cur.fetchone()
        return VfsEntry(**dict(row)) if row else None

    async def vfs_list_dir(self, chat_id: str, virtual_path: str) -> list[VfsEntry]:
        """Entries directly inside a directory (exact virtual_path match)."""
        async with self.conn.execute(
            "SELECT * FROM vfs_index WHERE chat_id = ? AND virtual_path = ? ORDER BY file_name",
            (chat_id, virtual_path),
        ) as cur:
            rows = await cur.fetchall()
        return [VfsEntry(**dict(r)) for r in rows]

    async def vfs_list_prefix(self, chat_id: str, prefix: str) -> list[VfsEntry]:
        """All entries at or below a directory prefix (recursive listing)."""
        async with self.conn.execute(
            """
            SELECT * FROM vfs_index
            WHERE chat_id = ? AND virtual_path >= ? AND virtual_path < ?
            ORDER BY virtual_path, file_name
            """,
            (chat_id, prefix, prefix[:-1] + "0"),  # '/' + 1 == '0' in ASCII: range scan on index
        ) as cur:
            rows = await cur.fetchall()
        return [VfsEntry(**dict(r)) for r in rows]

    async def vfs_move(self, entry_id: int, new_virtual_path: str, new_file_name: str) -> None:
        try:
            await self.conn.execute(
                "UPDATE vfs_index SET virtual_path = ?, file_name = ? WHERE id = ?",
                (new_virtual_path, new_file_name, entry_id),
            )
            await self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DatabaseError("Destination already exists") from exc

    async def vfs_delete(self, entry_id: int) -> None:
        await self.conn.execute("DELETE FROM vfs_index WHERE id = ?", (entry_id,))
        await self.conn.commit()

    async def vfs_find_by_hash(self, chat_id: str, file_hash: str) -> list[VfsEntry]:
        async with self.conn.execute(
            "SELECT * FROM vfs_index WHERE chat_id = ? AND file_hash = ?",
            (chat_id, file_hash),
        ) as cur:
            rows = await cur.fetchall()
        return [VfsEntry(**dict(r)) for r in rows]

    async def vfs_set_caption(self, entry_id: int, user_caption: str, tags: str) -> None:
        await self.conn.execute(
            "UPDATE vfs_index SET user_caption = ?, tags = ? WHERE id = ?",
            (user_caption, tags, entry_id),
        )
        await self.conn.commit()

    async def vfs_replace_content(
        self,
        entry_id: int,
        file_size: int,
        file_hash: str,
        telegram_message_id: int,
        mime_type: str,
    ) -> None:
        """Point an entry at a new Telegram message (save-through edit).

        Matches tup-cloud save semantics: the row is mutated in place, media
        kind becomes 'document', origin flips to 'upload' (tup now owns the
        message), and upload_timestamp advances.
        """
        await self.conn.execute(
            """
            UPDATE vfs_index SET file_size = ?, file_hash = ?, telegram_message_id = ?,
                telegram_file_id = '', mime_type = ?, media_kind = 'document',
                origin = 'upload', upload_timestamp = ?
            WHERE id = ?
            """,
            (file_size, file_hash, telegram_message_id, mime_type, utc_now_iso(), entry_id),
        )
        await self.conn.commit()

    async def vfs_update_message(
        self, entry_id: int, file_size: int, file_hash: str, telegram_message_id: int
    ) -> None:
        """Repoint an entry at another message without touching mime/kind
        (index reconstruction: a newer same-path message becomes current)."""
        await self.conn.execute(
            """
            UPDATE vfs_index SET file_size = ?, file_hash = ?, telegram_message_id = ?,
                telegram_file_id = '', upload_timestamp = ?
            WHERE id = ?
            """,
            (file_size, file_hash, telegram_message_id, utc_now_iso(), entry_id),
        )
        await self.conn.commit()

    async def vfs_list_by_tag(self, chat_id: str, tag: str) -> list[VfsEntry]:
        """Entries whose space-separated tags column contains `tag` (lowercased)."""
        needle = tag.lstrip("#").lower()
        async with self.conn.execute(
            """
            SELECT * FROM vfs_index
            WHERE chat_id = ? AND ' ' || tags || ' ' LIKE ?
            ORDER BY virtual_path, file_name
            """,
            (chat_id, f"% {needle} %"),
        ) as cur:
            rows = await cur.fetchall()
        return [VfsEntry(**dict(r)) for r in rows]

    # --- file versions ---------------------------------------------------------

    async def version_add(
        self,
        entry_id: int,
        chat_id: str,
        telegram_message_id: int,
        file_hash: str,
        file_size: int,
        saved_by: str = "",
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO file_versions
                (entry_id, chat_id, telegram_message_id, file_hash, file_size,
                 saved_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (entry_id, chat_id, telegram_message_id, file_hash, file_size, saved_by, utc_now_iso()),
        )
        await self.conn.commit()

    async def version_list(self, entry_id: int) -> list[FileVersion]:
        """Versions of an entry, newest first."""
        async with self.conn.execute(
            "SELECT * FROM file_versions WHERE entry_id = ? ORDER BY id DESC", (entry_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [FileVersion(**dict(r)) for r in rows]

    async def version_get(self, version_id: int) -> FileVersion | None:
        async with self.conn.execute(
            "SELECT * FROM file_versions WHERE id = ?", (version_id,)
        ) as cur:
            row = await cur.fetchone()
        return FileVersion(**dict(row)) if row else None

    async def version_delete(self, version_id: int) -> None:
        await self.conn.execute("DELETE FROM file_versions WHERE id = ?", (version_id,))
        await self.conn.commit()

    async def versions_over_cap(self, entry_id: int, cap: int) -> list[FileVersion]:
        """Versions beyond the newest `cap` (candidates for pruning)."""
        return (await self.version_list(entry_id))[cap:]

    # --- uploads log ----------------------------------------------------------

    async def log_upload(
        self,
        file_path: str,
        file_size: int,
        chat_id: str,
        upload_type: str,
        status: str,
        error_message: str | None = None,
        telegram_message_id: int | None = None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO uploads_log
                (timestamp, file_path, file_size, chat_id, upload_type,
                 status, error_message, telegram_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                file_path,
                file_size,
                chat_id,
                upload_type,
                status,
                error_message,
                telegram_message_id,
            ),
        )
        await self.conn.commit()

    async def log_recent(self, limit: int = 20, chat_id: str | None = None) -> list[UploadLogEntry]:
        query = "SELECT * FROM uploads_log"
        params: list[Any] = []
        if chat_id is not None:
            query += " WHERE chat_id = ?"
            params.append(chat_id)
        query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [UploadLogEntry(**dict(r)) for r in rows]

    # --- failed registry ------------------------------------------------------

    async def failed_add(
        self,
        file_path: str,
        chat_id: str,
        caption: str | None,
        upload_type: str,
        error_message: str,
    ) -> int:
        cur = await self.conn.execute(
            """
            INSERT INTO failed_registry (timestamp, file_path, chat_id, caption,
                                         upload_type, error_message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (utc_now_iso(), file_path, chat_id, caption, upload_type, error_message),
        )
        await self.conn.commit()
        assert cur.lastrowid is not None  # noqa: S101 - INSERT always yields a rowid
        return cur.lastrowid

    async def failed_pending(self, failed_id: int | None = None) -> list[FailedUpload]:
        query = "SELECT * FROM failed_registry WHERE status = 'pending'"
        params: list[Any] = []
        if failed_id is not None:
            query += " AND id = ?"
            params.append(failed_id)
        query += " ORDER BY id"
        async with self.conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [FailedUpload(**dict(r)) for r in rows]

    async def failed_mark(self, failed_id: int, status: str, *, bump_retry: bool = False) -> None:
        if status not in ("pending", "resolved", "abandoned"):
            raise DatabaseError(f"Invalid failed_registry status: {status!r}")
        await self.conn.execute(
            "UPDATE failed_registry SET status = ?, retry_count = retry_count + ? WHERE id = ?",
            (status, 1 if bump_retry else 0, failed_id),
        )
        await self.conn.commit()

    # --- sync state -----------------------------------------------------------

    async def sync_state_get(self, chat_id: str) -> int:
        async with self.conn.execute(
            "SELECT last_scanned_message_id FROM sync_state WHERE chat_id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["last_scanned_message_id"]) if row else 0

    async def sync_state_set(self, chat_id: str, last_message_id: int) -> None:
        await self.conn.execute(
            """
            INSERT INTO sync_state (chat_id, last_scanned_message_id, last_sync_timestamp)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                last_scanned_message_id = excluded.last_scanned_message_id,
                last_sync_timestamp = excluded.last_sync_timestamp
            """,
            (chat_id, last_message_id, utc_now_iso()),
        )
        await self.conn.commit()
