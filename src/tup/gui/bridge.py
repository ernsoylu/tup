"""Asyncio worker thread hosting tup's core; marshals results onto the Qt thread.

Qt owns the main thread; aiosqlite/Telethon/PTB need an asyncio loop. The
CoreBridge runs a private loop on a daemon thread, exposes the long-lived
Database connection, and delivers coroutine results back to the GUI thread
through a queued Qt signal so slots always run where widgets live.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from tup.config import Settings
from tup.database import Database


class CoreBridge(QObject):
    """Schedules tup core coroutines from the GUI thread and calls back into it."""

    _dispatch = pyqtSignal(object)  # zero-arg callable, executed on the GUI thread

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="tup-core", daemon=True)
        self._db: Database | None = None
        self._dispatch.connect(self._run_job)

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Start the worker loop and open the database (local + fast, so we wait)."""
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._open_db(), self._loop).result(timeout=30)

    def stop(self) -> None:
        if not self._thread.is_alive():
            return
        asyncio.run_coroutine_threadsafe(self._close_db(), self._loop).result(timeout=30)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _open_db(self) -> None:
        self._db = Database(self.settings.database_path)
        await self._db.connect()

    async def _close_db(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> Database:
        if self._db is None:
            raise RuntimeError("CoreBridge is not started")
        return self._db

    # -- scheduling ------------------------------------------------------------

    @staticmethod
    def _run_job(job: Callable[[], None]) -> None:
        job()

    def submit[T](
        self,
        coro: Coroutine[Any, Any, T],
        on_done: Callable[[T], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> Future[T]:
        """Run `coro` on the worker loop; callbacks fire later on the GUI thread."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _deliver(fut: Future[T]) -> None:
            if fut.cancelled():
                return
            exc = fut.exception()
            if exc is not None:
                if on_error is not None:
                    self._dispatch.emit(lambda: on_error(exc))
                return
            if on_done is not None:
                result = fut.result()
                self._dispatch.emit(lambda: on_done(result))

        future.add_done_callback(_deliver)
        return future
