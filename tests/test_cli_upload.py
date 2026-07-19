"""End-to-end CLI tests for setup gate, chat aliases, and uploads (respx-mocked)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import respx
from typer.testing import CliRunner
from typer.testing import Result as CliResult

from tests.conftest import CHAT_ID, FakeMtprotoClient
from tup.cli import app
from tup.config import default_database_path
from tup.database import Database, VfsEntry

runner = CliRunner()


def all_output(result: CliResult) -> str:
    try:
        return result.output + result.stderr
    except ValueError:
        return result.output


def read_vfs(chat_id: str, virtual_path: str, file_name: str) -> VfsEntry | None:
    async def _read() -> VfsEntry | None:
        async with Database(default_database_path()) as db:
            return await db.vfs_get(chat_id, virtual_path, file_name)

    return asyncio.run(_read())


def test_up_without_config_points_to_setup(tmp_path: Path) -> None:
    result = runner.invoke(app, ["up", "missing.txt"])
    assert result.exit_code == 1
    assert "setup" in all_output(result)


def test_chat_add_and_list(fake_env: str, telegram_api: respx.MockRouter) -> None:
    result = runner.invoke(app, ["chat", "add", "work", CHAT_ID])
    assert result.exit_code == 0, all_output(result)
    assert "Work Files" in all_output(result)

    listing = runner.invoke(app, ["chat", "list"])
    assert listing.exit_code == 0
    assert "work" in listing.output
    assert CHAT_ID in listing.output


def test_chat_remove(fake_env: str, telegram_api: respx.MockRouter) -> None:
    runner.invoke(app, ["chat", "add", "work", CHAT_ID])
    assert runner.invoke(app, ["chat", "remove", "work"]).exit_code == 0
    assert runner.invoke(app, ["chat", "remove", "work"]).exit_code == 1


def test_up_file_via_alias(
    fake_env: str,
    telegram_api: respx.MockRouter,
    mock_mtproto: FakeMtprotoClient,
    tmp_path: Path,
) -> None:
    runner.invoke(app, ["chat", "add", "work", CHAT_ID])
    f = tmp_path / "notes.txt"
    f.write_bytes(b"hello world")
    result = runner.invoke(app, ["up", str(f), "--to", "work", "--dest", "/docs"])
    assert result.exit_code == 0, all_output(result)
    entry = read_vfs(CHAT_ID, "/docs/", "notes.txt")
    assert entry is not None
    assert entry.telegram_message_id == 101


def test_bare_path_fallback_uploads_to_default_drive(
    fake_env: str, mock_mtproto: FakeMtprotoClient, tmp_path: Path
) -> None:
    f = tmp_path / "photo.dat"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 300)
    result = runner.invoke(app, [str(f)])  # no `up`: DefaultToUpGroup fallback
    assert result.exit_code == 0, all_output(result)
    entry = read_vfs(CHAT_ID, "/", "photo.dat")
    assert entry is not None
    # magic bytes routed it as browsable media, not a forced document
    assert mock_mtproto.sent[0]["force_document"] is False


def test_up_directory_mounts_under_own_name(
    fake_env: str, mock_mtproto: FakeMtprotoClient, tmp_path: Path
) -> None:
    src = tmp_path / "code"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"a")
    (src / "sub" / "b.txt").write_bytes(b"b")
    result = runner.invoke(app, ["up", str(src)])
    assert result.exit_code == 0, all_output(result)
    assert read_vfs(CHAT_ID, "/code/", "a.txt") is not None
    assert read_vfs(CHAT_ID, "/code/sub/", "b.txt") is not None
