"""Application-wide styling: one accent color, surface depth, comfortable rows.

Palette roles (palette(...)) are used wherever possible so the sheet adapts to
the OS light/dark theme; only the Telegram-blue accent is fixed.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QApplication

ACCENT = "#2AABEE"  # Telegram blue

_QSS = f"""
QTableView, QListView, QTreeView {{
    outline: 0;
    border: none;
}}
QTableView::item, QTreeView::item {{
    padding: 4px 6px;
}}
QTableView::item:selected, QListView::item:selected, QTreeView::item:selected {{
    background: {ACCENT};
    color: white;
}}
QProgressBar {{
    border: none;
    border-radius: 3px;
    background: palette(alternate-base);
    text-align: center;
}}
QProgressBar::chunk {{
    border-radius: 3px;
    background: {ACCENT};
}}
/* Sidebar and transfers dock sit one surface below the file listing. */
QTreeView#sidebar, QWidget#transfersPanel, QWidget#transfersPanel QTableWidget {{
    background: palette(window);
}}
QToolBar {{
    spacing: 4px;
    padding: 2px;
}}
QLineEdit {{
    border: 1px solid palette(mid);
    border-radius: 4px;
    padding: 2px 6px;
    background: palette(base);
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyleSheet(_QSS)
