"""PTB Application lifecycle, media routing, VFS caption protocol, and retries."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from telegram import Bot, Message
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application

from tup.config import Settings
from tup.database import Database
from tup.utils import MediaKind, detect_mime, normalize_vfs_path, sha256_file

logger = logging.getLogger("tup.uploader")

BOT_API_LIMIT_BYTES = 50 * 1024 * 1024

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
    hash_line_end = hash_match.end()
    tail = caption[hash_line_end:]
    tag_idx = tail.find("#vfs")
    body = tail[:tag_idx] if tag_idx != -1 else tail
    body = body.strip()
    if body:
        user_caption = body
    return CaptionMeta(
        full_path=path_match.group("path"),
        sha256=hash_match.group("hash"),
        user_caption=user_caption,
    )


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


def access_error(chat_id: str) -> TupError:
    return TupError(
        f"Bot lacks access to chat [{chat_id}].",
        hint="Ensure it is added as a member (Groups) or Administrator (Channels).",
    )


async def send_with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    max_retries: int,
    what: str,
) -> T:
    """Run a Telegram operation with RetryAfter and exponential-backoff handling.

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


def check_size_limit(path: Path, size: int, settings: Settings) -> None:
    if size > BOT_API_LIMIT_BYTES and settings.telegram_api_base_url is None:
        raise TupError(
            f"{path.name} is {size / (1024 * 1024):.1f} MB — the public Bot API caps uploads at 50 MB.",
            hint=(
                "Run a local Bot API server and set TELEGRAM_API_BASE_URL "
                "to raise the limit to 2 GB."
            ),
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


def extract_file_id(message: Message, kind: MediaKind) -> str:
    if kind == "photo" and message.photo:
        return message.photo[-1].file_id
    if kind == "video" and message.video:
        return message.video.file_id
    if kind == "audio" and message.audio:
        return message.audio.file_id
    if message.document:
        return message.document.file_id
    raise TupError("Telegram response did not include a file_id.")


async def copy_by_file_id(
    bot: Bot, chat_id: str, file_id: str, caption: str, *, max_retries: int
) -> Message:
    """Server-side duplication via send_document(file_id) — no re-upload (spec §7)."""
    try:
        return await send_with_retry(
            lambda: bot.send_document(chat_id=chat_id, document=file_id, caption=caption),
            max_retries=max_retries,
            what="copy",
        )
    except Forbidden as exc:
        raise access_error(chat_id) from exc
    except BadRequest as exc:
        raise TupError(f"Telegram rejected the copy: {exc}") from exc


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


def _stat_upload_size(local_path: Path) -> int:
    if not local_path.is_file():
        raise TupError(f"Not a file: {local_path}")
    return local_path.stat().st_size


async def _dispatch_send(
    bot: Bot,
    chat_id: str,
    path: Path,
    kind: MediaKind,
    caption: str,
    *,
    silent: bool,
) -> Message:
    with path.open("rb") as fh:
        if kind == "photo":
            return await bot.send_photo(
                chat_id=chat_id, photo=fh, caption=caption, disable_notification=silent
            )
        if kind == "video":
            return await bot.send_video(
                chat_id=chat_id, video=fh, caption=caption, disable_notification=silent
            )
        if kind == "audio":
            return await bot.send_audio(
                chat_id=chat_id, audio=fh, caption=caption, disable_notification=silent
            )
        return await bot.send_document(
            chat_id=chat_id,
            document=fh,
            filename=path.name,
            caption=caption,
            disable_notification=silent,
        )


async def upload_file(
    db: Database,
    settings: Settings,
    bot: Bot,
    local_path: Path,
    chat_id: str,
    dest_dir: str,
    *,
    as_doc: bool = False,
    as_video: bool = False,
    as_audio: bool = False,
    silent: bool = False,
    user_caption: str | None = None,
) -> Message:
    """Upload one file: preflight, caption, send with retry, then index + log.

    Exhausted retries are recorded in failed_registry and uploads_log before
    the TupError propagates (spec §8).
    """
    size = _stat_upload_size(local_path)
    check_size_limit(local_path, size, settings)

    virtual_dir = normalize_vfs_path(dest_dir, directory=True)
    full_path = virtual_dir + local_path.name if virtual_dir != "/" else "/" + local_path.name
    file_hash = sha256_file(local_path)
    caption = format_caption(full_path, file_hash, user_caption)
    _mime, kind = resolve_kind(local_path, as_doc=as_doc, as_video=as_video, as_audio=as_audio)

    try:
        message = await send_with_retry(
            lambda: _dispatch_send(bot, chat_id, local_path, kind, caption, silent=silent),
            max_retries=settings.max_retries,
            what=f"upload {local_path.name}",
        )
    except TupError as exc:
        await db.failed_add(str(local_path), chat_id, caption, kind, str(exc))
        await db.log_upload(str(local_path), size, chat_id, kind, "failed", error_message=str(exc))
        raise
    except Forbidden as exc:
        await db.log_upload(str(local_path), size, chat_id, kind, "failed", error_message=str(exc))
        raise access_error(chat_id) from exc
    except BadRequest as exc:
        await db.log_upload(str(local_path), size, chat_id, kind, "failed", error_message=str(exc))
        raise TupError(f"Telegram rejected {local_path.name}: {exc}") from exc

    file_id = extract_file_id(message, kind)
    await db.vfs_upsert(
        chat_id, virtual_dir, local_path.name, size, file_hash, file_id, message.message_id
    )
    await db.log_upload(
        str(local_path), size, chat_id, kind, "success", telegram_message_id=message.message_id
    )
    logger.info(
        "Uploaded %s -> chat %s as %s (message %d)", local_path, chat_id, kind, message.message_id
    )
    return message
