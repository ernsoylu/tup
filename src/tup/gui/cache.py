"""Local download cache mirroring each drive's folder structure.

Layout: `<cache root>/<chat_id>/<virtual folders...>/<file>` — the same tree
the drive has on Telegram, rooted in the hidden `~/.tui` directory (override
with the TUP_CACHE_DIR environment variable, e.g. in tests).
"""

from __future__ import annotations

import os
from pathlib import Path

from tup.database import VfsEntry


def cache_root() -> Path:
    override = os.environ.get("TUP_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path("~/.tui").expanduser()


def cached_path(entry: VfsEntry) -> Path:
    """Where this entry lives (or would live) in the local cache."""
    rel = entry.virtual_path.strip("/")
    base = cache_root() / entry.chat_id
    return (base / rel if rel else base) / entry.file_name


def is_cached(entry: VfsEntry) -> bool:
    """True when the file is fully downloaded (size matches the index)."""
    path = cached_path(entry)
    return path.is_file() and path.stat().st_size == entry.file_size
