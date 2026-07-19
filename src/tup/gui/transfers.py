"""Transfer queue core: sequential upload/download worker with pause/stop/skip.

Deliberately Qt-free — everything here runs on the CoreBridge's asyncio loop
and reports through a plain callback, so the queue logic is testable with
pytest-asyncio and the GUI only does marshaling.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from tup.uploader import DuplicateFileError
from tup.utils import is_hidden_within

TransferKind = Literal["upload", "download"]
TransferState = Literal["queued", "running", "done", "failed", "cancelled", "skipped"]

# Fire on_update at most every this many bytes per transfer (plus state changes).
_REPORT_STEP = 256 * 1024


@dataclass
class Transfer:
    """One queued piece of work; mutated only on the bridge loop."""

    id: int
    kind: TransferKind
    label: str  # file name
    detail: str  # human context, e.g. '→ /docs/'
    total: int  # bytes (0 = unknown)
    done: int = 0
    state: TransferState = "queued"
    error: str | None = None

    def snapshot(self) -> Transfer:
        """Immutable-ish copy safe to hand across threads."""
        return replace(self)


TransferRunner = Callable[[Transfer], Coroutine[Any, Any, None]]


class TransferManager:
    """Sequential transfer worker living on the bridge's asyncio loop.

    `on_update(transfer_snapshot)` fires on the loop thread whenever state or
    (throttled) progress changes; the GUI marshals it onto the Qt thread.
    Pausing holds the queue between items — an in-flight MTProto transfer
    cannot be suspended, only cancelled (stop/skip).
    """

    def __init__(self, on_update: Callable[[Transfer], None]) -> None:
        self._on_update = on_update
        self._queue: asyncio.Queue[tuple[Transfer, TransferRunner]] = asyncio.Queue()
        self._next_id = 1
        self._gate = asyncio.Event()
        self._gate.set()  # cleared == paused
        self._current: tuple[Transfer, asyncio.Task[None]] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._last_reported: dict[int, int] = {}
        self._transfers: dict[int, Transfer] = {}

    # -- lifecycle (bridge loop) ----------------------------------------------

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def shutdown(self) -> None:
        if self._current is not None:
            self._current[1].cancel()
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    async def wait_idle(self) -> None:
        """Block until every enqueued transfer has finished (tests)."""
        await self._queue.join()

    # -- queue operations (call via bridge.submit) ------------------------------

    async def enqueue(
        self, kind: TransferKind, label: str, detail: str, total: int, runner: TransferRunner
    ) -> Transfer:
        transfer = Transfer(id=self._next_id, kind=kind, label=label, detail=detail, total=total)
        self._next_id += 1
        self._transfers[transfer.id] = transfer
        self._notify(transfer)
        await self._queue.put((transfer, runner))
        return transfer

    async def pause(self) -> None:
        self._gate.clear()

    async def resume(self) -> None:
        self._gate.set()

    @property
    def paused(self) -> bool:
        return not self._gate.is_set()

    async def cancel(self, transfer_id: int, *, as_skip: bool = False) -> None:
        """Cancel a queued or running transfer by id."""
        if self._current is not None and self._current[0].id == transfer_id:
            self._current[0].state = "skipped" if as_skip else "cancelled"
            self._current[1].cancel()
            return
        # Queued items are marked now and skipped when the worker reaches them.
        transfer = self._transfers.get(transfer_id)
        if transfer is not None and transfer.state == "queued":
            transfer.state = "cancelled"
            self._notify(transfer)

    async def skip_current(self) -> None:
        if self._current is not None:
            await self.cancel(self._current[0].id, as_skip=True)

    # -- progress (called from runners on the loop thread) ----------------------

    def report(self, transfer: Transfer, done: int, total: int | None = None) -> None:
        transfer.done = done
        if total:
            transfer.total = total
        last = self._last_reported.get(transfer.id, -_REPORT_STEP)
        if done - last >= _REPORT_STEP or done >= transfer.total:
            self._last_reported[transfer.id] = done
            self._notify(transfer)

    # -- internals ---------------------------------------------------------------

    def _notify(self, transfer: Transfer) -> None:
        self._on_update(transfer.snapshot())

    def _set_state(self, transfer: Transfer, state: TransferState) -> None:
        transfer.state = state
        self._notify(transfer)

    async def _run(self) -> None:
        while True:
            transfer, runner = await self._queue.get()
            try:
                if transfer.state != "queued":  # cancelled while waiting
                    continue
                await self._gate.wait()
                if transfer.state != "queued":
                    continue
                self._set_state(transfer, "running")
                task = asyncio.create_task(runner(transfer))
                self._current = (transfer, task)
                try:
                    await task
                    transfer.done = transfer.total
                    self._set_state(transfer, "done")
                except DuplicateFileError as exc:
                    # Identical content already in the folder — a skip, not a failure.
                    transfer.error = str(exc)
                    self._set_state(transfer, "skipped")
                except asyncio.CancelledError:
                    if transfer.state not in ("cancelled", "skipped"):
                        transfer.state = "cancelled"
                    self._notify(transfer)
                    # Distinguish "the runner was stopped/skipped" (swallow and
                    # move on) from "the worker itself is being shut down"
                    # (re-raise, or shutdown() would await this task forever).
                    worker = asyncio.current_task()
                    if worker is not None and worker.cancelling():
                        raise
                except Exception as exc:  # runner failures never kill the worker
                    transfer.error = str(exc)
                    self._set_state(transfer, "failed")
                finally:
                    self._current = None
            finally:
                self._queue.task_done()


def collect_upload_targets(paths: list[Path], base_dir: str) -> list[tuple[Path, str, int]]:
    """Expand dropped files/folders into (file, destination dir, size) triples.

    A dropped folder mounts under the current directory by its own name,
    preserving its internal structure — mirroring `tup up <folder>`.
    """
    base = base_dir if base_dir.endswith("/") else base_dir + "/"
    out: list[tuple[Path, str, int]] = []
    for path in paths:
        if path.is_file():
            out.append((path, base, path.stat().st_size))
        elif path.is_dir():
            for file in sorted(p for p in path.rglob("*") if p.is_file()):
                if is_hidden_within(file, path):
                    continue  # dotfiles/.git/.DS_Store never become drive content
                rel = file.parent.relative_to(path)
                sub = base + path.name + "/"
                if str(rel) != ".":
                    sub += "/".join(rel.parts) + "/"
                out.append((file, sub, file.stat().st_size))
    return out
