"""Folder-tree/file-listing logic and the Qt models behind the explorer views."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QObject, QSortFilterProxyModel, Qt
from PyQt6.QtGui import QIcon
from rich.filesize import decimal as human_size

from tup.database import VfsEntry

__all__ = [
    "COLUMNS",
    "DirNode",
    "FileRow",
    "FileSortProxy",
    "FileTableModel",
    "all_dir_paths",
    "build_dir_tree",
    "build_rows",
    "child_dirs",
    "human_size",
]

COLUMNS = ("Name", "Size", "Kind", "Modified", "Status")


@dataclass
class DirNode:
    """One virtual directory; children keyed by segment name."""

    name: str
    path: str  # normalized with trailing slash, e.g. '/docs/sub/'
    children: dict[str, DirNode] = field(default_factory=dict)


def build_dir_tree(entries: list[VfsEntry]) -> DirNode:
    """Derive the full folder tree from every entry's virtual_path."""
    root = DirNode(name="/", path="/")
    for entry in entries:
        node = root
        current = "/"
        for part in (p for p in entry.virtual_path.split("/") if p):
            current += part + "/"
            node = node.children.setdefault(part, DirNode(name=part, path=current))
    return root


def child_dirs(entries: list[VfsEntry], base: str) -> list[str]:
    """Immediate subdirectory names of `base`, derived from deeper entries."""
    children: set[str] = set()
    for entry in entries:
        if entry.virtual_path == base or not entry.virtual_path.startswith(base):
            continue
        children.add(entry.virtual_path[len(base) :].split("/", 1)[0])
    return sorted(children)


def all_dir_paths(entries: list[VfsEntry]) -> list[str]:
    """Every directory path in the drive, root first, depth order."""
    paths: set[str] = {"/"}

    def walk(node: DirNode) -> None:
        for child in node.children.values():
            paths.add(child.path)
            walk(child)

    walk(build_dir_tree(entries))
    return sorted(paths)


def kind_label(name: str) -> str:
    stem, dot, suffix = name.rpartition(".")
    if dot and stem and suffix:
        return f"{suffix.upper()} file"
    return "File"


@dataclass(frozen=True)
class FileRow:
    """One row in the file panel: either a folder or an indexed file."""

    name: str
    is_dir: bool
    size: int  # -1 for folders
    kind: str
    modified: str  # ISO prefix 'YYYY-MM-DD HH:MM:SS' (sortable), '' for folders
    entry: VfsEntry | None = None
    downloaded: bool = False


def build_rows(
    entries: list[VfsEntry],
    dir_path: str,
    *,
    show_hidden: bool = False,
    is_downloaded: Callable[[VfsEntry], bool] | None = None,
) -> list[FileRow]:
    """Folders + files directly inside `dir_path`, honoring the hidden toggle."""
    rows = [
        FileRow(name=name, is_dir=True, size=-1, kind="Folder", modified="")
        for name in child_dirs(entries, dir_path)
        if show_hidden or not name.startswith(".")
    ]
    for entry in entries:
        if entry.virtual_path != dir_path:
            continue
        if not show_hidden and entry.file_name.startswith("."):
            continue
        rows.append(
            FileRow(
                name=entry.file_name,
                is_dir=False,
                size=entry.file_size,
                kind=kind_label(entry.file_name),
                modified=entry.upload_timestamp[:19].replace("T", " "),
                entry=entry,
                downloaded=bool(is_downloaded and is_downloaded(entry)),
            )
        )
    return rows


class FileTableModel(QAbstractTableModel):
    """Read-only table of FileRows; the same model backs details and icon views."""

    def __init__(
        self,
        folder_icon: QIcon | None = None,
        file_icon: QIcon | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._rows: list[FileRow] = []
        self._folder_icon = folder_icon or QIcon()
        self._file_icon = file_icon or QIcon()

    def set_rows(self, rows: list[FileRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, row: int) -> FileRow:
        return self._rows[row]

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return row.name
            if column == 1:
                return "—" if row.is_dir else human_size(row.size)
            if column == 2:
                return row.kind
            if column == 3:
                return row.modified
            if column == 4:
                return "✓ Downloaded" if row.downloaded else ""
        if role == Qt.ItemDataRole.DecorationRole and column == 0:
            return self._folder_icon if row.is_dir else self._file_icon
        return None


class FileSortProxy(QSortFilterProxyModel):
    """Folders-first sorting with type-aware keys, plus a name filter."""

    def __init__(self, source: FileTableModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._source = source
        self.setSourceModel(source)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterKeyColumn(0)

    def row_at(self, proxy_row: int) -> FileRow:
        source_index = self.mapToSource(self.index(proxy_row, 0))
        return self._source.row_at(source_index.row())

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        a = self._source.row_at(left.row())
        b = self._source.row_at(right.row())
        if a.is_dir != b.is_dir:
            # Folders stay on top in both sort orders (Qt inverts descending).
            ascending = self.sortOrder() == Qt.SortOrder.AscendingOrder
            return a.is_dir if ascending else not a.is_dir
        column = left.column()
        if column == 1 and a.size != b.size:
            return a.size < b.size
        if column == 2 and a.kind.lower() != b.kind.lower():
            return a.kind.lower() < b.kind.lower()
        if column == 3 and a.modified != b.modified:
            return a.modified < b.modified
        if column == 4 and a.downloaded != b.downloaded:
            return b.downloaded
        return a.name.lower() < b.name.lower()
