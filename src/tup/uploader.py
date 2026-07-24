"""Upload transport (Telethon/MTProto), VFS caption protocol, and PTB metadata ops.

All file uploads and server-side copies go through MTProto (Telethon with
bot-token login): one transport, a uniform 2 GB cap, and media reuse for
copies. The Bot API (PTB) is kept only for metadata operations — chat
validation, caption edits, deletes, and update draining — where its
queue semantics are the right tool.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from telegram import Bot, Message
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    DocumentAttributeVideo,
    InputPeerChannel,
    InputPeerChat,
    InputPeerUser,
)

from tup.config import Settings, config_dir
from tup.database import Database
from tup.progress import make_progress
from tup.utils import MediaKind, detect_mime, extract_tags, normalize_vfs_path, sha256_file

logger = logging.getLogger("tup.uploader")

MTPROTO_LIMIT_BYTES = 2 * 1024 * 1024 * 1024  # bot accounts cap at 2 GB

_CAPTION_PATH_RE = re.compile(r"📁 `(?P<path>/[^`]*)`")
_CAPTION_HASH_RE = re.compile(r"🔗 SHA256: (?P<hash>[0-9a-f]{64})")


class TupError(RuntimeError):
    """User-facing error rendered as a rich panel; never a raw traceback."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class DuplicateFileError(TupError):
    """An identical file (same SHA-256) already exists in the target folder.

    Not a failure: nothing is written to failed_registry, sync counts it as
    skipped, and the GUI transfer queue marks the item skipped.
    """


@dataclass(frozen=True)
class CaptionMeta:
    """Parsed VFS caption protocol block (spec §5)."""

    full_path: str
    sha256: str
    user_caption: str | None


def format_caption(full_path: str, sha256: str, user_caption: str | None = None) -> str:
    """Render the exact VFS caption protocol block (spec §5)."""
    virtual_dir = full_path.rsplit("/", 1)[0] or "/"
    folder = virtual_dir.rstrip("/").rsplit("/", 1)[-1] or "root"
    folder_tag = re.sub(r"\W+", "_", folder)
    parts = [f"📁 `{full_path}`", f"🔗 SHA256: {sha256}"]
    if user_caption:
        parts.append(f"\n{user_caption}")
    parts.append(f"\n#vfs #{folder_tag}")
    return "\n".join(parts)


def parse_caption(caption: str | None) -> CaptionMeta | None:
    """Parse a VFS caption block; None when the message is not tup-managed."""
    if not caption:
        return None
    path_match = _CAPTION_PATH_RE.search(caption)
    hash_match = _CAPTION_HASH_RE.search(caption)
    if not path_match or not hash_match:
        return None
    user_caption: str | None = None
    tail = caption[hash_match.end() :]
    tag_idx = tail.find("#vfs")
    body = (tail[:tag_idx] if tag_idx != -1 else tail).strip()
    if body:
        user_caption = body
    return CaptionMeta(
        full_path=path_match.group("path"),
        sha256=hash_match.group("hash"),
        user_caption=user_caption,
    )


def access_error(chat_id: str) -> TupError:
    return TupError(
        f"Bot lacks access to chat [{chat_id}].",
        hint="Ensure it is added as a member (Groups) or Administrator (Channels).",
    )


# --- Bot API (PTB): metadata operations only ----------------------------------


@asynccontextmanager
async def bot_session(settings: Settings) -> AsyncIterator[Bot]:
    """Short-lived PTB Application per CLI command (spec §4).

    `telegram.Bot` itself does not support `async with` in v20+; the
    Application context manager handles initialize/shutdown.
    """
    builder = Application.builder().token(settings.telegram_bot_token.get_secret_value())
    if settings.telegram_api_base_url is not None:
        base = str(settings.telegram_api_base_url).rstrip("/")
        if not base.endswith("/bot"):
            base += "/bot"
        builder = builder.base_url(base)
    app = builder.build()
    async with app:
        yield app.bot


