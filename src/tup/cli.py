"""Typer CLI application: command definitions and the async execution bridge."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from telegram import Bot, Update
from telegram.error import BadRequest, Conflict, Forbidden
from typer._click import Command, Context
from typer._click.exceptions import UsageError
from typer.core import TyperGroup

from tup import __version__
from tup.config import Settings, SetupRequiredError, log_file_path, migrate_legacy_config
from tup.database import Database, DatabaseError, VfsEntry
from tup.progress import console, error_console
from tup.uploader import (
    DuplicateFileError,
    TupError,
    access_error,
    bot_session,
    copy_message_media,
    edit_caption,
    fetch_existing_ids,
    format_caption,
    media_info,
    mtproto_session,
    parse_caption,
    send_with_retry,
    upload_file,
)
from tup.utils import (
    SecretScrubberFormatter,
    VfsPathError,
    extract_tags,
    is_hidden_within,
    normalize_vfs_path,
    sha256_file,
    split_vfs_path,
)
from tup.vfs_ops import (
    TRASH_PREFIX,
    full_path_of,
    is_trashed,
    op_empty_trash,
    op_list_trash,
    op_purge,
    op_restore,
    op_set_caption,
    op_trash,
    original_path_of,
    read_message_bytes,
    restore_version,
    save_content,
)

KEEP_FILE = ".keep"

state: dict[str, bool] = {"debug": False}


def setup_logging(level: str = "INFO") -> None:
    """Rich stderr logging plus scrubbed JSON-lines at ~/.tup/tup.log."""
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


def fail(message: str, hint: str | None = None) -> None:
    body = f"❌ Error: {message}"
    if hint:
        body += f"\n💡 {hint}"
    error_console.print(Panel(body, border_style="red", expand=False))


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Async execution bridge and the single user-facing error boundary.

    Domain errors render as rich panels; raw tracebacks only with --debug.
    """
    try:
        return asyncio.run(coro)
    except (TupError, SetupRequiredError) as exc:
        fail(str(exc), getattr(exc, "hint", None))
        raise typer.Exit(code=1) from exc
    except (DatabaseError, VfsPathError) as exc:
        fail(str(exc))
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        if state["debug"]:
            raise
        fail(f"Unexpected error: {exc}", hint="Re-run with --debug for the full traceback.")
        raise typer.Exit(code=1) from exc


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
            up_cmd = self.get_command(ctx, "up")
            if up_cmd is None:  # pragma: no cover - `up` is always registered
                raise
            return "up", up_cmd, args


