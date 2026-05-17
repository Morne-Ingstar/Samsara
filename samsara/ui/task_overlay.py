"""PySide6 overlay for the local task list.

Replaces the pywebview implementation. TaskOverlay keeps the same public API:
    show(tasks) / hide() / refresh(tasks)
so tasks.py needs no changes.
"""

import threading

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------------------
# Colours — match the rest of the Samsara Qt palette
# ---------------------------------------------------------------------------

_BG       = "#0b0e14"
_SURFACE  = "#131820"
_ACCENT   = "#5EEAD4"
_TEXT_PRI = "#e4e8ef"
_TEXT_SEC = "#5A5A62"
_BORDER   = "#2a3345"

_SS = f"""
QMainWindow, QWidget {{ background: {_BG}; color: {_TEXT_PRI};
    font-family: 'Segoe UI', sans-serif; font-size: 13px; }}
QListWidget {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    outline: none;
    padding: 4px 0;
}}
QListWidget::item {{ padding: 6px 10px; border-radius: 3px; }}
QListWidget::item:selected {{ background: transparent; }}
QScrollBar:vertical {{
    background: {_BG}; width: 6px; border: none;
}}
QScrollBar::handle:vertical {{
    background: {_BORDER}; border-radius: 3px; min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


# ---------------------------------------------------------------------------
# Internal Qt window
# ---------------------------------------------------------------------------

class _TaskWindow(QMainWindow):
    _refresh_sig = Signal(list)

    def __init__(self, tasks: list):
        super().__init__()
        self._tasks = tasks
        self.setWindowTitle("Tasks")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setStyleSheet(_SS)
        self.resize(340, 460)
        self.setMinimumSize(260, 300)
        self._setup_ui()
        self._render()
        self._refresh_sig.connect(self._on_refresh)

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(14, 14, 14, 10)
        lay.setSpacing(10)

        header = QLabel("Tasks")
        header.setFont(QFont("Segoe UI", 14, QFont.Bold))
        header.setStyleSheet(
            f"color: {_ACCENT}; padding-bottom: 8px;"
            f" border-bottom: 1px solid {_BORDER};"
        )
        lay.addWidget(header)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.NoSelection)
        self._list.setFocusPolicy(Qt.NoFocus)
        lay.addWidget(self._list, stretch=1)

        self._stats = QLabel()
        self._stats.setStyleSheet(
            f"color: {_TEXT_SEC}; font-size: 11px;"
            f" padding-top: 4px; border-top: 1px solid {_BORDER};"
        )
        lay.addWidget(self._stats)

    def _render(self):
        self._list.clear()
        active    = [t for t in self._tasks if not t.get("completed")]
        completed = [t for t in self._tasks if t.get("completed")]

        if not active and not completed:
            placeholder = QListWidgetItem('No tasks yet. Say "add to list" to create one.')
            placeholder.setForeground(QColor(_TEXT_SEC))
            placeholder.setFlags(Qt.NoItemFlags)
            self._list.addItem(placeholder)
        else:
            for i, task in enumerate(active, 1):
                item = QListWidgetItem(f"{i}.  {task['text']}")
                item.setForeground(QColor(_TEXT_PRI))
                num_font = QFont("Segoe UI", 13)
                item.setFont(num_font)
                self._list.addItem(item)

            if completed:
                sep = QListWidgetItem("  Completed")
                sep.setForeground(QColor(_TEXT_SEC))
                sep.setFont(QFont("Segoe UI", 10, QFont.Bold))
                sep.setFlags(Qt.NoItemFlags)
                self._list.addItem(sep)

                for task in completed:
                    item = QListWidgetItem(f"  {task['text']}")
                    font = QFont("Segoe UI", 13)
                    font.setStrikeOut(True)
                    item.setFont(font)
                    item.setForeground(QColor(_TEXT_SEC))
                    self._list.addItem(item)

        done  = len(completed)
        total = len(self._tasks)
        self._stats.setText(f"{done} of {total} completed" if total else "")
        self._stats.setVisible(total > 0)

    @Slot(list)
    def _on_refresh(self, tasks: list):
        self._tasks = tasks
        self._render()


# ---------------------------------------------------------------------------
# Public wrapper — same API as the old pywebview TaskOverlay
# ---------------------------------------------------------------------------

class TaskOverlay:
    def __init__(self):
        self._window: "_TaskWindow | None" = None
        self._thread: "threading.Thread | None" = None

    # ----------------------------------------------------------------
    # Public API (callable from any thread)
    # ----------------------------------------------------------------

    def show(self, tasks: list):
        if self._window is not None:
            self._window._refresh_sig.emit(tasks)
            QTimer.singleShot(0, self._window.show)
            QTimer.singleShot(0, self._window.raise_)
        else:
            self._thread = threading.Thread(
                target=self._create, args=(tasks,),
                daemon=True, name="task-overlay",
            )
            self._thread.start()

    def hide(self):
        if self._window is not None:
            QTimer.singleShot(0, self._window.hide)

    def refresh(self, tasks: list):
        if self._window is not None:
            self._window._refresh_sig.emit(tasks)

    # ----------------------------------------------------------------
    # Thread
    # ----------------------------------------------------------------

    def _create(self, tasks: list):
        qt_app = QApplication.instance()
        owns_app = qt_app is None
        if qt_app is None:
            qt_app = QApplication([])

        if owns_app:
            self._window = _TaskWindow(tasks)
            self._window.destroyed.connect(self._on_destroyed)
            self._window.show()
            qt_app.exec()
            self._window = None
        else:
            QTimer.singleShot(0, qt_app, lambda: self._init_window(tasks))

    def _init_window(self, tasks: list):
        self._window = _TaskWindow(tasks)
        self._window.destroyed.connect(self._on_destroyed)
        self._window.show()

    def _on_destroyed(self):
        self._window = None
