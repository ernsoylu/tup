"""GUI model logic: directory tree building, row building, sorting proxy."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from tests._qt import get_qapp
from tup.database import VfsEntry
from tup.gui.models import (
    FileSortProxy,
    FileTableModel,
    build_dir_tree,
    build_rows,
    child_dirs,
    kind_label,
)

HASH = "a1" * 32


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return get_qapp()


def entry(
    virtual_path: str,
    name: str,
    size: int = 10,
    ts: str = "2026-07-19T10:00:00+00:00",
    **extra: object,
) -> VfsEntry:
    return VfsEntry(
        id=1,
        chat_id="-100123",
        virtual_path=virtual_path,
        file_name=name,
        file_size=size,
        file_hash=HASH,
        telegram_file_id="",
        telegram_message_id=1,
        upload_timestamp=ts,
        **extra,  # type: ignore[arg-type]
    )


def test_build_dir_tree_derives_nested_folders() -> None:
    entries = [
        entry("/docs/", "a.pdf"),
        entry("/docs/sub/", "b.pdf"),
        entry("/media/", ".keep"),
        entry("/", "root.txt"),
    ]
    root = build_dir_tree(entries)
    assert set(root.children) == {"docs", "media"}
    assert set(root.children["docs"].children) == {"sub"}
    assert root.children["docs"].children["sub"].path == "/docs/sub/"


def test_child_dirs_immediate_only() -> None:
    entries = [entry("/docs/sub/deep/", "x"), entry("/docs/", "y"), entry("/media/", "z")]
    assert child_dirs(entries, "/") == ["docs", "media"]
    assert child_dirs(entries, "/docs/") == ["sub"]


def test_build_rows_hides_dotfiles_and_synthesizes_folders() -> None:
    entries = [
        entry("/", "root.txt"),
        entry("/docs/", ".keep"),
        entry("/", ".secret"),
    ]
    rows = build_rows(entries, "/")
    assert [(r.name, r.is_dir) for r in rows] == [("docs", True), ("root.txt", False)]

    rows_hidden = build_rows(entries, "/", show_hidden=True)
    assert {r.name for r in rows_hidden} == {"docs", "root.txt", ".secret"}

    docs = build_rows(entries, "/docs/")
    assert docs == []
    docs_hidden = build_rows(entries, "/docs/", show_hidden=True)
    assert [r.name for r in docs_hidden] == [".keep"]


def test_kind_labels() -> None:
    assert kind_label("clip.mp4") == "MP4 file"
    assert kind_label("README") == "File"
    assert kind_label(".keep") == "File"


def test_table_model_display_roles(qapp: QApplication) -> None:
    model = FileTableModel()
    model.set_rows(build_rows([entry("/", "a.txt", size=2048), entry("/docs/", "b.pdf")], "/"))
    assert model.rowCount() == 2  # 'docs' folder + a.txt
    folder_idx = model.index(0, 1)
    assert model.data(folder_idx) == "—"
    name = model.data(model.index(1, 0))
    assert name == "a.txt"
    assert model.data(model.index(1, 1)) == "2.0 kB"
    assert model.headerData(0, Qt.Orientation.Horizontal) == "Name"


def test_sort_proxy_keeps_folders_first_both_orders(qapp: QApplication) -> None:
    model = FileTableModel()
    entries = [
        entry("/", "zzz.txt", size=1),
        entry("/", "aaa.txt", size=100),
        entry("/sub/", "inner.txt"),
    ]
    model.set_rows(build_rows(entries, "/"))
    proxy = FileSortProxy(model)

    proxy.sort(1, Qt.SortOrder.AscendingOrder)  # by size
    names = [proxy.row_at(i).name for i in range(proxy.rowCount())]
    assert names == ["sub", "zzz.txt", "aaa.txt"]

    proxy.sort(1, Qt.SortOrder.DescendingOrder)
    names = [proxy.row_at(i).name for i in range(proxy.rowCount())]
    assert names == ["sub", "aaa.txt", "zzz.txt"]


def test_media_attribute_columns_display_and_sort(qapp: QApplication) -> None:
    from tup.gui.models import format_duration

    assert format_duration(159) == "2:39"
    assert format_duration(3723) == "1:02:03"
    assert format_duration(None) == ""

    model = FileTableModel()
    entries = [
        entry(
            "/",
            "tall.mp4",
            media_kind="video",
            mime_type="video/mp4",
            width=1080,
            height=1920,
            duration=159,
            source_mtime="2026-07-01T09:30:00+00:00",
        ),
        entry("/", "small.jpg", media_kind="photo", width=100, height=100),
    ]
    model.set_rows(build_rows(entries, "/"))
    proxy = FileSortProxy(model)

    tall = next(i for i in range(model.rowCount()) if model.row_at(i).name == "tall.mp4")
    assert model.data(model.index(tall, 2)) == "Video"
    assert model.data(model.index(tall, 3)) == "1080×1920"
    assert model.data(model.index(tall, 4)) == "2:39"
    assert model.data(model.index(tall, 5)) == "2026-07-01 09:30:00"  # source mtime wins

    proxy.sort(3, Qt.SortOrder.DescendingOrder)  # by pixel count
    assert proxy.row_at(0).name == "tall.mp4"
    proxy.sort(4, Qt.SortOrder.AscendingOrder)  # by duration (None → 0 first)
    assert proxy.row_at(0).name == "small.jpg"


def test_sort_proxy_name_filter(qapp: QApplication) -> None:
    model = FileTableModel()
    model.set_rows(build_rows([entry("/", "report.pdf"), entry("/", "notes.txt")], "/"))
    proxy = FileSortProxy(model)
    proxy.setFilterFixedString("rep")
    assert proxy.rowCount() == 1
    assert proxy.row_at(0).name == "report.pdf"
