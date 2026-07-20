"""Local download cache mirroring each drive's folder structure.

Layout: `<cache root>/<chat_id>/<virtual folders...>/<file>` — the same tree
the drive has on Telegram, rooted in tup's `~/.tup` home next to the .env
and registry.db (override with the TUP_CACHE_DIR environment variable).
"""

from __future__ import annotations

import os
from pathlib import Path

from tup.config import config_dir
from tup.database import VfsEntry


def cache_root() -> Path:
    override = os.environ.get("TUP_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return config_dir()


def cached_path(entry: VfsEntry) -> Path:
    """Where this entry lives (or would live) in the local cache."""
    rel = entry.virtual_path.strip("/")
    base = cache_root() / entry.chat_id
    return (base / rel if rel else base) / entry.file_name


def is_cached(entry: VfsEntry) -> bool:
    """True when the file is fully downloaded.

    Downloads land atomically (.part rename), so a file that exists is
    complete. Photos are re-encoded server-side by Telegram, so the
    downloaded size never matches the original upload's recorded size —
    existence alone decides for them; other kinds keep the strict check.
    """
    path = cached_path(entry)
    if not path.is_file():
        return False
    return entry.media_kind == "photo" or path.stat().st_size == entry.file_size


def evict(entry: VfsEntry) -> bool:
    """Delete the local copy only — the file stays on Telegram.

    Returns True when a cached file was actually removed.
    """
    try:
        cached_path(entry).unlink()
        return True
    except FileNotFoundError:
        return False
