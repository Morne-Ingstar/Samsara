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
from PySide6.QtGui import QIcon, QPainter
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QSizePolicy, QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)

from samsara import __version__, config_defaults
from samsara.ui import qt_runtime, theme, window_activation, window_geometry
from samsara.ui.tray_qt import MarkFrame, idle_brand_icon_path, paint_mark
from samsara.ui.command_marquee import build_example_strip, example_phrases, example_text
from samsara.ui.dictionary_panel_qt import DictionaryPanelQt
from samsara.ui.history_view import HistoryView
from samsara.ui.home_qt import (
    MEMOS, VOICE_HELP, AvaHeaderControl, HomePage, catalog_records, glyph_icon, open_settings_tab,
    listening_badge_view, pause_hands_free, resume_hands_free, stop_capture, talk_instructions,
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
# 839: reuse the actual Settings pill metrics; glyphs/order remain 825's.
from samsara.ui.settings.sidebar import PILL_RADIUS as NAV_RADIUS, ICON_BOX as NAV_ICON_PX
NAV_SIDE_MARGIN = theme.SPACE_1
PAUSED_RESUME = "Paused \u2014 resume"
#: Nav item -> icon key in home_qt.ICON_GLYPHS (one vocabulary, 38).
#: Queue 86: "Voice help" is in the nav on EVERY screen, healthy or not --
#: a user must be able to report "voice is not working" when the app has
#: detected no fault.
NAV_ICONS = {"Home": "home", "Commands": "commands", "History": "history", MEMOS: "memos",
             SNIPPETS: "snippets", "Dictionary": "dictionary",
             VOICE_HELP: "voice_help", "Settings": "settings"}
#: Queue 92: Memos sits beside History -- both are "things you said, kept".
#: It is a nav row and not a Home card because Home's card grid has no room
#: at the 900x650 default: a fourth card costs 130 px (a second row) and
#: Home has 7 px of slack in its worst state. See the queue 92 report.
#: Queue 121: Snippets sits beside Memos -- Memos is "things you said, kept",
#: Snippets is "things you said, kept to say again". Same reason as 92 for
#: being a nav row and not a Home card: Home's card grid has no room at the
#: 900x650 default.
NAV_ORDER = ("Home", "Commands", "History", MEMOS, SNIPPETS, "Dictionary", VOICE_HELP, "Settings")
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
    """Decorated small-caps label + primary-colored value, e.g. 'MODE  Hold'.
    Returns (container_widget, value_label) so callers can update just the
    value without rebuilding it.
    """
    from samsara.ui.widgets.status_label import StatusLabel
    w = QWidget()
    w.setStyleSheet("background: transparent;")
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)
    label = StatusLabel(label_text.upper())
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


def _hands_free_active(app) -> bool:
    """Whether an open-mic session currently owns the hands-free listener."""
    cfg = getattr(app, "config", {}) or {}
    command_cfg = cfg.get("command_mode", {}) or {}
    toggle_session = (
        bool(getattr(app, "command_mode_active", False))
        and command_cfg.get("mode", "hold") == "toggle"
    )
    return toggle_session or bool(getattr(app, "ava_command_session_active", False))


