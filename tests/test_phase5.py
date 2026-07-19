"""Phase 5: ~/.tui home migration, schema v2 attributes, same-folder SHA dedup."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from tests.conftest import CHAT_ID, FakeMtprotoClient, make_settings
from tup.config import migrate_legacy_config
from tup.database import _BASELINE_SQL, Database
from tup.gui.transfers import Transfer, TransferManager
from tup.uploader import DuplicateFileError, upload_file

HASH = "a1" * 32


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    async with Database(":memory:") as database:
        yield database


# --- ~/.tui home migration -----------------------------------------------------


def test_migrate_legacy_config_moves_files(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / ".env").write_text("TELEGRAM_BOT_TOKEN=x\n")
    (legacy / "registry.db").write_bytes(b"db")
    (legacy / "unrelated.txt").write_text("stays")
    target = tmp_path / "tui-home"

    moved = migrate_legacy_config(legacy, target)
    assert sorted(moved) == [".env", "registry.db"]
    assert (target / ".env").read_text() == "TELEGRAM_BOT_TOKEN=x\n"
    assert not (legacy / ".env").exists()
    assert (legacy / "unrelated.txt").exists()  # only tup's files move

    assert migrate_legacy_config(legacy, target) == []  # second run is a no-op


def test_migrate_legacy_config_never_overwrites(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / ".env").write_text("old")
    target = tmp_path / "tui-home"
    target.mkdir()
    (target / ".env").write_text("new")

    assert migrate_legacy_config(legacy, target) == []
    assert (target / ".env").read_text() == "new"
    assert (legacy / ".env").read_text() == "old"  # left in place, not clobbered


# --- schema v2 -----------------------------------------------------------------


async def test_v1_database_migrates_to_v2(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_BASELINE_SQL)
    conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (1, 'then')")
    conn.execute(
        "INSERT INTO vfs_index (chat_id, virtual_path, file_name, file_size, file_hash,"
        " telegram_file_id, telegram_message_id, upload_timestamp)"
        " VALUES (?, '/docs/', 'a.pdf', 11, ?, '', 11, 'then')",
        (CHAT_ID, HASH),
    )
    conn.commit()
    conn.close()

    async with Database(db_path) as db:
        async with db.conn.execute("SELECT MAX(version) AS v FROM schema_version") as cur:
            row = await cur.fetchone()
        assert row is not None and row["v"] == 2
        entry = await db.vfs_get(CHAT_ID, "/docs/", "a.pdf")
        assert entry is not None
        assert entry.mime_type == ""  # v1 rows get safe defaults
        assert entry.width is None
        await db.vfs_upsert(
            CHAT_ID,
            "/docs/",
            "b.mp4",
            5,
            "b" * 64,
            "",
            12,
            mime_type="video/mp4",
            media_kind="video",
            width=1080,
            height=1920,
            duration=159,
            source_mtime="2026-07-01T00:00:00+00:00",
        )
        stored = await db.vfs_get(CHAT_ID, "/docs/", "b.mp4")
        assert stored is not None
        assert (stored.width, stored.height, stored.duration) == (1080, 1920, 159)
        assert stored.media_kind == "video"


# --- upload stores attributes ---------------------------------------------------


async def test_upload_stores_file_attributes(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tup.uploader as uploader_module

    monkeypatch.setattr(uploader_module, "extract_media_metadata", lambda p: (1080, 1920, 159))
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"\x00" * 64)

    await upload_file(db, make_settings(), FakeMtprotoClient(), video, CHAT_ID, "/videos")

    entry = await db.vfs_get(CHAT_ID, "/videos/", "movie.mp4")
    assert entry is not None
    assert entry.media_kind == "video"
    assert entry.mime_type == "video/mp4"
    assert (entry.width, entry.height, entry.duration) == (1080, 1920, 159)
    assert entry.source_mtime  # ISO mtime of the local file


# --- same-folder SHA dedup ------------------------------------------------------


async def test_duplicate_upload_same_folder_is_blocked(db: Database, tmp_path: Path) -> None:
    client = FakeMtprotoClient()
    original = tmp_path / "notes.txt"
    original.write_bytes(b"hello world")
    await upload_file(db, make_settings(), client, original, CHAT_ID, "/docs")

    identical = tmp_path / "copy-of-notes.txt"
    identical.write_bytes(b"hello world")  # same content, different name
    with pytest.raises(DuplicateFileError, match="identical"):
        await upload_file(db, make_settings(), client, identical, CHAT_ID, "/docs")

    assert await db.failed_pending() == []  # a skip, not a failure
    assert len(client.sent) == 1  # nothing was sent for the duplicate

    # The same content is fine in a different folder.
    await upload_file(db, make_settings(), client, identical, CHAT_ID, "/backup")
    assert await db.vfs_get(CHAT_ID, "/backup/", "copy-of-notes.txt") is not None


async def test_cp_blocks_identical_content_in_destination(db: Database) -> None:
    from tup.gui.ops import op_cp

    await db.vfs_upsert(CHAT_ID, "/docs/", "a.pdf", 11, HASH, "", 11)
    await db.vfs_upsert(CHAT_ID, "/backup/", "renamed.pdf", 11, HASH, "", 12)
    entry = await db.vfs_get(CHAT_ID, "/docs/", "a.pdf")
    assert entry is not None
    with pytest.raises(DuplicateFileError, match="identical"):
        await op_cp(db, make_settings(), FakeMtprotoClient(), CHAT_ID, entry, "/backup/")


async def test_transfer_queue_marks_duplicates_skipped() -> None:
    states: dict[int, Transfer] = {}
    manager = TransferManager(lambda t: states.__setitem__(t.id, t))
    manager.start()

    async def duplicate(_t: Transfer) -> None:
        raise DuplicateFileError("identical file already there")

    transfer = await manager.enqueue("upload", "dup.txt", "→ /", 1, duplicate)
    import asyncio

    await asyncio.wait_for(manager.wait_idle(), timeout=5)
    await manager.shutdown()

    assert states[transfer.id].state == "skipped"
    assert states[transfer.id].error == "identical file already there"
