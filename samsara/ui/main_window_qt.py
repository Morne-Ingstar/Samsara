"""PySide6 main hub window for Samsara.

Drop-in replacement for MainWindow with the same public API:
    show() / hide() / close() / on_dictation_complete(text)

Layout:
    +--------------------------------------------------+
    | Samsara                          [status badge]  |
    +----------+---------------------------------------+
    | Home     |                                       |
    | History  |                                       |
    | Dictionary  (QStackedWidget content area)        |
    | Settings |                                       |
    +----------+---------------------------------------+
    | mode: X  wake: Y  mic: Z         Last: preview  |
    +--------------------------------------------------+

Settings nav item opens the Qt settings window via app.open_settings().
Home (the landing page, samsara/ui/home_qt.py), History and Dictionary are
embedded QWidget panels.
Close button hides to tray (closeEvent suppressed); app.close() force-closes.
"""

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)

from samsara import config_defaults
from samsara.ui import qt_runtime, theme
from samsara.ui.tray_qt import MarkFrame, paint_mark
from samsara.ui.dictionary_panel_qt import DictionaryPanelQt
from samsara.ui.history_view import HistoryView
from samsara.ui.home_qt import HomePage, glyph_icon

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
HEADER_MARK_PX = 26
HEADER_MARK_GAP = 12
HEADER_H = 64
NAV_ROW_H = 44
NAV_ICON_PX = 18
PAUSED_RESUME = "Paused \u2014 resume"
#: Nav item -> icon key in home_qt.ICON_GLYPHS (one vocabulary, 38).
NAV_ICONS = {"Home": "home", "History": "history", "Dictionary": "dictionary", "Settings": "settings"}
# The header mark follows the tray's live frame (spin while transcribing,
# listening pulse), so it polls faster than the 2 s status refresh; the
# timer runs only while the window is shown.
HEADER_MARK_POLL_MS = 80

