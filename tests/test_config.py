"""Config tests: .env loading, 0600 enforcement, setup gate."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from tup.config import (
    SECURE_MODE,
    Settings,
    SetupRequiredError,
    config_dir,
    ensure_secure_permissions,
    env_file_path,
    write_env_file,
)

FAKE_TOKEN = "123456789:AAEexampleexampleexampleexample12345"  # noqa: S105


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_config_dir_honors_override(isolate_config: Path) -> None:
    assert config_dir() == isolate_config


def test_write_env_file_sets_0600(isolate_config: Path) -> None:
    target = write_env_file({"telegram_bot_token": FAKE_TOKEN})
    assert target == isolate_config / ".env"
    assert _mode(target) == SECURE_MODE
    assert FAKE_TOKEN in target.read_text()


def test_ensure_secure_permissions_repairs_loose_mode(tmp_path: Path) -> None:
    f = tmp_path / "secret"
    f.write_text("x")
    f.chmod(0o644)
    ensure_secure_permissions(f)
    assert _mode(f) == SECURE_MODE


def test_load_missing_env_raises_setup_required() -> None:
    with pytest.raises(SetupRequiredError, match="tup setup"):
        Settings.load()


def test_load_invalid_env_raises_setup_required(isolate_config: Path) -> None:
    write_env_file({"default_chat_type": "group"})  # missing required token
    with pytest.raises(SetupRequiredError, match="invalid"):
        Settings.load()


def test_load_valid_env_and_repairs_permissions(isolate_config: Path) -> None:
    target = write_env_file({"telegram_bot_token": FAKE_TOKEN, "default_chat_id": "-1001234567890"})
    target.chmod(0o644)
    settings = Settings.load()
    assert settings.telegram_bot_token.get_secret_value() == FAKE_TOKEN
    assert settings.default_chat_id == "-1001234567890"
    assert settings.max_retries == 3
    assert _mode(target) == SECURE_MODE


def test_local_env_wins_over_config_dir(isolate_config: Path, tmp_path: Path) -> None:
    write_env_file({"telegram_bot_token": FAKE_TOKEN})
    local = tmp_path / ".env"
    local.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nDEFAULT_CHAT_ID=local-drive\n")
    assert env_file_path() == local
    settings = Settings.load()
    assert settings.default_chat_id == "local-drive"


def test_token_never_serialized_in_repr(isolate_config: Path) -> None:
    write_env_file({"telegram_bot_token": FAKE_TOKEN})
    settings = Settings.load()
    assert FAKE_TOKEN not in repr(settings)
    assert FAKE_TOKEN not in str(settings.telegram_bot_token)