def _status_separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.NoFrame)
    line.setFixedHeight(16)
    line.setFixedWidth(1)
    line.setStyleSheet(f"background: {theme.BORDER}; border: none;")
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
        self.setWindowIcon(QIcon(str(idle_brand_icon_path())))
        theme.install_app_scrollbars()
        # This constructor runs on qt_runtime's QApplication thread. The
        # accessor refuses earlier/wrong-thread creation, so this is the one
        # safe point to subscribe the already-open hub to palette changes.
        self._theme_change_signal = theme.theme_change_signal()
        self._theme_change_signal.changed.connect(self.apply_theme)
        self._theme_signal_manages_descendants = True
        self.setStyleSheet(_ss())
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self._build_ui()
        self._activate("Home")
        self._restore_geometry()
        self._refresh_status()
        self._poll_timer.start()
        self._dictation_sig.connect(self._on_dictation)

    @Slot(str)
    def apply_theme(self, _resolved_theme: str = "") -> None:
        """Rebind existing hub/page QSS without recreating any surface."""
        self.setStyleSheet(_ss())
        for widget in self.findChildren(QWidget):
            inline = widget.styleSheet()
            if inline:
                widget.setStyleSheet(theme.retheme_stylesheet(inline))
        for name, button in self._nav_btns.items():
            self._style_nav(button, name, button.isChecked())
        self._refresh_status()
        # QSystemTrayIcon is not in QApplication.topLevelWidgets(). Refresh
        # its existing MarkFrame through the app's Qt-thread method now.
        push_tray_icon = getattr(self._app, "_push_tray_icon", None)
        if callable(push_tray_icon):
            try:
                push_tray_icon()
            except Exception as exc:
                logger.debug(f"apply_theme tray icon refresh: {exc}")
        self.update()

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
        self._badge = QPushButton("Mic ready")  # PLACEHOLDER COPY (824)
        self._badge.setStyleSheet(_badge_css(theme.TEXT_SECONDARY))
        self._badge.setMinimumHeight(NAV_ROW_H)
        self._badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self._badge.clicked.connect(self._on_listening)
        # Paused is never invisible or inescapable (38): while hands-free is
        # snoozed the header shows this instead of the badge, and clicking it
        # resumes through the app's own resume_listening.
        self._paused_btn = QPushButton("Paused")  # PLACEHOLDER COPY (824)
        self._paused_btn.setMinimumHeight(NAV_ROW_H)
        self._paused_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._paused_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BG2}; color: {theme.WARNING};"
            f" border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 6px 14px; font-size: {theme.TYPE_BODY}px; }}"
            f"QPushButton:hover {{ border-color: {theme.ACCENT}; color: {theme.TEXT_PRIMARY}; }}")
        self._paused_btn.clicked.connect(self._on_listening)
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
        self._reset_btn = QPushButton("Reset size")  # PLACEHOLDER COPY (824)
        self._reset_btn.setObjectName("resetWindowSize")
        self._reset_btn.setAccessibleName("Reset window size")
        self._reset_btn.setToolTip("Reset window size")
        self._reset_btn.setFixedHeight(NAV_ROW_H)
        self._reset_btn.setMinimumWidth(NAV_ROW_H)
        self._reset_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid transparent;"
            f" border-radius: 6px; padding: 0 6px; color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px; }}"
            f"QPushButton:hover {{ background: {theme.BG1}; border-color: {theme.BORDER}; }}"
            f"QPushButton:focus {{ border: 2px solid {theme.ACCENT}; }}")
        self._reset_btn.clicked.connect(self._reset_geometry)
        hlay.addWidget(self._reset_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        hlay.addWidget(self._ava, alignment=Qt.AlignmentFlag.AlignVCenter)
        divider = _status_separator()
        divider.setObjectName("avaMicDivider")
        hlay.addWidget(divider, alignment=Qt.AlignmentFlag.AlignVCenter)
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
        slay.setContentsMargins(NAV_SIDE_MARGIN, 12, NAV_SIDE_MARGIN, 12)
        slay.setSpacing(2)

        self._nav_btns = {}
        for name in NAV_ORDER:
            if name == "Settings":
                version = QLabel(f"v{__version__}")
                version.setObjectName("runningVersion")
                version.setAccessibleName("App version")
                version.setStyleSheet(
                    f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_MIN}px; "
                    "background: transparent; padding-left: 16px;")
                slay.addWidget(version)
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
        sb.setStyleSheet("QStatusBar { padding: 6px 8px; } QStatusBar::item { border: none; }")
        self.setStatusBar(sb)

        from samsara.ui.widgets.status_label import StatusRow
        row = StatusRow(self, sb, MIN_WIDTH)
        row.setStyleSheet("background: transparent;")
        row_layout = QHBoxLayout(row)
        # Native QStatusBar starts its item at x=2; text starts at 16.
        row_layout.setContentsMargins(14, 0, 0, 0)
        row_layout.setSpacing(14)
        from PySide6.QtWidgets import QLayout
        row_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        mode_w, self._lbl_mode = _status_segment("Mode")
        wake_w, self._lbl_wake = _status_segment("Wake")
        mic_w,  self._lbl_mic  = _status_segment("Mic")

        for item in (mode_w, _status_separator(), wake_w, _status_separator(), mic_w):
            row_layout.addWidget(item, 0, Qt.AlignmentFlag.AlignVCenter)
        # No "Last: ..." preview here (41): Home's outcome row is the one
        # place the newest outcome is shown.

        # The blank right-hand end of the strip (89, changed in 111): ONE
        # complete command phrase from the catalog, stationary, with one
        # drawn chevron at the right. It scrolled until 111;
        # partially-visible moving text is the wrong default for someone who
        # needs to read an example long enough to say it out loud, and a
        # marquee has no "previous". It is NOT the diagnostic hint slot on
        # Home -- that one is actionable.
        self._example_strip = build_example_strip(self._app)
        self._status_examples = example_phrases(catalog_records(self._app), count=10000)
        self._example_strip.setSizePolicy(QSizePolicy.Policy.Expanding,
                                          QSizePolicy.Policy.Preferred)
        # Keep the 44px target and behavior; offset only its painted glyph.
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPen
        original = self._example_strip._next_btn

        class InsetChevron(type(original)):
            def paintEvent(self, event):
                painter = QPainter(self)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                ink = theme.ACCENT if self.underMouse() or self.hasFocus() else theme.TEXT_SECONDARY
                pen = QPen(theme.qcolor(ink), 1.75)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                x, y = self.width() / 2 + 3, self.height() / 2
                painter.drawPolyline([QPointF(x-2, y-4), QPointF(x+2, y), QPointF(x-2, y+4)])
                if self.hasFocus():
                    painter.drawRoundedRect(self.rect().adjusted(3, 3, -3, -3), 4, 4)

        control = InsetChevron()
        control.setAccessibleName(original.accessibleName())
        control.setToolTip(original.toolTip())
        control.setFixedSize(original.size())
        control.setCursor(original.cursor())
        control.setFocusPolicy(original.focusPolicy())
        control.setStyleSheet(original.styleSheet())
        control.setSizePolicy(original.sizePolicy())
        control.setProperty("paintOffsetX", 3)
        control.clicked.connect(self._example_strip.next)
        control.installEventFilter(self._example_strip)
        self._example_strip.layout().replaceWidget(original, control)
        control.setVisible(not original.isHidden())
        self._example_strip._next_btn = control
        original.hide()
        original.deleteLater()
        row_layout.addWidget(self._example_strip, 1)
        sb.addWidget(row, 1)
        sb.layout().setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

    @staticmethod
    def _nav_style(active: bool) -> str:
        """Settings' inset pill, with a heavier selected label (839)."""
        common = (f" border: none; border-radius: {NAV_RADIUS}px; text-align: left;"
                  f" padding-left: 16px; padding-right: 12px; min-height: {NAV_ROW_H}px;"
                  f" font-size: {theme.TYPE_NAV}px;")
        if active:
            return (f"QPushButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY};"
                    f" font-weight: 600;{common} }}"
                    f"QPushButton:hover {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; }}")
        return (f"QPushButton {{ background: transparent; color: {theme.TEXT_SECONDARY};"
                f" font-weight: 400;{common} }}"
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

    def _on_listening(self):
        action = listening_badge_view(self._app)[3]
        if action == 'stop':
            stop_capture(self._app)
        elif action == 'pause':
            pause_hands_free(self._app)
        elif action == 'resume':
            resume_hands_free(self._app)
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
        return self._panel_cache[name]

    def _on_page_shown(self, name: str):
        """Queue 92: Memos re-reads its store on entry -- a memo recorded
        while another page was up must be there when the user arrives."""
        page = self._panel_cache.get(name)
        if name == "Commands":
            page.refresh_commands()
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
        if name == "Commands":
            from samsara.ui.command_cheatsheet_qt import build_hub_page
            return build_hub_page(self._app, self._stack)
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

    def _fit_status_segments(self, layout, segments, need):
        """Reserve a whole example; wake then device details yield to keys."""
        strip = self._example_strip
        metrics = strip.label.fontMetrics()
        example = max((metrics.horizontalAdvance(example_text(p))
                       for p in self._status_examples), default=0)
        captions = [s.layout().itemAt(0).widget() for s in segments]
        full_need = need + sum(QLabel.sizeHint(c).width() - c.sizeHint().width() for c in captions)
        # Keep the established stacked layout for enlarged status fonts (388).
        stacked = self._lbl_mode.font().pixelSize() > theme.TYPE_BODY and full_need + 2 > self.width()
        reserve = 0 if stacked else example + strip.layout().spacing()
        # These captions repeat what the instructions already say. Keep their
        # accessible names and existing glyphs before dropping useful text.
        compact = not stacked and full_need + reserve + 2 > self.width()
        for caption in captions:
            if bool(caption.property('iconOnly')) != compact:
                caption.setProperty('iconOnly', compact)
                caption.setToolTip(caption.text().title() if compact else '')
                caption.updateGeometry()
                caption.update()
        need = full_need - sum(QLabel.sizeHint(c).width() - c.sizeHint().width() for c in captions)
        widths = [tuple(s.layout().itemAt(i).sizeHint().width() for i in (0, 1)) for s in segments]
        if stacked:
            need -= sum(sum(w) + s.layout().spacing() - max(w) for s, w in zip(segments, widths))
        for segment, separator in ((segments[1], layout.itemAt(3).widget()),
                                   (segments[2], layout.itemAt(1).widget())):
            available = segment is not segments[1] or bool(self._lbl_wake.text())
            show = available and need + reserve + 2 <= self.width()
            if not show:
                inner = segment.layout()
                width = widths[segments.index(segment)]
                need -= (max(width) if stacked else sum(width) + inner.spacing()) + 1 + layout.spacing() * 2
            if segment.isHidden() == show:
                segment.setVisible(show)
            if separator.isHidden() == show:
                separator.setVisible(show)
        return max(need, self.width() + 1) if stacked else need

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

        mic_id   = cfg.get('microphone')
        mic_name = "Default"
        for m in getattr(self._app, 'available_mics', []) or []:
            if m.get('id') == mic_id:
                mic_name = m.get('name', 'Default')
                break
        if len(mic_name) > 36:
            mic_name = mic_name[:35] + '...'
        primary, wake, ava = talk_instructions(self._app)
        self._lbl_mode.setText(primary + (f" \u00b7 {ava}" if ava else ""))  # PLACEHOLDER COPY (824)
        self._lbl_wake.setText(wake)
        self._lbl_mic.setText(mic_name)
        # Keep omitted instructions available to assistive tools and hover.
        self._lbl_mode.setToolTip(' \u00b7 '.join(p for p in (primary, wake, ava) if p))

        label, colour, tooltip, action = listening_badge_view(self._app)
        paused = action == 'resume'
        self._paused_btn.setVisible(paused)
        self._badge.setVisible(not paused)
        control = self._paused_btn if paused else self._badge
        control.setText(label)
        control.setStyleSheet(_badge_css(colour))
        control.setToolTip(tooltip)
        click = tooltip.rsplit('. ', 1)[-1]
        control.setAccessibleName(f"{label}. {click}")  # PLACEHOLDER COPY (824)
        control.setEnabled(action != 'none')

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

    def setMinimumWidth(self, width):
        # Content can ask for a wider floor after placement (for example on
        # a theme/layout refresh); the current monitor bound still wins.
        super().setMinimumWidth(min(width, self.maximumWidth()))

    def _frame_margins(self):
        # Create this widget's hidden native handle so the title/frame, in
        # logical pixels, are included before the first visible placement.
        self.winId()
        m = self.windowHandle().frameMargins()
        return m.left(), m.top(), m.right(), m.bottom()

    def _geometry_screens(self):
        screens = [s.availableGeometry().getRect() for s in QApplication.screens()]
        primary = QApplication.primaryScreen().availableGeometry().getRect()
        return screens, primary

    def _place_geometry(self, rect):
        # A small screen may be below the normal hub minimum. Bound both
        # minimum and maximum so layout hints cannot enlarge it off-screen.
        screens, primary = self._geometry_screens()
        margins = self._frame_margins()
        frame = window_geometry.outer_rect(rect, margins)
        target = next((s for s in screens if window_geometry.contains(s, frame)), primary)
        left, top, right, bottom = margins
        self.setMinimumSize(min(MIN_WIDTH, rect[2]), min(MIN_HEIGHT, rect[3]))
        self.setMaximumSize(max(1, target[2] - left - right), max(1, target[3] - top - bottom))
        self.setGeometry(*rect)

    def _restore_geometry(self):
        cfg = getattr(self._app, 'config', {}) or {}
        stored = None
        try:
            stored = (int(cfg['window_x']), int(cfg['window_y']),
                      max(MIN_WIDTH, int(cfg.get('window_width') or DEFAULT_WIDTH)),
                      max(MIN_HEIGHT, int(cfg.get('window_height') or DEFAULT_HEIGHT)))
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        screens, primary = self._geometry_screens()
        rect = window_geometry.restore_geometry(
            stored, screens, primary, (DEFAULT_WIDTH, DEFAULT_HEIGHT), self._frame_margins())
        if stored is not None and rect != stored:
            logger.info("[MAIN] saved geometry %s does not fit a current monitor; reset to %s", stored, rect)
        self._place_geometry(rect)

    def _reset_geometry(self):
        screens, primary = self._geometry_screens()
        current_frame = self.frameGeometry().getRect()
        self.showNormal()
        rect = window_geometry.reset_geometry(
            current_frame, screens, primary, (DEFAULT_WIDTH, DEFAULT_HEIGHT), self._frame_margins())
        self._place_geometry(rect)
        self._geom_timer.stop()
        self._save_geometry()

    def _save_geometry(self):
        try:
            g = self.normalGeometry() if self.isMaximized() or self.isMinimized() else self.geometry()
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
        if reason == "splash closed":
            # Splash dismissal is passive. Startup already showed the hub;
            # closing its overlay must not activate it over the user's work.
            logger.info("[MAIN] splash closed: main window remains visible")
            return
        attempted = window_activation.surface_requested(
            window, confirm=lambda: self._confirm_in_front(reason))
        if not attempted:
            logger.info("[MAIN] %s: active modal kept focus", reason)
            return
        g = window.geometry()
        logger.info(
            "[MAIN] %s: show() called, isVisible=%s windowState=%s geometry=%s,%s %sx%s",
            reason, window.isVisible(), _state_name(window), g.x(), g.y(), g.width(), g.height())

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

def placement_on_screens(x, y, w, h, screens, primary, default_size=None):
    """QRect adapter for the pure containment rule.

    Legacy callers use their supplied size as fallback; the hub startup
    explicitly uses its 900x650 defaults via window_geometry.
    """
    return window_geometry.restore_geometry(
        (x, y, w, h), [s.getRect() for s in screens], primary.getRect(),
        default_size if default_size is not None else (w, h))


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
