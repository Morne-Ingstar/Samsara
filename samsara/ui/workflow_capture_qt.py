"""Workflow Capture review window.

Presents captured events as a checkable list. The user unchecks anything they
don't want analyzed. The Analyze button is the privacy gate — NOTHING is sent
to any AI until it is clicked, and only checked items are included.

Persistent window (HIDE on close); content is replaced each time show_review()
is called. Active-capture indicator is a status bar label updated from outside.
"""

import threading

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QScrollArea, QCheckBox, QComboBox,
    QPushButton, QTextEdit, QFrame, QSizePolicy,
)

from samsara.ui import qt_runtime

_log = __import__('logging').getLogger(__name__)


# ---------------------------------------------------------------------------
# Signals bridge  (thread-safe calls into the Qt window)
# ---------------------------------------------------------------------------

class _Signals(QObject):
    load_events   = Signal(list)   # list[CaptureEvent]
    set_indicator = Signal(bool)   # True = capturing
    set_result    = Signal(str)    # AI response text
    set_busy      = Signal(bool)   # True = analysis running


# ---------------------------------------------------------------------------
# Review window widget
# ---------------------------------------------------------------------------

class _ReviewWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._app_ref = None    # Samsara app object; set by show_review()
        self._events: list = []
        self._checkboxes: list = []

        self.setWindowTitle("Workflow Capture — Review")
        self.setMinimumSize(700, 560)
        self.resize(780, 640)

        self._signals = _Signals(self)
        self._signals.load_events.connect(self._load_events)
        self._signals.set_indicator.connect(self._set_indicator)
        self._signals.set_result.connect(self._set_result)
        self._signals.set_busy.connect(self._set_busy)

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(10, 10, 10, 10)

        # --- title + indicator ---
        top = QHBoxLayout()
        self._title_label = QLabel("Workflow Capture Review")
        self._title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        top.addWidget(self._title_label)
        top.addStretch()
        self._indicator = QLabel("RECORDING")
        self._indicator.setStyleSheet(
            "color: white; background: #c0392b; "
            "padding: 2px 8px; border-radius: 4px; font-weight: bold;"
        )
        self._indicator.setVisible(False)
        top.addWidget(self._indicator)
        root.addLayout(top)

        # --- event count label ---
        self._count_label = QLabel("No events. Use 'start capture' to begin.")
        root.addWidget(self._count_label)

        # --- instruction ---
        hint = QLabel(
            "Uncheck items you do NOT want analyzed. "
            "Nothing is sent until you click Analyze."
        )
        hint.setStyleSheet("color: #777; font-size: 11px;")
        root.addWidget(hint)

        # --- scrollable checkbox list ---
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.StyledPanel)
        self._scroll.setMinimumHeight(220)

        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setSpacing(2)
        self._list_layout.setContentsMargins(6, 4, 6, 4)
        self._list_layout.addStretch()
        self._scroll.setWidget(self._list_widget)
        root.addWidget(self._scroll)

        # --- select-all / deselect-all ---
        sel_row = QHBoxLayout()
        btn_all = QPushButton("Select all")
        btn_all.setFixedWidth(90)
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none = QPushButton("Deselect all")
        btn_none.setFixedWidth(90)
        btn_none.clicked.connect(lambda: self._set_all(False))
        sel_row.addWidget(btn_all)
        sel_row.addWidget(btn_none)
        sel_row.addStretch()
        root.addLayout(sel_row)

        # --- separator ---
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        root.addWidget(line)

        # --- provider + analyze ---
        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("Analyze with:"))
        self._provider = QComboBox()
        self._provider.addItems(["Local (Ollama)", "Cloud LLM"])
        self._provider.setFixedWidth(160)
        ctrl_row.addWidget(self._provider)
        ctrl_row.addSpacing(16)
        self._analyze_btn = QPushButton("Analyze selected events")
        self._analyze_btn.setStyleSheet(
            "QPushButton { background: #2980b9; color: white; padding: 4px 14px; border-radius: 4px; }"
            "QPushButton:disabled { background: #7f8c8d; }"
        )
        self._analyze_btn.clicked.connect(self._on_analyze)
        ctrl_row.addWidget(self._analyze_btn)
        ctrl_row.addStretch()
        root.addLayout(ctrl_row)

        # --- status label ---
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #555; font-size: 11px;")
        root.addWidget(self._status_label)

        # --- result text ---
        result_label = QLabel("AI proposals will appear here:")
        root.addWidget(result_label)
        self._result = QTextEdit()
        self._result.setReadOnly(True)
        self._result.setMinimumHeight(160)
        self._result.setPlaceholderText("(Click 'Analyze selected events' to generate proposals.)")
        self._result.setStyleSheet("font-family: monospace; font-size: 11px;")
        root.addWidget(self._result)

        # --- close ---
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setFixedWidth(80)
        close_btn.clicked.connect(self.hide)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

    # ------------------------------------------------------------------
    # Close policy — HIDE (persistent window, never destroyed)
    # ------------------------------------------------------------------

    def closeEvent(self, e):
        e.ignore()
        self.hide()

    # ------------------------------------------------------------------
    # Slots (run on the Qt thread)
    # ------------------------------------------------------------------

    def _load_events(self, events: list):
        self._events = events
        self._rebuild_checkboxes()
        self._count_label.setText(
            f"{len(events)} event(s) captured. "
            f"Uncheck anything you don't want analyzed."
        )
        self._result.clear()
        self._status_label.setText("")
        self.show()
        self.raise_()
        self.activateWindow()

    def _rebuild_checkboxes(self):
        # Remove old checkboxes
        for cb in self._checkboxes:
            self._list_layout.removeWidget(cb)
            cb.deleteLater()
        self._checkboxes.clear()

        for ev in self._events:
            icon = {'chord': '[key]', 'click': '[clk]',
                    'focus': '[app]', 'text_entry': '[txt]',
                    'proc':  '[proc]'}.get(ev.kind, '[?]')
            cb = QCheckBox(f"{icon}  {ev.label}")
            cb.setChecked(True)
            cb.setStyleSheet("font-size: 11px;")
            # Insert before the trailing stretch
            self._list_layout.insertWidget(self._list_layout.count() - 1, cb)
            self._checkboxes.append(cb)

    def _set_all(self, checked: bool):
        for cb in self._checkboxes:
            cb.setChecked(checked)

    def _set_indicator(self, active: bool):
        self._indicator.setVisible(active)

    def _set_result(self, text: str):
        self._result.setPlainText(text)

    def _set_busy(self, busy: bool):
        self._analyze_btn.setEnabled(not busy)
        self._provider.setEnabled(not busy)
        self._status_label.setText("Analyzing..." if busy else "")

    # ------------------------------------------------------------------
    # Analyze
    # ------------------------------------------------------------------

    def _on_analyze(self):
        selected = [ev for ev, cb in zip(self._events, self._checkboxes)
                    if cb.isChecked()]
        if not selected:
            self._result.setPlainText("Nothing selected. Check at least one event.")
            return

        use_cloud = self._provider.currentIndex() == 1
        app = self._app_ref
        self._signals.set_busy.emit(True)
        self._result.setPlainText("Summarising and sending to AI…")

        threading.Thread(
            target=self._run_analysis,
            args=(selected, use_cloud, app),
            daemon=True,
            name='wf-capture-analyze',
        ).start()

    def _run_analysis(self, selected: list, use_cloud: bool, app):
        try:
            from plugins.commands.workflow_capture import summarize, analyze_local, analyze_cloud
            summary = summarize(selected)
            _log.debug("[CAPTURE] Sending %d events to AI", len(selected))
            if use_cloud:
                result = analyze_cloud(summary, app)
            else:
                result = analyze_local(summary, app)
        except Exception as exc:
            result = f"Analysis error: {exc}"
        finally:
            self._signals.set_busy.emit(False)
        self._signals.set_result.emit(result)


# ---------------------------------------------------------------------------
# Wrapper  (standard _init_posted pattern on qt_runtime)
# ---------------------------------------------------------------------------

_window: "_ReviewWindow | None" = None
_init_posted: bool = False


def _ensure_window() -> "_ReviewWindow":
    """Create window on first call; return existing window thereafter. Qt thread only."""
    global _window, _init_posted
    if _window is None:
        _window = _ReviewWindow()
    return _window


# ---------------------------------------------------------------------------
# Public API  (called from any thread)
# ---------------------------------------------------------------------------

def show_review(events: list, app) -> None:
    """Load events into the review window and bring it to front.

    Privacy gate: events are only shown to the user here.
    They are NOT sent anywhere until the user clicks Analyze.
    """
    def _show():
        win = _ensure_window()
        win._app_ref = app
        win._signals.load_events.emit(events)

    qt_runtime.ensure_started()
    qt_runtime.post(_show)


def set_active_indicator(active: bool) -> None:
    """Update the RECORDING indicator in the review window (if it exists)."""
    def _update():
        if _window is not None:
            _window._signals.set_indicator.emit(active)

    if qt_runtime.is_alive():
        qt_runtime.post(_update)
