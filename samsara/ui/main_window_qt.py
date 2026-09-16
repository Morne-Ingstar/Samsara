"""PySide6 main hub window for Samsara.

Drop-in replacement for MainWindow with the same public API:
    show() / hide() / close() / on_dictation_complete(text)

Layout:
    +--------------------------------------------------+
    | (mark) Samsara             Ava   [status badge]  |
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

import math
import sys

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QSizePolicy, QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)

from samsara import config_defaults
from samsara.ui import qt_runtime, theme
from samsara.ui.tray_qt import MarkFrame, paint_mark
from samsara.ui.command_marquee import build_example_strip
from samsara.ui.dictionary_panel_qt import DictionaryPanelQt
from samsara.ui.history_view import HistoryView
from samsara.ui.home_qt import (
    MEMOS, VOICE_HELP, AvaHeaderControl, HomePage, glyph_icon, open_settings_tab,
)
from samsara.ui.memos_qt import MemosPage
from samsara.ui.snippets_qt import SNIPPETS, SnippetsPage
from samsara.ui.voice_help_qt import VoiceHelpPage

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
#: 42: 26 px read too small beside the display wordmark; 34 px, vertically
#: centred in the unchanged HEADER_H band.
HEADER_MARK_PX = 34
HEADER_MARK_GAP = 12
HEADER_H = 64
NAV_ROW_H = 44
NAV_ICON_PX = 18
PAUSED_RESUME = "Paused \u2014 resume"
#: Nav item -> icon key in home_qt.ICON_GLYPHS (one vocabulary, 38).
#: Queue 86: "Voice help" is in the nav on EVERY screen, healthy or not --
#: a user must be able to report "voice is not working" when the app has
#: detected no fault.
NAV_ICONS = {"Home": "home", "History": "history", MEMOS: "memos",
             SNIPPETS: "snippets", "Dictionary": "dictionary",
             VOICE_HELP: "guides", "Settings": "settings"}
#: Queue 92: Memos sits beside History -- both are "things you said, kept".
#: It is a nav row and not a Home card because Home's card grid has no room
#: at the 900x650 default: a fourth card costs 130 px (a second row) and
#: Home has 7 px of slack in its worst state. See the queue 92 report.
#: Queue 121: Snippets sits beside Memos -- Memos is "things you said, kept",
#: Snippets is "things you said, kept to say again". Same reason as 92 for
#: being a nav row and not a Home card: Home's card grid has no room at the
#: 900x650 default.
NAV_ORDER = ("Home", "History", MEMOS, SNIPPETS, "Dictionary", VOICE_HELP, "Settings")
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

# Colour comes from samsara.ui.theme, never from a literal here: this
# module used to keep its own copy of the dark palette, which is exactly
# how a second palette leaves one window unreadable (queue 129).

def _ss() -> str:
    """The window's stylesheet, built on demand. Never a module
    constant: an f-string evaluated at import time freezes the
    palette that happened to be live then (queue 129)."""
    return f"""
    QMainWindow, QWidget {{
        background: {theme.BG0};
        color: {theme.TEXT_PRIMARY};
        font-family: 'Segoe UI', sans-serif;
        font-size: {theme.TYPE_BODY}px;
    }}
    QPushButton {{
        background: {theme.BG1};
        border: 1px solid {theme.BORDER};
        border-radius: 4px;
        color: {theme.TEXT_PRIMARY};
        padding: 5px 14px;
        font-size: {theme.TYPE_BODY}px;
    }}
    QPushButton:hover {{ background: {theme.BG2}; border-color: {theme.ACCENT}; }}
    QPushButton:pressed {{ background: {theme.ACCENT_DIM}; }}
    QStatusBar {{
        background: {theme.BG1};
        border-top: 1px solid {theme.BORDER};
        color: {theme.TEXT_SECONDARY};
        font-size: {theme.TYPE_SECTION_LABEL}px;
    }}
