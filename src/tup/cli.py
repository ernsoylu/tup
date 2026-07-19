"""Typer CLI application: command definitions and the async execution bridge."""

from __future__ import annotations

import logging
from typing import Annotated

import typer
from rich.logging import RichHandler
from typer._click import Command, Context
from typer._click.exceptions import UsageError
from typer.core import TyperGroup

from tup import __version__
from tup.config import log_file_path
from tup.progress import error_console
from tup.utils import SecretScrubberFormatter

state: dict[str, bool] = {"debug": False}


def setup_logging(level: str = "INFO") -> None:
    """Rich stderr logging plus scrubbed JSON-lines at ~/.config/tup/tup.log."""
    root = logging.getLogger("tup")
    root.setLevel(level)
    root.handlers.clear()

    rich_handler = RichHandler(console=error_console, show_path=False, rich_tracebacks=False)
    rich_handler.setLevel(level)
    root.addHandler(rich_handler)

    log_file = log_file_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(SecretScrubberFormatter())
    root.addHandler(file_handler)


class DefaultToUpGroup(TyperGroup):
    """Click group that routes unknown command tokens to the `up` command.

    `tup somefile.pdf` behaves as `tup up somefile.pdf`. Known command names
    always win: a local file literally named `tree` requires `tup up tree`.
    """

    def resolve_command(
        self, ctx: Context, args: list[str]
    ) -> tuple[str | None, Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except UsageError:
            up = self.get_command(ctx, "up")
            if up is None:  # pragma: no cover - `up` is always registered
                raise
            return "up", up, args


app = typer.Typer(
    cls=DefaultToUpGroup,
    name="tup",
    help="Telegram S3-style Virtual Filesystem and Uploader CLI.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"tup {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Show full tracebacks on errors.")] = False,
) -> None:
    state["debug"] = debug
    setup_logging("DEBUG" if debug else "INFO")


@app.command()
def up(
    path: Annotated[str, typer.Argument(help="Local file or directory to upload.")],
    to: Annotated[str | None, typer.Option("--to", help="Target drive (alias or chat_id).")] = None,
    dest: Annotated[str, typer.Option("--dest", help="Destination VFS path.")] = "/",
    as_doc: Annotated[bool, typer.Option("--as-doc", help="Force send_document.")] = False,
    as_video: Annotated[bool, typer.Option("--as-video", help="Force send_video.")] = False,
    as_audio: Annotated[bool, typer.Option("--as-audio", help="Force send_audio.")] = False,
    silent: Annotated[bool, typer.Option("--silent", help="Disable notification sound.")] = False,
) -> None:
    """Upload a local file or directory to a Telegram drive."""
    typer.echo(f"upload placeholder: {path}")
    raise typer.Exit(code=0)
