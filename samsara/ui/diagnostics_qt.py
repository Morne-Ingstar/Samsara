"""
PySide6 "Dictation Diagnostics" window for Samsara.

Reads from samsara.diagnostics's in-memory ring buffer -- per-utterance
audio stats, Whisper quality signals, per-stage timings, and a plain-English
verdict -- so problems like "wrong model configured", "mic too quiet", or
"smart_correct is the slow stage" are visible without log archaeology.

Persistent, hide-don't-destroy window (same family as history_qt.py):
built once on the Qt thread via qt_runtime.post(), closeEvent ignores the
close and hides instead of destroying, no wrapper close() method.
"""

import json
from dataclasses import asdict

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QCheckBox, QPushButton, QLabel, QPlainTextEdit, QFrame, QMenu,
)

from samsara.ui import qt_runtime, theme
from samsara import diagnostics

from samsara.log import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Colour palette -- sourced from samsara.ui.theme, same convention as
# history_qt.py (local aliases so the stylesheet below stays readable).
# ---------------------------------------------------------------------------

_BG        = theme.BG0
_SURFACE   = theme.BG1
_ELEVATED  = theme.BG2
_BORDER    = theme.BORDER
_ACCENT    = theme.ACCENT
_ACCENT_DIM = "#1a3a42"
_TEXT_PRI  = theme.TEXT_PRIMARY
_TEXT_SEC  = theme.TEXT_SECONDARY
_ERROR     = theme.ERROR
_WARNING   = theme.WARNING

_AUTO_REFRESH_MS = 2000
_ROW_HEIGHT = 28

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
    font-size: {theme.FONT_SIZE_CAPTION}px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}}
