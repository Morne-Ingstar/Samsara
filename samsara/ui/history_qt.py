"""
PySide6 dictation history window for Samsara.

Reads from app.history_db (HistoryManager / SQLite) with a fallback
to the legacy app.history list.  Runs on its own daemon thread —
same pattern as settings_qt.py.
"""

import threading
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QLineEdit, QComboBox, QPushButton, QLabel, QPlainTextEdit,
    QFrame, QMessageBox,
)

from samsara.ui import qt_runtime


# ---------------------------------------------------------------------------
# Stylesheet — self-contained dark theme matching Samsara palette
# ---------------------------------------------------------------------------

_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #0A0A0B;
    color: #E8E8EA;
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 13px;
}
QTableWidget {
    background-color: #111114;
    gridline-color: rgba(255,255,255,0.05);
    color: #E8E8EA;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 6px;
    outline: none;
}
QTableWidget::item {
    padding: 5px 8px;
    border: none;
}
QTableWidget::item:selected {
    background-color: rgba(94,234,212,0.15);
    color: #E8E8EA;
}
QTableWidget {
    alternate-background-color: rgba(255,255,255,0.02);
}
QHeaderView::section {
    background-color: #16161A;
    color: #8A8A92;
    padding: 6px 8px;
    border: none;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    border-right: 1px solid rgba(255,255,255,0.04);
    font-size: 12px;
    font-weight: 600;
}
QLineEdit {
    background-color: #16161A;
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 6px;
    padding: 7px 12px;
    color: #E8E8EA;
}
QLineEdit:focus {
    border-color: rgba(94,234,212,0.5);
}
QComboBox {
    background-color: #16161A;
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 6px;
    padding: 6px 10px;
    color: #E8E8EA;
    min-width: 100px;
}
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView {
    background-color: #16161A;
    color: #E8E8EA;
    selection-background-color: rgba(94,234,212,0.2);
    border: 1px solid rgba(255,255,255,0.14);
}
QPushButton {
    background-color: #5EEAD4;
    color: #0A0A0B;
    border: none;
    border-radius: 6px;
    padding: 7px 16px;
    font-weight: 600;
    font-size: 13px;
}
QPushButton:hover { background-color: #4DD8C2; }
QPushButton[class="secondary"] {
    background-color: transparent;
    color: #8A8A92;
    border: 1px solid rgba(255,255,255,0.14);
}
QPushButton[class="secondary"]:hover {
    background-color: rgba(255,255,255,0.05);
    color: #E8E8EA;
}
QPushButton[class="danger"] {
    background-color: rgba(200,60,60,0.15);
    color: #FF8888;
    border: 1px solid rgba(200,60,60,0.3);
}
QPushButton[class="danger"]:hover { background-color: rgba(200,60,60,0.25); }
QPlainTextEdit {
    background-color: #111114;
    border: none;
    color: #E8E8EA;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 12px;
    padding: 6px 8px;
}
"""

# Row colour by entry type / status
_COLORS = {
    'command':      QColor("#5EEAD4"),
    'wake_command': QColor("#5EEAD4"),
    'failed':       QColor("#FF6666"),
}


def _fmt_ts(ts: str) -> str:
    """ISO timestamp -> 'HH:MM:SS' for table, full ISO in detail."""
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except Exception:
        return (ts[:8] if ts else "")


def _sec_btn(label: str, width: int = 0) -> QPushButton:
    b = QPushButton(label)
    b.setProperty("class", "secondary")
    b.style().unpolish(b)
    b.style().polish(b)
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
        self.resize(720, 560)
        self.setMinimumSize(500, 380)
        self.setStyleSheet(_STYLESHEET)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # ---- Search + filter row ----------------------------------------
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search history…")
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
        self._table.setHorizontalHeaderLabels(["Time", "Type", "Mode", "Text"])
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.currentRowChanged.connect(self._on_row_changed)
        root.addWidget(self._table, stretch=1)

        # ---- Detail pane ------------------------------------------------
        detail_frame = QFrame()
        detail_frame.setStyleSheet(
            "QFrame{background:#111114;"
            "border:1px solid rgba(255,255,255,0.08);border-radius:6px;}"
        )
        detail_frame_layout = QVBoxLayout(detail_frame)
        detail_frame_layout.setContentsMargins(0, 0, 0, 0)
        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setFixedHeight(64)
        self._detail.setPlaceholderText("Select a row to see the full text.")
        detail_frame_layout.addWidget(self._detail)
        root.addWidget(detail_frame)

        # ---- Button bar -------------------------------------------------
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: #8A8A92; font-size: 12px;")
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

                # Col 0: timestamp — stores row_id in UserRole
                ts_item = QTableWidgetItem(_fmt_ts(str(row['timestamp'])))
                ts_item.setData(Qt.ItemDataRole.UserRole, row['id'])
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

                # Col 3: text — stores full text in UserRole
                full = str(row.get('display_text', '') or row.get('raw_text', ''))
                display = full if len(full) <= 90 else full[:87] + "…"
                text_item = QTableWidgetItem(display)
                text_item.setData(Qt.ItemDataRole.UserRole, full)
                self._table.setItem(r, 3, text_item)

                # Colour by type / status
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
    # Selection
    # ------------------------------------------------------------------

    def _on_row_changed(self, row: int):
        if row < 0:
            self._detail.clear()
            return
        item = self._table.item(row, 3)
        if item:
            self._detail.setPlainText(
                item.data(Qt.ItemDataRole.UserRole) or item.text()
            )

    def _current_row(self) -> int:
        return self._table.currentRow()

    def _current_row_id(self):
        row = self._current_row()
        if row < 0:
            return None
        item = self._table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _current_full_text(self) -> str:
        row = self._current_row()
        if row < 0:
            return ""
        item = self._table.item(row, 3)
        return (item.data(Qt.ItemDataRole.UserRole) or item.text()) if item else ""

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
            except Exception:
                pass

        self._reload()

    def _set_status(self, msg: str):
        self._status_lbl.setText(msg)
