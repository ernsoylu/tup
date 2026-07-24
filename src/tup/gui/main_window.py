"""Main explorer window: drive selector, folder tree, sortable file views."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import (
    QByteArray,
    QItemSelectionModel,
    QModelIndex,
    QPoint,
    QSize,
    Qt,
    QTimer,
    QUrl,
)
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QIcon,
    QStandardItem,
    QStandardItemModel,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTableView,
    QToolBar,
    QToolButton,
    QTreeView,
)

from tup.database import ChatAlias, UploadLogEntry, VfsEntry
from tup.gui.bridge import CoreBridge
from tup.gui.cache import cached_path, evict, is_cached
from tup.gui.cache import sweep as sweep_cache
from tup.gui.dialogs import ChatsDialog, FolderPickerDialog, LogsDialog
from tup.gui.models import (
    INTERNAL_MIME,
    PATH_ROLE,
    FileRow,
    FileSortProxy,
    FileTableModel,
    build_dir_model,
    build_rows,
    human_size,
)
from tup.gui.ops import op_cp, op_mkdir, op_mv, op_prune, op_retry_failed, op_rmdir
from tup.gui.transfers import Transfer, TransferManager, collect_upload_targets
from tup.gui.transfers_panel import TransfersPanel
from tup.uploader import download_media_file, upload_file
from tup.utils import VfsPathError, normalize_vfs_path
from tup.vfs_ops import (
    TRASH_PREFIX,
    is_trashed,
    op_empty_trash,
    op_purge,
    op_restore,
    op_set_caption,
    op_trash,
)

logger = logging.getLogger("tup.gui")


def _accept_drag(event: QDragEnterEvent | QDragMoveEvent | None) -> None:
    """Accept OS file drags and internal row drags alike."""
    if event is None:
        return
    mime = event.mimeData()
    if mime is not None and (mime.hasUrls() or mime.hasFormat(INTERNAL_MIME)):
        event.acceptProposedAction()


def _enable_drops(view: QAbstractItemView) -> None:
    view.setAcceptDrops(True)
    viewport = view.viewport()
    if viewport is not None:
        viewport.setAcceptDrops(True)
    view.setDropIndicatorShown(True)


class _DropTableView(QTableView):
    """Details view: OS drops upload; internal drags move/copy between folders."""

    def __init__(self, owner: MainWindow) -> None:
        super().__init__()
        self._owner = owner
        _enable_drops(self)
        self.setDragEnabled(True)

    def dragEnterEvent(self, e: QDragEnterEvent | None) -> None:
        _accept_drag(e)

    def dragMoveEvent(self, e: QDragMoveEvent | None) -> None:
        _accept_drag(e)

    def dropEvent(self, e: QDropEvent | None) -> None:
        self._owner.handle_view_drop(e, self)


class _DropListView(QListView):
    """Icon view: OS drops upload; internal drags move/copy between folders."""

    def __init__(self, owner: MainWindow) -> None:
        super().__init__()
        self._owner = owner
        _enable_drops(self)
        self.setDragEnabled(True)
        self.setMovement(QListView.Movement.Static)  # no free icon rearranging

    def dragEnterEvent(self, e: QDragEnterEvent | None) -> None:
        _accept_drag(e)

    def dragMoveEvent(self, e: QDragMoveEvent | None) -> None:
        _accept_drag(e)

    def dropEvent(self, e: QDropEvent | None) -> None:
        self._owner.handle_view_drop(e, self)


class _SidebarTree(QTreeView):
    """Folder sidebar: accepts file rows (move/copy) and OS files (upload)."""

    def __init__(self, owner: MainWindow) -> None:
        super().__init__()
        self._owner = owner
        _enable_drops(self)

    def dragEnterEvent(self, e: QDragEnterEvent | None) -> None:
        _accept_drag(e)

    def dragMoveEvent(self, e: QDragMoveEvent | None) -> None:
        _accept_drag(e)

    def dropEvent(self, e: QDropEvent | None) -> None:
        self._owner.handle_tree_drop(e, self)


def _entry_key(entry: VfsEntry) -> tuple[str, str, str]:
    """Identity of a file for download dedup: one live transfer per drive path."""
    return (entry.chat_id, entry.virtual_path, entry.file_name)


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
        self.open_files_externally = True  # tests flip this to avoid launching apps

        self.setWindowTitle("tup — Telegram Drive")
        self.resize(1150, 720)
        style = self.style()
        if style is not None:
            sp = QStyle.StandardPixmap
            self._folder_icon = style.standardIcon(sp.SP_DirIcon)
            self._file_icon = style.standardIcon(sp.SP_FileIcon)
            self._kind_icons = {
                "video": style.standardIcon(sp.SP_MediaPlay),
                "audio": style.standardIcon(sp.SP_MediaVolume),
                "photo": style.standardIcon(sp.SP_FileDialogContentsView),
            }
        else:  # pragma: no cover - style always exists in a real QApplication
            self._folder_icon = QIcon()
            self._file_icon = QIcon()
            self._kind_icons = {}

        self._pending_opens: dict[int, Path] = {}
        # One live download per file: entry key -> transfer id (None until queued).
        self._downloads_in_flight: dict[tuple[str, str, str], int | None] = {}
        self._download_keys: dict[int, tuple[str, str, str]] = {}
        self._have_loaded = False  # lets _on_entries skip no-op refreshes
        self.transfers = TransferManager(self._on_transfer_update_from_loop)

        self._build_body()
        self._build_toolbar()
        self._build_transfers_dock()
        self._status("Loading drives…")
        self.bridge.submit(self._start_transfers())
        self.bridge.submit(self._sweep_cache())
        self._reload_drives()

        # Keep the view fresh when the CLI (or another device) writes the same
        # SQLite index. _on_entries drops unchanged results, so idle polls are
        # cheap and never disturb selection or scroll position.
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setInterval(30_000)
        self._auto_refresh_timer.timeout.connect(self._auto_refresh)
        self._auto_refresh_timer.start()

    async def _start_transfers(self) -> None:
        # create_task needs a running loop, so the worker starts on the bridge.
        self.transfers.start()

    async def _sweep_cache(self) -> None:
        """Startup eviction of stale cached downloads (LRU by last open)."""
        ttl_seconds = self.bridge.settings.cache_ttl_hours * 3600
        loop = asyncio.get_running_loop()
        removed = await loop.run_in_executor(None, sweep_cache, ttl_seconds)
        if removed:
            logger.info("Cache sweep evicted %d stale download(s)", removed)

    # -- UI construction -------------------------------------------------------

    def _build_body(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        self.table_view = _DropTableView(self)
        self.icon_view = _DropListView(self)

        self.tree_view = _SidebarTree(self)
        self.tree_view.setObjectName("sidebar")  # theme: darker surface tone
        self.tree_model = QStandardItemModel(self)
        self.tree_view.setModel(self.tree_model)
        self.tree_view.setHeaderHidden(True)
        self.tree_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tree_selection = self.tree_view.selectionModel()
        if tree_selection is not None:
            tree_selection.currentChanged.connect(self._on_tree_current)
        splitter.addWidget(self.tree_view)

        self.file_model = FileTableModel(
            self._folder_icon, self._file_icon, self, kind_icons=self._kind_icons
        )
        self.file_proxy = FileSortProxy(self.file_model, self)

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
            # Interactive (not Stretch) so every column stays user-resizable.
            horizontal_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            horizontal_header.resizeSection(0, 340)
            horizontal_header.setStretchLastSection(False)
            # Residency indicator (model column 6) sits next to Name, iCloud-style.
            horizontal_header.moveSection(6, 1)
            horizontal_header.resizeSection(6, 28)
        self.table_view.doubleClicked.connect(self._on_double_clicked)
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._on_context_menu)

        self.icon_view.setModel(self.file_proxy)
        self.icon_view.setViewMode(QListView.ViewMode.IconMode)
        self.icon_view.setIconSize(QSize(48, 48))
        self.icon_view.setGridSize(QSize(120, 96))
        self.icon_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.icon_view.setWordWrap(True)
        self.icon_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.icon_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.icon_view.doubleClicked.connect(self._on_double_clicked)
        self.icon_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.icon_view.customContextMenuRequested.connect(self._on_context_menu)

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

        toolbar.addSeparator()

        # One prominent upload entry point: click = files, menu = files/folder.
        self.upload_button = QToolButton(self)
        self.upload_button.setText("⇪ Upload")
        self.upload_button.setToolTip("Upload files (menu: upload a folder)")
        self.upload_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        upload_menu = QMenu(self.upload_button)
        self.upload_files_action = upload_menu.addAction("Files…", self._pick_upload_files)
        self.upload_folder_action = upload_menu.addAction("Folder…", self._pick_upload_folder)
        self.upload_button.setMenu(upload_menu)
        self.upload_button.clicked.connect(self._pick_upload_files)
        toolbar.addWidget(self.upload_button)

        toolbar.addSeparator()

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

        self.transfers_action = QAction("Transfers", self)
        self.transfers_action.triggered.connect(
            lambda: self.transfers_dock.setVisible(not self.transfers_dock.isVisible())
        )
        toolbar.addAction(self.transfers_action)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMaximumWidth(180)
        self.filter_edit.textChanged.connect(self.file_proxy.setFilterFixedString)
        toolbar.addWidget(self.filter_edit)

        self._build_menu()

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
        if menu_bar is None:  # pragma: no cover - always present on a QMainWindow
            return
        drive_menu = menu_bar.addMenu("&Drive")
        if drive_menu is None:  # pragma: no cover
            return
        drive_menu.addAction("New folder…", self._prompt_new_folder)
        drive_menu.addSeparator()
        drive_menu.addAction("Recycle Bin", lambda: self.set_current_dir(TRASH_PREFIX))
        drive_menu.addAction("Empty Recycle Bin…", self.empty_trash)
        drive_menu.addSeparator()
        drive_menu.addAction("Prune deleted messages", self._confirm_prune)
        drive_menu.addAction("Retry failed uploads", self.retry_failed)
        drive_menu.addSeparator()
        drive_menu.addAction("Upload log…", self._show_logs)
        drive_menu.addAction("Manage drives…", self._show_chats)
        drive_menu.addSeparator()
        drive_menu.addAction("Telegram settings…", self._show_setup)

    def _build_transfers_dock(self) -> None:
        self.transfers_panel = TransfersPanel(
            on_pause=lambda: self.bridge.submit(self.transfers.pause()),
            on_resume=lambda: self.bridge.submit(self.transfers.resume()),
            on_skip=lambda: self.bridge.submit(self.transfers.skip_current()),
            on_cancel=lambda tid: self.bridge.submit(self.transfers.cancel(tid)),
            parent=self,
        )
        self.transfers_dock = QDockWidget("Transfers", self)
        self.transfers_dock.setWidget(self.transfers_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.transfers_dock)
        self.transfers_dock.hide()

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
        self._have_loaded = False  # a new drive must always render, even if equal
        self.refresh()

    def _auto_refresh(self) -> None:
        """Timer tick: re-query the index unless the user is mid-dialog or away."""
        from PyQt6.QtWidgets import QApplication

        if not self.isActiveWindow() or QApplication.activeModalWidget() is not None:
            return
        self.refresh(quiet=True)

    # -- data loading ----------------------------------------------------------

    def refresh(self, *, quiet: bool = False) -> None:
        chat_id = self._current_chat_id()
        if chat_id is None:
            return
        if not quiet:
            self._status("Loading…")

        async def _fetch() -> list[VfsEntry]:
            return await self.bridge.db.vfs_list_prefix(chat_id, "/")

        self.bridge.submit(_fetch(), self._on_entries, self._show_error)

    def _on_entries(self, entries: list[VfsEntry]) -> None:
        if self._have_loaded and entries == self.entries:
            return  # unchanged: keep selection, scroll, and expansion untouched
        self._have_loaded = True
        self.entries = entries
        self._rebuild_tree()
        target = self.current_dir if self._dir_exists(self.current_dir) else "/"
        self.set_current_dir(target)

    def _dir_exists(self, path: str) -> bool:
        if path == "/":
            return True
        return any(e.virtual_path.startswith(path) for e in self.entries)

    def _rebuild_tree(self) -> None:
        expanded = self._expanded_tree_paths()
        self.tree_model = build_dir_model(self.entries, self._folder_icon, self)
        if any(e.virtual_path.startswith(TRASH_PREFIX) for e in self.entries):
            trash_item = QStandardItem("🗑 Recycle Bin")
            trash_item.setData(TRASH_PREFIX, PATH_ROLE)
            trash_item.setEditable(False)
            self.tree_model.appendRow(trash_item)
        self.tree_view.setModel(self.tree_model)
        # setModel replaces the selection model, so reconnect navigation.
        selection = self.tree_view.selectionModel()
        if selection is not None:
            selection.currentChanged.connect(self._on_tree_current)
        if expanded:
            self._walk_tree_items(
                lambda item: self.tree_view.setExpanded(
                    self.tree_model.indexFromItem(item), item.data(PATH_ROLE) in expanded
                )
            )
        else:  # first load: sensible default depth
            self.tree_view.expandToDepth(1)

    def _expanded_tree_paths(self) -> set[str]:
        """Paths currently expanded in the sidebar, so refreshes keep them open."""
        expanded: set[str] = set()

        def visit(item: QStandardItem) -> None:
            if self.tree_view.isExpanded(self.tree_model.indexFromItem(item)):
                path = item.data(PATH_ROLE)
                if isinstance(path, str):
                    expanded.add(path)

        self._walk_tree_items(visit)
        return expanded

    def _walk_tree_items(self, visit: Callable[[QStandardItem], None]) -> None:
        def walk(item: QStandardItem) -> None:
            visit(item)
            for i in range(item.rowCount()):
                child = item.child(i)
                if child is not None:
                    walk(child)

        root = self.tree_model.item(0)
        if root is not None:
            walk(root)

    # -- navigation ------------------------------------------------------------

    def set_current_dir(self, path: str) -> None:
        # A refresh of the same listing must not eat the user's selection.
        keep_selection = self._selected_file_names() if path == self.current_dir else set()
        self.current_dir = path
        self.path_edit.setText(path)
        rows = build_rows(self.entries, path, show_hidden=self.show_hidden, is_downloaded=is_cached)
        self.file_model.set_rows(rows)
        if keep_selection:
            self._restore_selection(keep_selection)
        # Sparse columns appear only when the listing has something to show.
        self.table_view.setColumnHidden(3, not any(r.tags for r in rows))
        self.table_view.setColumnHidden(4, not any(r.width and r.height for r in rows))
        self.table_view.setColumnHidden(5, not any(r.duration for r in rows))
        folders = sum(1 for r in rows if r.is_dir)
        files = len(rows) - folders
        total = sum(r.size for r in rows if not r.is_dir)
        hidden = 0
        if not self.show_hidden:
            hidden = len(build_rows(self.entries, path, show_hidden=True)) - len(rows)
        hidden_note = f" (+{hidden} hidden — toggle 'Show hidden')" if hidden else ""
        drive = self.drive_combo.currentText() or "no drive"
        self.setWindowTitle(f"tup — {drive}")
        self._status(
            f"{drive} · {path} — {folders} folder(s), {files} file(s), "
            f"{human_size(total)}{hidden_note}"
        )
        self._select_tree_path(path)

    def _active_view(self) -> QTableView | QListView:
        return self.table_view if self.views.currentIndex() == 0 else self.icon_view

    def _selected_file_names(self) -> set[str]:
        selection = self._active_view().selectionModel()
        if selection is None:
            return set()
        return {
            self.file_proxy.row_at(index.row()).name
            for index in selection.selectedIndexes()
            if index.column() == 0
        }

    def _restore_selection(self, names: set[str]) -> None:
        selection = self._active_view().selectionModel()
        if selection is None:
            return
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for proxy_row in range(self.file_proxy.rowCount()):
            if self.file_proxy.row_at(proxy_row).name in names:
                selection.select(self.file_proxy.index(proxy_row, 0), flags)

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
            self.activate_row(row)

    def activate_row(self, row: FileRow) -> None:
        """Double-click: instant open when cached; otherwise download, then open."""
        self.open_row(row)

    def open_row(self, row: FileRow) -> None:
        self.download_row(row, open_after=True)

    def download_row(self, row: FileRow, *, open_after: bool = False) -> None:
        """Queue the file into the local cache; optionally open it when ready."""
        entry = row.entry
        if entry is None:
            return
        if is_cached(entry):
            if open_after:
                self._open_local(cached_path(entry))
            else:
                self._status(f"{entry.file_name} is already downloaded.")
            return
        key = _entry_key(entry)
        if key in self._downloads_in_flight:
            # Already queued/running — don't stack a duplicate transfer; just
            # upgrade it to open-on-arrival if that's what was asked for now.
            transfer_id = self._downloads_in_flight[key]
            if open_after and transfer_id is not None:
                self._pending_opens[transfer_id] = cached_path(entry)
            self._status(f"{entry.file_name} is already downloading.")
            return
        self._downloads_in_flight[key] = None
        self.transfers_dock.show()
        self.bridge.submit(
            self._enqueue_download(entry, open_after=open_after), on_error=self._show_error
        )

    async def _enqueue_download(self, entry: VfsEntry, *, open_after: bool) -> None:
        dest = cached_path(entry)

        async def runner(transfer: Transfer) -> None:
            client = await self.bridge.mtproto()
            await download_media_file(
                client,
                entry.chat_id,
                entry.telegram_message_id,
                dest,
                max_retries=self.bridge.settings.max_retries,
                progress=lambda got, total: self.transfers.report(transfer, got, total),
            )

        transfer = await self.transfers.enqueue(
            "download", entry.file_name, f"← {entry.virtual_path}", entry.file_size, runner
        )
        key = _entry_key(entry)
        self._downloads_in_flight[key] = transfer.id
        self._download_keys[transfer.id] = key
        if open_after:
            self._pending_opens[transfer.id] = dest

    # -- uploads ----------------------------------------------------------------

    def enqueue_upload_paths(self, paths: list[Path], dest_dir: str | None = None) -> None:
        """Upload dropped/picked files and folders into `dest_dir` (default: here).

        Folder enumeration (rglob + stat) runs off the GUI thread — a dropped
        folder with thousands of files must never freeze the window.
        """
        chat_id = self._current_chat_id()
        if chat_id is None:
            self._status("Add a drive before uploading.")
            return
        self._status(f"Scanning {len(paths)} dropped item(s)…")
        self.bridge.submit(
            self._collect_and_enqueue(chat_id, list(paths), dest_dir or self.current_dir),
            self._on_upload_scan_done,
            self._show_error,
        )

    async def _collect_and_enqueue(self, chat_id: str, paths: list[Path], base_dir: str) -> int:
        loop = asyncio.get_running_loop()
        targets = await loop.run_in_executor(None, collect_upload_targets, paths, base_dir)
        await self._enqueue_uploads(chat_id, targets)
        return len(targets)

    def _on_upload_scan_done(self, count: int) -> None:
        if count == 0:
            self._status("Nothing to upload.")
            return
        self.transfers_dock.show()
        self._status(f"Queued {count} file(s).")

    # -- drops (OS files and internal rows) --------------------------------------

    def handle_view_drop(self, event: QDropEvent | None, view: QTableView | QListView) -> None:
        """Drop on the file panel: into the hovered subfolder, else this directory."""
        if event is None:
            return
        dest = self.current_dir
        index = view.indexAt(event.position().toPoint())
        if index.isValid():
            row = self.file_model.row_at(self.file_proxy.mapToSource(index).row())
            if row.is_dir:
                dest = self.current_dir + row.name + "/"
        self._dispatch_drop(event, dest)

    def handle_tree_drop(self, event: QDropEvent | None, view: QTreeView) -> None:
        """Drop on the sidebar: destination is the hovered folder node."""
        if event is None:
            return
        path = view.indexAt(event.position().toPoint()).data(PATH_ROLE)
        if isinstance(path, str):
            self._dispatch_drop(event, path)

    def _dispatch_drop(self, event: QDropEvent, dest_dir: str) -> None:
        mime = event.mimeData()
        if mime is None:
            return
        if mime.hasFormat(INTERNAL_MIME):
            event.acceptProposedAction()
            raw = mime.data(INTERNAL_MIME)
            payload = raw.data() if isinstance(raw, QByteArray) else raw
            names = json.loads(payload.decode() or "[]")
            copy = bool(
                event.modifiers()
                & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
            )
            self.move_or_copy_names(names, dest_dir, copy=copy)
        elif mime.hasUrls():
            event.acceptProposedAction()
            paths = [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]
            if paths:
                self.enqueue_upload_paths(paths, dest_dir=dest_dir)

    def move_or_copy_names(self, names: list[str], dest_dir: str, *, copy: bool = False) -> None:
        """Move (default) or copy dragged files from the current listing."""
        by_name: dict[str, FileRow] = {}
        for i in range(self.file_model.rowCount()):
            row = self.file_model.row_at(i)
            if not row.is_dir:
                by_name[row.name] = row
        for name in names:
            dragged = by_name.get(name)
            if dragged is None or dragged.entry is None:
                continue
            if dragged.entry.virtual_path == dest_dir:
                continue  # dropped where it already lives
            if copy:
                self.copy_row(dragged, dest_dir)
            else:
                self.move_row(dragged, dest_dir)

    async def _enqueue_uploads(self, chat_id: str, targets: list[tuple[Path, str, int]]) -> None:
        for path, dest, size in targets:

            async def runner(transfer: Transfer, path: Path = path, dest: str = dest) -> None:
                client = await self.bridge.mtproto()

                def on_progress(sent: int, total: int, t: Transfer = transfer) -> None:
                    self.transfers.report(t, sent, total)

                await upload_file(
                    self.bridge.db,
                    self.bridge.settings,
                    client,
                    path,
                    chat_id,
                    dest,
                    progress_callback=on_progress,
                )

            await self.transfers.enqueue("upload", path.name, f"→ {dest}", size, runner)

    def _pick_upload_files(self) -> None:
        files, _filter = QFileDialog.getOpenFileNames(self, "Upload files", str(Path.home()))
        if files:
            self.enqueue_upload_paths([Path(f) for f in files])

    def _pick_upload_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Upload folder", str(Path.home()))
        if folder:
            self.enqueue_upload_paths([Path(folder)])

    # -- transfer updates --------------------------------------------------------

    def _on_transfer_update_from_loop(self, snapshot: Transfer) -> None:
        # Called on the bridge loop; hop onto the GUI thread before touching Qt.
        self.bridge.call_in_gui(lambda: self._on_transfer_update(snapshot))

    def _on_transfer_update(self, transfer: Transfer) -> None:
        self.transfers_panel.update_transfer(transfer)
        if transfer.state in ("done", "failed", "cancelled", "skipped"):
            key = self._download_keys.pop(transfer.id, None)
            if key is not None:
                self._downloads_in_flight.pop(key, None)
        if transfer.state == "done":
            if transfer.kind == "upload":
                self.refresh()
            else:
                self.set_current_dir(self.current_dir)  # refresh Downloaded badges
                pending = self._pending_opens.pop(transfer.id, None)
                if pending is not None:
                    self._open_local(pending)
        elif transfer.state == "failed":
            self._pending_opens.pop(transfer.id, None)
            self._status(f"{transfer.label}: {transfer.error}")
        elif transfer.state in ("cancelled", "skipped"):
            self._pending_opens.pop(transfer.id, None)

    def _open_local(self, path: Path) -> None:
        if path.is_file():
            path.touch()  # LRU refresh: recently opened files survive cache sweeps
        if self.open_files_externally:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _evict_row(self, entry: VfsEntry) -> None:
        """Free local disk space; the file itself stays on Telegram."""
        if evict(entry):
            self._status(f"Removed local copy of {entry.file_name} (still on Telegram).")
        else:
            self._status(f"{entry.file_name} has no local copy.")
        self.set_current_dir(self.current_dir)  # refresh Downloaded badges

    def _reveal_local(self, path: Path) -> None:
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/open", "-R", str(path)], check=False)  # noqa: S603
        else:  # pragma: no cover - non-macOS fallback opens the parent folder
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def _on_context_menu(self, pos: QPoint) -> None:
        view = self.table_view if self.views.currentIndex() == 0 else self.icon_view
        index = view.indexAt(pos)
        menu = QMenu(self)
        if not index.isValid():
            menu.addAction("New folder…", self._prompt_new_folder)
            menu.addAction("Upload files…", self._pick_upload_files)
            menu.addAction("⟳ Refresh", lambda: self.refresh())
        else:
            row = self.file_model.row_at(self.file_proxy.mapToSource(index).row())
            in_trash = row.entry is not None and is_trashed(row.entry)
            if row.is_dir:
                menu.addAction("Open", lambda: self._on_double_clicked(index))
                menu.addSeparator()
                menu.addAction("Delete folder", lambda: self._confirm_delete(row))
            elif in_trash:
                menu.addAction("Restore", lambda: self.restore_row(row))
                menu.addSeparator()
                menu.addAction("Delete permanently", lambda: self._confirm_delete(row))
            else:
                menu.addAction("Open", lambda: self.open_row(row))
                menu.addAction("Download", lambda: self.download_row(row))
                entry = row.entry
                if entry is not None and is_cached(entry):
                    menu.addAction("Show in Finder", lambda: self._reveal_local(cached_path(entry)))
                    menu.addAction("Remove download", lambda: self._evict_row(entry))
                menu.addSeparator()
                menu.addAction("Caption && tags…", lambda: self.edit_caption_row(row))
                menu.addAction("Copy to…", lambda: self._prompt_copy(row))
                menu.addAction("Move to…", lambda: self._prompt_move(row))
                menu.addSeparator()
                menu.addAction("Move to Recycle Bin", lambda: self._confirm_delete(row))
        viewport = view.viewport()
        anchor = viewport if viewport is not None else view
        menu.exec(anchor.mapToGlobal(pos))

    # -- file operations (CLI parity) --------------------------------------------

    def create_folder(self, name: str) -> None:
        chat_id = self._current_chat_id()
        if chat_id is None or not name:
            return
        target = self.current_dir + name

        async def _run() -> str:
            return await op_mkdir(self.bridge.db, chat_id, target)

        self.bridge.submit(_run(), self._after_op, self._show_error)

    def delete_row(self, row: FileRow) -> None:
        chat_id = self._current_chat_id()
        if chat_id is None:
            return
        if row.is_dir:

            async def _rmdir() -> str:
                removed = await op_rmdir(self.bridge.db, chat_id, self.current_dir + row.name)
                return f"Removed {removed}"

            self.bridge.submit(_rmdir(), self._after_op, self._show_error)
            return
        entry = row.entry
        if entry is None:
            return
        if is_trashed(entry):
            self.purge_row(row)
            return

        async def _trash() -> str:
            trashed = await op_trash(self.bridge.db, self.bridge.settings, chat_id, entry)
            return f"Moved to Recycle Bin: {trashed}"

        self.bridge.submit(_trash(), self._after_op, self._show_error)

    def purge_row(self, row: FileRow) -> None:
        """Permanently delete: version messages, the current message, the row."""
        chat_id = self._current_chat_id()
        entry = row.entry
        if chat_id is None or entry is None:
            return

        async def _purge() -> str:
            await op_purge(self.bridge.db, self.bridge.settings, chat_id, entry)
            return f"Permanently deleted {entry.virtual_path}{entry.file_name}"

        self.bridge.submit(_purge(), self._after_op, self._show_error)

    def restore_row(self, row: FileRow) -> None:
        chat_id = self._current_chat_id()
        entry = row.entry
        if chat_id is None or entry is None:
            return

        async def _restore() -> str:
            restored = await op_restore(self.bridge.db, self.bridge.settings, chat_id, entry)
            return f"Restored to {restored}"

        self.bridge.submit(_restore(), self._after_op, self._show_error)

    def empty_trash(self) -> None:
        chat_id = self._current_chat_id()
        if chat_id is None:
            return
        if not self.suppress_dialogs:
            answer = QMessageBox.question(
                self,
                "Empty Recycle Bin",
                "Permanently delete everything in the Recycle Bin (including versions)?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        async def _empty() -> str:
            purged = await op_empty_trash(self.bridge.db, self.bridge.settings, chat_id)
            return f"Recycle Bin emptied: {purged} file(s) permanently deleted"

        self.bridge.submit(_empty(), self._after_op, self._show_error)

    def edit_caption_row(self, row: FileRow) -> None:
        """Edit the user caption; hashtags in it become searchable tags."""
        chat_id = self._current_chat_id()
        entry = row.entry
        if chat_id is None or entry is None:
            return
        text, ok = QInputDialog.getMultiLineText(
            self,
            "Caption & tags",
            f"Caption for {entry.file_name} (hashtags become tags, e.g. #invoice #q3):",
            entry.user_caption,
        )
        if not ok:
            return

        async def _set() -> str:
            tags = await op_set_caption(
                self.bridge.db, self.bridge.settings, chat_id, entry, text.strip()
            )
            return f"Caption updated — tags: {tags or '(none)'}"

        self.bridge.submit(_set(), self._after_op, self._show_error)

    def move_row(self, row: FileRow, dest_dir: str) -> None:
        chat_id = self._current_chat_id()
        entry = row.entry
        if chat_id is None or entry is None:
            return

        async def _mv() -> str:
            moved = await op_mv(self.bridge.db, self.bridge.settings, chat_id, entry, dest_dir)
            return f"Moved to {moved}"

        self.bridge.submit(_mv(), self._after_op, self._show_error)

    def copy_row(self, row: FileRow, dest_dir: str) -> None:
        chat_id = self._current_chat_id()
        entry = row.entry
        if chat_id is None or entry is None:
            return

        async def _cp() -> str:
            client = await self.bridge.mtproto()
            copied = await op_cp(
                self.bridge.db, self.bridge.settings, client, chat_id, entry, dest_dir
            )
            return f"Copied to {copied} (server-side, no re-upload)"

        self.bridge.submit(_cp(), self._after_op, self._show_error)

    def prune_drive(self) -> None:
        chat_id = self._current_chat_id()
        if chat_id is None:
            return

        async def _prune() -> str:
            client = await self.bridge.mtproto()
            pruned = await op_prune(self.bridge.db, self.bridge.settings, client, chat_id)
            return f"Pruned {len(pruned)} stale row(s)."

        self.bridge.submit(_prune(), self._after_op, self._show_error)

    def retry_failed(self) -> None:
        async def _retry() -> str:
            client = await self.bridge.mtproto()
            resolved, still_failing = await op_retry_failed(
                self.bridge.db, self.bridge.settings, client
            )
            return f"Retry finished: {resolved} resolved, {still_failing} still pending."

        self.bridge.submit(_retry(), self._after_op, self._show_error)

    def _after_op(self, message: str) -> None:
        self._status(message)
        self.refresh()

    # -- prompts ------------------------------------------------------------------

    def _prompt_new_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if ok and name.strip():
            self.create_folder(name.strip())

    def _prompt_destination(self, title: str) -> str | None:
        dialog = FolderPickerDialog(self.entries, title, self._folder_icon, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected_path()

    def _prompt_move(self, row: FileRow) -> None:
        dest = self._prompt_destination(f"Move {row.name}")
        if dest is not None:
            self.move_row(row, dest)

    def _prompt_copy(self, row: FileRow) -> None:
        dest = self._prompt_destination(f"Copy {row.name}")
        if dest is not None:
            self.copy_row(row, dest)

    def _confirm_delete(self, row: FileRow) -> None:
        in_trash = row.entry is not None and is_trashed(row.entry)
        if not row.is_dir and not in_trash:
            # Reversible: moving to the Recycle Bin needs no confirmation.
            self.delete_row(row)
            return
        if not self.suppress_dialogs:
            what = f"folder {row.name}" if row.is_dir else row.name
            verb = "Permanently delete" if in_trash else "Delete"
            answer = QMessageBox.question(self, "Delete", f"{verb} {what} from Telegram?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.delete_row(row)

    def _confirm_prune(self) -> None:
        if not self.suppress_dialogs:
            answer = QMessageBox.question(
                self,
                "Prune",
                "Remove index rows whose Telegram messages were deleted natively?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.prune_drive()

    # -- dialogs ------------------------------------------------------------------

    def _show_logs(self) -> None:
        async def _fetch() -> list[UploadLogEntry]:
            return await self.bridge.db.log_recent(limit=50)

        def _open(entries: list[UploadLogEntry]) -> None:
            LogsDialog(entries, self).exec()

        self.bridge.submit(_fetch(), _open, self._show_error)

    def _show_chats(self) -> None:
        dialog = ChatsDialog(self.bridge, self)
        dialog.exec()
        self._reload_drives()

    def _show_setup(self) -> None:
        from tup.gui.setup_dialog import SetupDialog

        dialog = SetupDialog(existing=self.bridge.settings, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._status("Configuration saved — restart tup to apply it.")

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
        self._auto_refresh_timer.stop()  # no ticks into a stopping bridge
        with contextlib.suppress(Exception):  # bridge may already be stopped
            self.bridge.submit(self.transfers.shutdown()).result(timeout=5)
        self.bridge.stop()
        super().closeEvent(a0)
