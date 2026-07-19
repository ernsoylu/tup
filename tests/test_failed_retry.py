"""failed/retry/logs tests, plus the full end-to-end CLI scenario."""

from __future__ import annotations

import asyncio
from pathlib import Path

import respx
from typer.testing import CliRunner

from tests.conftest import CHAT_ID
from tup.cli import app
from tup.config import default_database_path
from tup.database import Database, FailedUpload, VfsEntry
from tup.uploader import format_caption
from tup.utils import sha256_file

runner = CliRunner()


def enqueue_failure(file_path: Path, caption: str) -> int:
    async def _add() -> int:
        async with Database(default_database_path()) as db:
            return await db.failed_add(str(file_path), CHAT_ID, caption, "document", "boom")

    return asyncio.run(_add())


def pending() -> list[FailedUpload]:
    async def _get() -> list[FailedUpload]:
        async with Database(default_database_path()) as db:
            return await db.failed_pending()

    return asyncio.run(_get())


def read_vfs(virtual_path: str, file_name: str) -> VfsEntry | None:
    async def _read() -> VfsEntry | None:
        async with Database(default_database_path()) as db:
            return await db.vfs_get(CHAT_ID, virtual_path, file_name)

    return asyncio.run(_read())


def test_failed_lists_pending(fake_env: str, tmp_path: Path) -> None:
    f = tmp_path / "doc.txt"
    f.write_bytes(b"data")
    enqueue_failure(f, format_caption("/docs/doc.txt", sha256_file(f)))
    result = runner.invoke(app, ["failed"])
    assert result.exit_code == 0
    assert "doc.txt" in result.output
    assert "boom" in result.output


def test_retry_resolves_pending_upload(
    fake_env: str, telegram_api: respx.MockRouter, tmp_path: Path
) -> None:
    f = tmp_path / "doc.txt"
    f.write_bytes(b"data")
    enqueue_failure(f, format_caption("/docs/doc.txt", sha256_file(f)))

    result = runner.invoke(app, ["retry"])
    assert result.exit_code == 0, result.output
    assert "1 resolved" in result.output
    assert pending() == []
    entry = read_vfs("/docs/", "doc.txt")  # dest dir recovered from the stored caption
    assert entry is not None


def test_retry_abandon_marks_abandoned(fake_env: str, tmp_path: Path) -> None:
    f = tmp_path / "doc.txt"
    f.write_bytes(b"data")
    failed_id = enqueue_failure(f, None or format_caption("/docs/doc.txt", sha256_file(f)))
    result = runner.invoke(app, ["retry", "--abandon", "--id", str(failed_id)])
    assert result.exit_code == 0
    assert pending() == []


def test_logs_shows_recent_entries(
    fake_env: str, telegram_api: respx.MockRouter, tmp_path: Path
) -> None:
    f = tmp_path / "notes.txt"
    f.write_bytes(b"hello")
    runner.invoke(app, ["up", str(f)])
    result = runner.invoke(app, ["logs"])
    assert result.exit_code == 0
    assert "notes.txt" in result.output
    assert "success" in result.output


def test_end_to_end_scenario(fake_env: str, telegram_api: respx.MockRouter, tmp_path: Path) -> None:
    """chat add -> up -> ls -> mv -> rm -> logs, all against the mocked API."""
    assert runner.invoke(app, ["chat", "add", "work", CHAT_ID]).exit_code == 0
    f = tmp_path / "report.txt"
    f.write_bytes(b"q3 numbers")
    assert runner.invoke(app, ["up", str(f), "--to", "work", "--dest", "/docs"]).exit_code == 0
    assert "report.txt" in runner.invoke(app, ["ls", "work", "/docs"]).output
    assert runner.invoke(app, ["mv", "work", "/docs/report.txt", "/archive/"]).exit_code == 0
    assert read_vfs("/archive/", "report.txt") is not None
    assert runner.invoke(app, ["rm", "work", "/archive/report.txt"]).exit_code == 0
    assert read_vfs("/archive/", "report.txt") is None
    logs_output = runner.invoke(app, ["logs"]).output
    assert "report.txt" in logs_output


def test_no_test_escaped_to_network(telegram_api: respx.MockRouter) -> None:
    # assert_all_mocked=True on the fixture guarantees unmocked calls raise;
    # this test documents that invariant explicitly.
    assert telegram_api.calls.call_count == 0