async def send_with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    max_retries: int,
    what: str,
) -> T:
    """Run a Bot API operation with RetryAfter and exponential-backoff handling.

    Raises TupError when retries are exhausted; Forbidden/BadRequest pass
    through for the caller to translate (they are not transient).
    """
    attempt = 0
    while True:
        try:
            return await operation()
        except RetryAfter as exc:
            attempt += 1
            if attempt > max_retries:
                raise TupError(
                    f"{what}: rate-limited after {max_retries} retries.",
                    hint="Re-run later or use [bold]tup retry[/bold].",
                ) from exc
            delay = exc.retry_after
            wait = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
            logger.warning("Rate limited (%s); sleeping %.1fs (attempt %d)", what, wait, attempt)
            await asyncio.sleep(wait)
        except BadRequest:
            # PTB quirk: BadRequest subclasses NetworkError, but a 400 is not
            # transient — retrying it just repeats the same rejection. Let the
            # caller translate it ("not found", "not modified", ...).
            raise
        except (TimedOut, NetworkError) as exc:
            attempt += 1
            if attempt > max_retries:
                raise TupError(
                    f"{what}: network failure after {max_retries} retries: {exc}",
                    hint="Check connectivity, then use [bold]tup retry[/bold].",
                ) from exc
            wait = float(2**attempt)
            logger.warning("Network error (%s): %s; backing off %.0fs", what, exc, wait)
            await asyncio.sleep(wait)


async def edit_caption(
    bot: Bot, chat_id: str, message_id: int, caption: str, *, max_retries: int
) -> None:
    """Edit a message caption; 'message is not modified' counts as success."""
    try:
        await send_with_retry(
            lambda: bot.edit_message_caption(
                chat_id=chat_id, message_id=message_id, caption=caption
            ),
            max_retries=max_retries,
            what="edit caption",
        )
    except Forbidden as exc:
        raise access_error(chat_id) from exc
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        raise TupError(f"Could not edit caption of message {message_id}: {exc}") from exc


async def delete_remote_message(
    bot: Bot, chat_id: str, message_id: int, *, max_retries: int
) -> bool:
    """Delete a message; returns False when it was already gone."""
    try:
        await send_with_retry(
            lambda: bot.delete_message(chat_id=chat_id, message_id=message_id),
            max_retries=max_retries,
            what="delete message",
        )
    except Forbidden as exc:
        raise access_error(chat_id) from exc
    except BadRequest as exc:
        if "not found" in str(exc).lower() or "message to delete" in str(exc).lower():
            return False
        raise TupError(f"Could not delete message {message_id}: {exc}") from exc
    return True


def media_info(message: Message) -> tuple[str, int] | None:
    """(file_id, file_size) of a Bot API message's media asset, if any."""
    if message.document:
        return message.document.file_id, message.document.file_size or 0
    if message.photo:
        photo = message.photo[-1]
        return photo.file_id, photo.file_size or 0
    if message.video:
        return message.video.file_id, message.video.file_size or 0
    if message.audio:
        return message.audio.file_id, message.audio.file_size or 0
    return None


# --- MTProto (Telethon): the upload transport ---------------------------------


async def connect_mtproto(settings: Settings) -> TelegramClient:
    """Connected Telethon client logged in with the bot token (no phone login).

    The caller owns the client and must eventually `disconnect()` it; use
    `mtproto_session` for scoped one-shot use.
    """
    if not (settings.telegram_api_id and settings.telegram_api_hash):
        raise TupError(
            "Uploads require MTProto credentials (TELEGRAM_API_ID / TELEGRAM_API_HASH).",
            hint="Create them in seconds at https://my.telegram.org → API development "
            "tools, then run [bold]tup setup[/bold] or add them to ~/.tup/.env.",
        )
    session_path = config_dir() / "tup-mtproto"
    client = TelegramClient(
        str(session_path),
        settings.telegram_api_id,
        settings.telegram_api_hash.get_secret_value(),
    )
    await client.start(bot_token=settings.telegram_bot_token.get_secret_value())
    _secure_session_file(session_path)
    return client


