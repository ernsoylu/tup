"""Shared VFS operations: Recycle Bin, captions/tags, save-through edits, versions.

Used by both the CLI and the GUI so semantics stay identical, and the
conventions (trash prefix, caption re-rendering, version cap, origin gating)
mirror tup-cloud exactly — all three frontends interoperate on one chat:

- Delete moves a file under `/.Trash/<original path>` by rewriting its Telegram
  caption to the trash path, so the state survives in Telegram itself and the
  DB stays rebuildable from the group. Restore strips the prefix again.
- Saving new content uploads a new message and keeps the superseded message as
  a version row (capped at VERSION_CAP); the current revision is always the
  vfs_index row. Messages tup does not own (origin='observed') are never
  edited, deleted, or versioned.
"""

from __future__ import annotations

import hashlib
import mimetypes
import tempfile
from pathlib import Path

from telethon import TelegramClient

from tup.config import Settings
from tup.database import Database, FileVersion, VfsEntry
from tup.uploader import (
    TupError,
    _mtproto_with_retry,
    bot_session,
    delete_remote_message,
    download_media_file,
    edit_caption,
    format_caption,
    resolve_peer,
)
from tup.utils import extract_tags, normalize_vfs_path

TRASH_PREFIX = "/.Trash/"
VERSION_CAP = 20


def full_path_of(entry: VfsEntry) -> str:
    return entry.virtual_path + entry.file_name


def is_trashed(entry: VfsEntry) -> bool:
    return entry.virtual_path.startswith(TRASH_PREFIX)


def original_path_of(entry: VfsEntry) -> str:
    """The pre-trash location encoded in a trashed entry's path."""
    return "/" + entry.virtual_path[len(TRASH_PREFIX) :] + entry.file_name


def _owns_message(entry: VfsEntry) -> bool:
    return entry.telegram_message_id > 0 and entry.origin != "observed"


async def _rewrite_caption(
    settings: Settings, chat_id: str, entry: VfsEntry, full_path: str
) -> None:
    """Re-render the protocol caption for a new path, preserving the user caption."""
    if not _owns_message(entry):
        return  # index-only for observed files and .keep rows
    caption = format_caption(full_path, entry.file_hash, entry.user_caption or None)
    async with bot_session(settings) as bot:
        await edit_caption(
            bot, chat_id, entry.telegram_message_id, caption, max_retries=settings.max_retries
        )


async def op_set_caption(
    db: Database, settings: Settings, chat_id: str, entry: VfsEntry, text: str
) -> str:
    """Set the user caption (tags are parsed from its hashtags); returns the tags."""
    tags = extract_tags(text)
    if _owns_message(entry):
        caption = format_caption(full_path_of(entry), entry.file_hash, text or None)
        async with bot_session(settings) as bot:
            await edit_caption(
                bot, chat_id, entry.telegram_message_id, caption, max_retries=settings.max_retries
            )
    await db.vfs_set_caption(entry.id, text, tags)
    return tags


def _dedup_name(taken: set[str], name: str) -> str:
    """Append ' (2)', ' (3)', … before the extension until the name is free."""
    if name not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    base, suffix = (stem, "." + ext) if dot else (name, "")
    counter = 2
    while f"{base} ({counter}){suffix}" in taken:
        counter += 1
    return f"{base} ({counter}){suffix}"


async def op_trash(db: Database, settings: Settings, chat_id: str, entry: VfsEntry) -> str:
    """Move a file to the Recycle Bin; returns its new /.Trash/ path."""
    if is_trashed(entry):
        raise TupError(f"{full_path_of(entry)} is already in the Recycle Bin.")
    trash_dir = TRASH_PREFIX + entry.virtual_path.lstrip("/")
    taken = {e.file_name for e in await db.vfs_list_dir(chat_id, trash_dir)}
    new_name = _dedup_name(taken, entry.file_name)
    await _rewrite_caption(settings, chat_id, entry, trash_dir + new_name)
    await db.vfs_move(entry.id, trash_dir, new_name)
    return trash_dir + new_name


async def op_restore(db: Database, settings: Settings, chat_id: str, entry: VfsEntry) -> str:
    """Move a trashed file back to its original folder; returns that path."""
    if not is_trashed(entry):
        raise TupError(f"{full_path_of(entry)} is not in the Recycle Bin.")
    original_dir = "/" + entry.virtual_path[len(TRASH_PREFIX) :]
    if await db.vfs_get(chat_id, original_dir, entry.file_name) is not None:
        raise TupError(
            f"Cannot restore: {original_dir}{entry.file_name} already exists.",
            hint="Move or delete the existing file first.",
        )
    full_path = original_dir + entry.file_name if original_dir != "/" else "/" + entry.file_name
    await _rewrite_caption(settings, chat_id, entry, full_path)
    await db.vfs_move(entry.id, original_dir, entry.file_name)
    return full_path


