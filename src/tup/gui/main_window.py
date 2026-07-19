"""Main explorer window: drive selector, folder tree, sortable file views."""

from __future__ import annotations

from PyQt6.QtCore import QModelIndex, QSize, Qt
from PyQt6.QtGui import QAction, QActionGroup, QCloseEvent, QIcon, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHeaderView,
    QLineEdit,
    QListView,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTableView,
    QToolBar,
    QTreeView,
)

from tup.database import ChatAlias, VfsEntry
from tup.gui.bridge import CoreBridge
from tup.gui.models import (
    DirNode,
    FileRow,
    FileSortProxy,
    FileTableModel,
    build_dir_tree,
    build_rows,
    human_size,
)
from tup.utils import VfsPathError, normalize_vfs_path

PATH_ROLE = Qt.ItemDataRole.UserRole + 1


def _plain(text: str) -> str:
    """Strip rich markup from hints that were written for the CLI."""
    return text.replace("[bold]", "").replace("[/bold]", "")


class MainWindow(QMainWindow):
    """Explorer over a drive's vfs_index: tree on the left, files on the right."""

    def __init__(self, bridge: CoreBridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.entries: list[VfsEntry] = []
        self.current_dir = "/"
        self.show_hidden = False
        self.suppress_dialogs = False  # tests flip this to avoid modal boxes

        self.setWindowTitle("tup — Telegram Drive")
        self.resize(1150, 720)
        style = self.style()
        if style is not None:
            self._folder_icon = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
            self._file_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        else:  # pragma: no cover - style always exists in a real QApplication
            self._folder_icon = QIcon()
            self._file_icon = QIcon()

        self._build_body()
        self._build_toolbar()
        self._status("Loading drives…")
        self._reload_drives()

    # -- UI construction -------------------------------------------------------

    def _build_body(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        self.tree_view = QTreeView()
        self.tree_model = QStandardItemModel(self)
        self.tree_view.setModel(self.tree_model)
        self.tree_view.setHeaderHidden(True)
        self.tree_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tree_selection = self.tree_view.selectionModel()
        if tree_selection is not None:
            tree_selection.currentChanged.connect(self._on_tree_current)
        splitter.addWidget(self.tree_view)

        self.file_model = FileTableModel(self._folder_icon, self._file_icon, self)
        self.file_proxy = FileSortProxy(self.file_model, self)

        self.table_view = QTableView()
        self.table_view.setModel(self.file_proxy)
        self.table_view.setSortingEnabled(True)
        self.table_view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_view.setShowGrid(False)
        vertical_header = self.table_view.verticalHeader()
        if vertical_header is not None:
            vertical_header.setVisible(False)
        horizontal_header = self.table_view.horizontalHeader()
        if horizontal_header is not None:
            horizontal_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            horizontal_header.setStretchLastSection(False)
        self.table_view.doubleClicked.connect(self._on_double_clicked)

        self.icon_view = QListView()
        self.icon_view.setModel(self.file_proxy)
        self.icon_view.setViewMode(QListView.ViewMode.IconMode)
        self.icon_view.setIconSize(QSize(48, 48))
        self.icon_view.setGridSize(QSize(120, 96))
        self.icon_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.icon_view.setWordWrap(True)
        self.icon_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.icon_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.icon_view.doubleClicked.connect(self._on_double_clicked)

        self.views = QStackedWidget()
        self.views.addWidget(self.table_view)
        self.views.addWidget(self.icon_view)
        splitter.addWidget(self.views)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([260, 890])
        self.setCentralWidget(splitter)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.drive_combo = QComboBox()
        self.drive_combo.setMinimumWidth(200)
        self.drive_combo.setToolTip("Drive (Telegram chat)")
        self.drive_combo.currentIndexChanged.connect(self._on_drive_changed)
        toolbar.addWidget(self.drive_combo)

        self.up_action = QAction("⬆ Up", self)
        self.up_action.triggered.connect(self._go_up)
        toolbar.addAction(self.up_action)

        self.path_edit = QLineEdit("/")
        self.path_edit.setToolTip("Virtual path — press Enter to navigate")
        self.path_edit.returnPressed.connect(self._on_path_entered)
        toolbar.addWidget(self.path_edit)

        self.refresh_action = QAction("⟳ Refresh", self)
        self.refresh_action.triggered.connect(lambda: self.refresh())
        toolbar.addAction(self.refresh_action)

        view_group = QActionGroup(self)
        self.details_action = QAction("Details", self)
        self.details_action.setCheckable(True)
        self.details_action.setChecked(True)
        self.details_action.triggered.connect(lambda: self.views.setCurrentIndex(0))
        self.icons_action = QAction("Icons", self)
        self.icons_action.setCheckable(True)
        self.icons_action.triggered.connect(lambda: self.views.setCurrentIndex(1))
        view_group.addAction(self.details_action)
        view_group.addAction(self.icons_action)
        toolbar.addAction(self.details_action)
        toolbar.addAction(self.icons_action)

        self.hidden_action = QAction("Show hidden", self)
        self.hidden_action.setCheckable(True)
        self.hidden_action.toggled.connect(self._on_hidden_toggled)
        toolbar.addAction(self.hidden_action)

    # -- drives ----------------------------------------------------------------

    def _reload_drives(self) -> None:
        async def _fetch() -> list[ChatAlias]:
            return await self.bridge.db.alias_list()

        self.bridge.submit(_fetch(), self._on_drives, self._show_error)

    def _on_drives(self, aliases: list[ChatAlias]) -> None:
        default = self.bridge.settings.default_chat_id
        self.drive_combo.blockSignals(True)
        self.drive_combo.clear()
        for alias in aliases:
            label = f"{alias.alias} — {alias.title}" if alias.title else alias.alias
            self.drive_combo.addItem(label, alias.chat_id)
        if default and not any(default in (a.alias, a.chat_id) for a in aliases):
            self.drive_combo.addItem(f"default — {default}", default)
        if default:
            for i, alias in enumerate(aliases):
                if default in (alias.alias, alias.chat_id):
                    self.drive_combo.setCurrentIndex(i)
                    break
        self.drive_combo.blockSignals(False)
        if self.drive_combo.count() > 0:
            self._on_drive_changed(self.drive_combo.currentIndex())
        else:
            self._status("No drives yet — add one with `tup chat add <alias> <chat_id>`.")

    def _current_chat_id(self) -> str | None:
        data = self.drive_combo.currentData()
        return str(data) if data is not None else None

    def _on_drive_changed(self, index: int) -> None:
        if index < 0:
            return
        self.current_dir = "/"
        self.refresh()

    # -- data loading ----------------------------------------------------------

    def refresh(self) -> None:
        chat_id = self._current_chat_id()
        if chat_id is None:
            return
        self._status("Loading…")

        async def _fetch() -> list[VfsEntry]:
            return await self.bridge.db.vfs_list_prefix(chat_id, "/")

        self.bridge.submit(_fetch(), self._on_entries, self._show_error)

    def _on_entries(self, entries: list[VfsEntry]) -> None:
        self.entries = entries
        self._rebuild_tree()
        target = self.current_dir if self._dir_exists(self.current_dir) else "/"
        self.set_current_dir(target)

    def _dir_exists(self, path: str) -> bool:
        if path == "/":
            return True
        return any(e.virtual_path.startswith(path) for e in self.entries)

    def _rebuild_tree(self) -> None:
        root_node = build_dir_tree(self.entries)
        self.tree_model.clear()
        root_item = QStandardItem("/")
        root_item.setData("/", PATH_ROLE)
        root_item.setEditable(False)
        root_item.setIcon(self._folder_icon)
        self.tree_model.appendRow(root_item)
        self._append_children(root_item, root_node)
        self.tree_view.expandToDepth(1)

    def _append_children(self, parent_item: QStandardItem, node: DirNode) -> None:
        for name in sorted(node.children):
            child = node.children[name]
            item = QStandardItem(name)
            item.setData(child.path, PATH_ROLE)
            item.setEditable(False)
            item.setIcon(self._folder_icon)
            parent_item.appendRow(item)
            self._append_children(item, child)

    # -- navigation ------------------------------------------------------------

    def set_current_dir(self, path: str) -> None:
        self.current_dir = path
        self.path_edit.setText(path)
        rows = build_rows(self.entries, path, show_hidden=self.show_hidden)
        self.file_model.set_rows(rows)
        folders = sum(1 for r in rows if r.is_dir)
        files = len(rows) - folders
        total = sum(r.size for r in rows if not r.is_dir)
        self._status(f"{folders} folder(s), {files} file(s), {human_size(total)}")
        self._select_tree_path(path)

    def _select_tree_path(self, path: str) -> None:
        item = self._find_tree_item(path)
        if item is None:
            return
        self.tree_view.setCurrentIndex(self.tree_model.indexFromItem(item))

    def _find_tree_item(self, path: str) -> QStandardItem | None:
        item = self.tree_model.item(0)
        if item is None or path == "/":
            return item
        for part in (p for p in path.split("/") if p):
            found: QStandardItem | None = None
            for i in range(item.rowCount()):
                child = item.child(i)
                if child is not None and child.text() == part:
                    found = child
                    break
            if found is None:
                return None
            item = found
        return item

    def _on_tree_current(self, current: QModelIndex, _previous: QModelIndex) -> None:
        path = current.data(PATH_ROLE)
        if isinstance(path, str) and path != self.current_dir:
            self.set_current_dir(path)

    def _on_double_clicked(self, index: QModelIndex) -> None:
        source_index = self.file_proxy.mapToSource(index)
        row = self.file_model.row_at(source_index.row())
        if row.is_dir:
            base = self.current_dir if self.current_dir.endswith("/") else self.current_dir + "/"
            self.set_current_dir(base + row.name + "/")
        else:
            self.open_row(row)

    def open_row(self, row: FileRow) -> None:
        # Phase 2 wires this to download-and-open; browsing-only for now.
        self._status(f"{row.name}: download-on-open arrives in the next update.")

    def _go_up(self) -> None:
        if self.current_dir == "/":
            return
        parent = self.current_dir.rstrip("/").rsplit("/", 1)[0] + "/"
        self.set_current_dir(parent if parent != "//" else "/")

    def _on_path_entered(self) -> None:
        try:
            path = normalize_vfs_path(self.path_edit.text() or "/", directory=True)
        except VfsPathError as exc:
            self._status(str(exc))
            return
        if self._dir_exists(path):
            self.set_current_dir(path)
        else:
            self._status(f"No such folder: {path}")

    def _on_hidden_toggled(self, checked: bool) -> None:
        self.show_hidden = checked
        self.set_current_dir(self.current_dir)

    # -- feedback --------------------------------------------------------------

    def _status(self, message: str) -> None:
        bar = self.statusBar()
        if bar is not None:
            bar.showMessage(message)

    def _show_error(self, exc: BaseException) -> None:
        hint = getattr(exc, "hint", None)
        text = str(exc)
        if hint:
            text += f"\n\n💡 {hint}"
        self._status(str(exc))
        if not self.suppress_dialogs:
            QMessageBox.critical(self, "tup — error", _plain(text))

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        self.bridge.stop()
        super().closeEvent(a0)
