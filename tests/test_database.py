"""Database tests: migrations, CRUD, constraints, and index usage."""

from __future__ import annotations

import stat
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from tup.database import Database, DatabaseError


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    async with Database(":memory:") as database:
        yield database


async def test_migration_creates_schema_version(db: Database) -> None:
    async with db.conn.execute("SELECT MAX(version) AS v FROM schema_version") as cur:
        row = await cur.fetchone()
    assert row is not None and row["v"] == 1


async def test_migration_is_idempotent(db: Database) -> None:
    await db._migrate()  # second run must be a no-op
    async with db.conn.execute("SELECT COUNT(*) AS c FROM schema_version") as cur:
        row = await cur.fetchone()
    assert row is not None and row["c"] == 1


async def test_file_database_gets_0600(tmp_path: Path) -> None:
    db_path = tmp_path / "sub" / "registry.db"
    async with Database(db_path):
        pass
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


async def test_alias_crud(db: Database) -> None:
    await db.alias_add("work", "-100123", "Work Files")
    assert (a := await db.alias_get("work")) is not None and a.chat_id == "-100123"
    assert await db.resolve_drive("work") == "-100123"
    assert await db.resolve_drive("-100999") == "-100999"  # raw id passes through
    assert len(await db.alias_list()) == 1
    assert await db.alias_remove("work") is True
    assert await db.alias_remove("work") is False


async def test_alias_unique_violation_raises_domain_error(db: Database) -> None:
    await db.alias_add("work", "-100123", None)
    with pytest.raises(DatabaseError):
        await db.alias_add("work", "-100456", None)
    with pytest.raises(DatabaseError):
        await db.alias_add("other", "-100123", None)  # chat_id UNIQUE


async def test_vfs_upsert_and_unique_replacement(db: Database) -> None:
    await db.vfs_upsert("-100123", "/docs/", "a.pdf", 10, "hash1", "fid1", 1)
    await db.vfs_upsert("-100123", "/docs/", "a.pdf", 20, "hash2", "fid2", 2)
    entry = await db.vfs_get("-100123", "/docs/", "a.pdf")
    assert entry is not None
    assert entry.file_size == 20
    assert entry.file_hash == "hash2"
    assert entry.telegram_message_id == 2
    assert len(await db.vfs_list_dir("-100123", "/docs/")) == 1


async def test_vfs_prefix_listing_is_recursive(db: Database) -> None:
    await db.vfs_upsert("-100123", "/docs/", "a.pdf", 1, "h", "f", 1)
    await db.vfs_upsert("-100123", "/docs/sub/", "b.pdf", 1, "h", "f", 2)
    await db.vfs_upsert("-100123", "/other/", "c.pdf", 1, "h", "f", 3)
    await db.vfs_upsert("-100999", "/docs/", "d.pdf", 1, "h", "f", 4)  # other drive
    entries = await db.vfs_list_prefix("-100123", "/docs/")
    assert [(e.virtual_path, e.file_name) for e in entries] == [
        ("/docs/", "a.pdf"),
        ("/docs/sub/", "b.pdf"),
    ]


async def test_vfs_prefix_query_uses_index(db: Database) -> None:
    async with db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM vfs_index "
        "WHERE chat_id = ? AND virtual_path >= ? AND virtual_path < ?",
        ("-100123", "/docs/", "/docs0"),
    ) as cur:
        plan = " ".join(str(row["detail"]) for row in await cur.fetchall())
    assert "idx_vfs_path" in plan


async def test_vfs_move_and_delete(db: Database) -> None:
    await db.vfs_upsert("-100123", "/docs/", "a.pdf", 1, "h", "f", 1)
    entry = await db.vfs_get("-100123", "/docs/", "a.pdf")
    assert entry is not None
    await db.vfs_move(entry.id, "/archive/", "a.pdf")
    assert await db.vfs_get("-100123", "/docs/", "a.pdf") is None
    moved = await db.vfs_get("-100123", "/archive/", "a.pdf")
    assert moved is not None
    await db.vfs_delete(moved.id)
    assert await db.vfs_get("-100123", "/archive/", "a.pdf") is None


async def test_vfs_move_onto_existing_raises(db: Database) -> None:
    await db.vfs_upsert("-100123", "/docs/", "a.pdf", 1, "h", "f", 1)
    await db.vfs_upsert("-100123", "/archive/", "a.pdf", 1, "h", "f", 2)
    entry = await db.vfs_get("-100123", "/docs/", "a.pdf")
    assert entry is not None
    with pytest.raises(DatabaseError):
        await db.vfs_move(entry.id, "/archive/", "a.pdf")


async def test_failed_registry_lifecycle(db: Database) -> None:
    fid = await db.failed_add("/files/a.pdf", "-100123", None, "document", "boom")
    pending = await db.failed_pending()
    assert [f.id for f in pending] == [fid]
    await db.failed_mark(fid, "resolved", bump_retry=True)
    assert await db.failed_pending() == []
    with pytest.raises(DatabaseError):
        await db.failed_mark(fid, "nonsense")


async def test_failed_pending_uses_partial_index(db: Database) -> None:
    async with db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM failed_registry WHERE status = 'pending'"
    ) as cur:
        plan = " ".join(str(row["detail"]) for row in await cur.fetchall())
    assert "idx_failed_status" in plan


async def test_uploads_log_and_recent_filter(db: Database) -> None:
    await db.log_upload("/a", 1, "-100123", "document", "success", telegram_message_id=1)
    await db.log_upload("/b", 2, "-100123", "document", "failed", error_message="x")
    await db.log_upload("/c", 3, "-100999", "photo", "success")
    assert len(await db.log_recent()) == 3
    assert len(await db.log_recent(chat_id="-100123")) == 2
    assert len(await db.log_recent(limit=1)) == 1


async def test_sync_state_roundtrip(db: Database) -> None:
    assert await db.sync_state_get("-100123") == 0
    await db.sync_state_set("-100123", 42)
    assert await db.sync_state_get("-100123") == 42
    await db.sync_state_set("-100123", 99)
    assert await db.sync_state_get("-100123") == 99
