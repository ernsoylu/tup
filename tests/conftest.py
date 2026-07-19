"""Shared fixtures: config isolation so tests never touch the real user's home."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect TUP_CONFIG_DIR and cwd into tmp_path for every test."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("TUP_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    return config_dir
