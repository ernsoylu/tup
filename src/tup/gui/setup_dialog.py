"""First-run configuration wizard: bot token + my.telegram.org API credentials.

This dialog runs before the CoreBridge exists (nothing else works without a
valid .env), so the live bot-token check happens on a plain QThread with its
own asyncio loop instead of the bridge.
"""

from __future__ import annotations

import asyncio

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tup.config import Settings, write_env_file
from tup.setup import validate_token

_GUIDE_HTML = """
<b>How to get your credentials</b>
<ol>
<li><b>Bot token</b> — in Telegram, message
<a href="https://t.me/BotFather">@BotFather</a>, send <code>/newbot</code>,
follow the prompts, and copy the token (looks like <code>123456789:AA…</code>).</li>
<li><b>API ID &amp; hash</b> (needed for uploads up to 2&nbsp;GB) — sign in at
<a href="https://my.telegram.org">my.telegram.org</a> with your phone number,
open <i>API development tools</i>, create an app (any name), then copy
<b>api_id</b> and <b>api_hash</b>.</li>
<li>Add the bot to your group (or as an <i>administrator</i> in a channel),
send <code>/start</code> there, then register the chat as a drive via
<i>Drive → Manage drives…</i></li>
</ol>
"""


class _ValidateWorker(QThread):
    """Checks the bot token live via get_me() off the GUI thread."""

    result = pyqtSignal(str, str)  # (username, error) — exactly one is non-empty

    def __init__(self, token: str, base_url: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._token = token
        self._base_url = base_url

    def run(self) -> None:
        try:
            username = asyncio.run(validate_token(self._token, self._base_url))
            self.result.emit(username, "")
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim in the dialog
            self.result.emit("", str(exc))


class SetupDialog(QDialog):
    """Collects and validates Telegram credentials, then writes ~/.tup/.env."""

    def __init__(self, existing: Settings | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._existing = existing
        self._worker: _ValidateWorker | None = None
        self.bot_username: str | None = None

        self.setWindowTitle("tup — Telegram setup")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)

        guide = QLabel(_GUIDE_HTML)
        guide.setWordWrap(True)
        guide.setOpenExternalLinks(True)
        layout.addWidget(guide)

        form = QFormLayout()
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("123456789:AA…")
        form.addRow("Bot token:", self.token_edit)

        self.api_id_edit = QLineEdit()
        self.api_id_edit.setPlaceholderText("api_id (digits, from my.telegram.org)")
        form.addRow("API ID:", self.api_id_edit)

        self.api_hash_edit = QLineEdit()
        self.api_hash_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_hash_edit.setPlaceholderText("api_hash (32 hex chars)")
        form.addRow("API hash:", self.api_hash_edit)

        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("optional — local Bot API server URL")
        form.addRow("Bot API URL:", self.base_url_edit)
        layout.addLayout(form)

        if existing is not None:
            self.token_edit.setText(existing.telegram_bot_token.get_secret_value())
            if existing.telegram_api_id is not None:
                self.api_id_edit.setText(str(existing.telegram_api_id))
            if existing.telegram_api_hash is not None:
                self.api_hash_edit.setText(existing.telegram_api_hash.get_secret_value())
            if existing.telegram_api_base_url is not None:
                self.base_url_edit.setText(str(existing.telegram_api_base_url))

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Validate && Save")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._on_save)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    # -- validation & save -------------------------------------------------------

    def done(self, result: int) -> None:
        # A QThread destroyed while running aborts the process — never let the
        # dialog (the worker's parent) be torn down under a live validation.
        if self._worker is not None:
            self._worker.wait(10_000)
        super().done(result)

    def _fail(self, message: str) -> None:
        self.status_label.setText(f"❌ {message}")
        self.save_button.setEnabled(True)

    def _on_save(self) -> None:
        token = self.token_edit.text().strip()
        api_id = self.api_id_edit.text().strip()
        api_hash = self.api_hash_edit.text().strip()
        if not token:
            self._fail("A bot token is required.")
            return
        if not api_id.isdigit():
            self._fail("API ID must be a number (from my.telegram.org).")
            return
        if not api_hash:
            self._fail("API hash is required (from my.telegram.org).")
            return
        self.save_button.setEnabled(False)
        self.status_label.setText("Checking the bot token with Telegram…")
        base_url = self.base_url_edit.text().strip() or None
        self._worker = _ValidateWorker(token, base_url, self)
        self._worker.result.connect(self._on_validated)
        self._worker.start()

    def _on_validated(self, username: str, error: str) -> None:
        if error:
            self._fail(f"Token validation failed: {error}")
            return
        existing = self._existing
        write_env_file(
            {
                "telegram_bot_token": self.token_edit.text().strip(),
                "default_chat_id": (existing.default_chat_id or "") if existing else "",
                "default_chat_type": existing.default_chat_type if existing else "group",
                "telegram_api_base_url": self.base_url_edit.text().strip(),
                "telegram_api_id": self.api_id_edit.text().strip(),
                "telegram_api_hash": self.api_hash_edit.text().strip(),
            }
        )
        self.bot_username = username
        self.accept()
