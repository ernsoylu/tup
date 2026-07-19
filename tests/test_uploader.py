"""Uploader tests: caption protocol, retries, size gate, routing, upload_file."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import respx
from telegram.error import RetryAfter, TimedOut

import tup.uploader as uploader_module
from tests.conftest import CHAT_ID, make_settings
from tup.config import Settings
from tup.database import Database
from tup.uploader import (
    TupError,
    bot_session,
    decide_transport,
    format_caption,
    parse_caption,
    resolve_kind,
    send_with_retry,
    upload_file,
)

HASH = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


@pytest.fixture
def settings() -> Settings:
    return make_settings(max_retries=2)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    async with Database(":memory:") as database:
        yield database


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace asyncio.sleep, recording requested delays."""
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return sleeps


# --- caption protocol ---------------------------------------------------------


def test_caption_round_trip() -> None:
    caption = format_caption("/docs/sub/file.pdf", HASH, "quarterly report")
    assert "📁 `/docs/sub/file.pdf`" in caption
    assert f"🔗 SHA256: {HASH}" in caption
    assert "#vfs #sub" in caption
    meta = parse_caption(caption)
    assert meta is not None
    assert meta.full_path == "/docs/sub/file.pdf"
    assert meta.sha256 == HASH
    assert meta.user_caption == "quarterly report"


def test_caption_root_file_tags_root() -> None:
    caption = format_caption("/file.pdf", HASH)
    assert "#vfs #root" in caption
    meta = parse_caption(caption)
    assert meta is not None
    assert meta.user_caption is None


def test_parse_caption_ignores_foreign_messages() -> None:
    assert parse_caption(None) is None
    assert parse_caption("just a chat message") is None
    assert parse_caption("📁 `/x` but no hash") is None


# --- retry logic --------------------------------------------------------------


async def test_retry_after_sleeps_then_succeeds(no_sleep: list[float]) -> None:
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryAfter(3)
        return "ok"

    assert await send_with_retry(op, max_retries=3, what="test") == "ok"
    assert no_sleep == [3.0]
    assert calls == 2


async def test_network_errors_back_off_exponentially(no_sleep: list[float]) -> None:
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise TimedOut()
        return "ok"

    assert await send_with_retry(op, max_retries=3, what="test") == "ok"
    assert no_sleep == [2.0, 4.0]


async def test_exhausted_retries_raise_tup_error(no_sleep: list[float]) -> None:
    async def op() -> str:
        raise RetryAfter(1)

    with pytest.raises(TupError, match="rate-limited"):
        await send_with_retry(op, max_retries=2, what="test")
    assert no_sleep == [1.0, 1.0]


# --- preflight & routing ------------------------------------------------------


BIG = 51 * 1024 * 1024


def test_size_gate_blocks_over_50mb_without_alternatives(
    settings: Settings, tmp_path: Path
) -> None:
    f = tmp_path / "big.bin"
    with pytest.raises(TupError, match="50 MB"):
        decide_transport(f, BIG, settings)


def test_small_files_use_bot_api(settings: Settings, tmp_path: Path) -> None:
    assert decide_transport(tmp_path / "small.bin", 1024, settings) == "botapi"


def test_local_api_server_keeps_bot_api_for_big_files(tmp_path: Path) -> None:
    settings = make_settings(base_url="http://localhost:8081")
    assert decide_transport(tmp_path / "big.bin", BIG, settings) == "botapi"


def test_mtproto_chosen_for_big_files_with_api_credentials(tmp_path: Path) -> None:
    settings = make_settings(api_id=12345, api_hash="f" * 32)
    assert decide_transport(tmp_path / "big.bin", BIG, settings) == "mtproto"


def test_resolve_kind_override_flags(tmp_path: Path) -> None:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00" * 16)
    assert resolve_kind(f)[1] == "video"  # mimetypes fallback on extension
    assert resolve_kind(f, as_doc=True)[1] == "document"
    assert resolve_kind(f, as_audio=True)[1] == "audio"


# --- upload_file --------------------------------------------------------------


async def test_upload_file_success_indexes_and_logs(
    settings: Settings, db: Database, telegram_api: respx.MockRouter, tmp_path: Path
) -> None:
    f = tmp_path / "notes.txt"
    f.write_bytes(b"hello world")
    async with bot_session(settings) as bot:
        message_id = await upload_file(db, settings, bot, f, CHAT_ID, "/docs")
    assert message_id == 101
    entry = await db.vfs_get(CHAT_ID, "/docs/", "notes.txt")
    assert entry is not None
    assert entry.file_hash == HASH
    assert entry.telegram_file_id == "fid-doc"
    logs = await db.log_recent()
    assert logs[0].status == "success"
    assert logs[0].telegram_message_id == 101


async def test_upload_file_routes_large_files_via_mtproto(
    db: Database, mock_bot: AsyncMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = make_settings(api_id=12345, api_hash="f" * 32)
    f = tmp_path / "movie.mp4"
    f.write_bytes(b"\x00" * 128)
    monkeypatch.setattr(uploader_module, "BOT_API_LIMIT_BYTES", 64)  # force the large path
    fake_mtproto = AsyncMock(return_value=777)
    monkeypatch.setattr(uploader_module, "upload_via_mtproto", fake_mtproto)

    message_id = await upload_file(db, settings, mock_bot, f, CHAT_ID, "/videos")

    assert message_id == 777
    fake_mtproto.assert_awaited_once()
    mock_bot.send_video.assert_not_called()  # Bot API path skipped entirely
    entry = await db.vfs_get(CHAT_ID, "/videos/", "movie.mp4")
    assert entry is not None
    assert entry.telegram_message_id == 777
    assert entry.telegram_file_id == ""  # MTProto yields no Bot API file_id
    logs = await db.log_recent()
    assert logs[0].status == "success"


async def test_upload_failure_lands_in_failed_registry(
    settings: Settings, db: Database, mock_bot: AsyncMock, no_sleep: list[float], tmp_path: Path
) -> None:
    f = tmp_path / "notes.txt"
    f.write_bytes(b"hello world")
    mock_bot.send_document.side_effect = TimedOut()
    with pytest.raises(TupError, match="network failure"):
        await upload_file(db, settings, mock_bot, f, CHAT_ID, "/docs")
    pending = await db.failed_pending()
    assert len(pending) == 1
    assert pending[0].file_path == str(f)
    assert pending[0].status == "pending"
    logs = await db.log_recent()
    assert logs[0].status == "failed"
