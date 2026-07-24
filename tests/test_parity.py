"""Cloud-parity features: tags, save-through versions, trash ops, backups, cache sweep."""

from __future__ import annotations

import gzip
import json
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
import respx
from telethon import TelegramClient

from tests.conftest import CHAT_ID, FakeMtprotoClient, make_settings
from tup.backup import dump_database, restore_database
from tup.database import Database
from tup.utils import extract_tags, fallback_kind
from tup.vfs_ops import (
    VERSION_CAP,
    op_purge,
    op_restore,
    op_set_caption,
    op_trash,
    restore_version,
    save_content,
)

HASH = "a1" * 32


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    async with Database(":memory:") as database:
        yield database


def client() -> TelegramClient:
    return cast(TelegramClient, FakeMtprotoClient())


# --- tag & kind helpers ---------------------------------------------------------


def test_extract_tags_matches_cloud_normalization() -> None:
    assert extract_tags("Invoice for #Q3 #budget #vfs #budget") == "budget q3"
    assert extract_tags(None) == ""
    assert extract_tags("no tags here") == ""


def test_fallback_kind_infers_from_extension_for_legacy_rows() -> None:
    assert fallback_kind("pic.jpg", "") == "photo"
    assert fallback_kind("movie.mp4", "") == "video"
    assert fallback_kind("song.mp3", "") == "audio"
    assert fallback_kind("doc.pdf", "") == "document"
    assert fallback_kind("mystery", "") == "document"
    assert fallback_kind("pic.jpg", "document") == "document"  # indexed kind wins


# --- save-through & versions ----------------------------------------------------


async def test_save_content_versions_and_caps(
    db: Database, telegram_api: respx.MockRouter
) -> None:
    fake = client()
    settings = make_settings()

    entry, changed = await save_content(db, settings, fake, CHAT_ID, "/notes/", "a.md", b"one")
    assert changed and entry.media_kind == "document"
    first_message = entry.telegram_message_id

    # Identical bytes: no-op, no new message.
    _, changed = await save_content(db, settings, fake, CHAT_ID, "/notes/", "a.md", b"one")
    assert not changed

    # New content: the old message becomes version 1.
    entry, changed = await save_content(db, settings, fake, CHAT_ID, "/notes/", "a.md", b"two")
    assert changed and entry.telegram_message_id != first_message
    versions = await db.version_list(entry.id)
    assert [v.telegram_message_id for v in versions] == [first_message]

    # The user caption survives content saves (carried into the new caption).
    await db.vfs_set_caption(entry.id, "keep me #tagged", "tagged")
    entry2, _ = await save_content(db, settings, fake, CHAT_ID, "/notes/", "a.md", b"three")
    sent = cast(FakeMtprotoClient, fake).sent[-1]
    assert "keep me #tagged" in sent["caption"]
    assert entry2.user_caption == "keep me #tagged"

    # History is capped: old versions beyond VERSION_CAP get pruned.
    for i in range(VERSION_CAP + 3):
        await save_content(db, settings, fake, CHAT_ID, "/notes/", "a.md", f"rev{i}".encode())
    assert len(await db.version_list(entry.id)) == VERSION_CAP


async def test_restore_version_resaves_old_content(db: Database) -> None:
    fake = client()
    settings = make_settings()
    entry, _ = await save_content(db, settings, fake, CHAT_ID, "/", "note.txt", b"current")
    await save_content(db, settings, fake, CHAT_ID, "/", "note.txt", b"newer")
    fresh = await db.vfs_get(CHAT_ID, "/", "note.txt")
    assert fresh is not None
    version = (await db.version_list(fresh.id))[0]

    # The fake client downloads b"data" for any message: restore re-saves that.
    restored = await restore_version(db, settings, fake, CHAT_ID, fresh, version)
    assert restored.file_size == len(b"data")
    # The replaced "newer" revision itself became a version (cloud semantics).
    message_ids = [v.telegram_message_id for v in await db.version_list(restored.id)]
    assert fresh.telegram_message_id in message_ids


# --- trash ops (op-level; CLI flow is covered in test_vfs.py) -------------------


async def test_trash_name_collision_dedups(
    db: Database, telegram_api: respx.MockRouter
) -> None:
    settings = make_settings()
    await db.vfs_upsert(CHAT_ID, "/d/", "a.txt", 1, "1" * 64, "", 21)
    await db.vfs_upsert(CHAT_ID, "/.Trash/d/", "a.txt", 1, "2" * 64, "", 22)
    entry = await db.vfs_get(CHAT_ID, "/d/", "a.txt")
    assert entry is not None
    trashed = await op_trash(db, settings, CHAT_ID, entry)
    assert trashed == "/.Trash/d/a (2).txt"


async def test_restore_refuses_when_original_occupied(
    db: Database, telegram_api: respx.MockRouter
) -> None:
    from tup.uploader import TupError

    settings = make_settings()
    await db.vfs_upsert(CHAT_ID, "/.Trash/d/", "a.txt", 1, "1" * 64, "", 21)
    await db.vfs_upsert(CHAT_ID, "/d/", "a.txt", 1, "2" * 64, "", 22)
    trashed = await db.vfs_get(CHAT_ID, "/.Trash/d/", "a.txt")
    assert trashed is not None
    with pytest.raises(TupError, match="already exists"):
        await op_restore(db, settings, CHAT_ID, trashed)


