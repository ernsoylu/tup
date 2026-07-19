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
from datetime import timedelta
from pathlib import Path
from typing import Any

from telegram import Bot, Message
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser

from tup.config import Settings, config_dir
from tup.database import Database
from tup.progress import make_progress
from tup.utils import MediaKind, detect_mime, normalize_vfs_path, sha256_file

logger = logging.getLogger("tup.uploader")

MTPROTO_LIMIT_BYTES = 2 * 1024 * 1024 * 1024  # bot accounts cap at 2 GB

_CAPTION_PATH_RE = re.compile(r"📁 `(?P<path>/[^`]*)`")
_CAPTION_HASH_RE = re.compile(r"🔗 SHA256: (?P<hash>[0-9a-f]{64})")


class TupError(RuntimeError):
    """User-facing error rendered as a rich panel; never a raw traceback."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


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


@asynccontextmanager
async def mtproto_session(settings: Settings) -> AsyncIterator[TelegramClient]:
    """Connected Telethon client logged in with the bot token (no phone login)."""
    if not (settings.telegram_api_id and settings.telegram_api_hash):
        raise TupError(
            "Uploads require MTProto credentials (TELEGRAM_API_ID / TELEGRAM_API_HASH).",
            hint="Create them in seconds at https://my.telegram.org → API development "
            "tools, then run [bold]tup setup[/bold] or add them to ~/.config/tup/.env.",
        )
    session_path = config_dir() / "tup-mtproto"
    client = TelegramClient(
        str(session_path),
        settings.telegram_api_id,
        settings.telegram_api_hash.get_secret_value(),
    )
    await client.start(bot_token=settings.telegram_bot_token.get_secret_value())
    _secure_session_file(session_path)
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


def _stat_upload_size(local_path: Path) -> int:
    if not local_path.is_file():
        raise TupError(f"Not a file: {local_path}")
    return local_path.stat().st_size


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
        except (ConnectionError, TimeoutError, OSError) as exc:
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
) -> int:
    """Upload one file over MTProto: preflight, caption, send, then index + log.

    Returns the Telegram message_id (chat-scoped, shared with the Bot API, so
    mv/rm interoperate). Terminal failures are recorded in failed_registry and
    uploads_log before the TupError propagates (spec §8).
    """
    size = _stat_upload_size(local_path)
    check_size_limit(local_path, size)

    virtual_dir = normalize_vfs_path(dest_dir, directory=True)
    full_path = virtual_dir + local_path.name if virtual_dir != "/" else "/" + local_path.name
    file_hash = sha256_file(local_path)
    caption = format_caption(full_path, file_hash, user_caption)
    _mime, kind = resolve_kind(local_path, as_doc=as_doc, as_video=as_video, as_audio=as_audio)

    async def _send() -> int:
        peer = await resolve_peer(client, chat_id)
        with make_progress(transient=True) as progress:
            task_id = progress.add_task(local_path.name, total=size)

            def on_progress(sent: int, _total: int) -> None:
                progress.update(task_id, completed=sent)

            message = await client.send_file(
                peer,
                str(local_path),
                caption=caption,
                parse_mode=None,  # keep the caption protocol block raw
                force_document=(kind == "document"),
                supports_streaming=(kind == "video"),
                silent=silent,
                progress_callback=on_progress,
            )
        return int(message.id)

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

    await db.vfs_upsert(chat_id, virtual_dir, local_path.name, size, file_hash, "", message_id)
    await db.log_upload(
        str(local_path), size, chat_id, kind, "success", telegram_message_id=message_id
    )
    logger.info("Uploaded %s -> chat %s as %s (message %d)", local_path, chat_id, kind, message_id)
    return message_id


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
