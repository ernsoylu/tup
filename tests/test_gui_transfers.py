"""Transfer queue semantics and the GUI upload flow (offscreen)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from tests._qt import get_qapp, pump
from tests.conftest import CHAT_ID, FakeMtprotoClient
from tup.config import Settings, default_database_path
from tup.database import Database
from tup.gui.transfers import Transfer, TransferManager, collect_upload_targets

# --- TransferManager (pure asyncio) -------------------------------------------


async def test_transfers_run_sequentially_and_report() -> None:
    updates: list[tuple[int, str]] = []
    manager = TransferManager(lambda t: updates.append((t.id, t.state)))
    manager.start()
    order: list[str] = []

    async def runner_a(_t: Transfer) -> None:
        order.append("a")

    async def runner_b(_t: Transfer) -> None:
        order.append("b")

    await manager.enqueue("upload", "a.txt", "→ /", 1, runner_a)
    await manager.enqueue("upload", "b.txt", "→ /", 1, runner_b)
    await asyncio.wait_for(manager.wait_idle(), timeout=5)
    await manager.shutdown()

    assert order == ["a", "b"]
    assert (1, "done") in updates
    assert (2, "done") in updates


async def test_pause_holds_queue_until_resume() -> None:
    manager = TransferManager(lambda t: None)
    manager.start()
    ran = asyncio.Event()

    async def runner(_t: Transfer) -> None:
        ran.set()

    await manager.pause()
    await manager.enqueue("upload", "a.txt", "→ /", 1, runner)
    await asyncio.sleep(0.05)
    assert not ran.is_set()

    await manager.resume()
    await asyncio.wait_for(ran.wait(), timeout=5)
    await manager.shutdown()


async def test_cancel_queued_and_skip_running() -> None:
    states: dict[int, str] = {}
    manager = TransferManager(lambda t: states.__setitem__(t.id, t.state))
    manager.start()
    started = asyncio.Event()

    async def slow(_t: Transfer) -> None:
        started.set()
        await asyncio.sleep(60)

    async def never(_t: Transfer) -> None:  # pragma: no cover - must not run
        raise AssertionError("cancelled transfer must not start")

    first = await manager.enqueue("download", "slow.bin", "← /", 10, slow)
    second = await manager.enqueue("download", "queued.bin", "← /", 10, never)
    await asyncio.wait_for(started.wait(), timeout=5)

    await manager.cancel(second.id)  # cancel while queued
    await manager.skip_current()  # skip the running one
    await asyncio.wait_for(manager.wait_idle(), timeout=5)
    await manager.shutdown()

    assert states[first.id] == "skipped"
    assert states[second.id] == "cancelled"


async def test_failed_runner_marks_failed_and_keeps_worker_alive() -> None:
    states: dict[int, str] = {}
    manager = TransferManager(lambda t: states.__setitem__(t.id, t.state))
    manager.start()

    async def boom(_t: Transfer) -> None:
        raise RuntimeError("kaput")

    async def fine(_t: Transfer) -> None:
        return None

    bad = await manager.enqueue("upload", "bad.txt", "→ /", 1, boom)
    good = await manager.enqueue("upload", "good.txt", "→ /", 1, fine)
    await asyncio.wait_for(manager.wait_idle(), timeout=5)
    await manager.shutdown()

    assert states[bad.id] == "failed"
    assert bad.error == "kaput"
    assert states[good.id] == "done"


# --- drop-target expansion ----------------------------------------------------


def test_collect_upload_targets_preserves_folder_structure(tmp_path: Path) -> None:
    (tmp_path / "single.txt").write_bytes(b"x")
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "readme.md").write_bytes(b"yy")
    (project / "src" / "main.py").write_bytes(b"zzz")

    targets = collect_upload_targets([tmp_path / "single.txt", project], "/docs/")
    mapped = {(str(p.name), dest, size) for p, dest, size in targets}
    assert mapped == {
        ("single.txt", "/docs/", 1),
        ("readme.md", "/docs/project/", 2),
        ("main.py", "/docs/project/src/", 3),
    }


# --- GUI upload end-to-end ----------------------------------------------------


def test_dropped_file_uploads_and_appears_in_listing(
    fake_env: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tup.gui.bridge import CoreBridge
    from tup.gui.main_window import MainWindow

    qapp = get_qapp()

    async def _seed() -> None:
        async with Database(default_database_path()) as db:
            await db.alias_add("work", CHAT_ID, "Work Files")

    asyncio.run(_seed())

    fake = FakeMtprotoClient()

    async def fake_connect(settings: Any) -> FakeMtprotoClient:
        return fake

    monkeypatch.setattr("tup.gui.bridge.connect_mtproto", fake_connect)

    upload = tmp_path / "report.pdf"
    upload.write_bytes(b"%PDF-1.4 test")

    bridge = CoreBridge(Settings.load())
    bridge.start()
    window = MainWindow(bridge)
    window.suppress_dialogs = True
    window.open_files_externally = False
    try:
        assert pump(qapp, lambda: window.drive_combo.count() > 0)
        window.enqueue_upload_paths([upload])  # same path the drop handler uses
        assert pump(qapp, lambda: window.file_model.rowCount() > 0), "upload never indexed"

        assert window.file_model.row_at(0).name == "report.pdf"
        assert fake.sent and fake.sent[0]["force_document"] is True
        assert not window.transfers_dock.isHidden()
        assert window.transfers_panel.table.rowCount() == 1
        assert window.transfers_panel.active_count() == 0  # finished

        async def _check() -> bool:
            async with Database(default_database_path()) as db:
                return await db.vfs_get(CHAT_ID, "/", "report.pdf") is not None

        assert asyncio.run(_check())
    finally:
        window.close()
        bridge.stop()


def test_panel_summary_and_terminal_bar_removal() -> None:
    """Finished rows lose their progress bar; the summary digests the queue."""
    from PyQt6.QtWidgets import QProgressBar

    from tests._qt import get_qapp
    from tup.gui.transfers_panel import TransfersPanel

    qapp = get_qapp()  # keep a reference: the QApplication must outlive the widget
    assert qapp is not None
    panel = TransfersPanel(
        on_pause=lambda: None,
        on_resume=lambda: None,
        on_skip=lambda: None,
        on_cancel=lambda _id: None,
    )
    running = Transfer(id=1, kind="upload", label="big.bin", detail="→ /", total=100)
    running.state = "running"
    running.done = 40
    panel.update_transfer(running.snapshot())
    assert isinstance(panel.table.cellWidget(0, 3), QProgressBar)  # live bar

    finished = running.snapshot()
    finished.state = "done"
    finished.done = 100
    panel.update_transfer(finished)
    assert panel.table.cellWidget(0, 3) is None  # bar removed on terminal state
    assert "1 done" in panel.summary_label.text()

    queued = Transfer(id=2, kind="download", label="clip.mp4", detail="← /", total=50)
    panel.update_transfer(queued.snapshot())
    assert "1 queued" in panel.summary_label.text()
    assert "1 done" in panel.summary_label.text()


def test_pause_button_reports_draining_state() -> None:
    """While a transfer drains, the pause toggle says so instead of 'Resume'."""
    from tests._qt import get_qapp
    from tup.gui.transfers_panel import TransfersPanel

    qapp = get_qapp()  # keep a reference: the QApplication must outlive the widget
    assert qapp is not None
    panel = TransfersPanel(
        on_pause=lambda: None,
        on_resume=lambda: None,
        on_skip=lambda: None,
        on_cancel=lambda _id: None,
    )
    running = Transfer(id=1, kind="upload", label="big.bin", detail="→ /", total=100)
    running.state = "running"
    panel.update_transfer(running.snapshot())

    panel.pause_button.setChecked(True)  # user clicks Pause mid-transfer
    assert "Pausing after current" in panel.pause_button.text()

    finished = running.snapshot()
    finished.state = "done"
    panel.update_transfer(finished)  # queue actually holds now
    assert "Resume queue" in panel.pause_button.text()

    panel.pause_button.setChecked(False)
    assert "Pause queue" in panel.pause_button.text()
