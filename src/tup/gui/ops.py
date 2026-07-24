"""VFS operations for the GUI, mirroring the CLI command semantics exactly.

Same primitives, same edge rules (no renames, .keep folders, empty-only
rmdir, destination-exists guards) — only the presentation differs.
"""

from __future__ import annotations

from pathlib import Path

from telegram.error import BadRequest, Forbidden
from telethon import TelegramClient

from tup.config import Settings
from tup.database import Database, VfsEntry
from tup.uploader import (
    DuplicateFileError,
    TupError,
    access_error,
    bot_session,
    copy_message_media,
    delete_remote_message,
    edit_caption,
    fetch_existing_ids,
    format_caption,
    parse_caption,
    send_with_retry,
    upload_file,
)
from tup.utils import normalize_vfs_path, split_vfs_path

KEEP_FILE = ".keep"


def _full_path(dest_dir: str, file_name: str) -> str:
    return dest_dir + file_name if dest_dir != "/" else "/" + file_name


async def op_mkdir(db: Database, chat_id: str, path: str) -> str:
    target = normalize_vfs_path(path, directory=True)
    if target == "/":
        raise TupError("Root '/' always exists.")
    if await db.vfs_get(chat_id, target, KEEP_FILE) is not None:
        raise TupError(f"Directory already exists: {target}")
    await db.vfs_upsert(chat_id, target, KEEP_FILE, 0, "", "", 0)
    return target


async def op_rmdir(db: Database, chat_id: str, path: str) -> str:
    target = normalize_vfs_path(path, directory=True)
    if target == "/":
        raise TupError("Cannot remove the root directory.")
    entries = await db.vfs_list_prefix(chat_id, target)
    keep = await db.vfs_get(chat_id, target, KEEP_FILE)
    contents = [e for e in entries if not (e.virtual_path == target and e.file_name == KEEP_FILE)]
    if contents:
        raise TupError(f"Directory not empty: {target}")
    if keep is None:
        raise TupError(f"No such directory: {target}")
    await db.vfs_delete(keep.id)
    return target


async def op_rm(db: Database, settings: Settings, chat_id: str, entry: VfsEntry) -> str:
    if entry.telegram_message_id > 0:
        async with bot_session(settings) as bot:
            await delete_remote_message(
                bot, chat_id, entry.telegram_message_id, max_retries=settings.max_retries
            )
    await db.vfs_delete(entry.id)
    return f"{entry.virtual_path}{entry.file_name}"


async def op_mv(
    db: Database, settings: Settings, chat_id: str, entry: VfsEntry, dest_dir: str
) -> str:
    dest = normalize_vfs_path(dest_dir, directory=True)
    if dest == entry.virtual_path:
        raise TupError("Source and destination are the same.")
    if await db.vfs_get(chat_id, dest, entry.file_name) is not None:
        raise TupError(f"Destination already exists: {dest}{entry.file_name}")
    full_path = _full_path(dest, entry.file_name)
    caption = format_caption(full_path, entry.file_hash, entry.user_caption or None)
    if entry.telegram_message_id > 0 and entry.origin != "observed":
        async with bot_session(settings) as bot:
            await edit_caption(
                bot, chat_id, entry.telegram_message_id, caption, max_retries=settings.max_retries
            )
    await db.vfs_move(entry.id, dest, entry.file_name)
    return full_path


async def op_cp(
    db: Database,
    settings: Settings,
    client: TelegramClient,
    chat_id: str,
    entry: VfsEntry,
    dest_dir: str,
) -> str:
    dest = normalize_vfs_path(dest_dir, directory=True)
    if await db.vfs_get(chat_id, dest, entry.file_name) is not None:
        raise TupError(f"Destination already exists: {dest}{entry.file_name}")
    same_hash = [
        e for e in await db.vfs_find_by_hash(chat_id, entry.file_hash) if e.virtual_path == dest
    ]
    if same_hash:
        raise DuplicateFileError(
            f"An identical file already exists in {dest} "
            f"as {same_hash[0].file_name} (same SHA-256)."
        )
    full_path = _full_path(dest, entry.file_name)
    caption = format_caption(full_path, entry.file_hash, entry.user_caption or None)
    message_id = await copy_message_media(
        client, chat_id, entry.telegram_message_id, caption, max_retries=settings.max_retries
    )
    await db.vfs_upsert(
        chat_id,
        dest,
        entry.file_name,
        entry.file_size,
        entry.file_hash,
        "",
        message_id,
        mime_type=entry.mime_type,
        media_kind=entry.media_kind,
        width=entry.width,
        height=entry.height,
        duration=entry.duration,
        source_mtime=entry.source_mtime,
        user_caption=entry.user_caption,
        tags=entry.tags,
    )
    return full_path


async def op_prune(
    db: Database, settings: Settings, client: TelegramClient, chat_id: str
) -> list[str]:
    """Drop index rows whose messages were deleted natively in Telegram."""
    entries = [
        e
        for e in await db.vfs_list_prefix(chat_id, "/")
        if e.telegram_message_id > 0  # .keep rows have no remote message
    ]
    if not entries:
        return []
    alive = await fetch_existing_ids(
        client,
        chat_id,
        [e.telegram_message_id for e in entries],
        max_retries=settings.max_retries,
    )
    pruned: list[str] = []
    for entry in entries:
        if entry.telegram_message_id not in alive:
            await db.vfs_delete(entry.id)
            pruned.append(f"{entry.virtual_path}{entry.file_name}")
    return pruned


async def op_retry_failed(
    db: Database, settings: Settings, client: TelegramClient
) -> tuple[int, int]:
    """Re-attempt all pending failed uploads; returns (resolved, still_failing)."""
    pending = await db.failed_pending()
    resolved = still_failing = 0
    for item in pending:
        meta = parse_caption(item.caption)
        dest_dir = split_vfs_path(meta.full_path)[0] if meta is not None else "/"
        try:
            await upload_file(
                db,
                settings,
                client,
                Path(item.file_path),
                item.chat_id,
                dest_dir,
                user_caption=meta.user_caption if meta else None,
            )
            await db.failed_mark(item.id, "resolved", bump_retry=True)
            resolved += 1
        except TupError:
            await db.failed_mark(item.id, "pending", bump_retry=True)
            still_failing += 1
    return resolved, still_failing


async def op_add_chat(db: Database, settings: Settings, alias: str, chat_id: str) -> str:
    """Validate a chat via the Bot API and register the alias; returns the title."""
    async with bot_session(settings) as bot:
        try:
            chat = await send_with_retry(
                lambda: bot.get_chat(chat_id),
                max_retries=settings.max_retries,
                what="validate chat",
            )
        except (Forbidden, BadRequest) as exc:
            raise access_error(chat_id) from exc
    title = chat.title or chat.full_name or chat.username or None
    await db.alias_add(alias, str(chat.id), title)
    return title or str(chat.id)


async def op_discover_chats(settings: Settings) -> list[tuple[str, str, str]]:
    """Peek pending updates for visible chats; returns (chat_id, type, title) rows.

    Non-destructive: no offset is passed, so `tup index` still sees the
    updates later. The Bot API cannot enumerate a bot's chats directly.
    """
    chats: dict[int, tuple[str, str]] = {}
    async with bot_session(settings) as bot:
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
    for update in updates:
        chat = update.effective_chat
        if chat is None:
            continue
        title = chat.title or chat.full_name or chat.username or "-"
        chats[chat.id] = (chat.type, title)
    return [(str(cid), ctype, title) for cid, (ctype, title) in sorted(chats.items())]
