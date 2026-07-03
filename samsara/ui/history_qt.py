"""
PySide6 dictation history window for Samsara.

Reads from app.history_db (HistoryManager / SQLite) with a fallback
to the legacy app.history list.  Runs on its own daemon thread --
same pattern as settings_qt.py.
"""

import threading
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QLineEdit, QComboBox, QPushButton, QLabel, QPlainTextEdit,
    QFrame, QMenu, QMessageBox,
)

from samsara.ui import qt_runtime, theme

from samsara.log import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Colour palette -- sourced from samsara.ui.theme, the shared design system.
# Local aliases kept so the ~30 usage sites in the stylesheet below don't
# need touching; only the source of truth moved, not the resulting look.
# _ACCENT_DIM has no theme.py equivalent yet -- it's a pre-theme constant
# also duplicated as-is in main_window_qt.py, command_cheatsheet_qt.py, and
# wake_word_debug_qt.py (none of which have migrated to theme.py either),
# so it stays a local literal rather than a one-off token only this file
# would use.
# ---------------------------------------------------------------------------

_BG         = theme.BG0
_SURFACE    = theme.BG1
_ELEVATED   = theme.BG2
_BORDER     = theme.BORDER
_ACCENT     = theme.ACCENT
_ACCENT_DIM = "#1a3a42"
_TEXT_PRI   = theme.TEXT_PRIMARY
_TEXT_SEC   = theme.TEXT_SECONDARY
_ERROR      = theme.ERROR


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

_STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {_BG};
    color: {_TEXT_PRI};
    font-family: {theme.FONT_FAMILY};
    font-size: {theme.FONT_SIZE_BODY}px;
}}
QTableWidget {{
    background-color: {_SURFACE};
    alternate-background-color: rgba(255,255,255,0.022);
    gridline-color: transparent;
    color: {_TEXT_PRI};
    border: none;
    outline: none;
}}
QTableWidget::item {{
    padding: 6px 10px;
    border: none;
}}
QTableWidget::item:selected {{
    background-color: {_ACCENT_DIM};
    color: {_ACCENT};
}}
QHeaderView::section {{
    background-color: {_SURFACE};
    color: {_TEXT_SEC};
    padding: 5px 10px;
    border: none;
    border-bottom: 1px solid {_BORDER};
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}}
QLineEdit {{
    background-color: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    padding: 6px 10px;
    color: {_TEXT_PRI};
    font-size: 12px;
}}
QLineEdit:focus {{
    border-color: {_ACCENT};
}}
QComboBox {{
    background-color: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    padding: 6px 10px;
    color: {_TEXT_PRI};
    font-size: 12px;
    min-width: 110px;
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background-color: {_ELEVATED};
    color: {_TEXT_PRI};
    selection-background-color: {_ACCENT_DIM};
    border: 1px solid {_BORDER};
}}
QPushButton {{
    background-color: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    color: {_TEXT_PRI};
    padding: 5px 14px;
    font-size: 12px;
}}
QPushButton:hover {{
    background-color: {_ELEVATED};
    border-color: {_ACCENT};
}}
QPushButton:pressed {{
    background-color: {_ACCENT_DIM};
}}
QPushButton[class="danger"] {{
    background-color: rgba(200,60,60,0.12);
    color: {_ERROR};
    border: 1px solid rgba(200,60,60,0.28);
}}
QPushButton[class="danger"]:hover {{
    background-color: rgba(200,60,60,0.22);
    border-color: {_ERROR};
}}
QPlainTextEdit {{
    background-color: {_ELEVATED};
    border: none;
    color: {_TEXT_PRI};
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 12px;
    padding: 8px 10px;
}}
QMenu {{
    background-color: {_ELEVATED};
    color: {_TEXT_PRI};
    border: 1px solid {_BORDER};
    padding: 4px 0;
    font-size: 12px;
}}
QMenu::item {{
    padding: 5px 24px 5px 16px;
}}
QMenu::item:selected {{
    background-color: {_ACCENT_DIM};
    color: {_ACCENT};
}}
QMenu::separator {{
    height: 1px;
    background-color: {_BORDER};
    margin: 2px 8px;
}}
"""

# Row foreground colour by entry type / status
_COLORS = {
    'command':      QColor(_ACCENT),
    'wake_command': QColor(_ACCENT),
    'failed':       QColor(_ERROR),
}


def _fmt_ts(ts: str) -> str:
    """ISO timestamp -> 'YYYY-MM-DD HH:MM:SS' for table; lexicographic == chronological."""
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts[:19] if ts else ""


def _sec_btn(label: str, width: int = 0) -> QPushButton:
    b = QPushButton(label)
    if width:
        b.setFixedWidth(width)
    return b


def _danger_btn(label: str, width: int = 0) -> QPushButton:
    b = QPushButton(label)
    b.setProperty("class", "danger")
    b.style().unpolish(b)
    b.style().polish(b)
    if width:
        b.setFixedWidth(width)
    return b


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class HistoryQt:
    def __init__(self, app):
        self.app = app
        self._window = None
        self._init_posted = False

    def show(self):
        if self._window is not None:
            qt_runtime.post(self._window.show)
            qt_runtime.post(self._window.raise_)
            qt_runtime.post(self._window.activateWindow)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(self._init_window)

    def _init_window(self):
        """Runs on the Qt thread."""
        self._window = _HistoryWindow(self.app)
        self._window.show()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class _HistoryWindow(QMainWindow):
    # Emitted from clipboard threads to update the status label safely
    _status_signal = Signal(str)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self._db = getattr(app, 'history_db', None)
        self._status_signal.connect(self._set_status)

        self.setWindowTitle("Dictation History")
        self.resize(820, 580)
        self.setMinimumSize(500, 380)
        self.setStyleSheet(_STYLESHEET)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # ---- Search + filter row ----------------------------------------
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search history...")
        self._search.textChanged.connect(lambda _: self._reload())
        top_row.addWidget(self._search, stretch=1)

        self._filter = QComboBox()
        self._filter.addItems(["All", "Dictation", "Commands", "Failed"])
        self._filter.currentTextChanged.connect(lambda _: self._reload())
        top_row.addWidget(self._filter)

        refresh_btn = _sec_btn("Refresh", 80)
        refresh_btn.clicked.connect(self._reload)
        top_row.addWidget(refresh_btn)

        root.addLayout(top_row)

        # ---- History table ----------------------------------------------
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Date / Time", "Type", "Mode", "Text"])
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(30)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.currentCellChanged.connect(
            lambda row, _col, _prev_row, _prev_col: self._on_row_changed(row)
        )

        # Right-click context menu
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        # Double-click to copy
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        root.addWidget(self._table, stretch=1)

        # ---- Detail pane ------------------------------------------------
        detail_frame = QFrame()
        detail_frame.setObjectName("detail_frame")
        detail_frame.setStyleSheet(
            f"QFrame#detail_frame {{ background-color: {_ELEVATED};"
            f" border-top: 1px solid {_BORDER}; }}"
        )
        detail_frame_layout = QVBoxLayout(detail_frame)
        detail_frame_layout.setContentsMargins(0, 0, 0, 0)
        detail_frame_layout.setSpacing(0)
        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setFixedHeight(76)
        self._detail.setPlaceholderText("Select a row -- or double-click to copy.")
        detail_frame_layout.addWidget(self._detail)
        root.addWidget(detail_frame)

        # ---- Button bar -------------------------------------------------
        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.setContentsMargins(0, 2, 0, 0)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"color: {_TEXT_SEC}; font-size: 12px;")
        bar.addWidget(self._status_lbl)
        bar.addStretch()

        copy_btn = _sec_btn("Copy")
        copy_btn.clicked.connect(self._copy_selected)
        bar.addWidget(copy_btn)

        copy_all_btn = _sec_btn("Copy All")
        copy_all_btn.clicked.connect(self._copy_all)
        bar.addWidget(copy_all_btn)

        del_btn = _danger_btn("Delete")
        del_btn.clicked.connect(self._delete_selected)
        bar.addWidget(del_btn)

        clear_btn = _danger_btn("Clear All")
        clear_btn.clicked.connect(self._clear_all)
        bar.addWidget(clear_btn)

        close_btn = QPushButton("Close")
        close_btn.setFixedWidth(70)
        close_btn.clicked.connect(self.close)
        bar.addWidget(close_btn)

        root.addLayout(bar)

        self._reload()

    # ------------------------------------------------------------------
    def closeEvent(self, e):
        e.ignore()
        self.hide()

    # Data
    # ------------------------------------------------------------------

    def _reload(self):
        query   = self._search.text().strip()
        filter_ = self._filter.currentText()

        rows = []
        if self._db is not None:
            try:
                if query:
                    rows = list(self._db.search(query, limit=200))
                elif filter_ == "Failed":
                    rows = list(self._db.recent_filtered("failed", limit=200))
                else:
                    rows = list(self._db.recent(limit=200))

                if filter_ == "Commands":
                    rows = [r for r in rows
                            if r['entry_type'] in ('command', 'wake_command')]
                elif filter_ == "Dictation":
                    rows = [r for r in rows
                            if r['entry_type'] not in ('command', 'wake_command')]
            except Exception as exc:
                print(f"[HISTORY] DB error: {exc}")

        # Fallback to in-memory legacy list
        if not rows:
            q_lower = query.lower()
            rows = [
                {
                    'id':         None,
                    'timestamp':  ts,
                    'entry_type': 'command' if is_cmd else 'dictation',
                    'mode':       '',
                    'display_text': text,
                    'status':     'success',
                }
                for ts, text, is_cmd in reversed(getattr(self.app, 'history', []))
                if not query or q_lower in text.lower()
            ]
            if filter_ == "Commands":
                rows = [r for r in rows if r['entry_type'] == 'command']
            elif filter_ == "Dictation":
                rows = [r for r in rows if r['entry_type'] == 'dictation']

        self._table.setUpdatesEnabled(False)
        self._table.setRowCount(0)
        try:
            for row in rows:
                r = self._table.rowCount()
                self._table.insertRow(r)

                # Col 0: timestamp -- UserRole = row_id, ToolTip = raw ISO for detail pane
                raw_ts = str(row['timestamp'])
                ts_item = QTableWidgetItem(_fmt_ts(raw_ts))
                ts_item.setData(Qt.ItemDataRole.UserRole, row['id'])
                ts_item.setToolTip(raw_ts)
                self._table.setItem(r, 0, ts_item)

                # Col 1: entry type
                etype = str(row.get('entry_type', 'dictation'))
                type_label = {
                    'dictation':    'Dictation',
                    'command':      'Command',
                    'wake_command': 'Wake Cmd',
                }.get(etype, etype.title())
                self._table.setItem(r, 1, QTableWidgetItem(type_label))

                # Col 2: mode
                self._table.setItem(r, 2, QTableWidgetItem(
                    str(row.get('mode', ''))
                ))

                # Col 3: text -- UserRole = full text for copy/detail operations
                full = str(row.get('display_text', '') or row.get('raw_text', ''))
                display = full if len(full) <= 90 else full[:87] + "..."
                text_item = QTableWidgetItem(display)
                text_item.setData(Qt.ItemDataRole.UserRole, full)
                self._table.setItem(r, 3, text_item)

                # Foreground colour by type / status
                status = str(row.get('status', 'success'))
                color = _COLORS.get(etype) or (_COLORS.get('failed') if status == 'failed' else None)
                if color:
                    brush = QBrush(color)
                    for col in range(4):
                        item = self._table.item(r, col)
                        if item:
                            item.setForeground(brush)
        finally:
            self._table.setUpdatesEnabled(True)

        n = self._table.rowCount()
        self._status_lbl.setText(f"{n} entr{'y' if n == 1 else 'ies'}")
        self._detail.clear()

    # ------------------------------------------------------------------
    # Selection + full-text helpers
    # ------------------------------------------------------------------

    def _full_text_for_row(self, row: int) -> str:
        """Return stored full text for table row index `row`."""
        if row < 0:
            return ""
        item = self._table.item(row, 3)
        return (item.data(Qt.ItemDataRole.UserRole) or item.text()) if item else ""

    def _on_row_changed(self, row: int):
        if row < 0:
            self._detail.clear()
            return
        full_text = self._full_text_for_row(row)
        ts_item = self._table.item(row, 0)
        raw_ts = ts_item.toolTip() if ts_item else ""
        if raw_ts:
            self._detail.setPlainText(f"[{raw_ts}]\n{full_text}")
        else:
            self._detail.setPlainText(full_text)

    def _current_row(self) -> int:
        return self._table.currentRow()

    def _current_row_id(self):
        row = self._current_row()
        if row < 0:
            return None
        item = self._table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _current_full_text(self) -> str:
        return self._full_text_for_row(self._current_row())

    # ------------------------------------------------------------------
    # Context menu + double-click affordances (Item A)
    # ------------------------------------------------------------------

    def _on_cell_double_clicked(self, row: int, _col: int):
        """Double-click on any cell copies that row's full text."""
        text = self._full_text_for_row(row)
        if text:
            QApplication.clipboard().setText(text)
            self._set_status("Copied to clipboard.")

    def _on_context_menu(self, pos):
        """Right-click context menu: resolves row under cursor, not current selection."""
        row = self._table.rowAt(pos.y())
        if row < 0:
            return
        self._table.selectRow(row)
        menu = QMenu(self)
        copy_act = menu.addAction("Copy")
        menu.addSeparator()
        del_act = menu.addAction("Delete")
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == copy_act:
            text = self._full_text_for_row(row)
            if text:
                QApplication.clipboard().setText(text)
                self._set_status("Copied to clipboard.")
        elif action == del_act:
            self._delete_selected()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _copy_selected(self):
        text = self._current_full_text()
        if not text:
            QMessageBox.information(self, "No Selection", "Select a row to copy.")
            return
        QApplication.clipboard().setText(text)
        self._set_status("Copied to clipboard.")

    def _copy_all(self):
        texts = []
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 3)
            if item:
                texts.append(item.data(Qt.ItemDataRole.UserRole) or item.text())
        if not texts:
            QMessageBox.information(self, "Empty", "No history to copy.")
            return
        QApplication.clipboard().setText("\n".join(texts))
        self._set_status(f"Copied {len(texts)} entries to clipboard.")

    def _delete_selected(self):
        row_id = self._current_row_id()
        row    = self._current_row()
        if row < 0:
            QMessageBox.information(self, "No Selection", "Select a row to delete.")
            return
        if row_id is not None and self._db is not None:
            try:
                self._db.delete(row_id)
            except Exception as exc:
                QMessageBox.warning(self, "Error", f"Delete failed: {exc}")
                return
        self._table.removeRow(row)
        self._detail.clear()
        n = self._table.rowCount()
        self._set_status(f"{n} entr{'y' if n == 1 else 'ies'}")

    def _clear_all(self):
        reply = QMessageBox.question(
            self, "Clear History",
            "Clear all dictation history? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if self._db is not None:
            try:
                self._db.prune(max_entries=0)
            except Exception as exc:
                QMessageBox.warning(self, "Error", f"Clear failed: {exc}")
                return

        legacy = getattr(self.app, 'history', None)
        if legacy is not None:
            legacy.clear()
        if hasattr(self.app, 'save_history'):
            try:
                self.app.save_history()
            except Exception as e:
                logger.debug(f"_clear_all: {e}")

        self._reload()

    def _set_status(self, msg: str):
        self._status_lbl.setText(msg)
