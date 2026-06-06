"""PySide6 interactive task overlay.

Public API (unchanged):
    show(tasks) / hide() / refresh(tasks)

Features:
    - Text input at top — type and Enter to add a task
    - Checkbox per row — click to toggle completion
    - Delete button per row — click to remove
    - Hover highlight on each row
    - Voice commands still work via refresh()

Architecture note
-----------------
All Qt operations are posted to the shared qt_runtime event loop via
qt_runtime.post().  The overlay creates its window once on first show and
reuses it on every subsequent show — closeEvent hides rather than destroys
so the Python reference stays valid and reopening is always reliable.
"""

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QPushButton,
    QVBoxLayout, QWidget,
)

from samsara import tasks_store
from samsara.ui import qt_runtime

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

_BG       = "#0A0A0B"
_SURFACE  = "#111114"
_ELEVATED = "#1a1a1f"
_BORDER   = "#2a2a32"
_ACCENT   = "#5EEAD4"
_TEXT_PRI = "#E8E8EA"
_TEXT_MUT = "#55555C"
_RED      = "#f87171"

_SS = f"""
QMainWindow, QWidget {{
    background: {_BG};
    color: {_TEXT_PRI};
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
}}
QLineEdit {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 5px;
    color: {_TEXT_PRI};
    padding: 7px 10px;
    font-size: 13px;
    selection-background-color: {_ACCENT};
}}
QLineEdit:focus {{
    border-color: {_ACCENT};
}}
QScrollBar:vertical {{
    background: {_BG}; width: 5px; border: none;
}}
QScrollBar::handle:vertical {{
    background: {_BORDER}; border-radius: 2px; min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QCheckBox {{
    spacing: 0px;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 2px solid {_BORDER};
    border-radius: 4px;
    background: {_SURFACE};
}}
QCheckBox::indicator:hover {{
    border-color: {_ACCENT};
}}
QCheckBox::indicator:checked {{
    background: {_ACCENT};
    border-color: {_ACCENT};
}}
"""


# ---------------------------------------------------------------------------
# Task row widget
# ---------------------------------------------------------------------------

