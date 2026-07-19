"""Smoke tests: CLI wiring, version, and unknown-token fallback to `up`."""

from typer.testing import CliRunner

from tup import __version__
from tup.cli import app

runner = CliRunner()


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "up" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_up_help() -> None:
    result = runner.invoke(app, ["up", "--help"])
    assert result.exit_code == 0
    assert "--to" in result.output


def test_unknown_token_falls_back_to_up() -> None:
    result = runner.invoke(app, ["somefile.pdf"])
    assert result.exit_code == 0
    assert "somefile.pdf" in result.output


def test_known_command_wins_over_fallback() -> None:
    # `up` is a known command name, so it must resolve as the command itself,
    # not be treated as a file to upload.
    result = runner.invoke(app, ["up"])
    assert result.exit_code != 0  # missing PATH argument -> usage error
