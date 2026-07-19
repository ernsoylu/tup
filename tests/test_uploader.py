"""Uploader tests: caption protocol, retries, size gate, routing, upload_file."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from telegram.error import BadRequest, RetryAfter, TimedOut

from tests.conftest import CHAT_ID, FakeMtprotoClient, make_settings
from tup.config import Settings
from tup.database import Database
from tup.uploader import (
    TupError,
    check_size_limit,
    copy_message_media,
    format_caption,
    parse_caption,
    resolve_kind,
    send_with_retry,
    upload_file,
    video_attributes,
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


# --- Bot API retry logic (used by metadata operations) ------------------------


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


async def test_bad_request_is_never_retried(no_sleep: list[float]) -> None:
    """PTB quirk: BadRequest subclasses NetworkError, but a 400 is permanent —
    it must pass straight through for the caller to translate."""
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        raise BadRequest("Message to delete not found")

    with pytest.raises(BadRequest):
        await send_with_retry(op, max_retries=3, what="test")
    assert calls == 1
    assert no_sleep == []


async def test_exhausted_retries_raise_tup_error(no_sleep: list[float]) -> None:
    async def op() -> str:
        raise RetryAfter(1)

    with pytest.raises(TupError, match="rate-limited"):
        await send_with_retry(op, max_retries=2, what="test")
    assert no_sleep == [1.0, 1.0]


# --- preflight & routing ------------------------------------------------------


def test_size_gate_blocks_over_2gb(tmp_path: Path) -> None:
    with pytest.raises(TupError, match="2 GB"):
        check_size_limit(tmp_path / "huge.bin", 3 * 1024**3)


def test_size_gate_allows_large_files(tmp_path: Path) -> None:
    check_size_limit(tmp_path / "big.bin", 1024**3)  # 1 GB: fine over MTProto


def test_resolve_kind_override_flags(tmp_path: Path) -> None:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00" * 16)
    assert resolve_kind(f)[1] == "video"  # mimetypes fallback on extension
    assert resolve_kind(f, as_doc=True)[1] == "document"
    assert resolve_kind(f, as_audio=True)[1] == "audio"


# --- upload_file (MTProto transport) ------------------------------------------


async def test_upload_file_success_indexes_and_logs(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    client = FakeMtprotoClient()
    f = tmp_path / "notes.txt"
    f.write_bytes(b"hello world")

    message_id = await upload_file(db, settings, client, f, CHAT_ID, "/docs")

    assert message_id == 101
    sent = client.sent[0]
    assert sent["force_document"] is True  # text routes as document
    assert "📁 `/docs/notes.txt`" in sent["caption"]
    assert sent["parse_mode"] is None  # caption protocol stays raw
    entry = await db.vfs_get(CHAT_ID, "/docs/", "notes.txt")
    assert entry is not None
    assert entry.file_hash == HASH
    assert entry.telegram_file_id == ""  # MTProto yields no Bot API file_id
    logs = await db.log_recent()
    assert logs[0].status == "success"
    assert logs[0].telegram_message_id == 101


async def test_upload_routes_video_as_streaming_media(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    client = FakeMtprotoClient()
    f = tmp_path / "movie.mp4"
    f.write_bytes(b"\x00" * 64)

    await upload_file(db, settings, client, f, CHAT_ID, "/videos")

    sent = client.sent[0]
    assert sent["force_document"] is False  # browsable in the media gallery
    assert sent["supports_streaming"] is True


def test_video_attributes_none_for_unparseable_file(tmp_path: Path) -> None:
    f = tmp_path / "garbage.mp4"
    f.write_bytes(b"\x00" * 64)  # not a valid container
    assert video_attributes(f) is None


async def test_upload_video_passes_extracted_dimensions(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import tup.uploader as uploader_module

    client = FakeMtprotoClient()
    f = tmp_path / "movie.mp4"
    f.write_bytes(b"\x00" * 64)
    sentinel = ["video-attrs-1080x1920"]
    monkeypatch.setattr(uploader_module, "video_attributes", lambda path: sentinel)

    await upload_file(db, settings, client, f, CHAT_ID, "/videos")

    assert client.sent[0]["attributes"] == sentinel


async def test_upload_document_passes_no_attributes(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    client = FakeMtprotoClient()
    f = tmp_path / "notes.txt"
    f.write_bytes(b"hello")

    await upload_file(db, settings, client, f, CHAT_ID, "/docs")

    assert client.sent[0]["attributes"] is None


async def test_upload_failure_lands_in_failed_registry(
    settings: Settings, db: Database, no_sleep: list[float], tmp_path: Path
) -> None:
    client = AsyncMock()
    client.get_input_entity = AsyncMock(return_value="peer")
    client.send_file.side_effect = ConnectionError("boom")
    f = tmp_path / "notes.txt"
    f.write_bytes(b"hello world")

    with pytest.raises(TupError, match="network failure"):
        await upload_file(db, settings, client, f, CHAT_ID, "/docs")

    pending = await db.failed_pending()
    assert len(pending) == 1
    assert pending[0].file_path == str(f)
    assert pending[0].status == "pending"
    logs = await db.log_recent()
    assert logs[0].status == "failed"


async def test_copy_message_media_reuses_media(settings: Settings) -> None:
    client = FakeMtprotoClient()
    new_id = await copy_message_media(
        client,
        CHAT_ID,
        11,
        format_caption("/archive/a.pdf", HASH),
        max_retries=2,
    )
    assert new_id == 101
    sent = client.sent[0]
    assert sent["file"] == "media-of-11"  # existing media object, no re-upload


class _NoMediaClient(FakeMtprotoClient):
    async def get_messages(self, peer: Any, ids: int | list[int]) -> Any:
        return None


async def test_copy_missing_media_raises(settings: Settings) -> None:
    with pytest.raises(TupError, match="no media"):
        await copy_message_media(_NoMediaClient(), CHAT_ID, 11, "cap", max_retries=2)
