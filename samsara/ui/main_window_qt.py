"""PySide6 main hub window for Samsara.

Drop-in replacement for MainWindow with the same public API:
    show() / hide() / close() / on_dictation_complete(text)

Layout:
    +--------------------------------------------------+
    | Samsara                          [status badge]  |
    +----------+---------------------------------------+
    | History  |                                       |
    | Dictionary  (QStackedWidget content area)        |
    | Settings |                                       |
    +----------+---------------------------------------+
    | mode: X  wake: Y  mic: Z         Last: preview  |
    +--------------------------------------------------+

Settings nav item opens the Qt settings window via app.open_settings().
History and Dictionary are embedded QWidget panels.
Close button hides to tray (closeEvent suppressed); app.close() force-closes.
"""

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)

from samsara.ui import qt_runtime
from samsara.ui.dictionary_panel_qt import DictionaryPanelQt
from samsara.ui.history_view import HistoryView

from samsara.log import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants — match Tkinter version
# ---------------------------------------------------------------------------

DEFAULT_WIDTH  = 900
DEFAULT_HEIGHT = 650
MIN_WIDTH      = 700
MIN_HEIGHT     = 500
STATUS_POLL_MS = 2000
SIDEBAR_W      = 180
HISTORY_LIMIT  = 500

_BG       = "#0b0e14"
_SURFACE  = "#131820"
_ELEVATED = "#1a2030"
_BORDER   = "#2a3345"
_ACCENT   = "#5cc4d4"
_ACCENT_DIM = "#1a3a42"
_TEXT_PRI = "#e4e8ef"
_TEXT_SEC = "#7a8599"
_TEXT_DIS = "#4a5568"
_SUCCESS  = "#6ee7a0"
_ERROR    = "#f87171"
_WARNING  = "#fbbf24"

