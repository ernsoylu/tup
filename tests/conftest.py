"""Shared fixtures: config isolation, fake .env, respx Telegram API mock."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from httpx import Response
from telegram import Bot

from tup.config import Settings, write_env_file

FAKE_TOKEN = "123456789:AAEexampleexampleexampleexample12345"  # noqa: S105
CHAT_ID = "-100123"
_BASE = r"https://api\.telegram\.org/bot[^/]+"


@pytest.fixture(autouse=True)
def isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect TUP_CONFIG_DIR and cwd into tmp_path for every test."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("TUP_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    return config_dir


def make_settings(max_retries: int = 2, base_url: str | None = None) -> Settings:
    """Build Settings from a plain dict (sidesteps strict-mypy call-arg checks)."""
    data: dict[str, Any] = {"telegram_bot_token": FAKE_TOKEN, "max_retries": max_retries}
    if base_url:
        data["telegram_api_base_url"] = base_url
    return Settings.model_validate(data)


@pytest.fixture
def fake_env(isolate_config: Path) -> str:
    """Write a valid .env so Settings.load() succeeds."""
    write_env_file({"telegram_bot_token": FAKE_TOKEN, "default_chat_id": CHAT_ID})
    return FAKE_TOKEN


def tg_json(result: Any) -> Response:
    return Response(200, json={"ok": True, "result": result})


def message_result(message_id: int = 101, **extra: Any) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "date": 1700000000,
        "chat": {"id": int(CHAT_ID), "type": "supergroup", "title": "Work Files"},
        **extra,
    }


@pytest.fixture
def telegram_api() -> Iterator[respx.MockRouter]:
    """Intercept all Bot API HTTP calls; the live API is never reachable."""
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(url__regex=_BASE + "/getMe").mock(
            return_value=tg_json(
                {"id": 42, "is_bot": True, "first_name": "tup", "username": "tup_bot"}
            )
        )
        router.post(url__regex=_BASE + "/getChat").mock(
            return_value=tg_json(
                {
                    "id": int(CHAT_ID),
                    "type": "supergroup",
                    "title": "Work Files",
                    "accent_color_id": 0,
                    "max_reaction_count": 11,
                }
            )
        )
        router.post(url__regex=_BASE + "/sendDocument").mock(
            return_value=tg_json(
                message_result(
                    document={"file_id": "fid-doc", "file_unique_id": "u1", "file_name": "a.bin"}
                )
            )
        )
        router.post(url__regex=_BASE + "/sendPhoto").mock(
            return_value=tg_json(
                message_result(
                    photo=[
                        {"file_id": "fid-photo", "file_unique_id": "u2", "width": 1, "height": 1}
                    ]
                )
            )
        )
        router.post(url__regex=_BASE + "/sendVideo").mock(
            return_value=tg_json(
                message_result(
                    video={
                        "file_id": "fid-video",
                        "file_unique_id": "u3",
                        "width": 1,
                        "height": 1,
                        "duration": 1,
                    }
                )
            )
        )
        router.post(url__regex=_BASE + "/sendAudio").mock(
            return_value=tg_json(
                message_result(
                    audio={"file_id": "fid-audio", "file_unique_id": "u4", "duration": 1}
                )
            )
        )
        router.post(url__regex=_BASE + "/editMessageCaption").mock(
            return_value=tg_json(message_result())
        )
        router.post(url__regex=_BASE + "/deleteMessage").mock(return_value=tg_json(True))
        router.post(url__regex=_BASE + "/getUpdates").mock(return_value=tg_json([]))
        yield router


@pytest.fixture
def mock_bot() -> AsyncMock:
    """AsyncMock standing in for telegram.Bot in pure unit tests."""
    return AsyncMock(spec=Bot)
