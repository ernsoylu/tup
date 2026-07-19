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
from telegram.error import BadRequest, Forbidden
from typer._click import Command, Context
from typer._click.exceptions import UsageError
from typer.core import TyperGroup

from tup import __version__
from tup.config import Settings, SetupRequiredError, log_file_path
from tup.database import Database, DatabaseError, VfsEntry
from tup.progress import console, error_console
from tup.uploader import (
    TupError,
    access_error,
    bot_session,
    copy_by_file_id,
    delete_remote_message,
    edit_caption,
    extract_file_id,
    format_caption,
    media_info,
    parse_caption,
    upload_file,
)
from tup.utils import (
    SecretScrubberFormatter,
    VfsPathError,
    normalize_vfs_path,
    sha256_file,
    split_vfs_path,
)

KEEP_FILE = ".keep"

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
    setup_logging("DEBUG" if debug else "INFO")


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
        if not file_path.is_file():
            continue
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
            async with bot_session(settings) as bot:
                failures = 0
                for file_path, dest_dir in targets:
                    try:
                        message = await upload_file(
                            db,
                            settings,
                            bot,
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
                            f"✅ {file_path.name} → drive {chat_id} (message {message.message_id})"
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
        return (parent or "/") if parent.endswith("/") else parent + "/" if parent else "/"
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
            entries = await db.vfs_list_prefix(chat_id, base)

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
) -> None:
    """List a VFS directory, POSIX `ls -lh` style (local index, no network)."""

    async def _run() -> None:
        settings = Settings.load()
        base = normalize_vfs_path(path, directory=True)
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            entries = await db.vfs_list_prefix(chat_id, base)

        table = Table("Name", "Size", "Uploaded", "Msg ID")
        if recursive:
            for entry in _visible(entries):
                table.add_row(
                    f"{entry.virtual_path}{entry.file_name}",
                    _human_size(entry.file_size),
                    entry.upload_timestamp,
                    str(entry.telegram_message_id),
                )
        else:
            for child in _child_dirs(entries, base):
                table.add_row(f"[bold blue]{child}/[/bold blue]", "-", "-", "-")
            for entry in _visible([e for e in entries if e.virtual_path == base]):
                table.add_row(
                    entry.file_name,
                    _human_size(entry.file_size),
                    entry.upload_timestamp,
                    str(entry.telegram_message_id),
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
            full_path = dest_dir + entry.file_name if dest_dir != "/" else "/" + entry.file_name
            caption = format_caption(full_path, entry.file_hash)
            async with bot_session(settings) as bot:
                message = await copy_by_file_id(
                    bot, chat_id, entry.telegram_file_id, caption, max_retries=settings.max_retries
                )
            try:
                file_id = extract_file_id(message, "document")
            except TupError:
                file_id = entry.telegram_file_id
            await db.vfs_upsert(
                chat_id,
                dest_dir,
                entry.file_name,
                entry.file_size,
                entry.file_hash,
                file_id,
                message.message_id,
            )
        console.print(f"✅ {src} → {full_path} (message {message.message_id}, no re-upload)")

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
            caption = format_caption(full_path, entry.file_hash)
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
) -> None:
    """Delete a file: remote message and index row."""

    async def _run() -> None:
        settings = Settings.load()
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            entry = await _require_entry(db, chat_id, path)
            async with bot_session(settings) as bot:
                deleted = await delete_remote_message(
                    bot, chat_id, entry.telegram_message_id, max_retries=settings.max_retries
                )
            if not deleted:
                error_console.print(
                    f"⚠️  Message {entry.telegram_message_id} was already gone; cleaning index."
                )
            await db.vfs_delete(entry.id)
        console.print(f"✅ Deleted {entry.virtual_path}{entry.file_name}")

    run_async(_run())


# --- sync & reconciliation ----------------------------------------------------


def _collect_sync_targets(local_dir: Path, remote_base: str) -> list[tuple[Path, str]]:
    """S3-style: the *contents* of local_dir map into remote_base."""
    targets = []
    for file_path in sorted(local_dir.rglob("*")):
        if not file_path.is_file():
            continue
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
            async with bot_session(settings) as bot:
                for file_path, dest_dir in targets:
                    local_hash = _hash_local(file_path)
                    existing = await db.vfs_get(chat_id, dest_dir, file_path.name)
                    if existing is not None and existing.file_hash == local_hash:
                        skipped += 1
                        continue
                    try:
                        await upload_file(db, settings, bot, file_path, chat_id, dest_dir)
                        uploaded += 1
                        console.print(f"⬆️  {file_path.name} → {dest_dir}")
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


@app.command(context_settings=NEGATIVE_ID_OK)
def index(
    drive: Annotated[str, typer.Argument(help="Drive alias or chat_id.")],
    reconstruct: Annotated[
        bool,
        typer.Option(
            "--reconstruct", help="Also index tup-captioned messages missing from the DB."
        ),
    ] = False,
) -> None:
    """Reconcile the local index with pending Telegram updates.

    Note: the Bot API has no message-history endpoint, so tup can only see
    pending updates (getUpdates) and its own uploads. Caption edits made
    natively in Telegram are applied to the index; with --reconstruct,
    tup-captioned messages unknown to the DB are (re-)indexed.
    """

    async def _run() -> None:
        settings = Settings.load()
        edits = added = seen = 0
        async with Database(settings.database_path) as db:
            chat_id = await db.resolve_drive(drive)
            last_update_id = await db.sync_state_get(chat_id)
            async with bot_session(settings) as bot:
                while True:
                    updates = await bot.get_updates(
                        offset=last_update_id + 1 if last_update_id else None,
                        timeout=0,
                        allowed_updates=[
                            "message",
                            "edited_message",
                            "channel_post",
                            "edited_channel_post",
                        ],
                    )
                    if not updates:
                        break
                    for update in updates:
                        last_update_id = max(last_update_id, update.update_id)
                        message = update.effective_message
                        if message is None or str(message.chat.id) != chat_id:
                            continue
                        seen += 1
                        meta = parse_caption(message.caption)
                        if meta is None:
                            continue
                        virtual_dir, file_name = split_vfs_path(meta.full_path)
                        is_edit = (
                            update.edited_message is not None
                            or update.edited_channel_post is not None
                        ) and message.edit_date is not None
                        existing = await db.vfs_get_by_message(chat_id, message.message_id)
                        if is_edit and existing is not None:
                            if (
                                existing.virtual_path != virtual_dir
                                or existing.file_name != file_name
                            ):
                                await db.vfs_move(existing.id, virtual_dir, file_name)
                                edits += 1
                        elif existing is None and reconstruct:
                            info = media_info(message)
                            if info is None:
                                continue
                            file_id, file_size = info
                            await db.vfs_upsert(
                                chat_id,
                                virtual_dir,
                                file_name,
                                file_size,
                                meta.sha256,
                                file_id,
                                message.message_id,
                            )
                            added += 1
            await db.sync_state_set(chat_id, last_update_id)
        console.print(
            f"✅ Index reconciled: {seen} message(s) seen, {edits} caption edit(s) applied, "
            f"{added} row(s) reconstructed."
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
            async with bot_session(settings) as bot:
                for item in pending:
                    file_path = Path(item.file_path)
                    meta = parse_caption(item.caption)
                    dest_dir = split_vfs_path(meta.full_path)[0] if meta is not None else "/"
                    try:
                        await upload_file(
                            db,
                            settings,
                            bot,
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
