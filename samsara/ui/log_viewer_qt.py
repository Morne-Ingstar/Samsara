"""
PySide6 "View Live Log" window for Samsara.

Tail-follows ~/.samsara/logs/samsara.log (the same RotatingFileHandler file
dictation.py's own logging bootstrap writes to -- see samsara/log.py's
SAMSARA_LOG_FILE) so the live log is one click away instead of requiring a
text editor.

Persistent, hide-don't-destroy window (same family as history_qt.py/
diagnostics_qt.py): built once on the Qt thread via qt_runtime.post(),
closeEvent ignores the close and hides instead of destroying, no wrapper
close() method.

Tail/rotation logic itself lives in samsara/log_tailer.py (pure, no Qt) --
this module only drives it on a QTimer and renders the result.
"""

import os
from collections import deque

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPlainTextEdit, QPushButton, QLineEdit, QLabel,
)

from samsara.ui import qt_runtime, theme
from samsara.log import get_logger, SAMSARA_LOG_FILE
from samsara.log_tailer import LogTailer

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Colour palette -- sourced from samsara.ui.theme, same convention as
# diagnostics_qt.py/history_qt.py.
# ---------------------------------------------------------------------------

_BG        = theme.BG0
_SURFACE   = theme.BG1
_ELEVATED  = theme.BG2
_BORDER    = theme.BORDER
_ACCENT    = theme.ACCENT
_ACCENT_DIM = "#1a3a42"
_TEXT_PRI  = theme.TEXT_PRIMARY
_TEXT_SEC  = theme.TEXT_SECONDARY

_POLL_MS = 500
_MAX_BLOCK_COUNT = 5000
_MAX_BUFFERED_LINES = 5000
_SCROLL_BOTTOM_TOLERANCE = 4

_STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {_BG};
    color: {_TEXT_PRI};
    font-family: {theme.FONT_FAMILY};
    font-size: {theme.FONT_SIZE_BODY}px;
}}
QPlainTextEdit {{
    background-color: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 6px;
    color: {_TEXT_PRI};
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: {theme.FONT_SIZE_CAPTION}px;
    padding: 8px;
}}
QLineEdit {{
    background-color: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 8px;
    padding: 8px 12px;
    color: {_TEXT_PRI};
    font-size: {theme.FONT_SIZE_BODY}px;
}}
QLineEdit:focus {{ border-color: {_ACCENT}; }}
QPushButton {{
    background-color: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 8px;
    color: {_TEXT_PRI};
    padding: 8px 16px;
    font-size: {theme.FONT_SIZE_BODY}px;
}}
QPushButton:hover {{
    background-color: {_ELEVATED};
    border-color: {_ACCENT};
}}
QPushButton:pressed {{ background-color: {_ACCENT_DIM}; }}
QPushButton:checkable:checked {{
    background-color: {_ACCENT_DIM};
    color: {_ACCENT};
    border-color: {_ACCENT};
}}
"""


def _matches_filter(line: str, filter_text: str) -> bool:
    if not filter_text:
        return True
    return filter_text.lower() in line.lower()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class LogViewerQt:
    """Persistent, hide-don't-destroy window wrapper (diagnostics_qt.py pattern)."""

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
        self._window = LogViewerWindow(self.app)
        self._window.show()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class LogViewerWindow(QMainWindow):

    def __init__(self, app):
        super().__init__()
        self.app = app

        self.setWindowTitle("Samsara — Live Log")
        self.resize(920, 620)
        self.setMinimumSize(560, 400)
        self.setStyleSheet(_STYLESHEET)

        self._log_path = SAMSARA_LOG_FILE
        self._tailer = LogTailer(self._log_path)
        self._all_lines = deque(maxlen=_MAX_BUFFERED_LINES)
        self._filter_text = ""

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # ---- Top bar --------------------------------------------------
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        self._follow_btn = QPushButton("Follow")
        self._follow_btn.setCheckable(True)
        self._follow_btn.setChecked(True)
        self._follow_btn.clicked.connect(self._on_follow_clicked)
        top_row.addWidget(self._follow_btn)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Filter…")
        self._filter_edit.textChanged.connect(self._on_filter_changed)
        top_row.addWidget(self._filter_edit, stretch=1)

        copy_btn = QPushButton("Copy All")
        copy_btn.clicked.connect(self._on_copy_all)
        top_row.addWidget(copy_btn)

        open_folder_btn = QPushButton("Open Log Folder")
        open_folder_btn.clicked.connect(self._on_open_log_folder)
        top_row.addWidget(open_folder_btn)

        root.addLayout(top_row)

        # ---- Log text -----------------------------------------------------
        self._text_edit = QPlainTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setMaximumBlockCount(_MAX_BLOCK_COUNT)
        self._text_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        root.addWidget(self._text_edit, stretch=1)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"color: {_TEXT_SEC}; font-size: {theme.FONT_SIZE_CAPTION}px;"
        )
        root.addWidget(self._status_lbl)

        self._load_initial()

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._on_timer_tick)
        self._timer.start()

    # ------------------------------------------------------------------
    def closeEvent(self, e):
        e.ignore()
        self.hide()

    # ------------------------------------------------------------------
    # Loading / polling
    # ------------------------------------------------------------------

    def _load_initial(self):
        try:
            lines = self._tailer.initial_tail()
        except Exception as exc:
            logger.debug(f"[LOGVIEW] initial_tail failed: {exc}")
            lines = []
        self._all_lines.extend(lines)
        self._render_all()
        self._scroll_to_bottom()
        self._set_status(f"{len(self._all_lines)} lines — {self._log_path}")

    def _on_timer_tick(self):
        if not self.isVisible():
            return
        try:
            new_lines = self._tailer.poll()
        except Exception as exc:
            logger.debug(f"[LOGVIEW] poll failed: {exc}")
            return
        if new_lines:
            self._append_lines(new_lines)

    def _append_lines(self, lines):
        was_at_bottom = self._is_at_bottom()

        for line in lines:
            self._all_lines.append(line)
            if _matches_filter(line, self._filter_text):
                self._text_edit.appendPlainText(line)

        if was_at_bottom or self._follow_btn.isChecked():
            self._scroll_to_bottom()
        else:
            # User scrolled away to read something -- stop fighting them;
            # the (unchecked) Follow button becomes the "resume" affordance.
            self._follow_btn.setChecked(False)

        self._set_status(f"{len(self._all_lines)} lines — {self._log_path}")

    # ------------------------------------------------------------------
    # Rendering / filtering
    # ------------------------------------------------------------------

    def _render_all(self):
        self._text_edit.clear()
        matching = [l for l in self._all_lines if _matches_filter(l, self._filter_text)]
        if matching:
            self._text_edit.setPlainText("\n".join(matching))

    def _on_filter_changed(self, text: str):
        self._filter_text = text.strip()
        was_at_bottom = self._is_at_bottom()
        self._render_all()
        if was_at_bottom or self._follow_btn.isChecked():
            self._scroll_to_bottom()

    # ------------------------------------------------------------------
    # Scroll helpers
    # ------------------------------------------------------------------

    def _is_at_bottom(self) -> bool:
        sb = self._text_edit.verticalScrollBar()
        return sb.value() >= sb.maximum() - _SCROLL_BOTTOM_TOLERANCE

    def _scroll_to_bottom(self):
        sb = self._text_edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_follow_clicked(self):
        if self._follow_btn.isChecked():
            self._scroll_to_bottom()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_copy_all(self):
        QApplication.clipboard().setText(self._text_edit.toPlainText())
        self._set_status("Copied to clipboard.")

    def _on_open_log_folder(self):
        try:
            os.startfile(str(self._log_path.parent))
        except Exception as exc:
            logger.debug(f"[LOGVIEW] open log folder failed: {exc}")

    def _set_status(self, msg: str):
        self._status_lbl.setText(msg)
