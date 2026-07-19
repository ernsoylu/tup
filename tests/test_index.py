"""Index tests: getUpdates draining, caption-edit application, reconstruction."""

from __future__ import annotations

import asyncio
from typing import Any

import respx
from typer.testing import CliRunner

from tests.conftest import CHAT_ID, tg_json
from tup.cli import app
from tup.config import default_database_path
from tup.database import Database, VfsEntry
from tup.uploader import format_caption

runner = CliRunner()

HASH = "a1" * 32  # must be valid lowercase hex for the caption protocol regex


def seed_file(virtual_path: str, file_name: str, message_id: int) -> None:
    async def _seed() -> None:
        async with Database(default_database_path()) as db:
            await db.vfs_upsert(CHAT_ID, virtual_path, file_name, 11, HASH, "fid-a", message_id)

    asyncio.run(_seed())


def read_vfs(virtual_path: str, file_name: str) -> VfsEntry | None:
    async def _read() -> VfsEntry | None:
        async with Database(default_database_path()) as db:
            return await db.vfs_get(CHAT_ID, virtual_path, file_name)

    return asyncio.run(_read())


def read_sync_state() -> int:
    async def _read() -> int:
        async with Database(default_database_path()) as db:
            return await db.sync_state_get(CHAT_ID)

    return asyncio.run(_read())


def message_payload(message_id: int, caption: str, *, edited: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message_id": message_id,
        "date": 1700000000,
        "chat": {"id": int(CHAT_ID), "type": "supergroup", "title": "Work Files"},
        "caption": caption,
        "document": {"file_id": "fid-a", "file_unique_id": "u1", "file_size": 11},
    }
    if edited:
        payload["edit_date"] = 1700000100
    return payload


def test_index_applies_native_caption_edit(fake_env: str, telegram_api: respx.MockRouter) -> None:
    seed_file("/docs/", "a.pdf", 11)
    new_caption = format_caption("/archive/a.pdf", HASH)
    update = {
        "update_id": 500,
        "edited_message": message_payload(11, new_caption, edited=True),
    }
    telegram_api["getUpdates"].mock(side_effect=[tg_json([update]), tg_json([])])

    result = runner.invoke(app, ["index", CHAT_ID])
    assert result.exit_code == 0, result.output
    assert "1 caption edit(s) applied" in result.output
    assert read_vfs("/docs/", "a.pdf") is None
    assert read_vfs("/archive/", "a.pdf") is not None
    assert read_sync_state() == 500


def test_index_without_reconstruct_ignores_unknown_messages(
    fake_env: str, telegram_api: respx.MockRouter
) -> None:
    caption = format_caption("/docs/new.pdf", HASH)
    update = {"update_id": 600, "message": message_payload(77, caption, edited=False)}
    telegram_api["getUpdates"].mock(side_effect=[tg_json([update]), tg_json([])])

    result = runner.invoke(app, ["index", CHAT_ID])
    assert result.exit_code == 0, result.output
    assert read_vfs("/docs/", "new.pdf") is None
    assert read_sync_state() == 600  # still consumed and advanced


def test_index_reconstruct_indexes_unknown_messages(
    fake_env: str, telegram_api: respx.MockRouter
) -> None:
    caption = format_caption("/docs/new.pdf", HASH)
    update = {"update_id": 700, "message": message_payload(77, caption, edited=False)}
    telegram_api["getUpdates"].mock(side_effect=[tg_json([update]), tg_json([])])

    result = runner.invoke(app, ["index", CHAT_ID, "--reconstruct"])
    assert result.exit_code == 0, result.output
    assert "1 row(s) reconstructed" in result.output
    entry = read_vfs("/docs/", "new.pdf")
    assert entry is not None
    assert entry.file_hash == HASH
    assert entry.telegram_message_id == 77


def test_index_ignores_foreign_chats_and_captionless(
    fake_env: str, telegram_api: respx.MockRouter
) -> None:
    other_chat = {
        "update_id": 800,
        "message": {
            "message_id": 9,
            "date": 1700000000,
            "chat": {"id": -999, "type": "supergroup", "title": "Other"},
            "caption": format_caption("/x.pdf", HASH),
            "document": {"file_id": "f", "file_unique_id": "u"},
        },
    }
    chatter = {
        "update_id": 801,
        "message": message_payload(10, "just chatting", edited=False),
    }
    telegram_api["getUpdates"].mock(side_effect=[tg_json([other_chat, chatter]), tg_json([])])
    result = runner.invoke(app, ["index", CHAT_ID, "--reconstruct"])
    assert result.exit_code == 0, result.output
    assert "0 row(s) reconstructed" in result.output
    assert read_sync_state() == 801


def test_index_prune_removes_rows_for_deleted_messages(
    fake_env: str, telegram_api: respx.MockRouter, mock_mtproto: object
) -> None:
    from tests.conftest import FakeMtprotoClient

    assert isinstance(mock_mtproto, FakeMtprotoClient)
    seed_file("/docs/", "kept.pdf", 11)
    seed_file("/docs/", "deleted.pdf", 12)
    seed_file("/other/", "also-deleted.pdf", 13)
    mock_mtproto.existing_ids = {11}  # only message 11 survives on Telegram

    result = runner.invoke(app, ["index", CHAT_ID, "--prune"])
    assert result.exit_code == 0, result.output
    assert "2 stale row(s) pruned" in result.output
    assert read_vfs("/docs/", "kept.pdf") is not None
    assert read_vfs("/docs/", "deleted.pdf") is None
    assert read_vfs("/other/", "also-deleted.pdf") is None


def test_index_prune_keeps_everything_when_nothing_deleted(
    fake_env: str, telegram_api: respx.MockRouter, mock_mtproto: object
) -> None:
    seed_file("/docs/", "a.pdf", 11)
    result = runner.invoke(app, ["index", CHAT_ID, "--prune"])
    assert result.exit_code == 0, result.output
    assert "0 stale row(s) pruned" in result.output
    assert read_vfs("/docs/", "a.pdf") is not None