QCheckBox {{ color: {_TEXT_PRI}; font-size: {theme.FONT_SIZE_BODY}px; }}
QPushButton {{
    background-color: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    color: {_TEXT_PRI};
    padding: 5px 14px;
    font-size: {theme.FONT_SIZE_CAPTION}px;
}}
QPushButton:hover {{
    background-color: {_ELEVATED};
    border-color: {_ACCENT};
}}
QPushButton:pressed {{
    background-color: {_ACCENT_DIM};
}}
QPlainTextEdit {{
    background-color: {_ELEVATED};
    border: none;
    color: {_TEXT_PRI};
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: {theme.FONT_SIZE_CAPTION}px;
    padding: 8px 10px;
}}
QMenu {{
    background-color: {_ELEVATED};
    color: {_TEXT_PRI};
    border: 1px solid {_BORDER};
    padding: 4px 0;
    font-size: {theme.FONT_SIZE_CAPTION}px;
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

_COLUMNS = ["Time", "Mode", "Audio (s)", "Total (ms)", "Transcribe (ms)",
            "Smart (ms)", "Verdict", "Text"]

# Verdict-string keyword buckets for row severity colouring.
_RED_KEYWORDS = ("hallucination", "no output")
_AMBER_KEYWORDS = ("slow", "small model", "fallback", "low confidence",
                   "accidental hold")


def _row_color(verdicts) -> "QColor | None":
    joined = " ".join(verdicts).lower()
    if any(kw in joined for kw in _RED_KEYWORDS):
        return QColor(_ERROR)
    if any(kw in joined for kw in _AMBER_KEYWORDS):
        return QColor(_WARNING)
    return None


def _fmt_ts(ts: str) -> str:
    """ISO timestamp -> 'HH:MM:SS' for the table (full ISO stays in tooltip)."""
    try:
        return ts.split("T")[1].split(".")[0] if "T" in ts else ts
    except Exception:
        return ts or ""


def _fmt_record_detail(rec) -> str:
    lines = [f"{k}: {v}" for k, v in asdict(rec).items() if k != 'verdicts']
    lines.append("verdicts:")
    for v in rec.verdicts:
        lines.append(f"  - {v}")
    return "\n".join(lines)


def _sec_btn(label: str, width: int = 0) -> QPushButton:
    b = QPushButton(label)
    if width:
        b.setFixedWidth(width)
    return b


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class DiagnosticsQt:
    """Persistent, hide-don't-destroy window wrapper (history_qt.py pattern)."""

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
        self._window = DiagnosticsWindow(self.app)
        self._window.show()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class DiagnosticsWindow(QMainWindow):

    def __init__(self, app):
        super().__init__()
        self.app = app

        self.setWindowTitle("Dictation Diagnostics")
        self.resize(920, 620)
        self.setMinimumSize(600, 420)
        self.setStyleSheet(_STYLESHEET)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # ---- Top bar ------------------------------------------------------
        top_row = QHBoxLayout()
        top_row.setSpacing(10)

        self._model_lbl = QLabel("")
        self._model_lbl.setStyleSheet(f"color: {_TEXT_SEC}; font-size: {theme.FONT_SIZE_CAPTION}px;")
        top_row.addWidget(self._model_lbl)

        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet(f"color: {_TEXT_SEC}; font-size: {theme.FONT_SIZE_CAPTION}px;")
        top_row.addWidget(self._count_lbl)

        top_row.addStretch()

        self._write_jsonl_cb = QCheckBox("Write to file")
        diag_cfg = self.app.config.get('diagnostics', {}) or {}
        self._write_jsonl_cb.setChecked(bool(diag_cfg.get('write_jsonl', False)))
        self._write_jsonl_cb.toggled.connect(self._on_write_jsonl_toggled)
        top_row.addWidget(self._write_jsonl_cb)

        clear_btn = _sec_btn("Clear")
        clear_btn.clicked.connect(self._on_clear)
        top_row.addWidget(clear_btn)

        refresh_btn = _sec_btn("Refresh")
        refresh_btn.clicked.connect(self._reload)
        top_row.addWidget(refresh_btn)

        close_btn = _sec_btn("Close", 70)
        close_btn.clicked.connect(self.hide)
        top_row.addWidget(close_btn)

        root.addLayout(top_row)

        # ---- Table ----------------------------------------------------------
        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        hh = self._table.horizontalHeader()
        for col in range(len(_COLUMNS) - 1):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(len(_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.currentCellChanged.connect(
            lambda row, _col, _prev_row, _prev_col: self._on_row_changed(row)
        )
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.cellDoubleClicked.connect(lambda row, _col: self._copy_text_for_row(row))
        self._table.installEventFilter(self)

        root.addWidget(self._table, stretch=1)

        # ---- Detail pane ------------------------------------------------
        detail_frame = QFrame()
        detail_frame.setObjectName("detail_frame")
        detail_frame.setStyleSheet(
            f"QFrame#detail_frame {{ background-color: {_ELEVATED};"
            f" border-top: 1px solid {_BORDER}; }}"
        )
        detail_layout = QVBoxLayout(detail_frame)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(0)
        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setFixedHeight(140)
        self._detail.setPlaceholderText("Select a row for full details.")
        detail_layout.addWidget(self._detail)
        root.addWidget(detail_frame)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"color: {_TEXT_SEC}; font-size: {theme.FONT_SIZE_CAPTION}px;")
        root.addWidget(self._status_lbl)

        self._records = []  # newest-first, parallel to table rows
        self._reload()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_timer_tick)
        self._timer.start(_AUTO_REFRESH_MS)

    # ------------------------------------------------------------------
    def closeEvent(self, e):
        e.ignore()
        self.hide()

    def eventFilter(self, obj, event):
        if obj is self._table and event.type() == event.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._copy_text_for_row(self._table.currentRow())
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _on_timer_tick(self):
        if self.isVisible():
            self._reload()

    def _reload(self):
        cfg = self.app.config
        model = cfg.get('model_size', '?')
        device = cfg.get('device', '?')
        compute = cfg.get('compute_type', '?')
        self._model_lbl.setText(f"Model: {model}   Device: {device} ({compute})")

        records = list(reversed(diagnostics.recent(200)))  # newest first
        self._records = records
        self._count_lbl.setText(f"{len(records)} utterance{'s' if len(records) != 1 else ''}")

        selected_row = self._table.currentRow()

        self._table.setUpdatesEnabled(False)
        try:
            self._table.setRowCount(0)
            for rec in records:
                r = self._table.rowCount()
                self._table.insertRow(r)

                ts_item = QTableWidgetItem(_fmt_ts(rec.ts))
                ts_item.setToolTip(rec.ts)
                self._table.setItem(r, 0, ts_item)
                self._table.setItem(r, 1, QTableWidgetItem(rec.mode))
                self._table.setItem(r, 2, QTableWidgetItem(f"{rec.audio_s:.2f}"))
                self._table.setItem(r, 3, QTableWidgetItem(str(rec.t_total_ms)))
                self._table.setItem(r, 4, QTableWidgetItem(str(rec.t_transcribe_ms)))
                self._table.setItem(r, 5, QTableWidgetItem(str(rec.t_smart_ms)))
                verdict_summary = rec.verdicts[0] if rec.verdicts else ""
                self._table.setItem(r, 6, QTableWidgetItem(verdict_summary))
                text_preview = rec.text if len(rec.text) <= 90 else rec.text[:87] + "..."
                self._table.setItem(r, 7, QTableWidgetItem(text_preview))

                color = _row_color(rec.verdicts)
                if color:
                    brush = QBrush(color)
                    for col in range(len(_COLUMNS)):
                        item = self._table.item(r, col)
                        if item:
                            item.setForeground(brush)
        finally:
            self._table.setUpdatesEnabled(True)

        if 0 <= selected_row < self._table.rowCount():
            self._table.selectRow(selected_row)
        else:
            self._detail.clear()

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------

    def _record_for_row(self, row: int):
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    def _on_row_changed(self, row: int):
        rec = self._record_for_row(row)
        if rec is None:
            self._detail.clear()
            return
        self._detail.setPlainText(_fmt_record_detail(rec))

    # ------------------------------------------------------------------
    # Context menu + copy actions
    # ------------------------------------------------------------------

    def _copy_text_for_row(self, row: int):
        rec = self._record_for_row(row)
        if rec is None:
            return
        QApplication.clipboard().setText(rec.text)
        self._set_status("Copied text to clipboard.")

    def _copy_full_record_for_row(self, row: int):
        rec = self._record_for_row(row)
        if rec is None:
            return
        try:
            payload = json.dumps(asdict(rec), indent=2)
        except Exception:
            payload = _fmt_record_detail(rec)
        QApplication.clipboard().setText(payload)
        self._set_status("Copied full record to clipboard.")

    def _on_context_menu(self, pos):
        row = self._table.rowAt(pos.y())
        if row < 0:
            return
        self._table.selectRow(row)
        menu = QMenu(self)
        copy_text_act = menu.addAction("Copy text")
        copy_full_act = menu.addAction("Copy full record")
        action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if action == copy_text_act:
            self._copy_text_for_row(row)
        elif action == copy_full_act:
            self._copy_full_record_for_row(row)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_clear(self):
        diagnostics.clear()
        self._reload()
        self._set_status("Cleared.")

    def _on_write_jsonl_toggled(self, checked: bool):
        try:
            diag_cfg = dict(self.app.config.get('diagnostics', {}) or {})
            diag_cfg['write_jsonl'] = checked
            self.app.update_config_and_save({'diagnostics': diag_cfg})
            self._set_status(f"Write to file: {'on' if checked else 'off'}")
        except Exception as exc:
            logger.debug(f"[DIAG-UI] write_jsonl toggle save failed: {exc}")

    def _set_status(self, msg: str):
        self._status_lbl.setText(msg)
