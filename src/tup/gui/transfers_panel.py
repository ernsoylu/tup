"""Bottom transfers panel: live progress rows with pause/skip/stop controls."""

from __future__ import annotations

import time
from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tup.gui.models import human_size
from tup.gui.transfers import Transfer, TransferState

_ID_ROLE = Qt.ItemDataRole.UserRole + 1
_TERMINAL = ("done", "failed", "cancelled", "skipped")
_COLUMNS = ("File", "Where", "Size", "Progress", "Status")


class TransfersPanel(QWidget):
    """Table of transfers plus queue controls; rows keyed by transfer id."""

    def __init__(
        self,
        on_pause: Callable[[], object],
        on_resume: Callable[[], object],
        on_skip: Callable[[], object],
        on_cancel: Callable[[int], object],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("transfersPanel")  # theme: sits one surface below
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_cancel = on_cancel
        self._transfers: dict[int, Transfer] = {}
        self._speed: dict[int, tuple[float, int, float]] = {}  # t, bytes, bytes/s

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        controls = QHBoxLayout()
        self.pause_button = QPushButton("⏸ Pause queue")
        self.pause_button.setCheckable(True)
        self.pause_button.setToolTip(
            "Hold the queue between items (the in-flight transfer keeps going)."
        )
        self.pause_button.toggled.connect(self._on_pause_toggled)
        controls.addWidget(self.pause_button)

        self.skip_button = QPushButton("⏭ Skip current")
        self.skip_button.clicked.connect(lambda: on_skip())
        controls.addWidget(self.skip_button)

        self.stop_button = QPushButton("✕ Stop selected")
        self.stop_button.clicked.connect(self._cancel_selected)
        controls.addWidget(self.stop_button)

        controls.addStretch(1)
        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("color: gray;")
        controls.addWidget(self.summary_label)

        # Housekeeping, not an operational control: low emphasis, far right.
        self.clear_button = QPushButton("Clear finished")
        self.clear_button.setFlat(True)
        self.clear_button.clicked.connect(self.clear_finished)
        controls.addWidget(self.clear_button)
        layout.addLayout(controls)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        vertical_header = self.table.verticalHeader()
        if vertical_header is not None:
            vertical_header.setVisible(False)
        horizontal_header = self.table.horizontalHeader()
        if horizontal_header is not None:
            # Interactive everywhere so the user can drag column widths;
            # the trailing Status column absorbs leftover space.
            horizontal_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            horizontal_header.resizeSection(0, 240)
            horizontal_header.resizeSection(3, 200)
            horizontal_header.setStretchLastSection(True)
        layout.addWidget(self.table)

    # -- updates ----------------------------------------------------------------

    def update_transfer(self, transfer: Transfer) -> None:
        self._transfers[transfer.id] = transfer
        row = self._find_row(transfer.id)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            arrow = "⬆" if transfer.kind == "upload" else "⬇"
            name_item = QTableWidgetItem(f"{arrow} {transfer.label}")
            name_item.setData(_ID_ROLE, transfer.id)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(transfer.detail))
            self.table.setItem(row, 2, QTableWidgetItem(human_size(max(transfer.total, 0))))
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            self.table.setCellWidget(row, 3, bar)
            self.table.setItem(row, 4, QTableWidgetItem(""))

        if transfer.state in _TERMINAL:
            # A full bright bar on a finished row reads as ongoing activity —
            # drop the bar and let the muted Status text carry the outcome.
            self.table.removeCellWidget(row, 3)
            self.table.setItem(row, 3, QTableWidgetItem(""))
        else:
            bar_widget = self.table.cellWidget(row, 3)
            if isinstance(bar_widget, QProgressBar):
                percent = int(transfer.done * 100 / transfer.total) if transfer.total else 0
                bar_widget.setValue(min(percent, 100))
        status_item = self.table.item(row, 4)
        if status_item is not None:
            status_item.setText(self._status_text(transfer))
            if transfer.state in _TERMINAL:
                status_item.setForeground(QColor(Qt.GlobalColor.gray))
        self._refresh_pause_label()
        self._refresh_summary()

    def _status_text(self, transfer: Transfer) -> str:
        if transfer.state == "running":
            speed = self._update_speed(transfer)
            rate = f" · {human_size(int(speed))}/s" if speed > 0 else ""
            return f"{human_size(transfer.done)} of {human_size(max(transfer.total, 0))}{rate}"
        if transfer.state == "done":
            return "✓ Done"
        if transfer.state == "failed":
            return f"✗ {transfer.error or 'failed'}"
        if transfer.state == "cancelled":
            return "Stopped"
        if transfer.state == "skipped":
            return f"Skipped — {transfer.error}" if transfer.error else "Skipped"
        return "Queued"

    def _update_speed(self, transfer: Transfer) -> float:
        now = time.monotonic()
        last_time, last_done, speed = self._speed.get(transfer.id, (now, transfer.done, 0.0))
        elapsed = now - last_time
        if elapsed >= 0.5:
            speed = max((transfer.done - last_done) / elapsed, 0.0)
            self._speed[transfer.id] = (now, transfer.done, speed)
        elif transfer.id not in self._speed:
            self._speed[transfer.id] = (now, transfer.done, 0.0)
        return speed

    # -- controls ----------------------------------------------------------------

    def _on_pause_toggled(self, checked: bool) -> None:
        self._refresh_pause_label()
        if checked:
            self._on_pause()
        else:
            self._on_resume()

    def _refresh_summary(self) -> None:
        """One-line queue digest so the dock reads at a glance."""
        states = [t.state for t in self._transfers.values()]
        shown: tuple[TransferState, ...] = ("running", "queued", "done", "failed", "skipped")
        parts = [f"{states.count(state)} {state}" for state in shown if states.count(state)]
        self.summary_label.setText(" · ".join(parts))

    def _refresh_pause_label(self) -> None:
        """Make the pause gate honest: while draining, say so instead of 'Resume'."""
        if not self.pause_button.isChecked():
            self.pause_button.setText("⏸ Pause queue")
        elif any(t.state == "running" for t in self._transfers.values()):
            self.pause_button.setText("⏸ Pausing after current file…")
        else:
            self.pause_button.setText("▶ Resume queue")

    def _cancel_selected(self) -> None:
        selection = self.table.selectionModel()
        if selection is None:
            return
        for index in selection.selectedRows():
            item = self.table.item(index.row(), 0)
            if item is not None:
                transfer_id = item.data(_ID_ROLE)
                if isinstance(transfer_id, int):
                    self._on_cancel(transfer_id)

    def clear_finished(self) -> None:
        for row in range(self.table.rowCount() - 1, -1, -1):
            item = self.table.item(row, 0)
            transfer_id = item.data(_ID_ROLE) if item is not None else None
            transfer = self._transfers.get(transfer_id) if isinstance(transfer_id, int) else None
            if transfer is not None and transfer.state in _TERMINAL:
                self.table.removeRow(row)
                self._transfers.pop(transfer.id, None)
                self._speed.pop(transfer.id, None)
        self._refresh_summary()

    # -- helpers -----------------------------------------------------------------

    def _find_row(self, transfer_id: int) -> int | None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(_ID_ROLE) == transfer_id:
                return row
        return None

    def active_count(self) -> int:
        return sum(1 for t in self._transfers.values() if t.state not in _TERMINAL)
