"""VFS operation tests: tree/ls/mkdir/rmdir/cp/mv/rm against a seeded index."""

from __future__ import annotations

import asyncio

import pytest
import respx
from typer.testing import CliRunner

from tests.conftest import CHAT_ID, FakeMtprotoClient
from tup.cli import app
from tup.config import default_database_path
from tup.database import Database, VfsEntry

runner = CliRunner()


def seed(entries: list[tuple[str, str, str, int]]) -> None:
    """Seed vfs_index rows: (virtual_path, file_name, file_id, message_id)."""

    async def _seed() -> None:
        async with Database(default_database_path()) as db:
            for virtual_path, file_name, file_id, message_id in entries:
                await db.vfs_upsert(
                    CHAT_ID, virtual_path, file_name, 11, "h" * 64, file_id, message_id
                )

    asyncio.run(_seed())


def read_vfs(virtual_path: str, file_name: str) -> VfsEntry | None:
    async def _read() -> VfsEntry | None:
        async with Database(default_database_path()) as db:
            return await db.vfs_get(CHAT_ID, virtual_path, file_name)

    return asyncio.run(_read())


def calls_to(telegram_api: respx.MockRouter, method: str) -> list[bytes]:
    return [
        call.request.content
        for call in telegram_api.calls
        if call.request.url.path.endswith(f"/{method}")
    ]


@pytest.fixture
def seeded(fake_env: str) -> None:
    seed(
        [
            ("/docs/", "a.pdf", "fid-a", 11),
            ("/docs/sub/", "b.pdf", "fid-b", 12),
            ("/", "root.txt", "fid-r", 13),
        ]
    )


def test_tree_renders_hierarchy(seeded: None) -> None:
    result = runner.invoke(app, ["tree", CHAT_ID])
    assert result.exit_code == 0, result.output
    for name in ("root.txt", "docs/", "a.pdf", "sub/", "b.pdf"):
        assert name in result.output


def test_tree_level_limits_depth(seeded: None) -> None:
    result = runner.invoke(app, ["tree", CHAT_ID, "-L", "1"])
    assert result.exit_code == 0
    assert "a.pdf" in result.output  # one level down
    assert "b.pdf" not in result.output  # two levels down


def test_ls_non_recursive_shows_dirs_and_files(seeded: None) -> None:
    result = runner.invoke(app, ["ls", CHAT_ID, "/docs"])
    assert result.exit_code == 0
    assert "a.pdf" in result.output
    assert "sub/" in result.output
    assert "b.pdf" not in result.output


def test_ls_recursive_shows_full_paths(seeded: None) -> None:
    result = runner.invoke(app, ["ls", CHAT_ID, "/docs", "-R"])
    assert result.exit_code == 0
    assert "/docs/sub/b.pdf" in result.output


def test_mkdir_rmdir_lifecycle(fake_env: str) -> None:
    assert runner.invoke(app, ["mkdir", CHAT_ID, "/inbox"]).exit_code == 0
    assert read_vfs("/inbox/", ".keep") is not None
    # duplicate mkdir refused
    assert runner.invoke(app, ["mkdir", CHAT_ID, "/inbox"]).exit_code == 1
    # .keep entries are hidden from listings
    listing = runner.invoke(app, ["ls", CHAT_ID, "/inbox"])
    assert ".keep" not in listing.output
    assert runner.invoke(app, ["rmdir", CHAT_ID, "/inbox"]).exit_code == 0
    assert read_vfs("/inbox/", ".keep") is None
    assert runner.invoke(app, ["rmdir", CHAT_ID, "/inbox"]).exit_code == 1  # gone


def test_rmdir_refuses_non_empty(seeded: None) -> None:
    seed([("/docs/", ".keep", "", 0)])
    result = runner.invoke(app, ["rmdir", CHAT_ID, "/docs"])
    assert result.exit_code == 1


def test_cp_duplicates_via_media_reuse(seeded: None, mock_mtproto: FakeMtprotoClient) -> None:
    result = runner.invoke(app, ["cp", CHAT_ID, "/docs/a.pdf", "/archive/"])
    assert result.exit_code == 0, result.output
    assert len(mock_mtproto.sent) == 1
    # the source message's media object was re-sent — no file bytes uploaded
    assert mock_mtproto.sent[0]["file"] == "media-of-11"
    copy = read_vfs("/archive/", "a.pdf")
    assert copy is not None
    assert copy.telegram_message_id == 101  # from the fake client
    assert read_vfs("/docs/", "a.pdf") is not None  # source untouched


def test_cp_refuses_existing_destination(seeded: None, mock_mtproto: FakeMtprotoClient) -> None:
    result = runner.invoke(app, ["cp", CHAT_ID, "/docs/a.pdf", "/docs/sub/a.pdf"])
    assert result.exit_code == 0
    again = runner.invoke(app, ["cp", CHAT_ID, "/docs/a.pdf", "/docs/sub/"])
    assert again.exit_code == 1


def test_mv_updates_caption_and_index(seeded: None, telegram_api: respx.MockRouter) -> None:
    result = runner.invoke(app, ["mv", CHAT_ID, "/docs/a.pdf", "/archive/"])
    assert result.exit_code == 0, result.output
    edits = calls_to(telegram_api, "editMessageCaption")
    assert len(edits) == 1
    assert b"archive" in edits[0]  # new virtual path in the caption
    assert read_vfs("/docs/", "a.pdf") is None
    assert read_vfs("/archive/", "a.pdf") is not None


def test_mv_rejects_rename(seeded: None, telegram_api: respx.MockRouter) -> None:
    result = runner.invoke(app, ["mv", CHAT_ID, "/docs/a.pdf", "/docs/renamed.pdf"])
    assert result.exit_code == 1
    assert read_vfs("/docs/", "a.pdf") is not None  # unchanged
    assert calls_to(telegram_api, "editMessageCaption") == []


def test_rm_deletes_message_and_row(seeded: None, telegram_api: respx.MockRouter) -> None:
    result = runner.invoke(app, ["rm", CHAT_ID, "/docs/a.pdf"])
    assert result.exit_code == 0, result.output
    deletes = calls_to(telegram_api, "deleteMessage")
    assert len(deletes) == 1
    assert b"11" in deletes[0]  # message_id of the seeded entry
    assert read_vfs("/docs/", "a.pdf") is None


def test_rm_missing_file_errors(fake_env: str, telegram_api: respx.MockRouter) -> None:
    result = runner.invoke(app, ["rm", CHAT_ID, "/nope.pdf"])
    assert result.exit_code == 1
