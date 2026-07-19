"""Qt test helpers: offscreen QApplication singleton and an event pump."""

from __future__ import annotations

import os

# Must be set before Qt initializes its platform plugin.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from collections.abc import Callable

from PyQt6.QtWidgets import QApplication


def get_qapp() -> QApplication:
    """Process-wide QApplication (Qt allows exactly one)."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(["tup-tests"])
    assert isinstance(app, QApplication)
    return app


def pump(app: QApplication, predicate: Callable[[], bool], timeout: float = 8.0) -> bool:
    """Process Qt events until `predicate` holds or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False