@asynccontextmanager
async def mtproto_session(settings: Settings) -> AsyncIterator[TelegramClient]:
    """Scoped MTProto client: connects on entry, disconnects on exit."""
    client = await connect_mtproto(settings)
    try:
        yield client
    finally:
        await client.disconnect()


def _secure_session_file(session_path: Path) -> None:
    # The .session file holds the MTProto auth key — same secrecy as the token.
    actual = session_path.with_suffix(".session")
    if actual.exists():
        actual.chmod(0o600)


async def resolve_peer(client: TelegramClient, chat_id: str) -> Any:
    """Resolve a chat_id (or @username) to an input peer a bot can address."""
    try:
        numeric_id = int(chat_id)
    except ValueError:
        try:
            return await client.get_input_entity(chat_id)
        except Exception as exc:
            raise TupError(f"Cannot resolve chat {chat_id!r}: {exc}") from exc
    try:
        return await client.get_input_entity(numeric_id)
    except ValueError:
        # Fresh bot sessions have an empty entity cache; bots may address
        # chats they are a member of with access_hash 0.
        if str(numeric_id).startswith("-100"):
            return InputPeerChannel(int(str(numeric_id)[4:]), 0)
        if numeric_id < 0:
            return InputPeerChat(-numeric_id)
        return InputPeerUser(numeric_id, 0)


def check_size_limit(path: Path, size: int) -> None:
    if size > MTPROTO_LIMIT_BYTES:
        raise TupError(
            f"{path.name} is {size / (1024**3):.2f} GB — Telegram bots cap uploads at 2 GB.",
            hint="Split the file or compress it below 2 GB.",
        )


def resolve_kind(
    path: Path, *, as_doc: bool = False, as_video: bool = False, as_audio: bool = False
) -> tuple[str, MediaKind]:
    """MIME + routing kind, honoring the CLI override flags."""
    mime, kind = detect_mime(path)
    if as_doc:
        return mime, "document"
    if as_video:
        return mime, "video"
    if as_audio:
        return mime, "audio"
    return mime, kind


def _stat_upload(local_path: Path) -> tuple[int, str]:
    """(size, ISO mtime) of the local file to upload."""
    if not local_path.is_file():
        raise TupError(f"Not a file: {local_path}")
    stat = local_path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
    return stat.st_size, mtime


def extract_media_metadata(path: Path) -> tuple[int | None, int | None, int | None]:
    """(width, height, duration seconds) via hachoir; Nones when unavailable."""
    try:
        from hachoir.metadata import extractMetadata
        from hachoir.parser import createParser

        parser = createParser(str(path))
        if parser is None:
            return None, None, None
        with parser:
            metadata = extractMetadata(parser)
        if metadata is None:
            return None, None, None
        width = int(metadata.get("width")) if metadata.has("width") else None
        height = int(metadata.get("height")) if metadata.has("height") else None
        duration = (
            int(metadata.get("duration").total_seconds()) if metadata.has("duration") else None
        )
        return width, height, duration
    except Exception:
        logger.warning("Could not extract media metadata from %s", path)
        return None, None, None


def video_attributes(path: Path) -> list[DocumentAttributeVideo] | None:
    """Real width/height/duration so Telegram renders the correct aspect ratio.

    Without an explicit DocumentAttributeVideo, Telegram guesses the display
    size and the video shows with wrong dimensions. Returns None when the
    container can't be parsed — Telethon then falls back to its own defaults.
    """
    width, height, duration = extract_media_metadata(path)
    if width is None or height is None:
        return None
    return [
        DocumentAttributeVideo(
            duration=duration or 0,
            w=width,
            h=height,
            supports_streaming=True,
        )
    ]