_SS = f"""
QMainWindow, QWidget {{
    background: {_BG};
    color: {_TEXT_PRI};
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
}}
QPushButton {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    color: {_TEXT_PRI};
    padding: 5px 14px;
    font-size: 12px;
}}
QPushButton:hover {{ background: {_ELEVATED}; border-color: {_ACCENT}; }}
QPushButton:pressed {{ background: {_ACCENT_DIM}; }}
QScrollBar:vertical {{
    background: {_BG};
    width: 6px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {_BORDER};
    border-radius: 3px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QStatusBar {{
    background: {_SURFACE};
    border-top: 1px solid {_BORDER};
    color: {_TEXT_SEC};
    font-size: 11px;
}}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _btn(text, *, accent=False):
    b = QPushButton(text)
    if accent:
        b.setStyleSheet(
            f"background: {_ACCENT_DIM}; color: {_ACCENT};"
            f" border-color: {_ACCENT}; border-radius: 4px;"
            f" padding: 5px 14px;"
        )
    return b


def _label(text, color=_TEXT_SEC, size=11, bold=False):
    lbl = QLabel(text)
    weight = "600" if bold else "400"
    lbl.setStyleSheet(
        f"color: {color}; font-size: {size}px; font-weight: {weight};"
        " background: transparent;"
    )
    return lbl


def _status_segment(label_text: str, value_text: str = "..."):
    """Muted small-caps label + primary-colored value, e.g. 'MODE  Hold'.

    Returns (container_widget, value_label) so callers can update just the
    value later without rebuilding the segment.
    """
    w = QWidget()
    w.setStyleSheet("background: transparent;")
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    label = QLabel(label_text.upper())
    label.setStyleSheet(
        f"color: {_TEXT_SEC}; font-size: 11px; font-weight: 700;"
        f" letter-spacing: 0.06em; background: transparent;"
    )
    value = QLabel(value_text)
    value.setStyleSheet(
        f"color: {_TEXT_PRI}; font-size: 14px; font-weight: 500; background: transparent;"
    )
    lay.addWidget(label)
    lay.addWidget(value)
    return w, value


def _status_separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFixedHeight(16)
    line.setStyleSheet(f"color: {_BORDER}; background: {_BORDER}; max-width: 1px; border: none;")
    return line


# ---------------------------------------------------------------------------
# Main Qt window
# ---------------------------------------------------------------------------

class _MainWindow(QMainWindow):
    _dictation_sig = Signal(str)

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._force_close = False
        self._panel_cache = {}
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(STATUS_POLL_MS)
        self._poll_timer.timeout.connect(self._refresh_status)
        self._geom_timer = QTimer(self)
        self._geom_timer.setSingleShot(True)
        self._geom_timer.setInterval(800)
        self._geom_timer.timeout.connect(self._save_geometry)

        self.setWindowTitle("Samsara")
        self.setStyleSheet(_SS)
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self._restore_geometry()
        self._build_ui()
        self._activate("History")
        self._poll_timer.start()
        self._dictation_sig.connect(self._on_dictation)

    # ---- Layout -------------------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(52)
        header.setStyleSheet(f"background: {_BG}; border-bottom: 1px solid {_BORDER};")
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(20, 0, 20, 0)
        title = QLabel("Samsara")
        title.setStyleSheet(f"color: {_TEXT_PRI}; font-size: 16px; font-weight: 700;")
        self._badge = QLabel("ready")
        self._badge.setStyleSheet(f"color: {_TEXT_SEC}; font-size: 11px;")
        hlay.addWidget(title)
        hlay.addStretch()
        hlay.addWidget(self._badge)
        outer.addWidget(header)

        # Body: sidebar + content
        body = QWidget()
        blay = QHBoxLayout(body)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(0)

        # Sidebar
        sidebar = QWidget()
        sidebar.setFixedWidth(SIDEBAR_W)
        sidebar.setStyleSheet(
            f"background: {_SURFACE}; border-right: 1px solid {_BORDER};")
        slay = QVBoxLayout(sidebar)
        slay.setContentsMargins(0, 12, 0, 12)
        slay.setSpacing(2)

        self._nav_btns = {}
        for name in ("History", "Dictionary", "Settings"):
            btn = QPushButton(name)
            btn.setFixedHeight(44)
            btn.setCheckable(True)
            btn.setStyleSheet(self._nav_style(False))
            btn.clicked.connect(lambda _, n=name: self._activate(n))
            slay.addWidget(btn)
            self._nav_btns[name] = btn
        slay.addStretch()

        blay.addWidget(sidebar)

        # Content stack
        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background: {_BG};")
        blay.addWidget(self._stack, stretch=1)
        outer.addWidget(body, stretch=1)

        # Status bar -- separated segments (muted small-caps label + primary
        # value), not one combined "mode: X" string. Real vertical padding
        # (6-8px) instead of the old 0px-vertical/horizontal-only padding.
        sb = QStatusBar()
        sb.setSizeGripEnabled(False)
        sb.setStyleSheet(f"padding: 6px 8px;")
        self.setStatusBar(sb)

        mode_w, self._lbl_mode = _status_segment("Mode")
        wake_w, self._lbl_wake = _status_segment("Wake")
        mic_w,  self._lbl_mic  = _status_segment("Mic")

        sb.addWidget(mode_w)
        sb.addWidget(_status_separator())
        sb.addWidget(wake_w)
        sb.addWidget(_status_separator())
        sb.addWidget(mic_w)

        self._lbl_prev = QLabel("")
        self._lbl_prev.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._lbl_prev.setStyleSheet(
            f"color: {_TEXT_SEC}; font-size: 14px; background: transparent;"
        )
        sb.addPermanentWidget(self._lbl_prev)

    @staticmethod
    def _nav_style(active: bool) -> str:
        if active:
            return (f"QPushButton {{ background: {_ACCENT_DIM}; color: {_ACCENT};"
                    f" border: none; border-left: 3px solid {_ACCENT};"
                    f" text-align: left; padding-left: 18px;"
                    f" font-size: 13px; font-weight: 600;"
                    f" border-radius: 0; }}")
        return (f"QPushButton {{ background: transparent; color: {_TEXT_SEC};"
                f" border: none; border-left: 3px solid transparent;"
                f" text-align: left; padding-left: 18px;"
                f" font-size: 13px; font-weight: 600;"
                f" border-radius: 0; }}"
                f"QPushButton:hover {{ background: {_ELEVATED}; color: {_TEXT_PRI}; }}")

    # ---- Navigation ---------------------------------------------------------

    def _activate(self, name: str):
        if name == "Settings":
            try:
                self._app.open_settings()
            except Exception as e:
                print(f"[MAIN] open_settings error: {e}")
            self._highlight(name)
            return

        if name not in self._panel_cache:
            panel = self._make_panel(name)
            if panel is None:
                return
            self._panel_cache[name] = panel
            self._stack.addWidget(panel)

        self._stack.setCurrentWidget(self._panel_cache[name])
        self._highlight(name)

    def _make_panel(self, name: str):
        if name == "History":
            store = getattr(self._app, 'history_store', None)
            return HistoryView(
                store,
                legacy_history_fn=lambda: getattr(self._app, 'history', []),
                legacy_clear_fn=self._clear_legacy_history,
            )
        if name == "Dictionary":
            return DictionaryPanelQt(self._app)
        return None

    def _clear_legacy_history(self):
        legacy = getattr(self._app, 'history', None)
        if legacy is not None:
            legacy.clear()
        if hasattr(self._app, 'save_history'):
            try:
                self._app.save_history()
            except Exception as e:
                logger.debug(f"_clear_legacy_history: {e}")

    def _highlight(self, active: str):
        for name, btn in self._nav_btns.items():
            btn.setChecked(name == active)
            btn.setStyleSheet(self._nav_style(name == active))

    # ---- Status -------------------------------------------------------------

    def _refresh_status(self):
        cfg = getattr(self._app, 'config', {}) or {}

        mode = cfg.get('mode', 'hold').title()
        self._lbl_mode.setText(mode)

        wake_on = cfg.get('wake_word_enabled', False)
        phrase  = cfg.get('wake_word_config', {}).get('phrase', 'samsara')
        self._lbl_wake.setText(f"{phrase} (on)" if wake_on else "Off")

        mic_id   = cfg.get('microphone')
        mic_name = "Default"
        for m in getattr(self._app, 'available_mics', []) or []:
            if m.get('id') == mic_id:
                mic_name = m.get('name', 'Default')
                break
        if len(mic_name) > 36:
            mic_name = mic_name[:35] + '...'
        self._lbl_mic.setText(mic_name)

        if getattr(self._app, 'snoozed', False):
            self._badge.setText("snoozed")
            self._badge.setStyleSheet(f"color: {_WARNING}; font-size: 11px;")
        elif getattr(self._app, 'recording', False):
            self._badge.setText("recording")
            self._badge.setStyleSheet(f"color: {_ERROR}; font-size: 11px;")
        elif (getattr(self._app, 'continuous_active', False) or
              getattr(self._app, 'wake_word_active', False)):
            self._badge.setText("listening")
            self._badge.setStyleSheet(f"color: {_SUCCESS}; font-size: 11px;")
        else:
            self._badge.setText("ready")
            self._badge.setStyleSheet(f"color: {_TEXT_SEC}; font-size: 11px;")

    @Slot(str)
    def _on_dictation(self, text: str):
        preview = text.replace('\n', ' ').strip()
        if preview:
            # Graceful font-metric elision (not a fixed char-count cutoff) --
            # full text still available via tooltip on hover.
            fm = self._lbl_prev.fontMetrics()
            available = max(self._lbl_prev.width(), 220) - 40
            elided = fm.elidedText(preview, Qt.TextElideMode.ElideRight, available)
            self._lbl_prev.setText(f"Last: {elided}")
            self._lbl_prev.setToolTip(preview)
        else:
            self._lbl_prev.setText("")
            self._lbl_prev.setToolTip("")

        panel = self._panel_cache.get("History")
        if panel is not None and self._stack.currentWidget() is panel:
            try:
                panel.refresh()
            except Exception as e:
                logger.debug(f"_on_dictation: {e}")

    # ---- Geometry -----------------------------------------------------------

    def _restore_geometry(self):
        cfg = getattr(self._app, 'config', {}) or {}
        w = max(MIN_WIDTH,  int(cfg.get('window_width',  DEFAULT_WIDTH)  or DEFAULT_WIDTH))
        h = max(MIN_HEIGHT, int(cfg.get('window_height', DEFAULT_HEIGHT) or DEFAULT_HEIGHT))
        x = cfg.get('window_x')
        y = cfg.get('window_y')
        if x is not None and y is not None:
            try:
                screen = QApplication.primaryScreen().geometry()
                x = max(0, min(int(x), screen.width()  - 100))
                y = max(0, min(int(y), screen.height() - 100))
                self.setGeometry(x, y, w, h)
                return
            except Exception as e:
                logger.debug(f"_restore_geometry: {e}")
        self.resize(w, h)

    def _save_geometry(self):
        try:
            g = self.geometry()
            changes = {
                'window_width':  g.width(),
                'window_height': g.height(),
                'window_x':      g.x(),
                'window_y':      g.y(),
            }
            if hasattr(self._app, 'update_config'):
                self._app.update_config(changes)
        except Exception as e:
            print(f"[MAIN] geometry save error: {e}")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._geom_timer.start()

    def moveEvent(self, e):
        super().moveEvent(e)
        self._geom_timer.start()

    # ---- Close / hide -------------------------------------------------------

    def closeEvent(self, e):
        if self._force_close:
            self._poll_timer.stop()
            self._save_geometry()
            e.accept()
        else:
            # Minimize to tray — the tray icon is the lifecycle owner.
            self.hide()
            e.ignore()

    def force_close(self):
        self._force_close = True
        self.close()


# ---------------------------------------------------------------------------
# Public wrapper — same API as the Tkinter MainWindow
# ---------------------------------------------------------------------------

class MainWindowQt:
    """Drop-in Qt replacement for MainWindow."""

    def __init__(self, app):
        self._app    = app
        self._window: "_MainWindow | None" = None
        self._init_posted = False

    # ---- Public API (callable from any thread) ------------------------------

    def show(self):
        if self._window is not None:
            qt_runtime.post(self._show_and_raise)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(self._init_window)

    def hide(self):
        if self._window is not None:
            qt_runtime.post(self._window.hide)

    def close(self):
        if self._window is not None:
            qt_runtime.post(self._window.force_close)
            self._window = None

    def on_dictation_complete(self, text: str):
        if self._window is not None:
            self._window._dictation_sig.emit(text)

    # ---- Qt-thread ----------------------------------------------------------

    def _show_and_raise(self):
        """Runs on the Qt thread. QWidget.show() alone does not restore a
        minimized window -- showNormal() clears the minimized state first;
        a non-minimized window just gets a plain show(). Either way,
        raise_()/activateWindow() bring it to the foreground -- this is
        what tray "Show Samsara" / tray-icon-click ultimately triggers."""
        window = self._window
        if window is None:
            return
        if window.isMinimized():
            window.showNormal()
        else:
            window.show()
        window.raise_()
        window.activateWindow()

    def _init_window(self):
        """Runs on the Qt thread."""
        self._window = _MainWindow(self._app)
        self._window.destroyed.connect(self._on_destroyed)
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def _on_destroyed(self):
        self._window = None
