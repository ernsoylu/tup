"""Main explorer window: drive selector, folder tree, sortable file views."""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QModelIndex, QPoint, QSize, Qt, QUrl
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
    QTreeView,
)

from tup.database import ChatAlias, UploadLogEntry, VfsEntry
from tup.gui.bridge import CoreBridge
from tup.gui.cache import cached_path, is_cached
from tup.gui.dialogs import ChatsDialog, LogsDialog
from tup.gui.models import (
    DirNode,
    FileRow,
    FileSortProxy,
    FileTableModel,
    all_dir_paths,
    build_dir_tree,
    build_rows,
    human_size,
)
from tup.gui.ops import op_cp, op_mkdir, op_mv, op_prune, op_retry_failed, op_rm, op_rmdir
from tup.gui.transfers import Transfer, TransferManager, collect_upload_targets
from tup.gui.transfers_panel import TransfersPanel
from tup.uploader import download_media_file, upload_file
from tup.utils import VfsPathError, normalize_vfs_path

PATH_ROLE = Qt.ItemDataRole.UserRole + 1

DropHandler = Callable[[list[Path]], None]


def _accept_file_drag(event: QDragEnterEvent | QDragMoveEvent | None) -> None:
    if event is None:
        return
    mime = event.mimeData()
    if mime is not None and mime.hasUrls():
        event.acceptProposedAction()


def _paths_from_drop(event: QDropEvent | None) -> list[Path]:
    if event is None:
        return []
    mime = event.mimeData()
    if mime is None or not mime.hasUrls():
        return []
    event.acceptProposedAction()
    return [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]


class _DropTableView(QTableView):
    """Details view accepting OS file drops (files and folders)."""

    def __init__(self, drop_handler: DropHandler) -> None:
        super().__init__()
        self._drop_handler = drop_handler
        self.setAcceptDrops(True)
        viewport = self.viewport()
        if viewport is not None:
            viewport.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def dragEnterEvent(self, e: QDragEnterEvent | None) -> None:
        _accept_file_drag(e)

    def dragMoveEvent(self, e: QDragMoveEvent | None) -> None:
        _accept_file_drag(e)

    def dropEvent(self, e: QDropEvent | None) -> None:
        paths = _paths_from_drop(e)
        if paths:
            self._drop_handler(paths)


class _DropListView(QListView):
    """Icon view accepting OS file drops (files and folders)."""

    def __init__(self, drop_handler: DropHandler) -> None:
        super().__init__()
        self._drop_handler = drop_handler
        self.setAcceptDrops(True)
        viewport = self.viewport()
        if viewport is not None:
            viewport.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def dragEnterEvent(self, e: QDragEnterEvent | None) -> None:
        _accept_file_drag(e)

    def dragMoveEvent(self, e: QDragMoveEvent | None) -> None:
        _accept_file_drag(e)

    def dropEvent(self, e: QDropEvent | None) -> None:
        paths = _paths_from_drop(e)
        if paths:
            self._drop_handler(paths)


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
            self._folder_icon = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
            self._file_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        else:  # pragma: no cover - style always exists in a real QApplication
            self._folder_icon = QIcon()
            self._file_icon = QIcon()

        self._pending_opens: dict[int, Path] = {}
        self.transfers = TransferManager(self._on_transfer_update_from_loop)

        self._build_body()
        self._build_toolbar()
        self._build_transfers_dock()
        self._status("Loading drives…")
        self.bridge.submit(self._start_transfers())
        self._reload_drives()

    async def _start_transfers(self) -> None:
        # create_task needs a running loop, so the worker starts on the bridge.
        self.transfers.start()

    # -- UI construction -------------------------------------------------------

    def _build_body(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        self.table_view = _DropTableView(self.enqueue_upload_paths)
        self.icon_view = _DropListView(self.enqueue_upload_paths)

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

        self.upload_files_action = QAction("⇪ Upload files…", self)
        self.upload_files_action.triggered.connect(self._pick_upload_files)
        toolbar.addAction(self.upload_files_action)

        self.upload_folder_action = QAction("⇪ Upload folder…", self)
        self.upload_folder_action.triggered.connect(self._pick_upload_folder)
        toolbar.addAction(self.upload_folder_action)

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
        drive_menu.addAction("Prune deleted messages", self._confirm_prune)
        drive_menu.addAction("Retry failed uploads", self.retry_failed)
        drive_menu.addSeparator()
        drive_menu.addAction("Upload log…", self._show_logs)
        drive_menu.addAction("Manage drives…", self._show_chats)

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
        rows = build_rows(self.entries, path, show_hidden=self.show_hidden, is_downloaded=is_cached)
        self.file_model.set_rows(rows)
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
        if open_after:
            self._pending_opens[transfer.id] = dest

    # -- uploads ----------------------------------------------------------------

    def enqueue_upload_paths(self, paths: list[Path]) -> None:
        """Upload dropped/picked files and folders into the current directory."""
        chat_id = self._current_chat_id()
        if chat_id is None:
            self._status("Add a drive before uploading.")
            return
        targets = collect_upload_targets(paths, self.current_dir)
        if not targets:
            self._status("Nothing to upload.")
            return
        self.transfers_dock.show()
        self.bridge.submit(self._enqueue_uploads(chat_id, targets), on_error=self._show_error)

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
        if self.open_files_externally:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

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
            if row.is_dir:
                menu.addAction("Open", lambda: self._on_double_clicked(index))
                menu.addSeparator()
                menu.addAction("Delete folder", lambda: self._confirm_delete(row))
            else:
                menu.addAction("Open", lambda: self.open_row(row))
                menu.addAction("Download", lambda: self.download_row(row))
                entry = row.entry
                if entry is not None and is_cached(entry):
                    menu.addAction("Show in Finder", lambda: self._reveal_local(cached_path(entry)))
                menu.addSeparator()
                menu.addAction("Copy to…", lambda: self._prompt_copy(row))
                menu.addAction("Move to…", lambda: self._prompt_move(row))
                menu.addSeparator()
                menu.addAction("Delete", lambda: self._confirm_delete(row))
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

        async def _rm() -> str:
            deleted = await op_rm(self.bridge.db, self.bridge.settings, chat_id, entry)
            return f"Deleted {deleted}"

        self.bridge.submit(_rm(), self._after_op, self._show_error)

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
        choices = all_dir_paths(self.entries)
        dest, ok = QInputDialog.getItem(self, title, "Destination folder:", choices, 0, True)
        return dest if ok and dest else None

    def _prompt_move(self, row: FileRow) -> None:
        dest = self._prompt_destination(f"Move {row.name}")
        if dest is not None:
            self.move_row(row, dest)

    def _prompt_copy(self, row: FileRow) -> None:
        dest = self._prompt_destination(f"Copy {row.name}")
        if dest is not None:
            self.copy_row(row, dest)

    def _confirm_delete(self, row: FileRow) -> None:
        if not self.suppress_dialogs:
            what = f"folder {row.name}" if row.is_dir else row.name
            answer = QMessageBox.question(self, "Delete", f"Delete {what} from Telegram?")
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
        with contextlib.suppress(Exception):  # bridge may already be stopped
            self.bridge.submit(self.transfers.shutdown()).result(timeout=5)
        self.bridge.stop()
        super().closeEvent(a0)