async def _mtproto_with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    max_retries: int,
    what: str,
) -> T:
    """Retry an MTProto operation on flood waits and transient network faults."""
    attempt = 0
    while True:
        try:
            return await operation()
        except FloodWaitError as exc:
            attempt += 1
            if attempt > max_retries:
                raise TupError(
                    f"{what}: rate-limited after {max_retries} retries.",
                    hint="Re-run later or use [bold]tup retry[/bold].",
                ) from exc
            logger.warning("Flood wait (%s): sleeping %ds", what, exc.seconds)
            await asyncio.sleep(float(exc.seconds))
        except OSError as exc:  # includes ConnectionError and TimeoutError
            attempt += 1
            if attempt > max_retries:
                raise TupError(
                    f"{what}: network failure after {max_retries} retries: {exc}",
                    hint="Check connectivity, then use [bold]tup retry[/bold].",
                ) from exc
            wait = float(2**attempt)
            logger.warning("Network error (%s): %s; backing off %.0fs", what, exc, wait)
            await asyncio.sleep(wait)


async def upload_file(
    db: Database,
    settings: Settings,
    client: TelegramClient,
    local_path: Path,
    chat_id: str,
    dest_dir: str,
    *,
    as_doc: bool = False,
    as_video: bool = False,
    as_audio: bool = False,
    silent: bool = False,
    user_caption: str | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> int:
    """Upload one file over MTProto: preflight, caption, send, then index + log.

    Returns the Telegram message_id (chat-scoped, shared with the Bot API, so
    mv/rm interoperate). Terminal failures are recorded in failed_registry and
    uploads_log before the TupError propagates (spec §8). When
    `progress_callback` is given (e.g. by the GUI transfer queue) it replaces
    the rich terminal progress bar.
    """
    size, source_mtime = _stat_upload(local_path)
    check_size_limit(local_path, size)

    virtual_dir = normalize_vfs_path(dest_dir, directory=True)
    full_path = virtual_dir + local_path.name if virtual_dir != "/" else "/" + local_path.name
    file_hash = sha256_file(local_path)
    caption = format_caption(full_path, file_hash, user_caption)
    mime, kind = resolve_kind(local_path, as_doc=as_doc, as_video=as_video, as_audio=as_audio)

    # Same-SHA files must not coexist within one folder (spec §5): checked
    # before any network work, and deliberately not recorded as a failure.
    same_hash_here = [
        e for e in await db.vfs_find_by_hash(chat_id, file_hash) if e.virtual_path == virtual_dir
    ]
    if same_hash_here:
        raise DuplicateFileError(
            f"{local_path.name} is identical (SHA-256) to "
            f"{virtual_dir}{same_hash_here[0].file_name} — not uploading a duplicate.",
            hint="Delete the existing file first, or upload to a different folder.",
        )

    attributes = video_attributes(local_path) if kind == "video" else None
    width = height = duration = None
    if kind in ("photo", "video", "audio"):
        width, height, duration = extract_media_metadata(local_path)

    async def _send() -> int:
        peer = await resolve_peer(client, chat_id)

        async def _do(callback: Callable[[int, int], None]) -> int:
            message = await client.send_file(
                peer,
                str(local_path),
                caption=caption,
                parse_mode=None,  # keep the caption protocol block raw
                force_document=(kind == "document"),
                supports_streaming=(kind == "video"),
                attributes=attributes,  # real w/h/duration so Telegram renders correctly
                silent=silent,
                progress_callback=callback,
            )
            return int(message.id)

        if progress_callback is not None:
            return await _do(progress_callback)
        with make_progress(transient=True) as progress:
            task_id = progress.add_task(local_path.name, total=size)

            def on_progress(sent: int, _total: int) -> None:
                progress.update(task_id, completed=sent)

            return await _do(on_progress)

    try:
        message_id = await _mtproto_with_retry(
            _send, max_retries=settings.max_retries, what=f"upload {local_path.name}"
        )
    except TupError as exc:
        await db.failed_add(str(local_path), chat_id, caption, kind, str(exc))
        await db.log_upload(str(local_path), size, chat_id, kind, "failed", error_message=str(exc))
        raise
    except Exception as exc:
        await db.failed_add(str(local_path), chat_id, caption, kind, str(exc))
        await db.log_upload(str(local_path), size, chat_id, kind, "failed", error_message=str(exc))
        raise TupError(f"Upload of {local_path.name} failed: {exc}") from exc

    await db.vfs_upsert(
        chat_id,
        virtual_dir,
        local_path.name,
        size,
        file_hash,
        "",
        message_id,
        mime_type=mime,
        media_kind=kind,
        width=width,
        height=height,
        duration=duration,
        source_mtime=source_mtime,
        user_caption=user_caption or "",
        tags=extract_tags(user_caption),
    )
    await db.log_upload(
        str(local_path), size, chat_id, kind, "success", telegram_message_id=message_id
    )
    logger.info("Uploaded %s -> chat %s as %s (message %d)", local_path, chat_id, kind, message_id)
    return message_id


def _prepare_download_dest(dest: Path) -> Path:
    """Ensure the parent folder exists; return the temporary .part path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest.with_name(dest.name + ".part")


def _finalize_download(part: str, dest: Path) -> None:
    Path(part).replace(dest)


async def download_media_file(
    client: TelegramClient,
    chat_id: str,
    message_id: int,
    dest: Path,
    *,
    max_retries: int,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Download a message's media to `dest` over MTProto.

    Writes to a `.part` file and renames on completion so an interrupted
    download never masquerades as a finished one.
    """

    async def _download() -> Path:
        peer = await resolve_peer(client, chat_id)
        message = await client.get_messages(peer, ids=message_id)
        if message is None or message.media is None:
            raise TupError(
                f"Message {message_id} has no media on Telegram.",
                hint="Run [bold]tup index --prune[/bold] to reconcile the local index.",
            )
        part = _prepare_download_dest(dest)
        result = await client.download_media(message, file=str(part), progress_callback=progress)
        if result is None:
            raise TupError(f"Download of message {message_id} produced no file.")
        _finalize_download(str(result), dest)
        return dest

    return await _mtproto_with_retry(
        _download, max_retries=max_retries, what=f"download {dest.name}"
    )


async def copy_message_media(
    client: TelegramClient, chat_id: str, message_id: int, caption: str, *, max_retries: int
) -> int:
    """Server-side duplication: re-send an existing message's media, no re-upload."""

    async def _copy() -> int:
        peer = await resolve_peer(client, chat_id)
        source = await client.get_messages(peer, ids=message_id)
        if source is None or source.media is None:
            raise TupError(
                f"Source message {message_id} has no media on Telegram.",
                hint="Run [bold]tup index[/bold] to reconcile the local index.",
            )
        message = await client.send_file(peer, source.media, caption=caption, parse_mode=None)
        return int(message.id)

    return await _mtproto_with_retry(_copy, max_retries=max_retries, what="copy")


async def fetch_existing_ids(
    client: TelegramClient, chat_id: str, message_ids: list[int], *, max_retries: int
) -> set[int]:
    """IDs from message_ids that still exist on Telegram (deleted ones vanish).

    The Bot API emits no deletion events, so this MTProto existence sweep is
    the only way to detect messages removed natively in Telegram.
    """
    peer = await resolve_peer(client, chat_id)
    found: set[int] = set()
    for start in range(0, len(message_ids), 100):
        chunk = message_ids[start : start + 100]

        async def _fetch(batch: list[int] = chunk) -> Any:
            return await client.get_messages(peer, ids=batch)

        messages = await _mtproto_with_retry(
            _fetch, max_retries=max_retries, what="verify messages"
        )
        for message in messages:
            if message is not None:
                found.add(int(message.id))
    return found
