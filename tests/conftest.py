"""Shared fixtures: config isolation, fake .env, respx Telegram API mock."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
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
    monkeypatch.setenv("TUP_CACHE_DIR", str(tmp_path / "tui-cache"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    # Wide console so rich tables don't truncate paths and messages don't wrap
    # mid-word, which would break substring assertions.
    monkeypatch.setenv("COLUMNS", "300")
    # Rich force-enables ANSI colors when it detects GitHub Actions, which
    # would salt captured output with escape codes and break substring asserts.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.chdir(tmp_path)
    return config_dir


def make_settings(
    max_retries: int = 2,
    base_url: str | None = None,
    api_id: int | None = None,
    api_hash: str | None = None,
) -> Settings:
    """Build Settings from a plain dict (sidesteps strict-mypy call-arg checks)."""
    data: dict[str, Any] = {"telegram_bot_token": FAKE_TOKEN, "max_retries": max_retries}
    if base_url:
        data["telegram_api_base_url"] = base_url
    if api_id:
        data["telegram_api_id"] = api_id
    if api_hash:
        data["telegram_api_hash"] = api_hash
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
        router.post(url__regex=_BASE + "/getMe", name="getMe").mock(
            return_value=tg_json(
                {"id": 42, "is_bot": True, "first_name": "tup", "username": "tup_bot"}
            )
        )
        router.post(url__regex=_BASE + "/getChat", name="getChat").mock(
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
        router.post(url__regex=_BASE + "/sendDocument", name="sendDocument").mock(
            return_value=tg_json(
                message_result(
                    document={"file_id": "fid-doc", "file_unique_id": "u1", "file_name": "a.bin"}
                )
            )
        )
        router.post(url__regex=_BASE + "/sendPhoto", name="sendPhoto").mock(
            return_value=tg_json(
                message_result(
                    photo=[
                        {"file_id": "fid-photo", "file_unique_id": "u2", "width": 1, "height": 1}
                    ]
                )
            )
        )
        router.post(url__regex=_BASE + "/sendVideo", name="sendVideo").mock(
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
        router.post(url__regex=_BASE + "/sendAudio", name="sendAudio").mock(
            return_value=tg_json(
                message_result(
                    audio={"file_id": "fid-audio", "file_unique_id": "u4", "duration": 1}
                )
            )
        )
        router.post(url__regex=_BASE + "/editMessageCaption", name="editMessageCaption").mock(
            return_value=tg_json(message_result())
        )
        router.post(url__regex=_BASE + "/deleteMessage", name="deleteMessage").mock(
            return_value=tg_json(True)
        )
        router.post(url__regex=_BASE + "/getUpdates", name="getUpdates").mock(
            return_value=tg_json([])
        )
        router.post(url__regex=_BASE + "/getWebhookInfo", name="getWebhookInfo").mock(
            return_value=tg_json(
                {"url": "", "has_custom_certificate": False, "pending_update_count": 0}
            )
        )
        yield router


@pytest.fixture
def mock_bot() -> AsyncMock:
    """AsyncMock standing in for telegram.Bot in pure unit tests."""
    return AsyncMock(spec=Bot)


class FakeMtprotoClient:
    """Stands in for a connected Telethon TelegramClient (MTProto never leaves the process)."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.next_id = 101
        self.existing_ids: set[int] | None = None  # None = every message exists
        self.download_payload = b"data"

    async def get_input_entity(self, peer: Any) -> Any:
        return peer

    async def disconnect(self) -> None:
        return None

    async def send_file(self, peer: Any, file: Any, **kwargs: Any) -> Any:
        self.sent.append({"peer": peer, "file": file, **kwargs})
        message = SimpleNamespace(id=self.next_id)
        self.next_id += 1
        return message

    def _message(self, message_id: int) -> Any:
        if self.existing_ids is not None and message_id not in self.existing_ids:
            return None  # Telethon yields None placeholders for deleted messages
        return SimpleNamespace(id=message_id, media=f"media-of-{message_id}")

    async def get_messages(self, peer: Any, ids: int | list[int]) -> Any:
        if isinstance(ids, list):
            return [self._message(i) for i in ids]
        return self._message(ids)

    def _write_download(self, file: Any) -> str:
        path = Path(str(file))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.download_payload)
        return str(path)

    async def download_media(
        self, message: Any, file: Any = None, progress_callback: Any = None
    ) -> str:
        result = self._write_download(file)
        if progress_callback is not None:
            progress_callback(len(self.download_payload), len(self.download_payload))
        return result


@pytest.fixture
def mock_mtproto(monkeypatch: pytest.MonkeyPatch) -> FakeMtprotoClient:
    """Replace the CLI's mtproto_session with an in-memory fake client."""
    fake = FakeMtprotoClient()

    @asynccontextmanager
    async def fake_session(settings: Any) -> AsyncIterator[FakeMtprotoClient]:
        yield fake

    monkeypatch.setattr("tup.cli.mtproto_session", fake_session)
    return fake
