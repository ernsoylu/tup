"""Sync tests: hash-based skip, re-upload on change, S3-style content mapping."""

from __future__ import annotations

from pathlib import Path

import respx
from typer.testing import CliRunner

from tests.conftest import CHAT_ID
from tup.cli import app

runner = CliRunner()


def send_count(telegram_api: respx.MockRouter) -> int:
    return sum(1 for call in telegram_api.calls if call.request.url.path.endswith("/sendDocument"))


def make_local_tree(tmp_path: Path) -> Path:
    src = tmp_path / "data"
    (src / "sub").mkdir(parents=True)
    (src / "one.txt").write_bytes(b"one")
    (src / "sub" / "two.txt").write_bytes(b"two")
    return src


def test_sync_uploads_then_skips_unchanged(
    fake_env: str, telegram_api: respx.MockRouter, tmp_path: Path
) -> None:
    src = make_local_tree(tmp_path)
    first = runner.invoke(app, ["sync", str(src), CHAT_ID, "/backup"])
    assert first.exit_code == 0, first.output
    assert send_count(telegram_api) == 2
    assert "2 uploaded" in first.output

    second = runner.invoke(app, ["sync", str(src), CHAT_ID, "/backup"])
    assert second.exit_code == 0
    assert send_count(telegram_api) == 2  # nothing re-sent
    assert "2 unchanged" in second.output


def test_sync_reuploads_changed_file(
    fake_env: str, telegram_api: respx.MockRouter, tmp_path: Path
) -> None:
    src = make_local_tree(tmp_path)
    runner.invoke(app, ["sync", str(src), CHAT_ID, "/backup"])
    (src / "one.txt").write_bytes(b"one v2")
    result = runner.invoke(app, ["sync", str(src), CHAT_ID, "/backup"])
    assert result.exit_code == 0
    assert send_count(telegram_api) == 3  # only the changed file re-sent
    assert "1 uploaded" in result.output
    assert "1 unchanged" in result.output


def test_sync_maps_contents_into_remote_path(
    fake_env: str, telegram_api: respx.MockRouter, tmp_path: Path
) -> None:
    src = make_local_tree(tmp_path)
    runner.invoke(app, ["sync", str(src), CHAT_ID, "/backup"])
    listing = runner.invoke(app, ["ls", CHAT_ID, "/backup", "-R"])
    assert "/backup/one.txt" in listing.output
    assert "/backup/sub/two.txt" in listing.output


def test_sync_rejects_file_argument(fake_env: str, tmp_path: Path) -> None:
    f = tmp_path / "single.txt"
    f.write_bytes(b"x")
    result = runner.invoke(app, ["sync", str(f), CHAT_ID, "/backup"])
    assert result.exit_code == 1
