"""GUI window smoke tests against a seeded database (offscreen, real bridge)."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from tests._qt import get_qapp, pump
from tests.conftest import CHAT_ID
from tup.config import Settings, default_database_path
from tup.database import Database

HASH = "a1" * 32


def seed_drive() -> None:
    async def _run() -> None:
        async with Database(default_database_path()) as db:
            await db.alias_add("work", CHAT_ID, "Work Files")
            await db.vfs_upsert(CHAT_ID, "/", "root.txt", 4, HASH, "", 1)
            await db.vfs_upsert(CHAT_ID, "/docs/", "a.pdf", 10, HASH, "", 2)
            await db.vfs_upsert(CHAT_ID, "/docs/", ".keep", 0, HASH, "", 3)

    asyncio.run(_run())


def test_window_browses_seeded_drive(fake_env: str) -> None:
    from tup.gui.bridge import CoreBridge
    from tup.gui.main_window import MainWindow

    qapp = get_qapp()
    seed_drive()
    bridge = CoreBridge(Settings.load())
    bridge.start()
    window = MainWindow(bridge)
    window.suppress_dialogs = True
    try:
        assert pump(qapp, lambda: window.file_model.rowCount() > 0), "drive never loaded"

        # Drive combo picked the default drive (alias matches DEFAULT_CHAT_ID).
        assert window.drive_combo.currentData() == CHAT_ID

        names = {window.file_model.row_at(i).name for i in range(window.file_model.rowCount())}
        assert names == {"docs", "root.txt"}  # folder synthesized, .keep hidden

        window.set_current_dir("/docs/")
        names = {window.file_model.row_at(i).name for i in range(window.file_model.rowCount())}
        assert names == {"a.pdf"}

        window.hidden_action.setChecked(True)  # reveals dotfiles synchronously
        names = {window.file_model.row_at(i).name for i in range(window.file_model.rowCount())}
        assert names == {"a.pdf", ".keep"}

        window._go_up()
        assert window.current_dir == "/"
        assert window.path_edit.text() == "/"
    finally:
        window.close()
        bridge.stop()


def test_drive_switch_swaps_content_and_reports_hidden(fake_env: str) -> None:
    """Changing the drive dropdown must reload the tree/list from the new chat,
    and folders whose only content is hidden must say so in the status bar."""
    from tup.gui.bridge import CoreBridge
    from tup.gui.main_window import MainWindow

    qapp = get_qapp()
    other = "-200999"

    async def _seed() -> None:
        async with Database(default_database_path()) as db:
            await db.alias_add("work", CHAT_ID, "Work Files")
            await db.alias_add("junk", other, "Junk Drive")
            await db.vfs_upsert(CHAT_ID, "/", "work-file.txt", 4, HASH, "", 1)
            await db.vfs_upsert(other, "/media/", "clip.mp4", 9, HASH, "", 2)
            await db.vfs_upsert(other, "/media/", ".DS_Store", 1, "b" * 64, "", 3)

    asyncio.run(_seed())

    bridge = CoreBridge(Settings.load())
    bridge.start()
    window = MainWindow(bridge)
    window.suppress_dialogs = True
    try:
        assert pump(qapp, lambda: window.file_model.rowCount() > 0)
        assert window.drive_combo.currentData() == CHAT_ID  # default drive selected
        names = {window.file_model.row_at(i).name for i in range(window.file_model.rowCount())}
        assert names == {"work-file.txt"}

        other_index = next(
            i for i in range(window.drive_combo.count()) if window.drive_combo.itemData(i) == other
        )
        window.drive_combo.setCurrentIndex(other_index)  # emits the same signal a click does
        assert pump(
            qapp,
            lambda: (
                {window.file_model.row_at(i).name for i in range(window.file_model.rowCount())}
                == {"media"}
            ),
        ), "drive switch never refreshed the listing"
        assert "Junk Drive" in window.windowTitle()

        window.set_current_dir("/media/")
        names = {window.file_model.row_at(i).name for i in range(window.file_model.rowCount())}
        assert names == {"clip.mp4"}  # .DS_Store hidden…
        bar = window.statusBar()
        assert bar is not None
        assert "+1 hidden" in bar.currentMessage()  # …but the status bar says so
    finally:
        window.close()
        bridge.stop()


def test_window_without_drives_shows_hint(fake_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from tup.gui.bridge import CoreBridge
    from tup.gui.main_window import MainWindow

    qapp = get_qapp()
    settings = Settings.load().model_copy(update={"default_chat_id": None})
    bridge = CoreBridge(settings)
    bridge.start()
    window = MainWindow(bridge)
    window.suppress_dialogs = True
    try:
        bar = window.statusBar()
        assert bar is not None
        assert pump(qapp, lambda: "No drives yet" in bar.currentMessage()), bar.currentMessage()
        assert window.drive_combo.count() == 0
    finally:
        window.close()
        bridge.stop()