app = typer.Typer(
    cls=DefaultToUpGroup,
    name="tup",
    help="Telegram S3-style Virtual Filesystem and Uploader CLI.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

chat_app = typer.Typer(help="Manage drive aliases for Telegram chats.", no_args_is_help=True)
app.add_typer(chat_app, name="chat")


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
    moved = migrate_legacy_config()
    setup_logging("DEBUG" if debug else "INFO")
    if moved:
        error_console.print(f"📦 Moved {', '.join(moved)} into ~/.tup (tup's home).")


# --- setup --------------------------------------------------------------------


@app.command("setup")
def setup_cmd() -> None:
    """Interactive first-run configuration wizard."""
    from tup.setup import run_wizard

    try:
        run_wizard()
    except TupError as exc:
        fail(str(exc), exc.hint)
        raise typer.Exit(code=1) from exc


@app.command("gui")
def gui_cmd() -> None:
    """Launch the graphical file explorer (requires the PyQt6 'gui' extra)."""
    try:
        from tup.gui.app import run_gui
    except ImportError as exc:
        fail(
            "The GUI requires PyQt6, which is not installed.",
            hint="Install it with [bold]uv sync --all-extras[/bold] "
            "or [bold]pip install 'tup[gui]'[/bold].",
        )
        raise typer.Exit(code=1) from exc
    run_gui()


# --- chat alias management ----------------------------------------------------


async def _resolve_drive_or_fail(db: Database, drive: str | None, settings: Settings) -> str:
    target = drive or settings.default_chat_id
    if not target:
        raise TupError(
            "No target drive given and no default configured.",
            hint="Pass [bold]--to <drive>[/bold] or set DEFAULT_CHAT_ID via tup setup.",
        )
    return await db.resolve_drive(target)


# Numeric chat IDs like "-100123" look like options to click; ignore_unknown_options
# lets them flow through to positional arguments.
NEGATIVE_ID_OK = {"ignore_unknown_options": True}


@chat_app.command("add", context_settings=NEGATIVE_ID_OK)
def chat_add(
    alias: Annotated[str, typer.Argument(help="Short name for the drive.")],
    chat_id: Annotated[str, typer.Argument(help="Numeric Telegram chat ID.")],
) -> None:
    """Validate a chat, fetch its title, and save the alias."""

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            async with bot_session(settings) as bot:
                try:
                    chat = await bot.get_chat(chat_id)
                except (Forbidden, BadRequest) as exc:
                    raise access_error(chat_id) from exc
            title = chat.title or chat.full_name or None
            await db.alias_add(alias, chat_id, title)
            console.print(f"✅ Drive [bold]{alias}[/bold] → {chat_id} ({title or 'untitled'})")

    run_async(_run())


@chat_app.command("list")
def chat_list() -> None:
    """List registered drive aliases."""

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            aliases = await db.alias_list()
        table = Table("Alias", "Chat ID", "Title", "Added")
        for entry in aliases:
            table.add_row(entry.alias, entry.chat_id, entry.title or "-", entry.created_at)
        console.print(table)

    run_async(_run())


@chat_app.command("remove")
def chat_remove(alias: Annotated[str, typer.Argument(help="Alias to delete.")]) -> None:
    """Remove a drive alias (does not touch the chat itself)."""

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            removed = await db.alias_remove(alias)
        if removed:
            console.print(f"✅ Removed alias [bold]{alias}[/bold]")
        else:
            raise TupError(f"No such alias: {alias!r}")

    run_async(_run())


# --- upload -------------------------------------------------------------------


def _resolve_local(path: str) -> Path:
    local = Path(path).expanduser()
    if not local.exists():
        raise TupError(f"No such file or directory: {path}")
    return local


def _collect_targets(local: Path, dest: str) -> list[tuple[Path, str]]:
    """Map a file or directory to (local_file, vfs_dest_dir) upload pairs."""
    if local.is_file():
        return [(local, dest)]
    # A local folder mounts under its own name: /code -> /code/ (spec §5)
    mount = normalize_vfs_path(dest, directory=True) + local.name
    targets = []
    for file_path in sorted(local.rglob("*")):
        if not file_path.is_file() or is_hidden_within(file_path, local):
            continue  # dotfiles/.git/.DS_Store never become drive content
        rel = file_path.parent.relative_to(local).as_posix()
        targets.append((file_path, mount if rel == "." else f"{mount}/{rel}"))
    if not targets:
        raise TupError(f"Directory is empty: {local}")
    return targets


@app.command()
def up(
    path: Annotated[str, typer.Argument(help="Local file or directory to upload.")],
    to: Annotated[str | None, typer.Option("--to", help="Target drive (alias or chat_id).")] = None,
    dest: Annotated[str, typer.Option("--dest", help="Destination VFS path.")] = "/",
    caption: Annotated[
        str | None, typer.Option("--caption", help="Optional user caption text.")
    ] = None,
    as_doc: Annotated[bool, typer.Option("--as-doc", help="Force send_document.")] = False,
    as_video: Annotated[bool, typer.Option("--as-video", help="Force send_video.")] = False,
    as_audio: Annotated[bool, typer.Option("--as-audio", help="Force send_audio.")] = False,
    silent: Annotated[bool, typer.Option("--silent", help="Disable notification sound.")] = False,
) -> None:
    """Upload a local file or directory to a Telegram drive."""

    async def _run() -> None:
        settings = Settings.load()
        local = _resolve_local(path)
        targets = _collect_targets(local, dest)
        async with Database(settings.database_path) as db:
            chat_id = await _resolve_drive_or_fail(db, to, settings)
            async with mtproto_session(settings) as client:
                failures = 0
                for file_path, dest_dir in targets:
                    try:
                        message_id = await upload_file(
                            db,
                            settings,
                            client,
                            file_path,
                            chat_id,
                            dest_dir,
                            as_doc=as_doc,
                            as_video=as_video,
                            as_audio=as_audio,
                            silent=silent,
                            user_caption=caption,
                        )
                        console.print(
                            f"✅ {file_path.name} → drive {chat_id} (message {message_id})"
                        )
                    except TupError as exc:
                        if len(targets) == 1:
                            raise
                        failures += 1
                        fail(f"{file_path}: {exc}", exc.hint)
                if failures:
                    raise TupError(
                        f"{failures}/{len(targets)} uploads failed.",
                        hint="See [bold]tup failed[/bold] and re-run with [bold]tup retry[/bold].",
                    )

    run_async(_run())


# --- POSIX VFS operations -----------------------------------------------------


def _visible(entries: list[VfsEntry]) -> list[VfsEntry]:
    return [e for e in entries if e.file_name != KEEP_FILE]


def _without_trash(entries: list[VfsEntry], base: str) -> list[VfsEntry]:
    """Hide Recycle Bin contents from normal listings (explicit /.Trash/ paths
    and `tup trash list` still show them)."""
    if base.startswith(TRASH_PREFIX):
        return entries
    return [e for e in entries if not e.virtual_path.startswith(TRASH_PREFIX)]


def _child_dirs(entries: list[VfsEntry], base: str) -> list[str]:
    """Immediate subdirectory names of `base` derived from deeper entries."""
    children = set()
    for entry in entries:
        if entry.virtual_path == base or not entry.virtual_path.startswith(base):
            continue
        children.add(entry.virtual_path[len(base) :].split("/", 1)[0])
    return sorted(children)


def _human_size(size: int) -> str:
    from rich.filesize import decimal

    return decimal(size)


def _dest_directory(dest: str, src_name: str) -> str:
    """Resolve a cp/mv destination to a directory, rejecting renames.

    Telegram cannot rename an uploaded file: captions carry the virtual path,
    but the download name is fixed at upload time. A dest whose basename
    differs from the source name is therefore treated as a directory path.
    """
    if dest.endswith("/"):
        return normalize_vfs_path(dest, directory=True)
    as_file = normalize_vfs_path(dest)
    if as_file == "/":
        return "/"
    parent, base = as_file.rsplit("/", 1)
    if base == src_name:
        if not parent:
            return "/"
        return parent if parent.endswith("/") else parent + "/"
    return normalize_vfs_path(dest, directory=True)


async def _require_entry(db: Database, chat_id: str, path: str) -> VfsEntry:
    virtual_dir, name = split_vfs_path(path)
    entry = await db.vfs_get(chat_id, virtual_dir, name)
    if entry is None:
        raise TupError(
            f"No such file in drive {chat_id}: {virtual_dir}{name}",
            hint="Use [bold]tup ls[/bold] to inspect the drive.",
        )
    return entry


@app.command(context_settings=NEGATIVE_ID_OK)
def tree(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    path: Annotated[str, typer.Argument(help="VFS directory to start from.")] = "/",
    level: Annotated[
        int | None, typer.Option("-L", "--level", help="Max depth (0 = current leaves only).")
    ] = None,
) -> None:
    """Render the drive's virtual filesystem as a tree (local index, no network)."""

    async def _run() -> None:
        from rich.tree import Tree as RichTree

        settings = Settings.load()
        base = normalize_vfs_path(path, directory=True)
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            entries = _without_trash(await db.vfs_list_prefix(chat_id, base), base)

        root = RichTree(f"[bold blue]{base}[/bold blue]")

        def populate(node: Any, current: str, remaining: int | None) -> None:
            for entry in _visible([e for e in entries if e.virtual_path == current]):
                node.add(f"{entry.file_name} [dim]({_human_size(entry.file_size)})[/dim]")
            for child in _child_dirs(entries, current):
                child_node = node.add(f"[bold blue]{child}/[/bold blue]")
                if remaining is None or remaining > 0:
                    populate(
                        child_node,
                        f"{current}{child}/",
                        None if remaining is None else remaining - 1,
                    )

        populate(root, base, level)
        console.print(root)

    run_async(_run())


@app.command(context_settings=NEGATIVE_ID_OK)
def ls(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    path: Annotated[str, typer.Argument(help="VFS directory to list.")] = "/",
    recursive: Annotated[bool, typer.Option("-R", "--recursive", help="List recursively.")] = False,
    tag: Annotated[
        str | None,
        typer.Option("--tag", help="List only files carrying this tag (drive-wide)."),
    ] = None,
) -> None:
    """List a VFS directory, POSIX `ls -lh` style (local index, no network)."""

    async def _run() -> None:
        settings = Settings.load()
        base = normalize_vfs_path(path, directory=True)
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            if tag is not None:
                tagged = _without_trash(await db.vfs_list_by_tag(chat_id, tag), base)
                table = Table("Path", "Size", "Tags", "Caption")
                for entry in _visible(tagged):
                    table.add_row(
                        f"{entry.virtual_path}{entry.file_name}",
                        _human_size(entry.file_size),
                        entry.tags,
                        entry.user_caption,
                    )
                console.print(table)
                return
            entries = _without_trash(await db.vfs_list_prefix(chat_id, base), base)

        table = Table("Name", "Size", "Uploaded", "Msg ID", "Tags")
        if recursive:
            for entry in _visible(entries):
                table.add_row(
                    f"{entry.virtual_path}{entry.file_name}",
                    _human_size(entry.file_size),
                    entry.upload_timestamp,
                    str(entry.telegram_message_id),
                    entry.tags,
                )
        else:
            for child in _child_dirs(entries, base):
                table.add_row(f"[bold blue]{child}/[/bold blue]", "-", "-", "-", "-")
            for entry in _visible([e for e in entries if e.virtual_path == base]):
                table.add_row(
                    entry.file_name,
                    _human_size(entry.file_size),
                    entry.upload_timestamp,
                    str(entry.telegram_message_id),
                    entry.tags,
                )
        console.print(table)

    run_async(_run())


@app.command(context_settings=NEGATIVE_ID_OK)
def mkdir(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    path: Annotated[str, typer.Argument(help="VFS directory to create.")],
) -> None:
    """Create an empty VFS directory (hidden .keep index entry, no network)."""

    async def _run() -> None:
        settings = Settings.load()
        target = normalize_vfs_path(path, directory=True)
        if target == "/":
            raise TupError("Root '/' always exists.")
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            if await db.vfs_get(chat_id, target, KEEP_FILE) is not None:
                raise TupError(f"Directory already exists: {target}")
            await db.vfs_upsert(chat_id, target, KEEP_FILE, 0, "", "", 0)
        console.print(f"✅ Created {target}")

    run_async(_run())


@app.command(context_settings=NEGATIVE_ID_OK)
def rmdir(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    path: Annotated[str, typer.Argument(help="VFS directory to remove (must be empty).")],
) -> None:
    """Remove an empty VFS directory."""

    async def _run() -> None:
        settings = Settings.load()
        target = normalize_vfs_path(path, directory=True)
        if target == "/":
            raise TupError("Cannot remove the root directory.")
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            entries = await db.vfs_list_prefix(chat_id, target)
            keep = await db.vfs_get(chat_id, target, KEEP_FILE)
            contents = [
                e for e in entries if not (e.virtual_path == target and e.file_name == KEEP_FILE)
            ]
            if contents:
                raise TupError(f"Directory not empty: {target}")
            if keep is None:
                raise TupError(f"No such directory: {target}")
            await db.vfs_delete(keep.id)
        console.print(f"✅ Removed {target}")

    run_async(_run())


@app.command(context_settings=NEGATIVE_ID_OK)
def cp(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    src: Annotated[str, typer.Argument(help="Source VFS file path.")],
    dest: Annotated[str, typer.Argument(help="Destination VFS directory.")],
) -> None:
    """Duplicate a file server-side via its file_id — no re-upload."""

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            entry = await _require_entry(db, chat_id, src)
            dest_dir = _dest_directory(dest, entry.file_name)
            if await db.vfs_get(chat_id, dest_dir, entry.file_name) is not None:
                raise TupError(f"Destination already exists: {dest_dir}{entry.file_name}")
            same_hash = [
                e
                for e in await db.vfs_find_by_hash(chat_id, entry.file_hash)
                if e.virtual_path == dest_dir
            ]
            if same_hash:
                raise DuplicateFileError(
                    f"An identical file already exists in {dest_dir} "
                    f"as {same_hash[0].file_name} (same SHA-256)."
                )
            full_path = dest_dir + entry.file_name if dest_dir != "/" else "/" + entry.file_name
            caption = format_caption(full_path, entry.file_hash, entry.user_caption or None)
            async with mtproto_session(settings) as client:
                message_id = await copy_message_media(
                    client,
                    chat_id,
                    entry.telegram_message_id,
                    caption,
                    max_retries=settings.max_retries,
                )
            await db.vfs_upsert(
                chat_id,
                dest_dir,
                entry.file_name,
                entry.file_size,
                entry.file_hash,
                "",
                message_id,
                mime_type=entry.mime_type,
                media_kind=entry.media_kind,
                user_caption=entry.user_caption,
                tags=entry.tags,
            )
        console.print(f"✅ {src} → {full_path} (message {message_id}, no re-upload)")

    run_async(_run())


@app.command(context_settings=NEGATIVE_ID_OK)
def mv(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    src: Annotated[str, typer.Argument(help="Source VFS file path.")],
    dest: Annotated[str, typer.Argument(help="Destination VFS directory.")],
) -> None:
    """Move a file to another VFS directory (path changes only — Telegram
    cannot rename the underlying file_name)."""

    async def _run() -> None:
        settings = Settings.load()
        dest_as_file = normalize_vfs_path(dest) if not dest.endswith("/") else None
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            entry = await _require_entry(db, chat_id, src)
            if dest_as_file is not None and dest_as_file != "/":
                base = dest_as_file.rsplit("/", 1)[1]
                if "." in base and base != entry.file_name:
                    raise TupError(
                        f"Cannot rename {entry.file_name!r} to {base!r}: Telegram fixes the "
                        "file name at upload time.",
                        hint="mv changes the virtual path only; the file name must stay the same.",
                    )
            dest_dir = _dest_directory(dest, entry.file_name)
            if dest_dir == entry.virtual_path:
                console.print("Nothing to do: source and destination are the same.")
                return
            if await db.vfs_get(chat_id, dest_dir, entry.file_name) is not None:
                raise TupError(f"Destination already exists: {dest_dir}{entry.file_name}")
            full_path = dest_dir + entry.file_name if dest_dir != "/" else "/" + entry.file_name
            caption = format_caption(full_path, entry.file_hash, entry.user_caption or None)
            async with bot_session(settings) as bot:
                await edit_caption(
                    bot,
                    chat_id,
                    entry.telegram_message_id,
                    caption,
                    max_retries=settings.max_retries,
                )
            await db.vfs_move(entry.id, dest_dir, entry.file_name)
        console.print(f"✅ {src} → {full_path}")

    run_async(_run())


@app.command(context_settings=NEGATIVE_ID_OK)
def rm(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    path: Annotated[str, typer.Argument(help="VFS file path to delete.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Permanently delete (message + versions), bypassing the bin."),
    ] = False,
) -> None:
    """Move a file to the Recycle Bin (/.Trash/); --force deletes permanently.

    Removing a file that is already in the bin always purges it. The trash
    move is a caption rewrite, so the state lives in Telegram itself and other
    tup frontends (cloud, GUI) see the same bin.
    """

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            entry = await _require_entry(db, chat_id, path)
            if force or is_trashed(entry):
                await op_purge(db, settings, chat_id, entry)
                console.print(f"✅ Permanently deleted {full_path_of(entry)}")
                return
            new_path = await op_trash(db, settings, chat_id, entry)
        console.print(
            f"🗑  Moved to Recycle Bin: {new_path}\n"
            f"   Restore with [bold]tup trash restore {drive} {new_path}[/bold]"
        )

    run_async(_run())


trash_app = typer.Typer(help="Recycle Bin: list, restore, and empty.", no_args_is_help=True)
app.add_typer(trash_app, name="trash")


@trash_app.command("list", context_settings=NEGATIVE_ID_OK)
def trash_list(drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")]) -> None:
    """List Recycle Bin contents with their original locations."""

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            entries = await op_list_trash(db, chat_id)
        table = Table("In bin", "Original path", "Size", "Deleted around")
        for entry in entries:
            table.add_row(
                full_path_of(entry),
                original_path_of(entry),
                _human_size(entry.file_size),
                entry.upload_timestamp,
            )
        console.print(table)
        if not entries:
            console.print("Recycle Bin is empty.")

    run_async(_run())


@trash_app.command("restore", context_settings=NEGATIVE_ID_OK)
def trash_restore(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    path: Annotated[str, typer.Argument(help="Path in the bin (or the original path).")],
) -> None:
    """Move a file out of the Recycle Bin back to its original folder."""

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            target = normalize_vfs_path(path)
            virtual_dir, name = split_vfs_path(target)
            entry = await db.vfs_get(chat_id, virtual_dir, name)
            if entry is None and not target.startswith(TRASH_PREFIX):
                # Convenience: accept the original (pre-trash) path too.
                candidates = [
                    e for e in await op_list_trash(db, chat_id) if original_path_of(e) == target
                ]
                entry = candidates[0] if candidates else None
            if entry is None:
                raise TupError(
                    f"Nothing matching {target} in the Recycle Bin.",
                    hint="See [bold]tup trash list[/bold].",
                )
            restored = await op_restore(db, settings, chat_id, entry)
        console.print(f"✅ Restored to {restored}")

    run_async(_run())


@trash_app.command("empty", context_settings=NEGATIVE_ID_OK)
def trash_empty(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Permanently delete everything in the Recycle Bin (messages + versions)."""

    async def _count() -> tuple[str, int]:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            return chat_id, len(await op_list_trash(db, chat_id))

    chat_id, count = run_async(_count())
    if count == 0:
        console.print("Recycle Bin is already empty.")
        return
    if not yes and not typer.confirm(f"Permanently delete {count} file(s) from the bin?"):
        raise typer.Abort()

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            purged = await op_empty_trash(db, settings, chat_id)
        console.print(f"✅ Recycle Bin emptied: {purged} file(s) permanently deleted.")

    run_async(_run())


# --- captions, tags, editing & versions ---------------------------------------


@app.command(context_settings=NEGATIVE_ID_OK)
def caption(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    path: Annotated[str, typer.Argument(help="VFS file path.")],
    text: Annotated[
        str, typer.Argument(help="Caption text; hashtags become tags. '' clears it.")
    ] = "",
) -> None:
    """Set a file's user caption (hashtags in it become searchable tags)."""

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            entry = await _require_entry(db, chat_id, path)
            tags = await op_set_caption(db, settings, chat_id, entry, text.strip())
        suffix = f" — tags: {tags}" if tags else ""
        console.print(f"✅ Caption {'cleared' if not text.strip() else 'updated'}{suffix}")

    run_async(_run())


@app.command(context_settings=NEGATIVE_ID_OK)
def tag(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    path: Annotated[str, typer.Argument(help="VFS file path.")],
    tags: Annotated[list[str], typer.Argument(help="Tags to add (with or without '#').")],
) -> None:
    """Add tags to a file (appended to its caption as hashtags)."""

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            entry = await _require_entry(db, chat_id, path)
            current = set(entry.tags.split())
            additions = [t.lstrip("#").lower() for t in tags]
            fresh = [t for t in additions if t and t not in current]
            if not fresh:
                console.print("Nothing to do: tag(s) already present.")
                return
            hashtags = " ".join(f"#{t}" for t in fresh)
            text = f"{entry.user_caption}\n{hashtags}".strip() if entry.user_caption else hashtags
            all_tags = await op_set_caption(db, settings, chat_id, entry, text)
        console.print(f"✅ Tags now: {all_tags}")

    run_async(_run())


@app.command(context_settings=NEGATIVE_ID_OK)
def edit(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    path: Annotated[str, typer.Argument(help="VFS file path (text files).")],
) -> None:
    """Edit a file in $EDITOR; saving uploads a new revision (old one is kept
    in the version history — see [bold]tup versions[/bold])."""

    async def _run() -> None:
        import os
        import shlex
        import subprocess
        import tempfile

        settings = Settings.load()
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            entry = await _require_entry(db, chat_id, path)
            async with mtproto_session(settings) as client:
                if entry.telegram_message_id > 0:
                    data = await read_message_bytes(
                        client, settings, chat_id, entry.telegram_message_id
                    )
                else:
                    data = b""
                with tempfile.TemporaryDirectory(prefix="tup-edit-") as tmp:
                    local = Path(tmp) / entry.file_name.replace("/", "_")
                    local.write_bytes(data)
                    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
                    result = await asyncio.to_thread(
                        subprocess.run,  # noqa: S603 - user's own $EDITOR choice
                        [*shlex.split(editor), str(local)],
                        check=False,
                    )
                    if result.returncode != 0:
                        raise TupError(f"Editor exited with status {result.returncode}; not saved.")
                    new_data = local.read_bytes()
                if new_data == data:
                    console.print("No changes — nothing uploaded.")
                    return
                saved, _ = await save_content(
                    db, settings, client, chat_id, entry.virtual_path, entry.file_name, new_data
                )
        console.print(
            f"✅ Saved {full_path_of(saved)} (message {saved.telegram_message_id}); "
            "the previous revision is in the version history."
        )

    run_async(_run())


@app.command(context_settings=NEGATIVE_ID_OK)
def versions(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    path: Annotated[str, typer.Argument(help="VFS file path.")],
    restore: Annotated[
        int | None,
        typer.Option("--restore", help="Version id to make current (re-saves its content)."),
    ] = None,
) -> None:
    """List a file's version history (kept when edits replace its content)."""

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            entry = await _require_entry(db, chat_id, path)
            if restore is not None:
                version = await db.version_get(restore)
                if version is None or version.entry_id != entry.id:
                    raise TupError(
                        f"No version {restore} for {path}.",
                        hint=f"See [bold]tup versions {drive} {path}[/bold].",
                    )
                async with mtproto_session(settings) as client:
                    saved = await restore_version(db, settings, client, chat_id, entry, version)
                console.print(
                    f"✅ Version {restore} is current again "
                    f"(message {saved.telegram_message_id}); the replaced revision was versioned."
                )
                return
            history = await db.version_list(entry.id)
        table = Table("Id", "Size", "SHA256", "Saved at", "Msg ID")
        for version in history:
            table.add_row(
                str(version.id),
                _human_size(version.file_size),
                version.file_hash[:12] + "…",
                version.created_at,
                str(version.telegram_message_id),
            )
        console.print(table)
        if not history:
            console.print("No versions yet — versions appear when a file's content is replaced.")

    run_async(_run())


@app.command(context_settings=NEGATIVE_ID_OK)
def backup(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    restore: Annotated[
        str | None,
        typer.Option("--restore", help="VFS path of a backup in /Backups/ to restore from."),
    ] = None,
    keep: Annotated[
        int, typer.Option("--keep", help="Backups to retain in /Backups/ after a new dump.")
    ] = 10,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the restore confirmation.")] = False,
) -> None:
    """Back up the registry as a gzipped-JSON VFS file under /Backups/.

    With --restore, replaces the local index from a dump (Telegram messages
    are untouched). Dumps share tup-cloud's structure, so the shared tables
    of a cloud backup restore here too.
    """
    from tup.backup import make_backup, restore_database

    if restore is not None and not yes:
        if not typer.confirm("Replace the ENTIRE local index with this backup?"):
            raise typer.Abort()

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            if restore is not None:
                entry = await _require_entry(db, chat_id, restore)
                async with mtproto_session(settings) as client:
                    data = await read_message_bytes(
                        client, settings, chat_id, entry.telegram_message_id
                    )
                counts = await restore_database(db, data)
                summary = ", ".join(f"{name}: {count}" for name, count in counts.items())
                console.print(f"✅ Index restored — rows loaded: {summary}")
                return
            async with mtproto_session(settings) as client:
                backup_path, pruned = await make_backup(
                    db, settings, client, chat_id, keep=keep
                )
        pruned_note = f" ({pruned} old backup(s) pruned)" if pruned else ""
        console.print(f"✅ Backup uploaded: {backup_path}{pruned_note}")

    run_async(_run())


# --- sync & reconciliation ----------------------------------------------------


def _collect_sync_targets(local_dir: Path, remote_base: str) -> list[tuple[Path, str]]:
    """S3-style: the *contents* of local_dir map into remote_base."""
    targets = []
    for file_path in sorted(local_dir.rglob("*")):
        if not file_path.is_file() or is_hidden_within(file_path, local_dir):
            continue  # dotfiles/.git/.DS_Store never become drive content
        rel = file_path.parent.relative_to(local_dir).as_posix()
        dest = (
            remote_base
            if rel == "."
            else normalize_vfs_path(f"{remote_base}/{rel}", directory=True)
        )
        targets.append((file_path, dest))
    return targets


def _hash_local(file_path: Path) -> str:
    return sha256_file(file_path)


@app.command(context_settings=NEGATIVE_ID_OK)
def sync(
    local_dir: Annotated[str, typer.Argument(help="Local directory to sync from.")],
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    remote_path: Annotated[str, typer.Argument(help="Destination VFS directory.")] = "/",
) -> None:
    """S3-style sync: upload directory contents, skipping unchanged files (SHA-256 match)."""

    async def _run() -> None:
        settings = Settings.load()
        local = _resolve_local(local_dir)
        if not local.is_dir():
            raise TupError(f"Not a directory: {local_dir}")
        remote_base = normalize_vfs_path(remote_path, directory=True)
        targets = _collect_sync_targets(local, remote_base)
        if not targets:
            raise TupError(f"Directory is empty: {local_dir}")

        uploaded = skipped = failed = 0
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            async with mtproto_session(settings) as client:
                for file_path, dest_dir in targets:
                    local_hash = _hash_local(file_path)
                    existing = await db.vfs_get(chat_id, dest_dir, file_path.name)
                    if existing is not None and existing.file_hash == local_hash:
                        skipped += 1
                        continue
                    try:
                        await upload_file(db, settings, client, file_path, chat_id, dest_dir)
                        uploaded += 1
                        console.print(f"⬆️  {file_path.name} → {dest_dir}")
                    except DuplicateFileError:
                        skipped += 1  # identical content already in that folder
                    except TupError as exc:
                        failed += 1
                        fail(f"{file_path}: {exc}", exc.hint)
        console.print(
            f"✅ Sync complete: {uploaded} uploaded, {skipped} unchanged, {failed} failed."
        )
        if failed:
            raise TupError(
                f"{failed} file(s) failed to sync.",
                hint="See [bold]tup failed[/bold]; re-run with [bold]tup retry[/bold].",
            )

    run_async(_run())


async def _fetch_pending_updates(
    bot: Bot, offset: int | None, max_retries: int
) -> tuple[Update, ...]:
    async def _fetch() -> tuple[Update, ...]:
        return await bot.get_updates(
            offset=offset,
            timeout=0,
            allowed_updates=[
                "message",
                "edited_message",
                "channel_post",
                "edited_channel_post",
            ],
        )

    return await send_with_retry(_fetch, max_retries=max_retries, what="drain updates")


async def _reconcile_update(
    db: Database, chat_id: str, update: Update, *, reconstruct: bool
) -> tuple[int, int, int]:
    """Apply one pending update to the index; returns (seen, edits, added) deltas."""
    message = update.effective_message
    if message is None or str(message.chat.id) != chat_id:
        return 0, 0, 0
    meta = parse_caption(message.caption)
    if meta is None:
        return 1, 0, 0
    virtual_dir, file_name = split_vfs_path(meta.full_path)
    user_caption = meta.user_caption or ""
    tags = extract_tags(user_caption)
    is_edit = (
        update.edited_message is not None or update.edited_channel_post is not None
    ) and message.edit_date is not None
    existing = await db.vfs_get_by_message(chat_id, message.message_id)
    if is_edit and existing is not None:
        changed = 0
        if existing.virtual_path != virtual_dir or existing.file_name != file_name:
            await db.vfs_move(existing.id, virtual_dir, file_name)
            changed = 1
        if existing.user_caption != user_caption:
            await db.vfs_set_caption(existing.id, user_caption, tags)
            changed = 1
        return 1, changed, 0
    if existing is None and reconstruct:
        info = media_info(message)
        if info is None:
            return 1, 0, 0
        file_id, file_size = info
        # Same-path duplicate = a version chain (cloud convention): the newest
        # message is the current revision, older ones are history.
        at_path = await db.vfs_get(chat_id, virtual_dir, file_name)
        if at_path is not None and at_path.telegram_message_id != message.message_id:
            known = {v.telegram_message_id for v in await db.version_list(at_path.id)}
            if message.message_id > at_path.telegram_message_id:
                if at_path.telegram_message_id > 0:
                    await db.version_add(
                        at_path.id,
                        chat_id,
                        at_path.telegram_message_id,
                        at_path.file_hash,
                        at_path.file_size,
                    )
                await db.vfs_update_message(
                    at_path.id, file_size, meta.sha256, message.message_id
                )
                await db.vfs_set_caption(at_path.id, user_caption, tags)
            elif message.message_id not in known:
                await db.version_add(
                    at_path.id, chat_id, message.message_id, meta.sha256, file_size
                )
            return 1, 0, 1
        await db.vfs_upsert(
            chat_id,
            virtual_dir,
            file_name,
            file_size,
            meta.sha256,
            file_id,
            message.message_id,
            user_caption=user_caption,
            tags=tags,
        )
        return 1, 0, 1
    return 1, 0, 0


async def _drain_updates(
    db: Database, bot: Bot, chat_id: str, *, reconstruct: bool, max_retries: int
) -> tuple[int, int, int]:
    """Drain pending getUpdates for one chat; returns (seen, edits, added)."""
    seen = edits = added = 0
    last_update_id = await db.sync_state_get(chat_id)
    while True:
        offset = last_update_id + 1 if last_update_id else None
        updates = await _fetch_pending_updates(bot, offset, max_retries)
        if not updates:
            break
        for update in updates:
            last_update_id = max(last_update_id, update.update_id)
            new_seen, new_edits, new_added = await _reconcile_update(
                db, chat_id, update, reconstruct=reconstruct
            )
            seen += new_seen
            edits += new_edits
            added += new_added
    await db.sync_state_set(chat_id, last_update_id)
    return seen, edits, added


async def _prune_deleted(db: Database, settings: Settings, chat_id: str) -> int:
    """Drop rows whose Telegram messages were deleted natively (MTProto sweep)."""
    entries = [
        e
        for e in await db.vfs_list_prefix(chat_id, "/")
        if e.telegram_message_id > 0  # .keep rows have no remote message
    ]
    if not entries:
        return 0
    async with mtproto_session(settings) as client:
        alive = await fetch_existing_ids(
            client,
            chat_id,
            [e.telegram_message_id for e in entries],
            max_retries=settings.max_retries,
        )
    pruned = 0
    for entry in entries:
        if entry.telegram_message_id not in alive:
            await db.vfs_delete(entry.id)
            pruned += 1
            console.print(
                f"🗑  pruned {entry.virtual_path}{entry.file_name} "
                f"(message {entry.telegram_message_id} deleted on Telegram)"
            )
    return pruned


@app.command(context_settings=NEGATIVE_ID_OK)
def index(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    reconstruct: Annotated[
        bool,
        typer.Option(
            "--reconstruct", help="Also index tup-captioned messages missing from the DB."
        ),
    ] = False,
    prune: Annotated[
        bool,
        typer.Option(
            "--prune",
            help="Remove index rows whose messages were deleted natively in Telegram "
            "(verified over MTProto — the Bot API emits no deletion events).",
        ),
    ] = False,
) -> None:
    """Reconcile the local index with pending Telegram updates.

    Note: the Bot API has no message-history endpoint, so tup can only see
    pending updates (getUpdates) and its own uploads. Caption edits made
    natively in Telegram are applied to the index; with --reconstruct,
    tup-captioned messages unknown to the DB are (re-)indexed; with --prune,
    rows for messages deleted directly in Telegram are removed.
    """

    async def _run() -> None:
        settings = Settings.load()
        seen = edits = added = pruned = 0
        drain_error: str | None = None

        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            async with bot_session(settings) as bot:
                try:
                    seen, edits, added = await _drain_updates(
                        db,
                        bot,
                        chat_id,
                        reconstruct=reconstruct,
                        max_retries=settings.max_retries,
                    )
                except TupError as exc:
                    drain_error = str(exc)
                except Conflict:
                    drain_error = (
                        "another process is polling getUpdates with this bot token, "
                        "so the update drain is unavailable"
                    )
            if prune:
                pruned = await _prune_deleted(db, settings, chat_id)
        if drain_error:
            error_console.print(
                f"⚠️  Update drain skipped: {drain_error}. "
                "Caption edits made in Telegram were not synced this run."
            )
        console.print(
            f"✅ Index reconciled: {seen} message(s) seen, {edits} caption edit(s) applied, "
            f"{added} row(s) reconstructed, {pruned} stale row(s) pruned."
        )

    run_async(_run())


# --- admin & retries ----------------------------------------------------------


@app.command()
def failed() -> None:
    """List pending failed uploads."""

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            pending = await db.failed_pending()
        table = Table("ID", "File", "Drive", "Retries", "Error", "When")
        for item in pending:
            table.add_row(
                str(item.id),
                item.file_path,
                item.chat_id,
                str(item.retry_count),
                item.error_message,
                item.timestamp,
            )
        console.print(table)
        if not pending:
            console.print("Nothing pending 🎉")

    run_async(_run())


@app.command()
def retry(
    failed_id: Annotated[
        int | None, typer.Option("--id", help="Retry only this failed_registry row.")
    ] = None,
    abandon: Annotated[
        bool, typer.Option("--abandon", help="Mark the selected item(s) abandoned instead.")
    ] = False,
) -> None:
    """Re-attempt pending failed uploads (or abandon them with --abandon)."""

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            pending = await db.failed_pending(failed_id)
            if not pending:
                console.print("Nothing to retry 🎉")
                return
            if abandon:
                for item in pending:
                    await db.failed_mark(item.id, "abandoned")
                console.print(f"✅ Abandoned {len(pending)} item(s).")
                return
            resolved = still_failing = 0
            async with mtproto_session(settings) as client:
                for item in pending:
                    file_path = Path(item.file_path)
                    meta = parse_caption(item.caption)
                    dest_dir = split_vfs_path(meta.full_path)[0] if meta is not None else "/"
                    try:
                        await upload_file(
                            db,
                            settings,
                            client,
                            file_path,
                            item.chat_id,
                            dest_dir,
                            user_caption=meta.user_caption if meta else None,
                        )
                        await db.failed_mark(item.id, "resolved", bump_retry=True)
                        resolved += 1
                        console.print(f"✅ Retried {file_path.name} → {dest_dir}")
                    except TupError as exc:
                        await db.failed_mark(item.id, "pending", bump_retry=True)
                        still_failing += 1
                        fail(f"{file_path}: {exc}", exc.hint)
        console.print(f"Retry finished: {resolved} resolved, {still_failing} still pending.")

    run_async(_run())


@app.command()
def logs(
    limit: Annotated[int, typer.Option("--limit", help="Number of entries to show.")] = 20,
    chat: Annotated[str | None, typer.Option("--chat", help="Filter by drive/chat_id.")] = None,
) -> None:
    """Show the most recent upload audit log entries."""

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(chat) if chat else None
            entries = await db.log_recent(limit=limit, chat_id=chat_id)
        table = Table("When", "File", "Drive", "Type", "Status", "Msg ID", "Error")
        for item in entries:
            table.add_row(
                item.timestamp,
                item.file_path,
                item.chat_id,
                item.upload_type,
                "[green]success[/green]" if item.status == "success" else "[red]failed[/red]",
                str(item.telegram_message_id or "-"),
                item.error_message or "-",
            )
        console.print(table)

    run_async(_run())


@chat_app.command("discover")
def chat_discover() -> None:
    """Show chats the bot can currently see, with their IDs.

    The Bot API cannot enumerate a bot's chats; discovery works by peeking at
    pending updates (non-destructively). Add the bot to a group/channel and
    send any message there, then run this command and `tup chat add`.
    """

    async def _run() -> None:
        settings = Settings.load()
        chats: dict[int, tuple[str, str]] = {}
        webhook_url = ""
        async with bot_session(settings) as bot:
            # No offset: peeking does not consume updates, so `tup index`
            # still sees them later.
            updates = await bot.get_updates(
                timeout=0,
                allowed_updates=[
                    "message",
                    "edited_message",
                    "channel_post",
                    "edited_channel_post",
                    "my_chat_member",
                ],
            )
            if not updates:
                webhook_url = (await bot.get_webhook_info()).url or ""
        for update in updates:
            chat = update.effective_chat
            if chat is None:
                continue
            title = chat.title or chat.full_name or chat.username or "-"
            chats[chat.id] = (chat.type, title)
        if not chats:
            if webhook_url:
                raise TupError(
                    f"A webhook is set ({webhook_url}); it intercepts all updates, so "
                    "getUpdates-based discovery sees nothing.",
                    hint="Remove it via https://api.telegram.org/bot<token>/deleteWebhook "
                    "and re-run discover.",
                )
            console.print(
                "No chats visible yet. Checklist:\n"
                "  • Groups: bots don't receive plain messages while [bold]group privacy[/bold] "
                "is on (the default). Send a [bold]command[/bold] like /start in the group, "
                "or disable privacy via @BotFather (/mybots → Bot Settings → Group Privacy) "
                "and re-add the bot.\n"
                "  • Channels: the bot must be an [bold]Administrator[/bold]; then post anything.\n"
                "  • Your own ID: open a direct chat with the bot and send /start.\n"
                "  • Telegram drops undelivered updates after ~24h — send a fresh message, "
                "then re-run [bold]tup chat discover[/bold]."
            )
            return
        table = Table("Chat ID", "Type", "Title")
        for chat_id, (chat_type, title) in sorted(chats.items()):
            table.add_row(str(chat_id), chat_type, title)
        console.print(table)
        console.print("Register one with [bold]tup chat add <alias> <chat_id>[/bold]")

    run_async(_run())
