"""Local download cache mirroring each drive's folder structure.

Layout: `<cache root>/<chat_id>/<virtual folders...>/<file>` — the same tree
the drive has on Telegram, rooted in tup's `~/.tup` home next to the .env
and registry.db (override with the TUP_CACHE_DIR environment variable).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from tup.config import config_dir
from tup.database import VfsEntry
from tup.utils import fallback_kind


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
    kind = fallback_kind(entry.file_name, entry.media_kind)
    return kind == "photo" or path.stat().st_size == entry.file_size


def evict(entry: VfsEntry) -> bool:
    """Delete the local copy only — the file stays on Telegram.

    Returns True when a cached file was actually removed.
    """
    try:
        cached_path(entry).unlink()
        return True
    except FileNotFoundError:
        return False


def touch(entry: VfsEntry) -> None:
    """Refresh the cached file's mtime so recently-opened files survive sweeps."""
    path = cached_path(entry)
    if path.is_file():
        path.touch()


_DRIVE_DIR_RE = re.compile(r"^-?\d+$")  # per-drive cache dirs are named by chat id


def sweep(ttl_seconds: float) -> int:
    """Evict stale downloads: partial .part files always, and cached files not
    touched within `ttl_seconds` (opening a file refreshes its mtime, so this
    is LRU-by-last-access — same policy as tup-cloud's server cache sweeper).
    Empty directories are removed. Returns the number of files deleted.

    Only per-drive directories (`<cache root>/<chat_id>/…`) are swept: the
    cache root is tup's home, which also holds .env, registry.db, bin/, and
    the MTProto session — those must never be touched.
    """
    root = cache_root()
    if not root.is_dir():
        return 0
    cutoff = time.time() - ttl_seconds
    removed = 0
    for drive_dir in root.iterdir():
        if not drive_dir.is_dir() or not _DRIVE_DIR_RE.match(drive_dir.name):
            continue
        # Reverse sort visits children before their parent directories.
        for path in sorted(drive_dir.rglob("*"), reverse=True):
            try:
                if path.is_file():
                    if path.suffix == ".part" or path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:  # pragma: no cover - racing deletes are fine to skip
                continue
    return removed