class _TaskRow(QWidget):
    def __init__(self, task: dict, on_change, parent=None):
        super().__init__(parent)
        self._task      = task
        self._on_change = on_change
        self._completed = task.get("completed", False)
        self._hovered   = False
        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 5, 6, 5)
        lay.setSpacing(8)

        self._check = QCheckBox()
        self._check.setChecked(self._completed)
        self._check.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self._check.toggled.connect(self._toggle)
        lay.addWidget(self._check)

        self._label = QLabel(self._task["text"])
        self._label.setWordWrap(False)
        self._label.setSizePolicy(
            self._label.sizePolicy().horizontalPolicy(),
            self._label.sizePolicy().verticalPolicy(),
        )
        lay.addWidget(self._label, stretch=1)

        self._del = QPushButton("x")
        self._del.setFixedSize(22, 22)
        self._del.setFocusPolicy(Qt.NoFocus)
        self._del.setCursor(Qt.PointingHandCursor)
        self._del.clicked.connect(self._remove)
        self._del.setStyleSheet(
            f"background: transparent; border: none;"
            f" color: {_TEXT_MUT}; font-size: 14px; font-weight: bold;"
            f" border-radius: 3px;"
        )
        self._del.enterEvent = lambda e: self._del.setStyleSheet(
            f"background: rgba(248,113,113,0.15); border: none;"
            f" color: {_RED}; font-size: 14px; font-weight: bold;"
            f" border-radius: 3px;"
        )
        self._del.leaveEvent = lambda e: self._del.setStyleSheet(
            f"background: transparent; border: none;"
            f" color: {_TEXT_MUT}; font-size: 14px; font-weight: bold;"
            f" border-radius: 3px;"
        )
        lay.addWidget(self._del)
        self._apply_text_style()

    def _apply_text_style(self):
        if self._completed:
            font = QFont("Segoe UI", 13)
            font.setStrikeOut(True)
            self._label.setFont(font)
            self._label.setStyleSheet(f"color: {_TEXT_MUT}; background: transparent;")
        else:
            self._label.setFont(QFont("Segoe UI", 13))
            self._label.setStyleSheet(f"color: {_TEXT_PRI}; background: transparent;")

    def _apply_style(self):
        bg = "rgba(255,255,255,0.04)" if self._hovered else "transparent"
        self.setStyleSheet(
            f"_TaskRow {{ background: {bg}; border-radius: 4px; }}"
        )

    def enterEvent(self, e):
        self._hovered = True
        self._apply_style()

    def leaveEvent(self, e):
        self._hovered = False
        self._apply_style()

    def _toggle(self, checked):
        if checked:
            tasks_store.complete_task(self._task["id"])
        else:
            tasks_store.uncomplete_task(self._task["id"])
        self._on_change()

    def _remove(self):
        tasks_store.remove_task(self._task["id"])
        self._on_change()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class _TaskWindow(QMainWindow):
    _refresh_sig = Signal(list)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tasks")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setStyleSheet(_SS)
        self.resize(340, 480)
        self.setMinimumSize(260, 320)
        self._setup_ui()
        self._render()
        self._refresh_sig.connect(self._on_refresh)

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)

        # Header
        header = QLabel("Tasks")
        header.setFont(QFont("Segoe UI", 14, QFont.Bold))
        header.setStyleSheet(
            f"color: {_ACCENT}; padding-bottom: 8px;"
            f" border-bottom: 1px solid {_BORDER}; background: transparent;"
        )
        lay.addWidget(header)

        # Input field
        self._input = QLineEdit()
        self._input.setPlaceholderText("Add a task...")
        self._input.returnPressed.connect(self._add_task)
        lay.addWidget(self._input)

        # Task list — no selection, custom row widgets via setItemWidget
        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.NoSelection)
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.setStyleSheet(
            f"QListWidget {{ background: transparent; border: none; outline: none; }}"
            f"QListWidget::item {{ padding: 0px; background: transparent; }}"
            f"QListWidget::item:selected {{ background: transparent; }}"
        )
        self._list.setSpacing(1)
        lay.addWidget(self._list, stretch=1)

        # Stats footer
        self._stats = QLabel()
        self._stats.setStyleSheet(
            f"color: {_TEXT_MUT}; font-size: 11px;"
            f" padding-top: 4px; border-top: 1px solid {_BORDER};"
            f" background: transparent;"
        )
        lay.addWidget(self._stats)

    def _render(self):
        self._list.clear()
        tasks     = tasks_store.get_all()
        active    = [t for t in tasks if not t.get("completed")]
        completed = [t for t in tasks if t.get("completed")]

        if not active and not completed:
            item = QListWidgetItem()
            item.setFlags(Qt.NoItemFlags)
            lbl = QLabel('No tasks yet. Say "add to list" to add one.')
            lbl.setStyleSheet(
                f"color: {_TEXT_MUT}; padding: 12px 10px;"
                f" background: transparent; font-style: italic;"
            )
            item.setSizeHint(lbl.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, lbl)
        else:
            for task in active + completed:
                row  = _TaskRow(task, on_change=self._render)
                item = QListWidgetItem()
                item.setSizeHint(row.sizeHint())
                item.setFlags(Qt.NoItemFlags)
                self._list.addItem(item)
                self._list.setItemWidget(item, row)

        done  = len(completed)
        total = len(tasks)
        self._stats.setText(f"{done} of {total} completed" if total else "")
        self._stats.setVisible(total > 0)

    def _add_task(self):
        text = self._input.text().strip()
        if not text:
            return
        tasks_store.add_task(text)
        self._input.clear()
        self._render()

    def showEvent(self, e):
        super().showEvent(e)
        # 3-arg form: binds to self so it fires on the Qt thread even if
        # showEvent is triggered by a cross-thread show() call.
        QTimer.singleShot(0, self, self._input.setFocus)

    def closeEvent(self, e):
        # Hide instead of destroy — keeps the Python reference valid so
        # reopening never requires recreating the window.
        e.ignore()
        self.hide()

    @Slot(list)
    def _on_refresh(self, _tasks: list):
        self._render()


# ---------------------------------------------------------------------------
# Public wrapper — unchanged API
# ---------------------------------------------------------------------------

class TaskOverlay:
    """Thread-safe wrapper around _TaskWindow using the shared qt_runtime."""

    def __init__(self):
        self._window: "_TaskWindow | None" = None
        self._init_posted = False

    def show(self, tasks: list):
        if self._window is not None:
            self._window._refresh_sig.emit(tasks)
            qt_runtime.post(self._window.show)
            qt_runtime.post(self._window.raise_)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(self._init_window)

    def hide(self):
        if self._window is not None:
            qt_runtime.post(self._window.hide)

    def refresh(self, tasks: list):
        if self._window is not None:
            self._window._refresh_sig.emit(tasks)

    def _init_window(self):
        """Runs on the Qt thread via qt_runtime.post()."""
        self._window = _TaskWindow()
        self._window.show()
