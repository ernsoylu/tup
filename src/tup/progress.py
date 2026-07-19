"""Rich progress UI and a file-like reader that reports upload progress."""

from __future__ import annotations

from typing import IO

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

console = Console()
error_console = Console(stderr=True)


def make_progress(*, transient: bool = False) -> Progress:
    """Standard tup transfer progress bar."""
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=transient,
    )


class ProgressFileReader:
    """Wraps a binary file object, advancing a rich progress task on read().

    Only the file-object surface PTB/httpx actually use is implemented.
    """

    def __init__(self, fileobj: IO[bytes], progress: Progress, task_id: TaskID, name: str) -> None:
        self._fileobj = fileobj
        self._progress = progress
        self._task_id = task_id
        self.name = name

    def read(self, size: int = -1) -> bytes:
        chunk = self._fileobj.read(size)
        if chunk:
            self._progress.advance(self._task_id, len(chunk))
        return chunk

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._fileobj.seek(offset, whence)

    def tell(self) -> int:
        return self._fileobj.tell()

    def close(self) -> None:
        self._fileobj.close()
