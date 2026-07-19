"""Unit tests: MIME routing, hashing, VFS path normalization, secret scrubbing."""

from __future__ import annotations

import hashlib
import io
import json
import logging
from pathlib import Path

import pytest

from tup.progress import ProgressFileReader, make_progress
from tup.utils import (
    SecretScrubberFormatter,
    VfsPathError,
    detect_mime,
    mask_token,
    normalize_vfs_path,
    scrub_secrets,
    sha256_file,
    split_vfs_path,
)

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300
FAKE_TOKEN = "123456789:AAEexampleexampleexampleexample12345"  # noqa: S105


# --- MIME detection & routing -------------------------------------------------


def test_magic_bytes_png_routes_as_photo(tmp_path: Path) -> None:
    f = tmp_path / "image.dat"  # wrong extension on purpose: magic bytes must win
    f.write_bytes(PNG_HEADER)
    mime, kind = detect_mime(f)
    assert mime == "image/png"
    assert kind == "photo"


def test_unknown_magic_falls_back_to_mimetypes(tmp_path: Path) -> None:
    f = tmp_path / "notes.txt"
    f.write_bytes(b"just plain text, no magic bytes")
    mime, kind = detect_mime(f)
    assert mime == "text/plain"
    assert kind == "document"


def test_mp3_extension_fallback_routes_as_audio(tmp_path: Path) -> None:
    f = tmp_path / "song.mp3"
    f.write_bytes(b"\x00" * 32)  # no valid magic header
    mime, kind = detect_mime(f)
    assert mime == "audio/mpeg"
    assert kind == "audio"


def test_totally_unknown_defaults_to_octet_stream(tmp_path: Path) -> None:
    f = tmp_path / "mystery.zzz"
    f.write_bytes(b"\x00\x01\x02\x03")
    mime, kind = detect_mime(f)
    assert mime == "application/octet-stream"
    assert kind == "document"


def test_svg_routes_as_document(tmp_path: Path) -> None:
    f = tmp_path / "logo.svg"
    f.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    _mime, kind = detect_mime(f)
    assert kind == "document"


# --- SHA-256 ------------------------------------------------------------------


def test_sha256_known_vector(tmp_path: Path) -> None:
    f = tmp_path / "hello.txt"
    f.write_bytes(b"hello world")
    assert sha256_file(f) == hashlib.sha256(b"hello world").hexdigest()
    assert sha256_file(f) == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_sha256_streams_large_file(tmp_path: Path) -> None:
    data = b"x" * (3 * 1024 * 1024 + 17)
    f = tmp_path / "big.bin"
    f.write_bytes(data)
    assert sha256_file(f) == hashlib.sha256(data).hexdigest()


# --- VFS path normalization ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "directory", "expected"),
    [
        ("/", True, "/"),
        ("/", False, "/"),
        ("docs", True, "/docs/"),
        ("/docs/", True, "/docs/"),
        ("/docs//sub/./", True, "/docs/sub/"),
        ("/docs/file.pdf", False, "/docs/file.pdf"),
        ("docs/file.pdf", False, "/docs/file.pdf"),
        ("/a/b/../c/file.txt", False, "/a/c/file.txt"),
        ("/a/./b/", True, "/a/b/"),
    ],
)
def test_normalize_vfs_path(raw: str, directory: bool, expected: str) -> None:
    assert normalize_vfs_path(raw, directory=directory) == expected


@pytest.mark.parametrize("raw", ["", "   ", "/../etc/passwd", "/a/../../b"])
def test_normalize_rejects_bad_paths(raw: str) -> None:
    with pytest.raises(VfsPathError):
        normalize_vfs_path(raw)


def test_split_vfs_path() -> None:
    assert split_vfs_path("/docs/file.pdf") == ("/docs/", "file.pdf")
    assert split_vfs_path("file.pdf") == ("/", "file.pdf")
    with pytest.raises(VfsPathError):
        split_vfs_path("/")


# --- Secret scrubbing ---------------------------------------------------------


def test_scrub_secrets_removes_tokens() -> None:
    text = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendDocument failed"
    scrubbed = scrub_secrets(text)
    assert FAKE_TOKEN not in scrubbed
    assert "[SCRUBBED_TOKEN]" in scrubbed


def test_formatter_emits_scrubbed_json() -> None:
    formatter = SecretScrubberFormatter()
    record = logging.LogRecord(
        name="tup.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="upload failed for bot %s",
        args=(FAKE_TOKEN,),
        exc_info=None,
    )
    line = formatter.format(record)
    payload = json.loads(line)
    assert FAKE_TOKEN not in line
    assert payload["level"] == "ERROR"
    assert "[SCRUBBED_TOKEN]" in payload["message"]


def test_mask_token() -> None:
    assert mask_token(FAKE_TOKEN) == f"{FAKE_TOKEN[:4]}...{FAKE_TOKEN[-4:]}"
    assert mask_token("short") == "..."


# --- ProgressFileReader -------------------------------------------------------


def test_progress_file_reader_advances_task() -> None:
    progress = make_progress(transient=True)
    data = b"a" * 1000
    with progress:
        task_id = progress.add_task("test", total=len(data))
        reader = ProgressFileReader(io.BytesIO(data), progress, task_id, "test.bin")
        chunks = []
        while chunk := reader.read(256):
            chunks.append(chunk)
        assert b"".join(chunks) == data
        assert progress.tasks[0].completed == len(data)
