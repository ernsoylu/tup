"""GUI entry point: `tup gui` or the standalone `tup-gui` script."""

from __future__ import annotations

import sys


def run_gui() -> None:
    """Launch the explorer; exits the process with Qt's return code."""
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except ImportError:  # pragma: no cover - exercised only without the gui extra
        print(
            "The tup GUI requires PyQt6. Install it with: uv sync --all-extras "
            "(or: pip install 'tup[gui]')",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    from tup.cli import setup_logging
    from tup.config import Settings, SetupRequiredError, migrate_legacy_config

    migrate_legacy_config()
    setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("tup")
    app.setApplicationDisplayName("tup — Telegram Drive")

    try:
        settings = Settings.load()
    except SetupRequiredError as exc:
        QMessageBox.critical(
            None,
            "tup — setup required",
            str(exc).replace("[bold]", "").replace("[/bold]", ""),
        )
        raise SystemExit(1) from None

    from tup.gui.bridge import CoreBridge
    from tup.gui.main_window import MainWindow

    bridge = CoreBridge(settings)
    bridge.start()
    window = MainWindow(bridge)
    window.show()
    exit_code = app.exec()
    bridge.stop()
    raise SystemExit(exit_code)
