"""Secondary dialogs: upload logs and drive/chat management."""

from __future__ import annotations

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from tup.database import ChatAlias, UploadLogEntry, VfsEntry
from tup.gui.bridge import CoreBridge
from tup.gui.models import PATH_ROLE, build_dir_model
from tup.gui.ops import op_add_chat, op_discover_chats


def _make_table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    vertical = table.verticalHeader()
    if vertical is not None:
        vertical.setVisible(False)
    horizontal = table.horizontalHeader()
    if horizontal is not None:
        # Interactive keeps columns user-draggable (ResizeToContents locks them);
        # callers do a one-shot resizeColumnsToContents() after populating.
        horizontal.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        horizontal.setStretchLastSection(True)
    return table


class FolderPickerDialog(QDialog):
    """Pick a destination folder from the drive's tree (used by Copy/Move)."""

    def __init__(
        self,
        entries: list[VfsEntry],
        title: str,
        folder_icon: QIcon,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(360, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Destination folder:"))

        self.tree = QTreeView()
        self.tree.setHeaderHidden(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._model = build_dir_model(entries, folder_icon, self)
        self.tree.setModel(self._model)
        self.tree.expandAll()
        self.tree.setCurrentIndex(self._model.index(0, 0))  # root preselected
        self.tree.doubleClicked.connect(lambda _index: self.accept())
        layout.addWidget(self.tree)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_path(self) -> str | None:
        path = self.tree.currentIndex().data(PATH_ROLE)
        return path if isinstance(path, str) else None


class LogsDialog(QDialog):
    """Read-only view of the most recent uploads_log entries."""

    def __init__(self, entries: list[UploadLogEntry], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("tup — upload log")
        self.resize(900, 420)
        layout = QVBoxLayout(self)
        self.table = _make_table(["When", "File", "Drive", "Type", "Status", "Msg ID", "Error"])
        for entry in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                entry.timestamp,
                entry.file_path,
                entry.chat_id,
                entry.upload_type,
                entry.status,
                str(entry.telegram_message_id or "-"),
                entry.error_message or "-",
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class ChatsDialog(QDialog):
    """Manage drive aliases: list, add (validated live), remove, discover."""

    def __init__(self, bridge: CoreBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.setWindowTitle("tup — drives (Telegram chats)")
        self.resize(680, 480)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Registered drives:"))
        self.alias_table = _make_table(["Alias", "Chat ID", "Title"])
        layout.addWidget(self.alias_table)

        add_row = QHBoxLayout()
        self.alias_edit = QLineEdit()
        self.alias_edit.setPlaceholderText("alias (e.g. work)")
        self.chat_id_edit = QLineEdit()
        self.chat_id_edit.setPlaceholderText("chat id (e.g. -100123…)")
        self.add_button = QPushButton("Add")
        self.add_button.clicked.connect(self._on_add)
        self.remove_button = QPushButton("Remove selected")
        self.remove_button.clicked.connect(self._on_remove)
        add_row.addWidget(self.alias_edit)
        add_row.addWidget(self.chat_id_edit)
        add_row.addWidget(self.add_button)
        add_row.addWidget(self.remove_button)
        layout.addLayout(add_row)

        layout.addWidget(QLabel("Visible chats (from pending updates — send /start in a chat):"))
        self.discovered_table = _make_table(["Chat ID", "Type", "Title"])
        layout.addWidget(self.discovered_table)

        bottom = QHBoxLayout()
        self.discover_button = QPushButton("🔍 Discover")
        self.discover_button.clicked.connect(self._on_discover)
        self.status_label = QLabel("")
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom.addWidget(self.discover_button)
        bottom.addWidget(self.status_label, 1)
        bottom.addWidget(close)
        layout.addLayout(bottom)

        self.reload_aliases()

    # -- data ------------------------------------------------------------------

    def reload_aliases(self) -> None:
        async def _fetch() -> list[ChatAlias]:
            return await self.bridge.db.alias_list()

        self.bridge.submit(_fetch(), self._on_aliases, self._on_error)

    def _on_aliases(self, aliases: list[ChatAlias]) -> None:
        self.alias_table.setRowCount(0)
        for alias in aliases:
            row = self.alias_table.rowCount()
            self.alias_table.insertRow(row)
            self.alias_table.setItem(row, 0, QTableWidgetItem(alias.alias))
            self.alias_table.setItem(row, 1, QTableWidgetItem(alias.chat_id))
            self.alias_table.setItem(row, 2, QTableWidgetItem(alias.title or "-"))
        self.alias_table.resizeColumnsToContents()

    # -- actions ---------------------------------------------------------------

    def _on_add(self) -> None:
        alias = self.alias_edit.text().strip()
        chat_id = self.chat_id_edit.text().strip()
        if not alias or not chat_id:
            self.status_label.setText("Enter both an alias and a chat id.")
            return
        self.status_label.setText(f"Validating {chat_id}…")

        async def _add() -> str:
            return await op_add_chat(self.bridge.db, self.bridge.settings, alias, chat_id)

        def _done(title: str) -> None:
            self.status_label.setText(f"Added {alias} — {title}")
            self.alias_edit.clear()
            self.chat_id_edit.clear()
            self.reload_aliases()

        self.bridge.submit(_add(), _done, self._on_error)

    def _on_remove(self) -> None:
        selection = self.alias_table.selectionModel()
        if selection is None or not selection.selectedRows():
            self.status_label.setText("Select a drive to remove.")
            return
        aliases = []
        for index in selection.selectedRows():
            item = self.alias_table.item(index.row(), 0)
            if item is not None:
                aliases.append(item.text())

        async def _remove() -> int:
            removed = 0
            for alias in aliases:
                removed += 1 if await self.bridge.db.alias_remove(alias) else 0
            return removed

        def _done(removed: int) -> None:
            self.status_label.setText(f"Removed {removed} drive(s).")
            self.reload_aliases()

        self.bridge.submit(_remove(), _done, self._on_error)

    def _on_discover(self) -> None:
        self.status_label.setText("Peeking at pending updates…")

        async def _discover() -> list[tuple[str, str, str]]:
            return await op_discover_chats(self.bridge.settings)

        def _done(rows: list[tuple[str, str, str]]) -> None:
            self.discovered_table.setRowCount(0)
            for chat_id, chat_type, title in rows:
                row = self.discovered_table.rowCount()
                self.discovered_table.insertRow(row)
                self.discovered_table.setItem(row, 0, QTableWidgetItem(chat_id))
                self.discovered_table.setItem(row, 1, QTableWidgetItem(chat_type))
                self.discovered_table.setItem(row, 2, QTableWidgetItem(title))
            self.discovered_table.resizeColumnsToContents()
            self.status_label.setText(
                f"{len(rows)} chat(s) visible."
                if rows
                else "Nothing visible — send /start in the chat and retry."
            )

        self.bridge.submit(_discover(), _done, self._on_error)

    def _on_error(self, exc: BaseException) -> None:
        self.status_label.setText(str(exc))