async def op_purge(db: Database, settings: Settings, chat_id: str, entry: VfsEntry) -> None:
    """Permanently delete: version messages, the current message, the index row.

    Message deletions are best-effort for observed files (tup does not own
    them); for owned files a failed delete still surfaces as a warning-level
    outcome via delete_remote_message's False return, never an exception.
    """
    async with bot_session(settings) as bot:
        for version in await db.version_list(entry.id):
            await delete_remote_message(
                bot, chat_id, version.telegram_message_id, max_retries=settings.max_retries
            )
            await db.version_delete(version.id)
        if entry.telegram_message_id > 0:
            await delete_remote_message(
                bot, chat_id, entry.telegram_message_id, max_retries=settings.max_retries
            )
    await db.vfs_delete(entry.id)


async def op_list_trash(db: Database, chat_id: str) -> list[VfsEntry]:
    return await db.vfs_list_prefix(chat_id, TRASH_PREFIX)


async def op_empty_trash(db: Database, settings: Settings, chat_id: str) -> int:
    entries = await op_list_trash(db, chat_id)
    for entry in entries:
        await op_purge(db, settings, chat_id, entry)
    return len(entries)


# --- save-through edits & versions --------------------------------------------


async def save_content(
    db: Database,
    settings: Settings,
    client: TelegramClient,
    chat_id: str,
    dest_dir: str,
    file_name: str,
    data: bytes,
    *,
    saved_by: str = "",
) -> tuple[VfsEntry, bool]:
    """Write bytes to a VFS file: upload a new message, version the old one.

    Mirrors tup-cloud's save pipeline: identical content is a no-op; the
    superseded message is kept as a FileVersion row (pruned past VERSION_CAP);
    the entry row mutates in place so the file's identity (and versions)
    survive edits. Returns (entry, changed).
    """
    virtual_dir = normalize_vfs_path(dest_dir, directory=True)
    full_path = virtual_dir + file_name if virtual_dir != "/" else "/" + file_name
    file_hash = hashlib.sha256(data).hexdigest()
    existing = await db.vfs_get(chat_id, virtual_dir, file_name)
    if existing is not None and existing.file_hash == file_hash:
        return existing, False

    caption = format_caption(
        full_path, file_hash, (existing.user_caption if existing else "") or None
    )
    mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

    async def _send() -> int:
        peer = await resolve_peer(client, chat_id)
        with tempfile.TemporaryDirectory(prefix="tup-save-") as spool:
            # The upload's file name comes from the path, so spool under the
            # real name (scrubbed of path separators).
            spool_path = Path(spool) / file_name.replace("/", "_")
            spool_path.write_bytes(data)
            message = await client.send_file(
                peer,
                str(spool_path),
                caption=caption,
                parse_mode=None,  # keep the caption protocol block raw
                force_document=True,
            )
        return int(message.id)

    message_id = await _mtproto_with_retry(
        _send, max_retries=settings.max_retries, what=f"save {file_name}"
    )

    if existing is not None:
        if _owns_message(existing):
            await db.version_add(
                existing.id,
                chat_id,
                existing.telegram_message_id,
                existing.file_hash,
                existing.file_size,
                saved_by,
            )
        await db.vfs_replace_content(existing.id, len(data), file_hash, message_id, mime)
        await prune_versions(db, settings, chat_id, existing.id)
    else:
        await db.vfs_upsert(
            chat_id,
            virtual_dir,
            file_name,
            len(data),
            file_hash,
            "",
            message_id,
            mime_type=mime,
            media_kind="document",
        )

    await db.log_upload(
        full_path, len(data), chat_id, "document", "success", telegram_message_id=message_id
    )
    entry = await db.vfs_get(chat_id, virtual_dir, file_name)
    assert entry is not None  # noqa: S101 - row exists: updated or inserted above
    return entry, True


async def prune_versions(db: Database, settings: Settings, chat_id: str, entry_id: int) -> int:
    """Delete versions beyond VERSION_CAP (Telegram message + row); returns count."""
    over = await db.versions_over_cap(entry_id, VERSION_CAP)
    if not over:
        return 0
    async with bot_session(settings) as bot:
        for version in over:
            await delete_remote_message(
                bot, chat_id, version.telegram_message_id, max_retries=settings.max_retries
            )
            await db.version_delete(version.id)
    return len(over)


async def read_message_bytes(
    client: TelegramClient, settings: Settings, chat_id: str, message_id: int
) -> bytes:
    """Download a message's media into memory (current content or a version)."""
    with tempfile.TemporaryDirectory(prefix="tup-read-") as spool:
        dest = Path(spool) / "content.bin"
        await download_media_file(
            client, chat_id, message_id, dest, max_retries=settings.max_retries
        )
        return dest.read_bytes()


async def restore_version(
    db: Database,
    settings: Settings,
    client: TelegramClient,
    chat_id: str,
    entry: VfsEntry,
    version: FileVersion,
) -> VfsEntry:
    """Make an old version current by re-saving its content (cloud semantics:
    the current revision becomes a new version; no pointer swapping)."""
    data = await read_message_bytes(client, settings, chat_id, version.telegram_message_id)
    saved, _ = await save_content(
        db, settings, client, chat_id, entry.virtual_path, entry.file_name, data
    )
    return saved
