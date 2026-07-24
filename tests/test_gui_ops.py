"""GUI ops mirror CLI semantics: mkdir/rmdir/rm/mv/cp/prune/retry, and window wiring."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import respx

pytest.importorskip("PyQt6.QtWidgets")

from tests._qt import get_qapp, pump
from tests.conftest import CHAT_ID, FakeMtprotoClient, make_settings
from tup.config import Settings, default_database_path
from tup.database import Database
from tup.gui.models import all_dir_paths
from tup.gui.ops import (
    op_cp,
    op_mkdir,
    op_mv,
    op_prune,
    op_retry_failed,
    op_rm,
    op_rmdir,
)
from tup.uploader import TupError, format_caption

HASH = "a1" * 32


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    async with Database(":memory:") as database:
        yield database


async def seed(db: Database, virtual_path: str, name: str, message_id: int) -> None:
    await db.vfs_upsert(CHAT_ID, virtual_path, name, 11, HASH, "", message_id)


# --- mkdir / rmdir (no network) ------------------------------------------------


async def test_mkdir_and_rmdir_roundtrip(db: Database) -> None:
    created = await op_mkdir(db, CHAT_ID, "/photos")
    assert created == "/photos/"
    assert await db.vfs_get(CHAT_ID, "/photos/", ".keep") is not None
    with pytest.raises(TupError, match="already exists"):
        await op_mkdir(db, CHAT_ID, "/photos")

    removed = await op_rmdir(db, CHAT_ID, "/photos")
    assert removed == "/photos/"
    assert await db.vfs_get(CHAT_ID, "/photos/", ".keep") is None


async def test_rmdir_refuses_non_empty(db: Database) -> None:
    await op_mkdir(db, CHAT_ID, "/docs")
    await seed(db, "/docs/", "a.pdf", 11)
    with pytest.raises(TupError, match="not empty"):
        await op_rmdir(db, CHAT_ID, "/docs")


# --- rm / mv over the Bot API (respx) ------------------------------------------


async def test_op_rm_deletes_remote_and_row(db: Database, telegram_api: respx.MockRouter) -> None:
    await seed(db, "/docs/", "a.pdf", 11)
    entry = await db.vfs_get(CHAT_ID, "/docs/", "a.pdf")
    assert entry is not None
    deleted = await op_rm(db, make_settings(), CHAT_ID, entry)
    assert deleted == "/docs/a.pdf"
    assert telegram_api["deleteMessage"].called
    assert await db.vfs_get(CHAT_ID, "/docs/", "a.pdf") is None


async def test_op_mv_edits_caption_and_moves_row(
    db: Database, telegram_api: respx.MockRouter
) -> None:
    await seed(db, "/docs/", "a.pdf", 11)
    entry = await db.vfs_get(CHAT_ID, "/docs/", "a.pdf")
    assert entry is not None
    moved = await op_mv(db, make_settings(), CHAT_ID, entry, "/archive/")
    assert moved == "/archive/a.pdf"
    assert telegram_api["editMessageCaption"].called
    assert await db.vfs_get(CHAT_ID, "/archive/", "a.pdf") is not None
    assert await db.vfs_get(CHAT_ID, "/docs/", "a.pdf") is None


async def test_op_mv_rejects_existing_destination(
    db: Database, telegram_api: respx.MockRouter
) -> None:
    await seed(db, "/docs/", "a.pdf", 11)
    await seed(db, "/archive/", "a.pdf", 12)
    entry = await db.vfs_get(CHAT_ID, "/docs/", "a.pdf")
    assert entry is not None
    with pytest.raises(TupError, match="already exists"):
        await op_mv(db, make_settings(), CHAT_ID, entry, "/archive/")


# --- cp / prune / retry over MTProto (fake client) ------------------------------


async def test_op_cp_copies_server_side(db: Database) -> None:
    await seed(db, "/docs/", "a.pdf", 11)
    entry = await db.vfs_get(CHAT_ID, "/docs/", "a.pdf")
    assert entry is not None
    client = FakeMtprotoClient()
    copied = await op_cp(db, make_settings(), client, CHAT_ID, entry, "/backup/")
    assert copied == "/backup/a.pdf"
    assert client.sent[0]["file"] == "media-of-11"  # media reuse, no re-upload
    duplicate = await db.vfs_get(CHAT_ID, "/backup/", "a.pdf")
    assert duplicate is not None
    assert duplicate.telegram_message_id == 101


async def test_op_prune_drops_deleted_rows(db: Database) -> None:
    await seed(db, "/docs/", "kept.pdf", 11)
    await seed(db, "/docs/", "gone.pdf", 12)
    await db.vfs_upsert(CHAT_ID, "/dir/", ".keep", 0, "", "", 0)  # no remote message
    client = FakeMtprotoClient()
    client.existing_ids = {11}
    pruned = await op_prune(db, make_settings(), client, CHAT_ID)
    assert pruned == ["/docs/gone.pdf"]
    assert await db.vfs_get(CHAT_ID, "/docs/", "kept.pdf") is not None
    assert await db.vfs_get(CHAT_ID, "/dir/", ".keep") is not None


async def test_op_retry_failed_reuploads_pending(db: Database, tmp_path: Any) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"hello world")
    caption = format_caption("/docs/notes.txt", HASH)
    await db.failed_add(str(source), CHAT_ID, caption, "document", "boom")

    resolved, still_failing = await op_retry_failed(db, make_settings(), FakeMtprotoClient())
    assert (resolved, still_failing) == (1, 0)
    assert await db.failed_pending() == []
    assert await db.vfs_get(CHAT_ID, "/docs/", "notes.txt") is not None


# --- helpers -------------------------------------------------------------------


def test_all_dir_paths_lists_every_folder(db: Database) -> None:
    async def _run() -> list[str]:
        await seed(db, "/docs/sub/", "x.pdf", 11)
        await seed(db, "/media/", "y.mp4", 12)
        return all_dir_paths(await db.vfs_list_prefix(CHAT_ID, "/"))

    assert asyncio.run(_run()) == ["/", "/docs/", "/docs/sub/", "/media/"]


# --- window wiring -------------------------------------------------------------


def test_window_folder_and_move_operations(
    fake_env: str, telegram_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tup.gui.bridge import CoreBridge
    from tup.gui.main_window import MainWindow

    qapp = get_qapp()

    async def _seed() -> None:
        async with Database(default_database_path()) as db:
            await db.alias_add("work", CHAT_ID, "Work Files")
            await db.vfs_upsert(CHAT_ID, "/docs/", "a.pdf", 11, HASH, "", 11)

    asyncio.run(_seed())

    fake = FakeMtprotoClient()

    async def fake_connect(settings: Any) -> FakeMtprotoClient:
        return fake

    monkeypatch.setattr("tup.gui.bridge.connect_mtproto", fake_connect)

    bridge = CoreBridge(Settings.load())
    bridge.start()
    window = MainWindow(bridge)
    window.suppress_dialogs = True
    window.open_files_externally = False
    try:
        assert pump(qapp, lambda: window.file_model.rowCount() > 0)

        # New folder in the current (root) directory.
        window.create_folder("inbox")
        assert pump(
            qapp,
            lambda: (
                "inbox"
                in {
                    w.name
                    for w in map(window.file_model.row_at, range(window.file_model.rowCount()))
                }
            ),
        ), "folder never appeared"

        # Move /docs/a.pdf to /inbox/ (caption edit over the mocked Bot API).
        window.set_current_dir("/docs/")
        row = window.file_model.row_at(0)
        assert row.name == "a.pdf"
        window.move_row(row, "/inbox/")
        assert pump(
            qapp,
            lambda: (
                asyncio.run(_exists("/inbox/", "a.pdf"))
                and not asyncio.run(_exists("/docs/", "a.pdf"))
            ),
        ), "move never landed"
        assert telegram_api["editMessageCaption"].called

        # Delete the moved file: default deletion moves it to the Recycle Bin
        # (caption rewrite, never a remote delete).
        window.set_current_dir("/inbox/")
        assert pump(qapp, lambda: window.file_model.rowCount() > 0)
        file_row = next(
            window.file_model.row_at(i)
            for i in range(window.file_model.rowCount())
            if not window.file_model.row_at(i).is_dir
        )
        window.delete_row(file_row)
        assert pump(qapp, lambda: not asyncio.run(_exists("/inbox/", "a.pdf"))), (
            "trash move never landed"
        )
        assert asyncio.run(_exists("/.Trash/inbox/", "a.pdf"))
        assert not telegram_api["deleteMessage"].called

        # Deleting from within the bin purges for real.
        window.set_current_dir("/.Trash/inbox/")
        assert pump(qapp, lambda: window.file_model.rowCount() > 0)
        trashed_row = window.file_model.row_at(0)
        window.delete_row(trashed_row)
        assert pump(qapp, lambda: not asyncio.run(_exists("/.Trash/inbox/", "a.pdf"))), (
            "purge never landed"
        )
        assert telegram_api["deleteMessage"].called
    finally:
        window.close()
        bridge.stop()


async def _exists(virtual_path: str, name: str) -> bool:
    async with Database(default_database_path()) as db:
        return await db.vfs_get(CHAT_ID, virtual_path, name) is not None
