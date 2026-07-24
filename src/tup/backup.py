"""Registry backups to Telegram: gzipped-JSON dumps as VFS files under /Backups/.

The dump structure matches tup-cloud's backup format (format/version/
created_at/tables, one column dict per row) so dumps are mutually readable.
The desktop format name differs ('tup-backup') because the table sets differ;
restore accepts both names and loads only tables/columns the local schema
knows, so a cloud dump restores its shared tables here and vice versa.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from typing import Any

from telethon import TelegramClient

from tup.config import Settings
from tup.database import Database
from tup.uploader import TupError
from tup.utils import utc_now_iso
from tup.vfs_ops import op_purge, save_content

BACKUP_DIR = "/Backups/"
BACKUP_PREFIX = "tup-backup-"
FORMAT_NAME = "tup-backup"
CLOUD_FORMAT_NAME = "tup-cloud-backup"
FORMAT_VERSION = 1

# Insert order matters on restore: vfs_index rows must exist before
# file_versions (FK); deletes run in reverse.
_TABLES = [
    "chat_aliases",
    "vfs_index",
    "file_versions",
    "uploads_log",
    "failed_registry",
    "sync_state",
]


async def dump_database(db: Database) -> bytes:
    tables: dict[str, list[dict[str, Any]]] = {}
    for name in _TABLES:
        async with db.conn.execute(f"SELECT * FROM {name}") as cur:  # noqa: S608 - fixed list
            rows = await cur.fetchall()
        tables[name] = [dict(r) for r in rows]
    payload = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "created_at": utc_now_iso(),
        "tables": tables,
    }
    return gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


async def make_backup(
    db: Database, settings: Settings, client: TelegramClient, chat_id: str, *, keep: int = 10
) -> tuple[str, int]:
    """Upload a dump to /Backups/ and prune old ones; returns (path, pruned)."""
    data = await dump_database(db)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    name = f"{BACKUP_PREFIX}{stamp}.json.gz"
    await save_content(db, settings, client, chat_id, BACKUP_DIR, name, data)

    entries = [
        e
        for e in await db.vfs_list_dir(chat_id, BACKUP_DIR)
        if e.file_name.startswith(BACKUP_PREFIX)
    ]
    entries.sort(key=lambda e: e.file_name, reverse=True)  # names sort chronologically
    pruned = 0
    for stale in entries[max(keep, 1) :]:
        await op_purge(db, settings, chat_id, stale)
        pruned += 1
    return BACKUP_DIR + name, pruned


async def restore_database(db: Database, data: bytes) -> dict[str, int]:
    """Transactionally replace all known tables with a dump's contents.

    Telegram messages are untouched — only the local index is replaced.
    Index rows of current /Backups/ files the dump predates are preserved
    (matching tup-cloud), so newer backups stay restorable afterwards.
    """
    try:
        payload = json.loads(gzip.decompress(data))
    except (OSError, ValueError) as exc:
        raise TupError("Not a readable tup backup file (gzipped JSON expected).") from exc
    if (
        payload.get("format") not in (FORMAT_NAME, CLOUD_FORMAT_NAME)
        or payload.get("version") != FORMAT_VERSION
    ):
        raise TupError(
            f"Unsupported backup format: {payload.get('format')!r} v{payload.get('version')!r}."
        )
    tables = payload.get("tables", {})
    conn = db.conn

    async with conn.execute(
        "SELECT * FROM vfs_index WHERE virtual_path = ?", (BACKUP_DIR,)
    ) as cur:
        current_backups = [dict(r) for r in await cur.fetchall()]

    counts: dict[str, int] = {}
    for name in reversed(_TABLES):
        await conn.execute(f"DELETE FROM {name}")  # noqa: S608 - fixed list
    for name in _TABLES:
        async with conn.execute(f"PRAGMA table_info({name})") as cur:
            info = await cur.fetchall()
        known = {r["name"] for r in info}
        # Cross-frontend dumps may omit local-only NOT NULL columns (e.g. a
        # cloud dump has no telegram_file_id) — fill type-appropriate defaults.
        required = {
            r["name"]: r["type"]
            for r in info
            if r["notnull"] and r["dflt_value"] is None and not r["pk"]
        }
        inserted = 0
        for row in tables.get(name, []):
            values = {c: row[c] for c in row if c in known}
            if not values:
                continue
            for col, col_type in required.items():
                if col not in values:
                    values[col] = 0 if "INT" in col_type.upper() else ""
            cols = list(values)
            await conn.execute(
                f"INSERT OR REPLACE INTO {name} ({', '.join(cols)}) "  # noqa: S608
                f"VALUES ({', '.join('?' for _ in cols)})",
                [values[c] for c in cols],
            )
            inserted += 1
        counts[name] = inserted

    restored_names = {
        (r.get("chat_id"), r.get("file_name")) for r in tables.get("vfs_index", [])
    }
    for row in current_backups:
        if (row["chat_id"], row["file_name"]) in restored_names:
            continue
        row.pop("id", None)
        cols = list(row)
        await conn.execute(
            f"INSERT OR IGNORE INTO vfs_index ({', '.join(cols)}) "  # noqa: S608
            f"VALUES ({', '.join('?' for _ in cols)})",
            [row[c] for c in cols],
        )
    await conn.commit()
    return counts