""" + theme.SCROLLBAR_QSS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _badge_css(colour: str) -> str:
    """Header state badge (79). Every state keeps `background: transparent`:
    the refresh used to replace the sheet with colour + size only, so the
    window-wide `QWidget { background }` rule painted a BG0 box the full
    64 px height of the header, over its bottom border -- the notch under
    "ready" -- and "ready" used a private slate grey instead of the
    TEXT_SECONDARY token every other secondary label uses."""
    return (f"color: {colour}; font-size: {theme.TYPE_SECTION_LABEL}px;"
            " background: transparent; border: none;")


def _btn(text, *, accent=False):
    b = QPushButton(text)
    if accent:
        b.setStyleSheet(
            f"background: {theme.ACCENT_DIM}; color: {theme.ACCENT};"
            f" border-color: {theme.ACCENT}; border-radius: 4px;"
            f" padding: 5px 14px;"
        )
    return b


def _label(text, color=None, size=theme.TYPE_SECTION_LABEL, bold=False):
    # None, not the token itself: a default argument is evaluated when the
    # def runs, so a token there keeps the palette that was live at import
    # (queue 129).
    if color is None:
        color = theme.TEXT_SECONDARY
    lbl = QLabel(text)
    weight = "600" if bold else "400"
    lbl.setStyleSheet(
        f"color: {color}; font-size: {size}px; font-weight: {weight};"  # type-floor: exempt -- size is the TYPE_* token in this helper's signature
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
        f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_SECTION_LABEL}px; font-weight: 700;"
        f" letter-spacing: {theme.LETTER_SPACING_SECTION}; background: transparent;"
    )
    value = QLabel(value_text)
    value.setStyleSheet(
        f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px; font-weight: 500; background: transparent;"
    )
    lay.addWidget(label)
    lay.addWidget(value)
    return w, value


def _status_separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFixedHeight(16)
    line.setStyleSheet(f"color: {theme.BORDER}; background: {theme.BORDER}; max-width: 1px; border: none;")
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
    The ring turns with the tray (42): the frame carries the app's live
    _icon_rotation, which the chase timer advances in every capture state
    (recording 1.5 s/turn) and holds still at rest.
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
        else:
            # create_icon_image() with no argument describes rotation 0; the
            # header shows the tray's live angle so recording visibly spins.
            rotation = vars(self._app).get("_icon_rotation") if hasattr(self._app, "__dict__") else None
            if isinstance(rotation, (int, float)):
                frame = frame._replace(rotation=math.degrees(rotation))
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
        self.setStyleSheet(_ss())
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
        self._badge.setStyleSheet(_badge_css(theme.TEXT_SECONDARY))
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
        # Ava lives here now (89), left of the listening indicator: she is
        # app-wide state, not a Home topic, and Home's single column has to
        # fit a 650 px window. One control -- avatar, name, state in a word,
        # click goes to her settings.
        self._ava = AvaHeaderControl(
            self._app, on_click=lambda: open_settings_tab(self._app, "Ava / Cloud"))
        hlay.addStretch()
        hlay.addWidget(self._ava, alignment=Qt.AlignmentFlag.AlignVCenter)
        hlay.addSpacing(HEADER_MARK_GAP)
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
        for name in NAV_ORDER:
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

        # The blank right-hand end of the strip (89, changed in 111): ONE
        # complete command phrase from the catalog, stationary, with an arrow
        # either side to step through the pool. It scrolled until 111;
        # partially-visible moving text is the wrong default for someone who
        # needs to read an example long enough to say it out loud, and a
        # marquee has no "previous". It is NOT the diagnostic hint slot on
        # Home -- that one is actionable.
        sb.addWidget(_status_separator())
        self._example_strip = build_example_strip(self._app)
        self._example_strip.setSizePolicy(QSizePolicy.Policy.Expanding,
                                          QSizePolicy.Policy.Preferred)
        sb.addWidget(self._example_strip, 1)

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
        self._on_page_shown(name)

    def _on_page_shown(self, name: str):
        """Queue 92: Memos re-reads its store on entry -- a memo recorded
        while another page was up must be there when the user arrives."""
        page = self._panel_cache.get(name)
        reload_fn = getattr(page, "reload", None) if name in (MEMOS, SNIPPETS) else None
        if callable(reload_fn):
            try:
                reload_fn()
            except Exception as exc:
                logger.debug(f"_on_page_shown {name}: {exc}")

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
        if name == MEMOS:
            return MemosPage(self._app)
        if name == SNIPPETS:
            return SnippetsPage(self._app)
        if name == "Dictionary":
            return DictionaryPanelQt(self._app)
        if name == VOICE_HELP:
            return VoiceHelpPage(self._app, open_page=self._activate)
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

        # Ava is header state now (89), so she follows the hub's poll, not
        # Home's refresh -- she is shown on every page.
        self._ava.refresh()

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
            self._badge.setStyleSheet(_badge_css(theme.WARNING))
        elif getattr(self._app, 'recording', False):
            self._badge.setText("recording")
            self._badge.setStyleSheet(_badge_css(theme.ERROR))
        elif (getattr(self._app, 'continuous_active', False) or
              getattr(self._app, 'wake_word_active', False)):
            self._badge.setText("listening")
            self._badge.setStyleSheet(_badge_css(theme.SUCCESS))
        else:
            self._badge.setText("ready")
            self._badge.setStyleSheet(_badge_css(theme.TEXT_SECONDARY))

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
                screens = [s.availableGeometry() for s in QApplication.screens()]
                primary = QApplication.primaryScreen().availableGeometry()
                nx, ny, nw, nh = placement_on_screens(int(x), int(y), w, h, screens, primary)
                if (nx, ny, nw, nh) != (int(x), int(y), w, h):
                    logger.info(
                        "[MAIN] saved geometry %s,%s %sx%s is not on a current monitor; "
                        "placed at %s,%s %sx%s", x, y, w, h, nx, ny, nw, nh)
                self.setGeometry(nx, ny, nw, nh)
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

    def open_page(self, name: str) -> bool:
        """Show the hub on `name` (queue 92, for the tray's memo entry).

        Returns False when the window has not been built yet, so a caller
        can fall back rather than silently doing nothing. The window is
        raised first: opening a page behind another window is not opening it.
        """
        if self._window is None:
            self.show()
            return False
        def _go():
            self._show_and_raise()
            self._window._activate(name)
        qt_runtime.post(_go)
        return True

    # ---- Qt-thread ----------------------------------------------------------

    def _show_and_raise(self):
        """Runs on the Qt thread. QWidget.show() alone does not restore a
        minimized window -- showNormal() clears the minimized state first;
        a non-minimized window just gets a plain show(). Either way,
        raise_()/activateWindow() bring it to the foreground -- this is
        what tray "Show Samsara" / tray-icon-click ultimately triggers."""
        self._surface("show requested")

    def _surface(self, reason: str):
        """Runs on the Qt thread: show, raise, activate, then confirm the
        window really is in front (see _confirm_in_front)."""
        window = self._window
        if window is None:
            return
        if window.isMinimized():
            window.showNormal()
        else:
            window.show()
        window.raise_()
        window.activateWindow()
        g = window.geometry()
        logger.info(
            "[MAIN] %s: show() called, isVisible=%s windowState=%s geometry=%s,%s %sx%s",
            reason, window.isVisible(), _state_name(window), g.x(), g.y(), g.width(), g.height())
        self._confirm_in_front(reason)

    def _confirm_in_front(self, reason: str):
        """Windows foreground lock: a process that is not the foreground
        process (the user clicked or typed elsewhere while Samsara was
        booting, or another startup app took focus) cannot take the
        foreground -- activateWindow() silently degrades to a taskbar flash
        and the window stays behind whatever is in front. That is the
        "window exists, taskbar click brings it up" symptom.

        We do not fight the lock for keyboard focus (no AttachThreadInput /
        synthetic input). Instead the window is placed at the top of the
        normal z-order WITHOUT activation (SWP_NOACTIVATE), so it is on
        screen, and the taskbar button is flashed so the focus route is
        obvious. The outcome is logged either way."""
        window = self._window
        if window is None or sys.platform != "win32":
            return
        try:
            if _is_foreground(window):
                logger.info("[MAIN] %s: activation succeeded (window is foreground)", reason)
                return
            raised = _raise_without_activation(window)
            QApplication.alert(window, 0)
            logger.warning(
                "[MAIN] %s: activation refused by the Windows foreground lock "
                "(foreground belongs to another process); raised without focus=%s, "
                "taskbar button flashed", reason, raised)
        except Exception:
            logger.exception("[MAIN] %s: could not confirm the window is in front", reason)

    def _init_window(self):
        """Runs on the Qt thread."""
        logger.info("[MAIN] _init_window entered")
        try:
            self._window = _MainWindow(self._app)
            self._window.destroyed.connect(self._on_destroyed)
        except Exception:
            # QTimer.singleShot callbacks have no caller to raise into; make
            # the failure readable instead of leaving a silent missing window.
            logger.exception("[MAIN] _init_window: main window construction failed")
            self._init_posted = False
            return
        self._surface("startup")
        self._undo_launch_minimized()
        self._watch_splash()
        logger.info("[MAIN] _init_window completed")

    def _undo_launch_minimized(self):
        """A process started minimized -- a shortcut set to "Run: Minimized",
        `start /min` -- carries STARTUPINFO wShowWindow=SW_SHOWMINNOACTIVE,
        and Windows applies it to the process's first ShowWindow call on an
        ordinary window, whatever Qt asked for. The frameless splash does not
        consume it; the main window does, and comes up minimized with only a
        taskbar button (2026-09-15 02:57 boot: "windowState=minimized" right
        after show(); both Samsara shortcuts on the owner's desktop and Start
        menu were WindowStyle=7). The override applies to the first call only,
        so a second showNormal() restores the window. Samsara has no
        start-minimized setting: startup always ends with Home shown."""
        window = self._window
        if window is None or not window.isMinimized():
            return
        logger.warning(
            "[MAIN] startup: window came up minimized on its first show (launch show "
            "state %s -- a 'Run: Minimized' shortcut or start /min); restoring it",
            _launch_show_state())
        self._surface("startup restore")
        if window.isMinimized():
            logger.warning("[MAIN] startup restore: window is still minimized; open it from "
                           "the taskbar button or the tray icon")

    def _watch_splash(self):
        """The startup splash is a stay-on-top window that closes ~5 s after
        the main window is shown. When it goes, re-surface the main window
        so startup ends with Home in front (unless the user hid it)."""
        for w in QApplication.topLevelWidgets():
            if w.objectName() == SPLASH_OBJECT_NAME and w.isVisible():
                logger.info("[MAIN] splash still open; main window will re-surface when it closes")
                w.destroyed.connect(lambda *_: QTimer.singleShot(0, self._after_splash_closed))
                return

    def _after_splash_closed(self):
        window = self._window
        if window is None:
            return
        if not window.isVisible() or window.isMinimized():
            logger.info("[MAIN] splash closed; main window hidden/minimized by user, left as is")
            return
        self._surface("splash closed")

    def _on_destroyed(self):
        self._window = None


# ---------------------------------------------------------------------------
# Placement and Win32 foreground helpers
# ---------------------------------------------------------------------------

#: objectName of samsara.ui.splash_qt._SplashWidget.
SPLASH_OBJECT_NAME = "samsaraStartupSplash"
#: How much of the title-bar strip must lie on a monitor for the window to be
#: reachable (grab-able) there.
_MIN_VISIBLE_W = 120
_MIN_VISIBLE_H = 40


def placement_on_screens(x, y, w, h, screens, primary):
    """Return (x, y, w, h) that is reachable on one of ``screens`` (QRects of
    each monitor's available area). A saved position whose title-bar strip
    lies on a current monitor is kept -- including secondary monitors at
    negative or large coordinates. Otherwise (monitor disconnected, layout
    changed) the window is centred on ``primary``, shrunk to fit."""
    for s in screens:
        overlap_w = min(x + w, s.x() + s.width()) - max(x, s.x())
        overlap_h = min(y + _MIN_VISIBLE_H, s.y() + s.height()) - max(y, s.y())
        if overlap_w >= _MIN_VISIBLE_W and overlap_h >= _MIN_VISIBLE_H:
            return x, y, w, h
    w = min(w, primary.width())
    h = min(h, primary.height())
    return (primary.x() + (primary.width() - w) // 2,
            primary.y() + (primary.height() - h) // 2, w, h)


def _state_name(window) -> str:
    if window.isMinimized():
        return "minimized"
    if window.isMaximized():
        return "maximized"
    return "normal"


_SHOW_STATE_NAMES = {0: "SW_HIDE", 1: "SW_SHOWNORMAL", 2: "SW_SHOWMINIMIZED", 3: "SW_SHOWMAXIMIZED",
                     6: "SW_MINIMIZE", 7: "SW_SHOWMINNOACTIVE", 10: "SW_SHOWDEFAULT"}


def _launch_show_state() -> str:
    """This process's STARTUPINFO show state, for the log ("not set" when the
    launcher did not pass one)."""
    if sys.platform != "win32":
        return "n/a"
    try:
        import ctypes
        from ctypes import wintypes

        class _StartupInfo(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                        ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

        si = _StartupInfo()
        si.cb = ctypes.sizeof(si)
        ctypes.windll.kernel32.GetStartupInfoW(ctypes.byref(si))
        if not si.dwFlags & 0x1:   # STARTF_USESHOWWINDOW
            return "not set"
        return f"{_SHOW_STATE_NAMES.get(si.wShowWindow, 'SW_?')}={si.wShowWindow}"
    except Exception as exc:
        return f"unknown ({exc})"


def _hwnd(window) -> int:
    return int(window.winId())


def _is_foreground(window) -> bool:
    import ctypes
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    return (user32.GetForegroundWindow() or 0) == _hwnd(window)


def _raise_without_activation(window) -> bool:
    """Topmost-then-not-topmost with SWP_NOACTIVATE: lands the window at the
    top of the normal z-order without taking focus, which the foreground
    lock permits."""
    import ctypes
    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    flags = 0x0001 | 0x0002 | 0x0010 | 0x0040   # NOSIZE|NOMOVE|NOACTIVATE|SHOWWINDOW
    hwnd = _hwnd(window)
    ok_top = user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, flags)
    ok_normal = user32.SetWindowPos(hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, flags)
    return bool(ok_top and ok_normal)