_CAPTURE_WORDS = {
    "idle": "idle",
    "listening": "listening",
    "recording": "recording",
    "ava": "Ava",
}
_EYE_WORDS = {
    "off": "hands-free off",
    "asleep": "hands-free asleep",
    "armed": "hands-free armed",
    "heard": "wake phrase heard",
}

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
    font-size: {theme.TYPE_BODY}px;
}}
QPushButton {{
    background: {_SURFACE};
    border: 1px solid {_BORDER};
    border-radius: 4px;
    color: {_TEXT_PRI};
    padding: 5px 14px;
    font-size: {theme.TYPE_BODY}px;
}}
QPushButton:hover {{ background: {_ELEVATED}; border-color: {_ACCENT}; }}
QPushButton:pressed {{ background: {_ACCENT_DIM}; }}
QStatusBar {{
    background: {_SURFACE};
    border-top: 1px solid {_BORDER};
    color: {_TEXT_SEC};
    font-size: {theme.TYPE_SECTION_LABEL}px;
}}
""" + theme.SCROLLBAR_QSS


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


def _label(text, color=_TEXT_SEC, size=theme.TYPE_SECTION_LABEL, bold=False):
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
        f"color: {_TEXT_SEC}; font-size: {theme.TYPE_SECTION_LABEL}px; font-weight: 700;"
        f" letter-spacing: {theme.LETTER_SPACING_SECTION}; background: transparent;"
    )
    value = QLabel(value_text)
    value.setStyleSheet(
        f"color: {_TEXT_PRI}; font-size: {theme.TYPE_BODY}px; font-weight: 500; background: transparent;"
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


def mark_accessible_name(frame) -> str:
    """Accessible name for a MarkFrame, e.g. "Samsara - listening, hands-free armed" (em dash)."""
    capture = _CAPTURE_WORDS.get(frame.capture, frame.capture)
    eye = _EYE_WORDS.get(frame.eye, frame.eye)
    return f"Samsara \u2014 {capture}, {eye}"


class _HeaderMark(QWidget):
    """The live Samsara mark left of the header wordmark.

    State comes from the app's own tray frame (DictationApp.create_icon_image,
    which resolves DictationApp._tray_mark) -- no second copy of the priority
    logic -- and is drawn with the one mark routine, tray_qt.paint_mark.
    """

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self._app = app
        self._frame = MarkFrame("idle", "off")
        # Other views of the same frame (Home's 60 px mark): called with the
        # new MarkFrame whenever it changes, so there is one source only.
        self.listeners = []
        self.setFixedSize(HEADER_MARK_PX, HEADER_MARK_PX)
        self.setStyleSheet("background: transparent;")
        self.setAccessibleName(mark_accessible_name(self._frame))
        self._timer = QTimer(self)
        self._timer.setInterval(HEADER_MARK_POLL_MS)
        self._timer.timeout.connect(self.refresh)

    @property
    def frame(self):
        return self._frame

    def start(self):
        self.refresh()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def refresh(self):
        source = getattr(self._app, "create_icon_image", None)
        try:
            frame = source() if callable(source) else None
        except Exception as e:
            logger.debug(f"_HeaderMark.refresh: {e}")
            frame = None
        if not isinstance(frame, MarkFrame):
            frame = MarkFrame("idle", "off")
        if frame != self._frame:
            name_changed = (frame.capture, frame.eye) != (self._frame.capture, self._frame.eye)
            self._frame = frame
            if name_changed:
                self.setAccessibleName(mark_accessible_name(frame))
            self.update()
            for listener in list(self.listeners):
                try:
                    listener(frame)
                except Exception as e:
                    logger.debug(f"_HeaderMark listener: {e}")

    def paintEvent(self, event):
        painter = QPainter(self)
        frame = self._frame
        # Brand presentation (38): the lockup beside the app's name is
        # ACCENT at the brand weight with the eye present in every
        # hands-free state; only RECORDING red while recording. The tray
        # keeps its own 16 px state vocabulary.
        paint_mark(painter, QRectF(0, 0, self.width(), self.height()),
                   frame.capture, frame.eye, frame.rotation, frame.opacity, brand=True)
        painter.end()


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
        theme.install_app_scrollbars()
        self.setStyleSheet(_SS)
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self._restore_geometry()
        self._build_ui()
        self._activate("Home")
        self._poll_timer.start()
        self._dictation_sig.connect(self._on_dictation)

    # ---- Layout -------------------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header band (38): the brand mark and the wordmark in the display
        # face, on BG0, separated from the content by the BORDER token.
        header = QWidget()
        header.setObjectName("hubHeader")
        header.setFixedHeight(HEADER_H)
        header.setStyleSheet(
            f"QWidget#hubHeader {{ background: {theme.BG0}; border-bottom: 1px solid {theme.BORDER}; }}")
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(20, 0, 20, 0)
        title = QLabel("Samsara")
        title.setObjectName("hubWordmark")
        title.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-family: {theme.FONT_FAMILY_DISPLAY};"
            f" font-size: {theme.FONT_SIZE_DISPLAY}px; letter-spacing: {theme.LETTER_SPACING_DISPLAY};"
            " background: transparent; border: none;")
        self._badge = QLabel("ready")
        self._badge.setStyleSheet(
            f"color: {_TEXT_SEC}; font-size: {theme.TYPE_SECTION_LABEL}px; background: transparent; border: none;")
        # Paused is never invisible or inescapable (38): while hands-free is
        # snoozed the header shows this instead of the badge, and clicking it
        # resumes through the app's own resume_listening.
        self._paused_btn = QPushButton(PAUSED_RESUME)
        self._paused_btn.setAccessibleName(PAUSED_RESUME)
        self._paused_btn.setMinimumHeight(NAV_ROW_H)
        self._paused_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._paused_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BG2}; color: {theme.WARNING};"
            f" border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 6px 14px; font-size: {theme.TYPE_BODY}px; }}"
            f"QPushButton:hover {{ border-color: {theme.ACCENT}; color: {theme.TEXT_PRIMARY}; }}")
        self._paused_btn.clicked.connect(self._on_resume)
        self._paused_btn.setVisible(False)
        # Live mark, app-avatar style: in the shared header, so every page
        # shows the state. Polled only while the window is shown.
        self._header_mark = _HeaderMark(self._app)
        hlay.addWidget(self._header_mark, alignment=Qt.AlignmentFlag.AlignVCenter)
        hlay.addSpacing(HEADER_MARK_GAP)
        hlay.addWidget(title)
        hlay.addStretch()
        hlay.addWidget(self._paused_btn)
        hlay.addWidget(self._badge)
        outer.addWidget(header)

        # Body: sidebar + content
        body = QWidget()
        blay = QHBoxLayout(body)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(0)

        # Sidebar (38): on BG0 so it reads as a different plane from the
        # BG1 content. Rows are 44 px with an icon from the same vocabulary
        # as Home's capability cards; the selected row carries a 2 px ACCENT
        # rail, a BG2 fill, TEXT_PRIMARY and a heavier weight.
        sidebar = QWidget()
        sidebar.setObjectName("hubSidebar")
        sidebar.setFixedWidth(SIDEBAR_W)
        sidebar.setStyleSheet(
            f"QWidget#hubSidebar {{ background: {theme.BG0}; border-right: 1px solid {theme.BORDER}; }}")
        slay = QVBoxLayout(sidebar)
        slay.setContentsMargins(0, 12, 0, 12)
        slay.setSpacing(2)

        self._nav_btns = {}
        for name in ("Home", "History", "Dictionary", "Settings"):
            btn = QPushButton(name)
            btn.setAccessibleName(name)
            btn.setMinimumHeight(NAV_ROW_H)
            btn.setCheckable(True)
            btn.setIconSize(QSize(NAV_ICON_PX, NAV_ICON_PX))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._style_nav(btn, name, False)
            btn.clicked.connect(lambda _, n=name: self._activate(n))
            slay.addWidget(btn)
            self._nav_btns[name] = btn
        slay.addStretch()

        blay.addWidget(sidebar)

        # Content stack on BG1 (the sidebar is BG0)
        self._stack = QStackedWidget()
        self._stack.setObjectName("hubContent")
        self._stack.setStyleSheet(f"QStackedWidget#hubContent {{ background: {theme.BG1}; }}")
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
        # No "Last: ..." preview here (41): Home's outcome row is the one
        # place the newest outcome is shown.

    @staticmethod
    def _nav_style(active: bool) -> str:
        """Nav row stylesheet (38). Selected differs from rest in more than
        colour: a 2 px ACCENT left rail, a BG2 fill and a heavier weight.
        Rest is TEXT_SECONDARY with a real hover (BG1 fill, TEXT_PRIMARY)."""
        common = (f" border: none; border-radius: 0; text-align: left;"
                  f" padding-left: 16px; padding-right: 12px; min-height: {NAV_ROW_H}px;"
                  f" font-size: {theme.TYPE_NAV}px;")
        if active:
            return (f"QPushButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY};"
                    f" border-left: 2px solid {theme.ACCENT}; font-weight: 600;{common} }}"
                    f"QPushButton:hover {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; }}")
        return (f"QPushButton {{ background: transparent; color: {theme.TEXT_SECONDARY};"
                f" border-left: 2px solid transparent; font-weight: 400;{common} }}"
                f"QPushButton:hover {{ background: {theme.BG1}; color: {theme.TEXT_PRIMARY}; }}"
                f"QPushButton:focus {{ background: {theme.BG1}; color: {theme.TEXT_PRIMARY}; }}")

    def _style_nav(self, btn, name: str, active: bool) -> None:
        btn.setStyleSheet(self._nav_style(active))
        btn.setIcon(glyph_icon(NAV_ICONS.get(name, ""),
                               theme.TEXT_PRIMARY if active else theme.TEXT_SECONDARY, NAV_ICON_PX))

    def _on_resume(self):
        fn = getattr(self._app, 'resume_listening', None)
        if callable(fn):
            try:
                fn()
            except Exception as e:
                logger.warning(f"[MAIN] resume_listening failed: {e}")
        self._refresh_status()

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
        if name == "Home":
            page = HomePage(self._app, open_page=self._activate)
            page.set_mark_frame(self._header_mark.frame)
            self._header_mark.listeners.append(page.set_mark_frame)
            return page
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
            self._style_nav(btn, name, name == active)

    # ---- Status -------------------------------------------------------------

    def _refresh_status(self):
        cfg = getattr(self._app, 'config', {}) or {}

        home = self._panel_cache.get("Home")
        if home is not None:
            try:
                home.refresh()
            except Exception as e:
                logger.debug(f"_refresh_status home: {e}")

        mode = cfg.get('mode', 'hold').title()
        self._lbl_mode.setText(mode)

        wake_on = cfg.get('wake_word_enabled', False)
        phrase  = cfg.get('wake_word_config', {}).get(
            'phrase', config_defaults.DEFAULTS['wake_word_config.phrase'])
        # A spoken phrase is always shown quoted (41).
        self._lbl_wake.setText(f"\"{phrase}\" (on)" if wake_on else "Off")

        mic_id   = cfg.get('microphone')
        mic_name = "Default"
        for m in getattr(self._app, 'available_mics', []) or []:
            if m.get('id') == mic_id:
                mic_name = m.get('name', 'Default')
                break
        if len(mic_name) > 36:
            mic_name = mic_name[:35] + '...'
        self._lbl_mic.setText(mic_name)

        snoozed = bool(getattr(self._app, 'snoozed', False))
        self._paused_btn.setVisible(snoozed)
        self._badge.setVisible(not snoozed)
        if snoozed:
            self._badge.setText("snoozed")
            self._badge.setStyleSheet(f"color: {_WARNING}; font-size: {theme.TYPE_SECTION_LABEL}px;")
        elif getattr(self._app, 'recording', False):
            self._badge.setText("recording")
            self._badge.setStyleSheet(f"color: {_ERROR}; font-size: {theme.TYPE_SECTION_LABEL}px;")
        elif (getattr(self._app, 'continuous_active', False) or
              getattr(self._app, 'wake_word_active', False)):
            self._badge.setText("listening")
            self._badge.setStyleSheet(f"color: {_SUCCESS}; font-size: {theme.TYPE_SECTION_LABEL}px;")
        else:
            self._badge.setText("ready")
            self._badge.setStyleSheet(f"color: {_TEXT_SEC}; font-size: {theme.TYPE_SECTION_LABEL}px;")

    @Slot(str)
    def _on_dictation(self, text: str):
        for name in ("History", "Home"):
            panel = self._panel_cache.get(name)
            if panel is not None and (name == "Home" or self._stack.currentWidget() is panel):
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

    def showEvent(self, e):
        super().showEvent(e)
        self._header_mark.start()

    def hideEvent(self, e):
        # Minimise-to-tray and close both arrive here: the header mark stops
        # polling whenever nobody can see it.
        self._header_mark.stop()
        super().hideEvent(e)

    def closeEvent(self, e):
        if self._force_close:
            self._poll_timer.stop()
            self._header_mark.stop()
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
