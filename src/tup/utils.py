"""MIME detection, SHA-256 hashing, VFS path normalization, and secret scrubbing."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import posixpath
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import filetype

MediaKind = Literal["photo", "video", "audio", "document"]

# Telegram bot tokens look like "123456789:AAE...35-char-secret" and may appear
# bare, as env assignments, or embedded in api.telegram.org/bot<token>/ URLs —
# no leading \b: "bot<digits>" has no word boundary before the digits.
_TOKEN_RE = re.compile(r"\d{5,12}:[A-Za-z0-9_-]{30,64}")  # bounded: linear-time matching

HASH_CHUNK_SIZE = 1024 * 1024


class VfsPathError(ValueError):
    """Raised when a VFS path is malformed or escapes the root."""


def utc_now_iso() -> str:
    """UTC timestamp as a strict ISO 8601 string (spec §6 timestamp convention)."""
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 of a file, chunked to bound memory usage."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def detect_mime(path: Path) -> tuple[str, MediaKind]:
    """Detect a file's MIME type and Telegram routing kind.

    Magic bytes via `filetype` first; `mimetypes.guess_type` as the mandatory
    fallback (spec §5); `application/octet-stream` document as last resort.
    """
    mime: str | None = None
    kind = filetype.guess(str(path))
    if kind is not None:
        mime = kind.mime
    if mime is None:
        mime, _ = mimetypes.guess_type(path.name)
    if mime is None:
        return "application/octet-stream", "document"
    return mime, _route_for_mime(mime)


def _route_for_mime(mime: str) -> MediaKind:
    if mime.startswith("image/") and mime not in ("image/svg+xml", "image/x-icon"):
        return "photo"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


def normalize_vfs_path(path: str, *, directory: bool = False) -> str:
    """Normalize a VFS path to a root-relative POSIX path starting with '/'.

    Collapses '.' and '..' segments; rejects paths that traverse above the
    root. Directory paths always carry a trailing slash (spec §6 invariant);
    file paths never do. The root itself is always '/'.
    """
    candidate = path.strip()
    if not candidate:
        raise VfsPathError("VFS path must not be empty")
    # Segment-wise resolution: posixpath.normpath silently clamps '/..' to '/',
    # but escaping the VFS root must be an explicit error.
    stack: list[str] = []
    for segment in candidate.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not stack:
                raise VfsPathError(f"VFS path escapes root: {path!r}")
            stack.pop()
        else:
            stack.append(segment)
    if not stack:
        return "/"
    normalized = "/" + "/".join(stack)
    if directory:
        return normalized + "/"
    return normalized


def split_vfs_path(path: str) -> tuple[str, str]:
    """Split a normalized VFS file path into (virtual_path, file_name).

    virtual_path keeps its trailing slash per the vfs_index schema.
    """
    normalized = normalize_vfs_path(path)
    if normalized == "/":
        raise VfsPathError("Root '/' is a directory, not a file path")
    parent, name = posixpath.split(normalized)
    virtual_path = parent if parent.endswith("/") else parent + "/"
    return virtual_path, name


def is_hidden_within(path: Path, root: Path) -> bool:
    """True when any component of `path` below `root` starts with a dot.

    Used by directory walks (up <folder>, sync, GUI drops) to keep OS junk
    like .DS_Store or .git/ out of the drive. Explicit single-file uploads
    bypass this check.
    """
    return any(part.startswith(".") for part in path.relative_to(root).parts)


def mask_token(token: str) -> str:
    """Mask a secret for CLI display: first 4 + '...' + last 4 characters."""
    if len(token) <= 8:
        return "..."
    return f"{token[:4]}...{token[-4:]}"


def scrub_secrets(text: str) -> str:
    """Replace anything that looks like a Telegram bot token with a placeholder."""
    return _TOKEN_RE.sub("[SCRUBBED_TOKEN]", text)


class SecretScrubberFormatter(logging.Formatter):
    """JSON-lines log formatter that scrubs bot tokens from every record."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub_secrets(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = scrub_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)
