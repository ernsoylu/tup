"""Tests for `tup chat discover`: chat-ID discovery from pending updates."""

from __future__ import annotations

import respx
from typer.testing import CliRunner

from tests.conftest import tg_json
from tup.cli import app

runner = CliRunner()


def test_discover_lists_chats_from_updates(fake_env: str, telegram_api: respx.MockRouter) -> None:
    bot_user = {"id": 42, "is_bot": True, "first_name": "tup"}
    updates = [
        {
            "update_id": 900,
            "message": {
                "message_id": 1,
                "date": 1700000000,
                "chat": {"id": -100123, "type": "supergroup", "title": "Work Files"},
                "text": "hi",
            },
        },
        {
            "update_id": 901,
            "my_chat_member": {
                "chat": {"id": -100555, "type": "channel", "title": "Backups"},
                "from": {"id": 7, "is_bot": False, "first_name": "Eren"},
                "date": 1700000001,
                "old_chat_member": {"status": "left", "user": bot_user},
                "new_chat_member": {"status": "member", "user": bot_user},
            },
        },
        {
            "update_id": 902,
            "message": {
                "message_id": 2,
                "date": 1700000002,
                "chat": {"id": 7, "type": "private", "first_name": "Eren"},
                "text": "/start",
            },
        },
    ]
    telegram_api["getUpdates"].mock(return_value=tg_json(updates))

    result = runner.invoke(app, ["chat", "discover"])
    assert result.exit_code == 0, result.output
    assert "-100123" in result.output
    assert "Work Files" in result.output
    assert "-100555" in result.output  # visible via my_chat_member (no message needed)
    assert "Backups" in result.output
    assert "Eren" in result.output  # private chats show the person's name


def test_discover_with_no_updates_gives_guidance(
    fake_env: str, telegram_api: respx.MockRouter
) -> None:
    result = runner.invoke(app, ["chat", "discover"])
    assert result.exit_code == 0
    assert "Add the bot" in result.output
