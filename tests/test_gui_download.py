"""Download cache: path layout, is_cached checks, and download-on-open flow."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from tests._qt import get_qapp, pump
from tests.conftest import CHAT_ID, FakeMtprotoClient
from tup.config import Settings, default_database_path
from tup.database import Database, VfsEntry
from tup.gui.cache import cache_root, cached_path, evict, is_cached
from tup.uploader import download_media_file

HASH = "a1" * 32


def make_entry(virtual_path: str = "/docs/", name: str = "a.pdf", size: int = 4) -> VfsEntry:
    return VfsEntry(
        id=1,
        chat_id=CHAT_ID,
        virtual_path=virtual_path,
        file_name=name,
        file_size=size,
        file_hash=HASH,
        telegram_file_id="",
        telegram_message_id=11,
        upload_timestamp="2026-07-19T10:00:00+00:00",
    )


def test_cached_path_mirrors_drive_structure() -> None:
    entry = make_entry("/docs/sub/", "movie.mp4")
    path = cached_path(entry)
    assert path == cache_root() / CHAT_ID / "docs" / "sub" / "movie.mp4"
    root_entry = make_entry("/", "root.txt")
    assert cached_path(root_entry) == cache_root() / CHAT_ID / "root.txt"


def test_is_cached_requires_full_size() -> None:
    entry = make_entry(size=4)
    path = cached_path(entry)
    assert not is_cached(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"da")  # truncated download
    assert not is_cached(entry)
    path.write_bytes(b"data")
    assert is_cached(entry)


def test_is_cached_photo_ignores_size_mismatch() -> None:
    """Telegram re-encodes photos, so the downloaded size never matches the
    original upload's recorded size — existence alone must decide."""
    from dataclasses import replace

    entry = replace(make_entry(name="pic.jpg", size=413_900), media_kind="photo")
    assert not is_cached(entry)
    path = cached_path(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"re-encoded jpeg, different size")
    assert is_cached(entry)


async def test_download_media_file_writes_atomically() -> None:
    client = FakeMtprotoClient()
    entry = make_entry()
    dest = cached_path(entry)
    result = await download_media_file(
        client, CHAT_ID, entry.telegram_message_id, dest, max_retries=2
    )
    assert result == dest
    assert dest.read_bytes() == b"data"
    assert not dest.with_name(dest.name + ".part").exists()


async def test_download_media_file_missing_message_raises() -> None:
    from tup.uploader import TupError

    client = FakeMtprotoClient()
    client.existing_ids = set()  # everything deleted on Telegram
    with pytest.raises(TupError, match="no media"):
        await download_media_file(client, CHAT_ID, 99, Path("unused.bin"), max_retries=2)


def test_evict_removes_only_local_copy() -> None:
    entry = make_entry(size=4)
    path = cached_path(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    assert is_cached(entry)
    assert evict(entry) is True
    assert not path.exists()
    assert evict(entry) is False  # already gone: no error, just a no-op


def test_activate_row_downloads_then_opens(
    fake_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Double-click on a non-cached file queues one download, then auto-opens it.

    Repeated double-clicks while the download is in flight must not stack
    duplicate transfers.
    """
    from tup.gui.bridge import CoreBridge
    from tup.gui.main_window import MainWindow

    qapp = get_qapp()

    async def _seed() -> None:
        async with Database(default_database_path()) as db:
            await db.alias_add("work", CHAT_ID, "Work Files")
            await db.vfs_upsert(CHAT_ID, "/docs/", "a.pdf", 4, HASH, "", 11)

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
    opened: list[Path] = []
    monkeypatch.setattr(window, "_open_local", opened.append)
    try:
        assert pump(qapp, lambda: window.file_model.rowCount() > 0)
        window.set_current_dir("/docs/")
        row = window.file_model.row_at(0)
        assert not row.downloaded

        window.activate_row(row)
        window.activate_row(row)  # impatient second double-click: no duplicate transfer
        assert pump(qapp, lambda: window.file_model.row_at(0).downloaded), "never downloaded"
        assert pump(qapp, lambda: len(opened) > 0), "never opened"
        entry = window.file_model.row_at(0).entry
        assert entry is not None
        assert opened == [cached_path(entry)]
        downloads = [t for t in window.transfers._transfers.values() if t.kind == "download"]
        assert len(downloads) == 1
        assert window._downloads_in_flight == {}

        # Now cached: a further double-click opens instantly, no new transfer.
        window.activate_row(window.file_model.row_at(0))
        assert len(opened) == 2
        downloads = [t for t in window.transfers._transfers.values() if t.kind == "download"]
        assert len(downloads) == 1
    finally:
        window.close()
        bridge.stop()


def test_double_click_downloads_and_marks_row(
    fake_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tup.gui.bridge import CoreBridge
    from tup.gui.main_window import MainWindow

    qapp = get_qapp()

    async def _seed() -> None:
        async with Database(default_database_path()) as db:
            await db.alias_add("work", CHAT_ID, "Work Files")
            await db.vfs_upsert(CHAT_ID, "/docs/", "a.pdf", 4, HASH, "", 11)

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
        window.set_current_dir("/docs/")
        row = window.file_model.row_at(0)
        assert row.name == "a.pdf"
        assert not row.downloaded

        window.open_row(row)
        assert pump(qapp, lambda: window.file_model.row_at(0).downloaded), "never downloaded"
        entry = window.file_model.row_at(0).entry
        assert entry is not None
        assert cached_path(entry).read_bytes() == b"data"

        # Second open hits the cache: no further MTProto downloads needed.
        window.open_row(window.file_model.row_at(0))
        assert is_cached(entry)
    finally:
        window.close()
        bridge.stop()
