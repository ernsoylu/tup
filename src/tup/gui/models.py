"""Folder-tree/file-listing logic and the Qt models behind the explorer views."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from PyQt6.QtCore import (
    QAbstractTableModel,
    QMimeData,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
)
from PyQt6.QtGui import QIcon, QStandardItem, QStandardItemModel
from rich.filesize import decimal as human_size

from tup.database import VfsEntry
from tup.utils import fallback_kind

__all__ = [
    "COLUMNS",
    "INTERNAL_MIME",
    "PATH_ROLE",
    "DirNode",
    "FileRow",
    "FileSortProxy",
    "FileTableModel",
    "all_dir_paths",
    "build_dir_model",
    "build_dir_tree",
    "build_rows",
    "child_dirs",
    "human_size",
]

# Last column is the residency indicator (✓ = downloaded); header stays blank.
COLUMNS = ("Name", "Size", "Kind", "Tags", "Dimensions", "Duration", "Modified", "")

PATH_ROLE = Qt.ItemDataRole.UserRole + 1

# Internal drag payload: JSON list of file names from the current listing.
INTERNAL_MIME = "application/x-tup-files"


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@dataclass
class DirNode:
    """One virtual directory; children keyed by segment name."""

    name: str
    path: str  # normalized with trailing slash, e.g. '/docs/sub/'
    children: dict[str, DirNode] = field(default_factory=dict)


def build_dir_tree(entries: list[VfsEntry], *, include_hidden: bool = False) -> DirNode:
    """Derive the full folder tree from every entry's virtual_path.

    Dot-directories (notably the /.Trash/ Recycle Bin) are excluded unless
    `include_hidden` — the bin gets its own dedicated sidebar node instead.
    """
    root = DirNode(name="/", path="/")
    for entry in entries:
        node = root
        current = "/"
        for part in (p for p in entry.virtual_path.split("/") if p):
            if not include_hidden and part.startswith("."):
                break
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


def build_dir_model(
    entries: list[VfsEntry], folder_icon: QIcon, parent: QObject | None = None
) -> QStandardItemModel:
    """Folder tree as a QStandardItemModel: root '/' item, paths in PATH_ROLE.

    Shared by the sidebar tree and the Copy/Move folder picker so both always
    show the same structure.
    """
    model = QStandardItemModel(parent)
    root_item = QStandardItem("/")
    root_item.setData("/", PATH_ROLE)
    root_item.setEditable(False)
    root_item.setIcon(folder_icon)
    model.appendRow(root_item)

    def append_children(parent_item: QStandardItem, node: DirNode) -> None:
        for name in sorted(node.children):
            child = node.children[name]
            item = QStandardItem(name)
            item.setData(child.path, PATH_ROLE)
            item.setEditable(False)
            item.setIcon(folder_icon)
            parent_item.appendRow(item)
            append_children(item, child)

    append_children(root_item, build_dir_tree(entries))
    return model


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
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    tags: str = ""  # space-separated, no '#'

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)


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
        if entry.media_kind:
            kind = entry.media_kind.capitalize()
        else:
            # Legacy rows carry no media_kind; infer from the extension so
            # photos and videos imported before v2 still render as media.
            inferred = fallback_kind(entry.file_name, "")
            kind = (
                inferred.capitalize()
                if inferred in ("photo", "video", "audio")
                else kind_label(entry.file_name)
            )
        modified = entry.source_mtime or entry.upload_timestamp
        rows.append(
            FileRow(
                name=entry.file_name,
                is_dir=False,
                size=entry.file_size,
                kind=kind,
                modified=modified[:19].replace("T", " "),
                entry=entry,
                downloaded=bool(is_downloaded and is_downloaded(entry)),
                width=entry.width,
                height=entry.height,
                duration=entry.duration,
                tags=entry.tags,
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
        *,
        kind_icons: dict[str, QIcon] | None = None,
    ) -> None:
        super().__init__(parent)
        self._rows: list[FileRow] = []
        self._folder_icon = folder_icon or QIcon()
        self._file_icon = file_icon or QIcon()
        self._kind_icons = kind_icons or {}  # keyed by media_kind: video/audio/photo

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
                # Folders show nothing: blank cells scan better than filler dashes.
                return "" if row.is_dir else human_size(row.size)
            if column == 2:
                return "" if row.is_dir else row.kind
            if column == 3:
                return " ".join(f"#{t}" for t in row.tags.split()) if row.tags else ""
            if column == 4:
                return f"{row.width}×{row.height}" if row.width and row.height else ""
            if column == 5:
                return format_duration(row.duration)
            if column == 6:
                return row.modified
            if column == 7:
                return "✓" if row.downloaded else ""
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == 7 and row.downloaded:
                return "Downloaded — stored in the local cache"
            if row.entry is not None and row.entry.user_caption:
                return row.entry.user_caption
        if role == Qt.ItemDataRole.DecorationRole and column == 0:
            if row.is_dir:
                return self._folder_icon
            kind = (
                fallback_kind(row.entry.file_name, row.entry.media_kind)
                if row.entry is not None
                else ""
            )
            return self._kind_icons.get(kind, self._file_icon)
        return None

    # -- internal drag support (file rows only; folders cannot be moved) --------

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.isValid() and 0 <= index.row() < len(self._rows):
            if not self._rows[index.row()].is_dir:
                flags |= Qt.ItemFlag.ItemIsDragEnabled
        return flags

    def mimeTypes(self) -> list[str]:
        return [INTERNAL_MIME]

    def mimeData(self, indexes: Iterable[QModelIndex]) -> QMimeData | None:
        names = sorted(
            {
                self._rows[i.row()].name
                for i in indexes
                if i.isValid() and not self._rows[i.row()].is_dir
            }
        )
        if not names:
            return None
        data = QMimeData()
        data.setData(INTERNAL_MIME, json.dumps(names).encode())
        return data

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction


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
        if column == 3 and a.tags != b.tags:
            return a.tags < b.tags
        if column == 4 and a.pixels != b.pixels:
            return a.pixels < b.pixels
        if column == 5 and (a.duration or 0) != (b.duration or 0):
            return (a.duration or 0) < (b.duration or 0)
        if column == 6 and a.modified != b.modified:
            return a.modified < b.modified
        if column == 7 and a.downloaded != b.downloaded:
            return b.downloaded
        return a.name.lower() < b.name.lower()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        """The filter box matches names AND tags (type '#tag' or just the tag)."""
        pattern = self.filterRegularExpression().pattern()
        if not pattern:
            return True
        if super().filterAcceptsRow(source_row, source_parent):
            return True
        row = self._source.row_at(source_row)
        return pattern.lstrip("\\#").lower() in row.tags.lower()