async def test_purge_deletes_versions_too(db: Database, telegram_api: respx.MockRouter) -> None:
    settings = make_settings()
    await db.vfs_upsert(CHAT_ID, "/d/", "a.txt", 1, "1" * 64, "", 21)
    entry = await db.vfs_get(CHAT_ID, "/d/", "a.txt")
    assert entry is not None
    await db.version_add(entry.id, CHAT_ID, 19, "0" * 64, 1)
    await op_purge(db, settings, CHAT_ID, entry)
    deletes = [
        c for c in telegram_api.calls if c.request.url.path.endswith("/deleteMessage")
    ]
    assert len(deletes) == 2  # version message + current message
    assert await db.vfs_get(CHAT_ID, "/d/", "a.txt") is None
    assert await db.version_list(entry.id) == []


async def test_observed_files_never_get_caption_edits(
    db: Database, telegram_api: respx.MockRouter
) -> None:
    settings = make_settings()
    await db.vfs_upsert(CHAT_ID, "/d/", "seen.bin", 1, "tg:9", "", 33, origin="observed")
    entry = await db.vfs_get(CHAT_ID, "/d/", "seen.bin")
    assert entry is not None
    await op_set_caption(db, settings, CHAT_ID, entry, "hello #x")
    await op_trash(db, settings, CHAT_ID, entry)
    edits = [
        c for c in telegram_api.calls if c.request.url.path.endswith("/editMessageCaption")
    ]
    assert edits == []  # index-only: tup does not own the message
    moved = await db.vfs_get(CHAT_ID, "/.Trash/d/", "seen.bin")
    assert moved is not None and moved.tags == "x"


# --- backups --------------------------------------------------------------------


async def test_backup_dump_restore_round_trip(db: Database) -> None:
    await db.alias_add("work", CHAT_ID, "Work")
    await db.vfs_upsert(CHAT_ID, "/d/", "a.txt", 1, "1" * 64, "", 21, tags="x")
    entry = await db.vfs_get(CHAT_ID, "/d/", "a.txt")
    assert entry is not None
    await db.version_add(entry.id, CHAT_ID, 19, "0" * 64, 1)
    dump = await dump_database(db)

    payload = json.loads(gzip.decompress(dump))
    assert payload["format"] == "tup-backup" and payload["version"] == 1

    # Wipe, then restore: everything comes back.
    await db.vfs_delete(entry.id)
    await db.alias_remove("work")
    counts = await restore_database(db, dump)
    assert counts["vfs_index"] == 1 and counts["chat_aliases"] == 1
    restored = await db.vfs_get(CHAT_ID, "/d/", "a.txt")
    assert restored is not None and restored.tags == "x"
    assert len(await db.version_list(restored.id)) == 1


async def test_restore_accepts_cloud_dumps_with_unknown_tables(db: Database) -> None:
    payload: dict[str, Any] = {
        "format": "tup-cloud-backup",
        "version": 1,
        "created_at": "2026-07-21T00:00:00+00:00",
        "tables": {
            "users": [{"id": 1, "phone": "n/a"}],  # cloud-only: ignored
            "vfs_index": [
                {
                    "id": 5,
                    "chat_id": CHAT_ID,
                    "virtual_path": "/cloud/",
                    "file_name": "c.txt",
                    "file_size": 3,
                    "file_hash": "9" * 64,
                    "telegram_message_id": 77,
                    "upload_timestamp": "2026-07-20T00:00:00+00:00",
                    "uploaded_by": "someone@cloud",  # unknown column: ignored
                }
            ],
        },
    }
    counts = await restore_database(db, gzip.compress(json.dumps(payload).encode()))
    assert counts["vfs_index"] == 1
    entry = await db.vfs_get(CHAT_ID, "/cloud/", "c.txt")
    assert entry is not None and entry.telegram_message_id == 77


# --- cache sweep ----------------------------------------------------------------


def test_cache_sweep_evicts_only_stale_drive_files(isolate_config: Path) -> None:
    from tup.gui.cache import cache_root, sweep

    root = cache_root()
    drive = root / CHAT_ID / "docs"
    drive.mkdir(parents=True)
    stale = drive / "old.bin"
    stale.write_bytes(b"x")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    fresh = drive / "new.bin"
    fresh.write_bytes(b"x")
    partial = drive / "half.bin.part"
    partial.write_bytes(b"x")
    # Non-drive content in tup's home must never be touched, whatever its age.
    binary = root / "bin" / "tup"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"#!")
    os.utime(binary, (time.time() - 7200, time.time() - 7200))

    removed = sweep(ttl_seconds=3600)
    assert removed == 2  # stale file + .part
    assert not stale.exists() and not partial.exists()
    assert fresh.exists() and binary.exists()
