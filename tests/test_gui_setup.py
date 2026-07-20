"""First-run GUI setup wizard: field validation, live token check, .env write."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from tests._qt import get_qapp, pump
from tests.conftest import FAKE_TOKEN
from tup.config import env_file_path


def test_setup_dialog_validates_and_writes_env(
    isolate_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tup.gui.setup_dialog import SetupDialog

    qapp = get_qapp()

    async def fake_validate(token: str, base_url: str | None) -> str:
        assert token == FAKE_TOKEN
        assert base_url is None
        return "mybot"

    monkeypatch.setattr("tup.gui.setup_dialog.validate_token", fake_validate)

    dialog = SetupDialog()
    dialog.token_edit.setText(FAKE_TOKEN)
    dialog.api_id_edit.setText("12345")
    dialog.api_hash_edit.setText("ab" * 16)
    dialog._on_save()
    assert pump(qapp, lambda: dialog.bot_username is not None), "validation never finished"
    assert dialog.bot_username == "mybot"

    env = env_file_path()
    assert env.is_file()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    content = env.read_text()
    assert f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}" in content
    assert "TELEGRAM_API_ID=12345" in content
    assert f"TELEGRAM_API_HASH={'ab' * 16}" in content


def test_setup_dialog_rejects_bad_input_without_network(isolate_config: Path) -> None:
    from tup.gui.setup_dialog import SetupDialog

    get_qapp()
    dialog = SetupDialog()
    dialog._on_save()  # everything empty
    assert "bot token" in dialog.status_label.text().lower()

    dialog.token_edit.setText(FAKE_TOKEN)
    dialog.api_id_edit.setText("not-a-number")
    dialog._on_save()
    assert "api id" in dialog.status_label.text().lower()

    dialog.api_id_edit.setText("12345")
    dialog._on_save()  # api_hash still missing
    assert "api hash" in dialog.status_label.text().lower()
    assert not env_file_path().exists()


def test_setup_dialog_surfaces_validation_failure(
    isolate_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tup.gui.setup_dialog import SetupDialog

    qapp = get_qapp()

    async def failing_validate(token: str, base_url: str | None) -> str:
        raise RuntimeError("Unauthorized")

    monkeypatch.setattr("tup.gui.setup_dialog.validate_token", failing_validate)

    dialog = SetupDialog()
    dialog.token_edit.setText("bad-token")
    dialog.api_id_edit.setText("12345")
    dialog.api_hash_edit.setText("ab" * 16)
    dialog._on_save()
    assert pump(qapp, lambda: "Unauthorized" in dialog.status_label.text())
    assert dialog.save_button.isEnabled()  # user can correct and retry
    assert not env_file_path().exists()
    dialog.reject()  # waits for the worker thread before teardown
