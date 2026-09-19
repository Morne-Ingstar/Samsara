"""
PySide6 settings window for Samsara.

All Qt operations are posted to the shared qt_runtime event loop.
"""

import copy
import re
import shutil
import threading
from datetime import datetime
from string import Template

from PySide6.QtCore import Qt, QTimer, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QStackedWidget, QScrollArea,
    QLabel, QComboBox, QCheckBox, QPushButton, QFrame,
    QDoubleSpinBox, QSpinBox, QLineEdit, QSlider,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QDialog, QMessageBox, QFileDialog, QFormLayout, QGridLayout, QSizePolicy,
    QInputDialog,
)

from samsara import config_defaults
from samsara import session_modes
from samsara.config_transfer import (
    ConfigTransferError,
    export_config,
    load_config_export,
    merge_import,
)
from samsara.constants import (
    DEFAULT_CONTINUOUS_COMMIT_HOTKEY,
    DEFAULT_CONTINUOUS_COMMIT_TRIGGER,
    DEFAULT_WAKE_PHRASE,
    DEFAULT_WAKE_PHRASE_OPTIONS,
)
from samsara.ui_scale import UI_SCALE_OPTIONS, ui_scale_label
from samsara.ui import qt_runtime, theme
from samsara.runtime import thread_registry
from samsara.audio_devices import pick_index_by_name
from samsara import smart_corrections
from samsara.support_feedback import (
    BETA_SUPPORT_EMAIL,
    BETA_SUPPORT_MAILTO,
    BUG_REPORT_URL,
    DOCUMENTATION_URL,
    build_safe_diagnostic_summary,
)

from samsara.log import get_logger

logger = get_logger(__name__)


def _format_alarm_next(next_at, *, enabled=True, active=False, now=None) -> str:
    """Format an AlarmManager next-trigger timestamp for the settings table."""
    if not enabled:
        return "—"
    if active:
        return "Active"
    if next_at is None:
        return "Paused"
    if now is None:
        now = datetime.now().timestamp()
    if next_at <= now:
        return "Due"

    trigger = datetime.fromtimestamp(next_at)
    current = datetime.fromtimestamp(now)
    if trigger.date() == current.date():
        return trigger.strftime("%I:%M %p").lstrip("0")
    return trigger.strftime("%b %d, %I:%M %p").replace(" 0", " ")


# ---------------------------------------------------------------------------
# Hotkey capture helpers
# ---------------------------------------------------------------------------

# Sort modifiers before regular keys
_MOD_ORDER = {'ctrl': 0, 'shift': 1, 'alt': 2, 'altgr': 3, 'win': 4}

_MODIFIER_KEYS = {
    Qt.Key.Key_Control: 'ctrl',
    Qt.Key.Key_Shift:   'shift',
    Qt.Key.Key_Alt:     'alt',
    Qt.Key.Key_Meta:    'win',
    Qt.Key.Key_AltGr:   'altgr',
}

_SPECIAL_KEYS = {
    Qt.Key.Key_Escape:    'escape',
    Qt.Key.Key_Tab:       'tab',
    Qt.Key.Key_Return:    'enter',
    Qt.Key.Key_Enter:     'enter',
    Qt.Key.Key_Backspace: 'backspace',
    Qt.Key.Key_Delete:    'delete',
    Qt.Key.Key_Insert:    'insert',
    Qt.Key.Key_Home:      'home',
    Qt.Key.Key_End:       'end',
    Qt.Key.Key_PageUp:    'page up',
    Qt.Key.Key_PageDown:  'page down',
    Qt.Key.Key_Left:      'left',
    Qt.Key.Key_Right:     'right',
    Qt.Key.Key_Up:        'up',
    Qt.Key.Key_Down:      'down',
    Qt.Key.Key_Space:     'space',
    Qt.Key.Key_CapsLock:  'capslock',
    Qt.Key.Key_F1:        'f1',
    Qt.Key.Key_F2:        'f2',
    Qt.Key.Key_F3:        'f3',
    Qt.Key.Key_F4:        'f4',
    Qt.Key.Key_F5:        'f5',
    Qt.Key.Key_F6:        'f6',
    Qt.Key.Key_F7:        'f7',
    Qt.Key.Key_F8:        'f8',
    Qt.Key.Key_F9:        'f9',
    Qt.Key.Key_F10:       'f10',
    Qt.Key.Key_F11:       'f11',
    Qt.Key.Key_F12:       'f12',
    Qt.Key.Key_F13:       'f13',
    Qt.Key.Key_NumLock:   'num lock',
    Qt.Key.Key_ScrollLock: 'scroll lock',
    Qt.Key.Key_Pause:     'pause',
    Qt.Key.Key_Print:     'print screen',
}


# ---------------------------------------------------------------------------
# Commands tab constants
# ---------------------------------------------------------------------------

_CMD_BUTTON_OPTIONS = {
    'Mouse 4':              'mouse4',
    'Mouse 5':              'mouse5',
    'Right Ctrl (default)': 'rctrl',
    'Left Ctrl':            'lctrl',
    'Right Alt':            'ralt',
    'Left Alt':             'lalt',
    'Right Shift':          'rshift',
    'Left Shift':           'lshift',
    **{f'F{n}': f'f{n}' for n in range(13, 25)},
}
_CMD_BUTTON_KEY_TO_LABEL = {v: k for k, v in _CMD_BUTTON_OPTIONS.items()}


def _collect_command_rows(executor) -> list[dict]:
    """Return builtin and plugin commands for the Settings command table."""
    if executor is None:
        return []

    rows: dict[str, dict] = {}
    for phrase, data in (getattr(executor, 'commands', {}) or {}).items():
        rows[phrase] = {
            'phrase': phrase,
            'source': 'builtin',
            'type': data.get('type', ''),
            'pack': data.get('pack', 'core'),
            'description': data.get('description', ''),
            'aliases': [],
            'data': data,
        }

    matcher = getattr(executor, '_matcher', None)
    if matcher is not None and hasattr(matcher, 'list_commands'):
        for entry in matcher.list_commands():
            phrase = entry.get('phrase', '')
            if not phrase or phrase in rows:
                continue
            rows[phrase] = {
                'phrase': phrase,
                'source': entry.get('source', 'plugin'),
                'type': entry.get('type', 'plugin'),
                'pack': entry.get('pack', 'core'),
                'description': entry.get('description', ''),
                'aliases': entry.get('aliases', []),
                'data': entry,
            }

    return [rows[phrase] for phrase in sorted(rows)]

# ---------------------------------------------------------------------------
# Ava Command Session tab constants
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cloud LLM / Ava tab constants
# ---------------------------------------------------------------------------


def _key_name(key: int) -> str | None:
    """Convert a Qt key integer to a keyboard-library-compatible name."""
    if key in _MODIFIER_KEYS:
        return _MODIFIER_KEYS[key]
    if key in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[key]
    # Printable ASCII (letters, digits, punctuation)
    if 0x20 <= key <= 0x7E:
        return chr(key).lower()
    return None


def _combo_str(held: set) -> str:
    return '+'.join(sorted(held, key=lambda k: (_MOD_ORDER.get(k, 99), k)))


# ---------------------------------------------------------------------------
# theme.py token helper
# ---------------------------------------------------------------------------

_RGBA_RE = re.compile(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)')


def _css_color_to_qcolor(css: str) -> QColor:
    """Parse a theme.py color token into a QColor. QColor's string
    constructor understands hex/named colors but not CSS rgba(...) syntax,
    and several theme.py tokens (e.g. TEXT_DISABLED) use that form."""
    m = _RGBA_RE.match(css.strip())
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        return QColor(r, g, b, round(a * 255))
    return QColor(css)


def _cloud_ai_state(app, *, reachable: bool | None = None) -> tuple[str, str]:
    """Return a private state code and honest Cloud AI wording.

    A configured key, the user's enabled switch, and the result of an actual
    connection test are different facts. Keep them separate so the Advanced
    page cannot call a stored key "not configured" again.
    """
    cfg = getattr(app, "config", {}).get("cloud_llm", {}) or {}
    if not str(cfg.get("api_key", "")).strip():
        return "not_keyed", "Cloud AI not configured — no API key"
    if not bool(cfg.get("enabled", False)):
        return "keyed_disabled", "Cloud AI configured — disabled"
    if reachable is True:
        return "enabled_reachable", "Cloud AI enabled — reachable"
    if reachable is False:
        return "enabled_unreachable", "Cloud AI enabled — unreachable"
    return "enabled_unchecked", "Cloud AI enabled — connection not yet checked"


# ---------------------------------------------------------------------------
# Hotkey capture button
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Modes tab claims (08d, 2026-09-13 manual review). Every description that
# asserts runtime behaviour lives here so tests can pin it to its source.
# ---------------------------------------------------------------------------


class _HotkeyButton(QPushButton):
    """Shows the current hotkey combo; captures a new one when clicked."""

    @staticmethod
    def _idle_qss() -> str:
        """Built per call: a stylesheet assigned in the class body is
        evaluated once, when the module is imported, so it would keep
        the startup palette for the life of the process (queue 129)."""
        return (
            "QPushButton {"
            f" background-color: {theme.BG2};"
            f" border: 1px solid {theme.wash(0.14)};"
            " border-radius: 6px;"
            f" color: {theme.TEXT_PRIMARY};"
            f" font-size: {theme.TYPE_MIN}px;"
            " font-family: 'Consolas', 'Courier New', monospace;"
            " padding: 6px 14px;"
            "}"
            "QPushButton:hover {"
            f" background-color: {theme.BG1};"
            "}"
        )

    @staticmethod
    def _capturing_qss() -> str:
        """The capturing state. See _idle_qss()."""
        return (
            "QPushButton {"
            f" background-color: {theme.tint(theme.ACCENT, 0.08)};"
            f" border: 1px solid {theme.ACCENT};"
            " border-radius: 6px;"
            f" color: {theme.ACCENT};"
            f" font-size: {theme.TYPE_MIN}px;"
            " font-family: 'Consolas', 'Courier New', monospace;"
            " padding: 6px 14px;"
            "}"
        )

    # Side mouse buttons a capture may bind (main hotkey only; see allow_mouse).
    _MOUSE_CAPTURE = {
        Qt.MouseButton.XButton1: 'mouse4',
        Qt.MouseButton.XButton2: 'mouse5',
    }

    def __init__(self, combo: str, on_change=None, allow_mouse: bool = False):
        self._allow_mouse = allow_mouse
        super().__init__(self._idle_text(combo))
        self._combo = combo
        self._capturing = False
        self._held: set[str] = set()
        self._on_change = on_change   # optional callable(), fired after a new combo is captured
        self.setMinimumWidth(180)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(self._idle_qss())
        self.clicked.connect(self._start_capture)

    @property
    def combo(self) -> str:
        return self._combo

    @staticmethod
    def _idle_text(combo: str) -> str:
        if combo in ('mouse4', 'mouse5'):
            return _readable_hotkey(combo)
        return combo or "\u2014"

    def _capture_prompt(self) -> str:
        return "Press keys or Mouse 4/5..." if self._allow_mouse else "Press keys..."

    def _start_capture(self):
        self._capturing = True
        self._held = set()
        self.setText(self._capture_prompt())
        self.setStyleSheet(self._capturing_qss())
        self.setFocus()

    def _finish_capture(self):
        self._capturing = False
        if self._held:
            self._combo = _combo_str(self._held)
        self.setText(self._idle_text(self._combo))
        self.setStyleSheet(self._idle_qss())
        if self._on_change is not None:
            self._on_change()

    def mousePressEvent(self, event):
        if self._capturing and self._allow_mouse:
            name = self._MOUSE_CAPTURE.get(event.button())
            if name is not None:
                self._held = set()
                self._combo = name
                self._finish_capture()
                event.accept()
                return
            if event.button() in (
                Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton,
            ):
                event.accept()   # ignored while capturing: keep waiting
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if not self._capturing:
            super().keyPressEvent(event)
            return
        name = _key_name(event.key())
        if name:
            self._held.add(name)
            self.setText(_combo_str(self._held) or self._capture_prompt())
        event.accept()

    def keyReleaseEvent(self, event):
        if not self._capturing:
            super().keyReleaseEvent(event)
            return
        # Finalize on first key release (same behaviour as Tkinter version)
        self._finish_capture()
        event.accept()

    def focusOutEvent(self, event):
        if self._capturing:
            self._finish_capture()
        super().focusOutEvent(event)


def _readable_hotkey(combo: str) -> str:
    """Human-facing hotkey spelling while keeping stored bindings stable."""
    if combo in ('mouse4', 'mouse5'):
        return _CMD_BUTTON_KEY_TO_LABEL[combo]   # 'Mouse 4' / 'Mouse 5'
    names = {
        'ctrl': 'Ctrl', 'shift': 'Shift', 'alt': 'Alt',
        'left_ctrl': 'Left Ctrl', 'right_ctrl': 'Right Ctrl',
        'left_shift': 'Left Shift', 'right_shift': 'Right Shift',
        'left_alt': 'Left Alt', 'right_alt': 'Right Alt',
    }
    return '+'.join(
        names.get(part, part.upper() if part.startswith('f') and part[1:].isdigit()
                  else part.title())
        for part in (combo or '').split('+') if part
    )


class _AlarmHotkeyButton(_HotkeyButton):
    """Alarm shortcut capture with a readable action-labelled idle state."""

    @staticmethod
    def _alarm_idle_qss() -> str:
        """The alarm rows read as sentences, not as key caps: one step up in
        size, semibold, and the UI face instead of the monospace one."""
        return _HotkeyButton._idle_qss().replace(
            f"font-size: {theme.TYPE_MIN}px;",
            f"font-size: {theme.TYPE_BODY}px; font-weight: 600;"
        ).replace("'Consolas', 'Courier New', monospace", "'Segoe UI', sans-serif")

    def __init__(self, combo: str, action: str):
        self._alarm_action = action
        super().__init__(combo)
        self.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.setStyleSheet(self._alarm_idle_qss())
        self._show_idle_text()

    def _show_idle_text(self) -> None:
        readable = _readable_hotkey(self._combo) or "Not set"
        self.setText(f"{readable} — {self._alarm_action}")
        self.setAccessibleName(f"{self._alarm_action} shortcut: {readable}")
        self.setToolTip(f"Click to change the {self._alarm_action.lower()} shortcut")

    def _finish_capture(self):
        super()._finish_capture()
        self.setStyleSheet(self._alarm_idle_qss())
        self._show_idle_text()


class _HeightForWidthWidget(QWidget):
    """A QWidget that keeps its minimum height in sync with its layout's
    heightForWidth() result for the widget's current width, AND forces its
    own internal layout to redistribute using that corrected height.

    Qt's built-in sizePolicy-based height-for-width propagation does not
    reliably reach through multiple levels of addLayout()-in-addLayout()
    nesting (e.g. a setting row's layout, inside a card's layout, inside a
    scroll area's content layout) -- setting hasHeightForWidth(True) at
    every level still leaves the outermost setGeometry() pass using a
    pre-wrap sizeHint. Recomputing minimumHeight directly on resize fixes
    this widget's OWN outer size as seen by its parent layout.

    That alone is not sufficient at live (non-default) window widths,
    though: raising minimumHeight here only takes effect on this widget's
    *next* resize (driven by its parent, once the parent notices the new
    constraint) -- Qt does not synchronously re-run this widget's *own*
    internal layout for the corrected size. Confirmed by direct inspection:
    even after the parent grows this widget to the corrected height, this
    widget's own layout().geometry() stays cached at the pre-growth rect,
    so a child like a wrapped description QLabel is still confined to its
    old single-line slot and its second line renders past the widget's
    padding into whatever sits below/beside it. Explicitly calling
    layout().setGeometry() with this widget's current rect forces that
    redistribution every time, regardless of which resize pass we're in.
    """

    def resizeEvent(self, event):
        super().resizeEvent(event)
        layout = self.layout()
        if layout is None:
            return
        if layout.hasHeightForWidth():
            needed = layout.heightForWidth(self.width())
            if needed > 0 and needed != self.minimumHeight():
                self.setMinimumHeight(needed)
        layout.setGeometry(self.rect())


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

def stylesheet() -> str:
    """The Settings window's own sheet, built on demand.

    Not a module constant: a Template substituted at import time freezes
    whichever palette was live then -- and this is the one window the user
    changes the theme FROM (queue 129).

    _SettingsWindow's sheet overrides the shared theme's QComboBox
    subcontrols, so the real high-contrast chevron is repeated here:
    reserving a drop-down area without an arrow made every selector look
    like a read-only text field. A window's own sheet also outranks the
    application sheet, so the shared scrollbar rule comes along too."""
    return (Template(f"""
QMainWindow, QWidget {{
    background-color: ${{BG0}};
    color: ${{TEXT_PRIMARY}};
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: {theme.TYPE_BODY}px;
}}
QListWidget {{
    background-color: ${{BG1}};
    border-right: 1px solid {theme.wash(0.08)};
    color: {theme.TEXT_SECONDARY};
    font-size: {theme.TYPE_BODY}px;
    padding: 8px 0;
    outline: none;
}}
QListWidget::item {{
    padding: 10px 20px;
    min-height: {theme.HIT_TARGET_MIN}px;
    border: none;
}}
QListWidget::item:selected {{
    background-color: {theme.tint(theme.ACCENT, 0.12)};
    color: ${{ACCENT}};
    border-left: 2px solid ${{ACCENT}};
}}
QListWidget::item:hover {{
    background-color: {theme.wash(0.03)};
}}
QLabel {{
    color: ${{TEXT_PRIMARY}};
    /* QLabel inherits from QWidget, so without this it picks up the
       QMainWindow, QWidget rule's background-color (BG0) above as an
       opaque bar behind every label -- most visible against the lighter
       BG1 section-card background (see _section_card). Transparent
       lets each label show whatever surface (card, panel, window) it
       actually sits on instead of painting its own opaque rectangle. */
    background-color: transparent;
}}
QLabel[class="description"] {{
    color: {theme.TEXT_SECONDARY};
    font-size: {theme.TYPE_BODY}px;
}}
QLabel[class="section-title"] {{
    color: ${{ACCENT}};
    font-size: {theme.TYPE_HEADING}px;
    font-weight: bold;
}}
QComboBox {{
    background-color: ${{BG2}};
    border: 1px solid {theme.wash(0.14)};
    border-radius: 6px;
    padding: 8px 12px;
    color: ${{TEXT_PRIMARY}};
    min-width: 200px;
    min-height: {theme.HIT_TARGET_MIN}px;
}}
QComboBox::drop-down {{
    border: none;
    width: 30px;
}}
QComboBox QAbstractItemView {{
    background-color: ${{BG2}};
    color: ${{TEXT_PRIMARY}};
    selection-background-color: {theme.tint(theme.ACCENT, 0.2)};
    border: 1px solid {theme.wash(0.14)};
}}
QCheckBox {{
    color: ${{TEXT_PRIMARY}};
    spacing: 8px;
    /* Same cascade cause as the QLabel fix above (9b7f00f): QCheckBox is a
       QWidget with no background-color of its own, so it otherwise picks
       up the QMainWindow, QWidget rule's BG0 as an opaque bar across
       the row -- the ::indicator sub-control below already paints its own
       background correctly and is untouched. */
    background-color: transparent;
    min-height: {theme.HIT_TARGET_MIN}px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid {theme.wash(0.14)};
    background-color: ${{BG2}};
}}
QCheckBox::indicator:checked {{
    background-color: ${{ACCENT}};
    border-color: ${{ACCENT}};
    /* The checked circle is a shape cue, so state does not depend on
       perceiving its accent colour. */
    border-radius: 9px;
    border: 4px solid ${{TEXT_ON_ACCENT}};
}}
QPushButton {{
    background-color: ${{ACCENT}};
    color: ${{TEXT_ON_ACCENT}};
    border: none;
    border-radius: 6px;
    padding: 10px 24px;
    font-weight: 600;
    font-size: {theme.TYPE_BODY}px;
    min-height: {theme.HIT_TARGET_MIN}px;
}}
QPushButton:hover {{
    background-color: ${{ACCENT_HOVER}};
}}
QPushButton[class="secondary"] {{
    background-color: transparent;
    color: {theme.TEXT_SECONDARY};
    border: 1px solid {theme.wash(0.14)};
}}
QPushButton[class="secondary"]:hover {{
    background-color: {theme.wash(0.05)};
    color: ${{TEXT_PRIMARY}};
}}
QScrollArea {{
    border: none;
    background-color: transparent;
}}
QSpinBox, QDoubleSpinBox {{
    background-color: ${{BG2}};
    border: 1px solid {theme.wash(0.14)};
    border-radius: 6px;
    padding: 6px 10px;
    color: ${{TEXT_PRIMARY}};
    min-width: 80px;
    min-height: {theme.HIT_TARGET_MIN}px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    background-color: transparent;
    border: none;
    width: 20px;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: none;
    width: 0;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: none;
    width: 0;
}}
QLineEdit {{
    background-color: ${{BG2}};
    border: 1px solid {theme.wash(0.14)};
    border-radius: 6px;
    padding: 8px 12px;
    color: ${{TEXT_PRIMARY}};
    font-size: {theme.TYPE_BODY}px;
    min-height: {theme.HIT_TARGET_MIN}px;
}}
QLineEdit:focus {{
    border-color: {theme.tint(theme.ACCENT, 0.5)};
}}
QTableWidget {{
    background-color: ${{BG1}};
    gridline-color: {theme.wash(0.05)};
    color: ${{TEXT_PRIMARY}};
    border: 1px solid {theme.wash(0.08)};
    border-radius: 6px;
    font-size: {theme.TYPE_BODY}px;
    outline: none;
}}
QTableWidget::item {{
    padding: 5px 8px;
    border: none;
}}
QTableWidget::item:selected {{
    background-color: {theme.tint(theme.ACCENT, 0.15)};
    color: ${{TEXT_PRIMARY}};
}}
QHeaderView::section {{
    background-color: ${{BG2}};
    color: {theme.TEXT_SECONDARY};
    padding: 6px 8px;
    border: none;
    border-bottom: 1px solid {theme.wash(0.08)};
    border-right: 1px solid {theme.wash(0.04)};
    font-size: {theme.TYPE_BODY}px;
    font-weight: 600;
}}
QDialog {{
    background-color: ${{BG0}};
}}
    """).substitute(
        BG0=theme.BG0,
        TEXT_PRIMARY=theme.TEXT_PRIMARY,
        BG1=theme.BG1,
        ACCENT=theme.ACCENT,
        BG2=theme.BG2,
        TEXT_ON_ACCENT=theme.TEXT_ON_ACCENT,
        ACCENT_HOVER=theme.ACCENT_HOVER,
    )
            + f"""
QComboBox::down-arrow {{
    image: url({theme.ARROW_PATH});
    width: 10px;
    height: 6px;
    margin-right: 10px;
}}
"""
            + theme.SCROLLBAR_QSS)

_CONTENT_MAX_WIDTH = 1000  # each tab's scrollable content column caps here;
                           # cards expand to fill it below the cap and stop
                           # growing above it (see each _build_*_tab's
                           # container/outer widget). Lives once per tab at
                           # this shared container level, not per card --
                           # _section_card itself carries no width cap.

_TAB_NAMES = [
    "General",
    "Modes",
    "Commands",
    "Sounds",
    "TTS",
    "Ava / Cloud",
    "Alarms",
    "Health",
    "Advanced",
    "Help & Support",
    "Music",
]  # order matches self._stack.addWidget(...) calls in __init__ -- tab
   # INDICES must not change; _SIDEBAR_GROUPS below only changes their
   # VISUAL order/grouping in the sidebar.

# Two visually distinct sidebar groups. "Settings" tabs go through Apply &&
# Close; "Tools" tabs (Commands editor, Alarms manager, Health log) save
# instantly -- see each tab's "apply immediately" caption.
_SIDEBAR_GROUPS = [
    ("Settings", ["General", "Modes", "Sounds", "TTS", "Ava / Cloud", "Music", "Advanced"]),
    ("Tools",    ["Commands", "Alarms", "Health"]),
    ("Support", ["Help & Support"]),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class SettingsQt:
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
        self._window = _SettingsWindow(self.app)
        self._window.show()


# ---------------------------------------------------------------------------
# Pages (samsara/ui/settings/): one mixin per settings page, imported here --
# after every shared name above exists, because each page module imports
# those names back from this module (see samsara/ui/settings/__init__.py).
# ---------------------------------------------------------------------------

from samsara.ui.settings.general_qt import GeneralPage  # noqa: E402
from samsara.ui.settings.modes_qt import ModesPage  # noqa: E402
from samsara.ui.settings.commands_qt import CommandsPage  # noqa: E402
from samsara.ui.settings.sounds_qt import SoundsPage  # noqa: E402
from samsara.ui.settings.tts_qt import TTSPage  # noqa: E402
from samsara.ui.settings.ava_cloud_qt import AvaCloudPage  # noqa: E402
from samsara.ui.settings.alarms_qt import AlarmsPage  # noqa: E402
from samsara.ui.settings.health_qt import HealthPage  # noqa: E402
from samsara.ui.settings.advanced_qt import AdvancedPage  # noqa: E402
from samsara.ui.settings.help_qt import HelpPage  # noqa: E402
from samsara.ui.settings.music_qt import MusicPage  # noqa: E402


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class _SettingsWindow(
    GeneralPage,
    ModesPage,
    CommandsPage,
    SoundsPage,
    TTSPage,
    AvaCloudPage,
    AlarmsPage,
    HealthPage,
    AdvancedPage,
    HelpPage,
    MusicPage,
    QMainWindow,
):
    # Emitted from worker threads to update the test-connection label safely
    _test_result = Signal(str, str)  # (message, css-color)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self._widgets = {}
        self._cloud_reachable: bool | None = None
        # Each _build_x_tab registers a save callable here: fn(updates_so_far)
        # -> dict of top-level config updates for its own tab. Called in
        # registration order (== tab-build order below) by _apply_and_close.
        # updates_so_far lets a later tab's fn merge onto an earlier tab's
        # partial write to the same nested key (command_mode, wake_word_config)
        # instead of clobbering it -- same semantics the old monolith had by
        # reading self.app.config.get(...) / updates.get(...) procedurally.
        self._save_fns: list = []
        self._test_result.connect(self._on_test_result)

        self.setWindowTitle("Samsara Settings")
        # UI scale composes with Windows display scaling. A hard 860x600
        # logical minimum becomes taller than a 1080p work area at 150% DPI
        # plus the 130% accessibility scale, so size against the logical work
        # area and let scrollable pages handle the remaining content.
        self.setMinimumSize(720, 480)
        available = self.screen().availableGeometry()
        self.resize(
            min(920, max(720, available.width() - 40)),
            min(700, max(480, available.height() - 40)),
        )
        theme.install_app_scrollbars()
        self.setStyleSheet(stylesheet())

        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Search bar: accessibility-driven live filter across every tab.
        # Pinned above the sidebar/content body (not inside any one tab) so
        # one box always filters everything. textChanged fires per keystroke;
        # a restarted single-shot QTimer debounces the actual filter pass by
        # ~150ms so fast typing doesn't re-walk the registry on every char.
        # Each tab's content column is centered within (window width minus
        # sidebar width) and capped at _CONTENT_MAX_WIDTH via
        # scroll.setAlignment(AlignHCenter) + container.setMaximumWidth(...)
        # -- that is QScrollArea's own resize-to-fit-then-center behavior,
        # not something a plain QHBoxLayout's stretch items reproduce (a
        # stretch-based attempt here left the search box's left edge
        # nowhere near the real, QScrollArea-centered content column's, see
        # commit history). Reuse the identical QScrollArea recipe instead
        # of hand-rolling the same centering-with-a-cap math a second time.
        search_wrap = QWidget()
        search_outer = QHBoxLayout(search_wrap)
        search_outer.setContentsMargins(0, 14, 0, 6)
        search_outer.setSpacing(0)
        search_outer.addSpacing(176)  # sidebar width, set on self._sidebar below

        search_scroll = QScrollArea()
        search_scroll.setWidgetResizable(True)
        search_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        search_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        search_scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        search_scroll.setFixedHeight(theme.HIT_TARGET_MIN + 2)

        search_center = QWidget()
        search_center.setMaximumWidth(_CONTENT_MAX_WIDTH)
        search_center_layout = QHBoxLayout(search_center)
        search_center_layout.setContentsMargins(28, 0, 20, 0)  # 28 matches
            # every _build_*_tab container's own left inset
        search_center_layout.setSpacing(0)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search settings…")
        self._search_edit.setClearButtonEnabled(True)  # built-in × affordance
        self._search_edit.setMaximumWidth(420)
        search_center_layout.addWidget(self._search_edit)
        search_center_layout.addStretch()

        search_scroll.setWidget(search_center)
        search_outer.addWidget(search_scroll, 1)
        root.addWidget(search_wrap)

        self._search_rows: list = []  # populated by _build_search_registry() below
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(150)
        self._search_debounce.timeout.connect(self._apply_search_filter)
        self._search_edit.textChanged.connect(lambda _text: self._search_debounce.start())

        # Body: sidebar + stacked content
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        root.addWidget(body, stretch=1)

        self._sidebar = QListWidget()
        self._sidebar.setFixedWidth(176)
        self._sidebar.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # Sidebar rows no longer map 1:1 to stack indices (group headers are
        # interspersed and unselectable) -- build an explicit row->stack-index
        # map instead of relying on row == index.
        name_to_stack_index = {name: i for i, name in enumerate(_TAB_NAMES)}
        self._advanced_stack_index = name_to_stack_index.get('Advanced')
        self._sidebar_row_to_stack_index: dict = {}
        row = 0
        for group_label, tab_names in _SIDEBAR_GROUPS:
            header_item = QListWidgetItem(group_label.upper())
            # Enabled means Qt honours the explicit token foreground. A
            # disabled item is painted with the platform's disabled palette
            # (near-black in the dark theme), even after setForeground().
            # It remains non-selectable, so clicks still do nothing.
            header_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            header_item.setForeground(QColor(theme.TEXT_PRIMARY))
            header_item.setFont(theme.qfont(theme.TYPE_MIN, weight=QFont.Weight.Bold))
            self._sidebar.addItem(header_item)
            row += 1

            for name in tab_names:
                self._sidebar.addItem(QListWidgetItem(name))
                self._sidebar_row_to_stack_index[row] = name_to_stack_index[name]
                row += 1

        # First selectable row is always row 1 (row 0 is the first group's header).
        first_selectable_row = 1
        self._sidebar.setCurrentRow(first_selectable_row)
        body_layout.addWidget(self._sidebar)

        self._stack = QStackedWidget()
        body_layout.addWidget(self._stack, stretch=1)

        self._stack.addWidget(self._build_general_tab())    # 0  General
        self._stack.addWidget(self._build_modes_tab())      # 1  Modes
        self._stack.addWidget(self._build_commands_tab())   # 2  Commands
        self._stack.addWidget(self._build_sounds_tab())     # 3  Sounds
        self._stack.addWidget(self._build_tts_tab())         # 4  TTS
        self._stack.addWidget(self._build_ava_cloud_tab())  # 5  Ava / Cloud
        self._stack.addWidget(self._build_alarms_tab())     # 6  Alarms
        self._stack.addWidget(self._build_health_tab())     # 7  Health
        self._stack.addWidget(self._build_advanced_tab())      # 8  Advanced
        self._stack.addWidget(self._build_support_tab())       # 9  Help & Support
        self._stack.addWidget(self._build_music_tab())         # 10 Music

        self._apply_metric_minimum_widths()
        self._build_search_registry()

        self._stack.setCurrentIndex(self._sidebar_row_to_stack_index[first_selectable_row])
        self._sidebar.currentRowChanged.connect(self._on_sidebar_row_changed)

        # Separator above button bar
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background-color: {theme.wash(0.08)}; max-height: 1px;")
        root.addWidget(sep)

        # Button bar
        btn_bar = QWidget()
        btn_bar.setFixedHeight(theme.HIT_TARGET_MIN + 24)
        btn_bar.setStyleSheet(f"background-color: {theme.BG0};")
        btn_layout = QHBoxLayout(btn_bar)
        btn_layout.setContentsMargins(20, 12, 20, 12)
        btn_layout.addStretch()

        # "Close", not "Cancel" -- several tabs (Commands editor, Alarms
        # manager, license activate/remove, sound theme apply) save
        # instantly, so "Cancel" would imply a rollback that doesn't exist
        # for those. This button just dismisses the window.
        close_btn = QPushButton("Close")
        close_btn.setProperty("class", "secondary")
        close_btn.style().unpolish(close_btn)
        close_btn.style().polish(close_btn)
        close_btn.setMinimumWidth(max(100, close_btn.sizeHint().width()))
        close_btn.clicked.connect(self.close)
        btn_layout.addWidget(close_btn)

        apply_btn = QPushButton("Apply && Close")
        apply_btn.setStyleSheet(
            "QPushButton {"
            f" background-color: {theme.ACCENT};"
            f" color: {theme.TEXT_ON_ACCENT};"
            " border: none;"
            " border-radius: 6px;"
            " padding: 10px 24px;"
            " font-weight: 600;"
            f" font-size: {theme.TYPE_BODY}px;"
            "}"
            f"QPushButton:hover {{ background-color: {theme.ACCENT_HOVER}; }}"
        )
        apply_btn.setMinimumWidth(max(140, apply_btn.sizeHint().width()))
        apply_btn.clicked.connect(self._apply_and_close)
        btn_layout.addWidget(apply_btn)

        root.addWidget(btn_bar)

    def closeEvent(self, e):
        e.ignore()
        self.hide()

    def _on_sidebar_row_changed(self, row: int) -> None:
        """Map a sidebar row to its stack index. Header rows aren't in the
        map (they're unselectable, so this shouldn't normally fire for one,
        but .get() makes that a no-op rather than a crash either way)."""
        stack_index = self._sidebar_row_to_stack_index.get(row)
        if stack_index is not None:
            self._stack.setCurrentIndex(stack_index)
            if stack_index == self._advanced_stack_index:
                self._refresh_sc_status()

    def _refresh_sc_status(self) -> None:
        """Refresh the Smart Corrections "Active backend" status line --
        called on tab build and every time the Advanced tab is shown, since
        Ollama reachability can change between settings-window visits."""
        label = self._widgets.get('sc_status_label')
        if label is None:
            return
        try:
            backend = smart_corrections.describe_backend_status(self.app)
            _state, cloud = _cloud_ai_state(
                self.app, reachable=self._cloud_reachable
            )
            status = f"{backend} · {cloud}"
        except Exception as e:
            logger.debug(f"[SETTINGS] Smart Corrections status refresh failed: {e}")
            status = "unknown"
        label.setText(f"Active backend: {status}")

    def _refresh_sc_cloud_hint(self) -> None:
        """Keep the Cloud-backend warning aligned with the status line."""
        hint = self._widgets.get('sc_cloud_hint')
        backend = self._widgets.get('sc_backend')
        if hint is None or backend is None:
            return
        state, message = _cloud_ai_state(
            self.app, reachable=self._cloud_reachable
        )
        hint.setText(message)
        hint.setVisible(
            backend.currentText() == 'cloud' and state != 'enabled_reachable'
        )

    # ------------------------------------------------------------------
    # Search: live filter across all tabs
    # ------------------------------------------------------------------

    #: Buttons whose minimum width was a literal tuned against Segoe UI
    #: metrics. The literal stays as the touch-target FLOOR (never narrower
    #: than it renders today, per the 40px+ accessibility rule); the effective
    #: minimum is whichever is larger, the floor or the button's own sizeHint.
    _METRIC_MIN_WIDTH_BUTTONS = {
        "microphoneRefreshButton": 104,
    }

    def _apply_metric_minimum_widths(self) -> None:
        """Re-derive hardcoded button minimums from each button's own metrics.

        Runs AFTER the pages are in self._stack, which is when the window's
        stylesheet() cascade actually reaches them: a QPushButton's sizeHint()
        only includes the stylesheet's padding once it is parented and
        polished (bare 98px -> 146px styled, measured). Reading it inside the
        page builder returns the unstyled width, which is exactly the mistake
        the literals encoded -- they were correct for Segoe UI and clipped the
        label under any wider font stack.
        """
        for object_name, floor_px in self._METRIC_MIN_WIDTH_BUTTONS.items():
            button = self.findChild(QPushButton, object_name)
            if button is None:
                continue
            button.ensurePolished()
            button.setMinimumWidth(max(floor_px, button.sizeHint().width()))

    def _build_search_registry(self) -> None:
        """Discover every setting row across all tabs by walking the widget
        tree the tab builders already constructed, instead of hand-listing
        one registration call at each of the ~60 _setting_row(...) call
        sites. Any future setting that uses _setting_row is covered
        automatically.

        Every _setting_row(...) call produces a distinctive shape: a
        QHBoxLayout whose first item is a QWidget (objectName
        "settingRowLabelColumn" -- hosting the label column in a real
        widget, not a bare addLayout(), is what gives its word-wrapped
        description a working height-for-width layout pass) whose OWN
        layout starts with a QLabel (the row label, optionally followed by
        a description QLabel), and whose remaining row items include the
        control widget. That shape is unique in this file, so walking for
        it can't pick up an unrelated QHBoxLayout (button rows, header
        rows, etc.) by accident.

        Two tabs -- Commands and Health -- build their content from
        QTableWidget-based editors rather than _setting_row, so they
        contribute zero rows here; _apply_search_filter() below leaves
        those tabs untouched by the filter rather than falsely marking
        them "no match".
        """
        self._search_rows = []
        for tab_index in range(self._stack.count()):
            tab_widget = self._stack.widget(tab_index)
            content = tab_widget.widget() if isinstance(tab_widget, QScrollArea) else tab_widget
            if content is None:
                continue
            for row in content.findChildren(QHBoxLayout):
                if row.count() < 2:
                    continue
                left_item = row.itemAt(0)
                left_widget = left_item.widget() if left_item is not None else None
                left_layout = left_widget.layout() if left_widget is not None else None
                if left_layout is None or left_layout.count() < 1:
                    continue
                label_item = left_layout.itemAt(0)
                label_widget = label_item.widget() if label_item is not None else None
                if not isinstance(label_widget, QLabel):
                    continue
                desc_item = left_layout.itemAt(1) if left_layout.count() > 1 else None
                desc_widget = desc_item.widget() if desc_item is not None else None
                if not isinstance(desc_widget, QLabel):
                    desc_widget = None
                # Responsive rows may place a stretch between the descriptive
                # column and the control. Find the first actual widget instead
                # of assuming the control is item 1.
                control_widget = None
                for item_index in range(1, row.count()):
                    control_item = row.itemAt(item_index)
                    candidate = (
                        control_item.widget() if control_item is not None else None
                    )
                    if candidate is not None:
                        control_widget = candidate
                        break
                if control_widget is None:
                    continue
                self._search_rows.append((
                    label_widget.text(),
                    desc_widget.text() if desc_widget is not None else "",
                    tab_index,
                    label_widget,
                    desc_widget,
                    control_widget,
                ))

    def _apply_search_filter(self) -> None:
        """Show/hide each registered row by case-insensitive substring match
        against its label (and description, since that's cheap to include
        too) and dim the sidebar entry for any tab left with zero matches.
        Empty query restores everything."""
        query = self._search_edit.text().strip().lower()

        if not query:
            for _label, _desc, _tab, label_w, desc_w, control_w in self._search_rows:
                label_w.setVisible(True)
                if desc_w is not None:
                    desc_w.setVisible(True)
                control_w.setVisible(True)
            self._set_sidebar_dimmed(set())
            return

        tabs_with_rows: set = set()
        matched_tabs: set = set()
        for label_text, desc_text, tab_index, label_w, desc_w, control_w in self._search_rows:
            tabs_with_rows.add(tab_index)
            match = query in label_text.lower() or query in desc_text.lower()
            label_w.setVisible(match)
            if desc_w is not None:
                desc_w.setVisible(match)
            control_w.setVisible(match)
            if match:
                matched_tabs.add(tab_index)

        self._set_sidebar_dimmed(tabs_with_rows - matched_tabs)

    def _set_sidebar_dimmed(self, no_match_stack_indices: set) -> None:
        """Dim -- not disable -- the sidebar entry for each tab in
        no_match_stack_indices. Chose dimming over QListWidgetItem
        disabling so every tab stays reachable even while filtered out:
        the label/description substring match is a hint, not ground
        truth (it won't catch a setting whose on-screen wording differs
        from what was typed), so locking navigation away from a tab could
        strand the user looking for something that's actually there."""
        dimmed = _css_color_to_qcolor(theme.TEXT_DISABLED)
        normal = QColor(theme.TEXT_PRIMARY)
        for row, stack_index in self._sidebar_row_to_stack_index.items():
            item = self._sidebar.item(row)
            if item is None:
                continue
            item.setForeground(dimmed if stack_index in no_match_stack_indices else normal)

    # ------------------------------------------------------------------
    # Tab builders
    # ------------------------------------------------------------------

    def _build_placeholder(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel("Coming soon — this tab is being migrated.")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(label)
        return w

    def show_tab(self, name: str) -> None:
        """Show the window on the named tab (a _TAB_NAMES entry)."""
        stack_index = _TAB_NAMES.index(name)
        for row, index in self._sidebar_row_to_stack_index.items():
            if index == stack_index:
                self._sidebar.setCurrentRow(row)
                break
        self._stack.setCurrentIndex(stack_index)
        self.show()
        self.raise_()
        self.activateWindow()

    # Amber "may conflict" style, shared by the Modes tab collision banner.
    @staticmethod
    def _collision_warn_style() -> str:
        """See _HotkeyButton._idle_qss(): built per call, not per import."""
        return (            f"color: {theme.WARNING}; font-size: {theme.TYPE_MIN}px; "
            f"background-color: {theme.tint(theme.WARNING, 0.07)}; "
            f"border: 1px solid {theme.tint(theme.WARNING, 0.2)}; "
            "border-radius: 6px; padding: 6px 10px;"
        )

    # ------------------------------------------------------------------
    # Commands tab helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Sounds tab helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # TTS tab
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Alarms tab
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Ava / Cloud tab slots and helpers
    # ------------------------------------------------------------------

    def _on_test_result(self, msg: str, color: str):
        label = self._widgets.get('cloud_test_status')
        if label:
            label.setText(msg)
            label.setStyleSheet(f"color: {color}; font-size: {theme.TYPE_MIN}px;")
        if msg.startswith("Connected to "):
            self._cloud_reachable = True
        elif msg.startswith(("Failed:", "Error:")):
            self._cloud_reachable = False
        self._refresh_sc_status()
        self._refresh_sc_cloud_hint()

    def apply_theme(self) -> None:
        """Refresh this live window without rebuilding unsaved controls."""
        self.setStyleSheet(stylesheet())
        for widget in self.findChildren(QWidget):
            inline = widget.styleSheet()
            if inline:
                widget.setStyleSheet(theme.retheme_stylesheet(inline))
        for row in range(self._sidebar.count()):
            item = self._sidebar.item(row)
            if row in self._sidebar_row_to_stack_index:
                item.setForeground(QColor(theme.TEXT_PRIMARY))
            else:
                item.setForeground(QColor(theme.TEXT_PRIMARY))
        self._apply_search_filter()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _section_title(self, text):
        label = QLabel(text)
        label.setStyleSheet(f"color: {theme.ACCENT}; font-size: {theme.TYPE_HEADING}px; font-weight: bold;")
        return label

    def _section_card(self, title: str, subtitle: str | None = None):
        # No per-card max-width: the cap lives once at each tab's shared
        # scroll-content container (_CONTENT_MAX_WIDTH). Expanding here lets
        # the card fill whatever width that container gives it, so every
        # card in a tab shares the same left/right edges regardless of its
        # own content's natural size.
        card = QFrame()
        card.setObjectName("settingsSectionCard")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        card.setStyleSheet(
            "QFrame#settingsSectionCard {"
            f" background-color: {theme.BG1};"
            f" border: 1px solid {theme.wash(0.12)};"
            " border-radius: 12px;"
            "}"
        )

        v = QVBoxLayout(card)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(12)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"color: {theme.ACCENT}; font-size: {theme.TYPE_HEADING}px; font-weight: 700;"
        )
        v.addWidget(title_label)

        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setWordWrap(True)
            subtitle_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_MIN}px;")
            v.addWidget(subtitle_label)
        return card, v

    def _setting_row(self, label, description, widget, control_width: int | None = 260):
        """Build a label+description row with a right-aligned control.

        control_width is a MINIMUM, not an exact width: widget gets
        setMinimumWidth(control_width) only (no maximum), with a
        Minimum/Fixed sizePolicy, so a control whose own content needs more
        room than the minimum (a QComboBox with long option text, a
        checkbox with a long trailing label) can grow to fit it instead of
        clipping. Every row still right-aligns its control within the row's
        own full width, so all controls in the same card share one right
        edge regardless of how wide any individual control's content makes
        it -- that alignment does not depend on the widths matching.
        """
        row = QHBoxLayout()
        row.setContentsMargins(0, 10, 0, 10)
        row.setSpacing(20)

        # The label column is hosted in a _HeightForWidthWidget rather than a
        # bare QVBoxLayout added via addLayout(). A word-wrapped QLabel's
        # height depends on its resolved width, and Qt's height-for-width
        # sizePolicy propagation does not reliably reach through this row's
        # full nesting depth (row -> card -> scroll content) even when every
        # level advertises hasHeightForWidth(True) -- see
        # _HeightForWidthWidget's docstring. It self-corrects its own
        # minimum height on resize instead, which works regardless of how
        # much nesting surrounds it.
        left_widget = _HeightForWidthWidget()
        left_widget.setObjectName("settingRowLabelColumn")
        left_widget.setStyleSheet(
            "QWidget#settingRowLabelColumn { background-color: transparent; }"
        )
        left = QVBoxLayout(left_widget)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(4)

        lbl = QLabel(label)
        lbl.setStyleSheet(f"font-weight: 600; font-size: {theme.TYPE_BODY}px; color: {theme.TEXT_PRIMARY};")
        left.addWidget(lbl)

        if description:
            desc = QLabel(description)
            desc.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
            desc.setWordWrap(True)
            left.addWidget(desc)

        # No addStretch() here: left_widget already carries the row's only
        # stretch factor (1), and the control widget below is fixed-width,
        # so an extra stretch item only competed with the label column for
        # the same leftover space without adding anything.
        row.addWidget(left_widget, 1)

        interactive_height = theme.HIT_TARGET_MIN
        if isinstance(widget, QCheckBox):
            widget.setMinimumHeight(theme.HIT_TARGET_MIN)
        elif isinstance(widget, (QPushButton, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QSlider)):
            widget.setMinimumHeight(interactive_height)
        else:
            widget.setMinimumHeight(interactive_height)

        if control_width is not None:
            width = max(theme.HIT_TARGET_MIN, 170, control_width)
            widget.setMinimumWidth(width)

        widget.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        row.addWidget(widget, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return row

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _apply_and_close(self):
        """Collect updates from each tab's registered save fn, in
        registration order (== tab-build order in __init__), merge into one
        dict, then a single locked config.update + save_config.

        Registration order preserves the merge semantics tabs depend on:
        Modes (command_mode mode/debounce/timeout/miss_limit/button/suppress_button,
        wake_word_config wake_command_timeout/quick_silence/oww_threshold,
        ava_command_session) is registered before Advanced (wake_word_config
        manual speech_threshold) -- Advanced's fn reads the accumulated
        `updates` dict to merge onto the Modes tab's partial write instead
        of clobbering it.
        """
        updates: dict = {}
        for save_fn in self._save_fns:
            updates.update(save_fn(updates))

        # Switch the live stream before the bulk config write. If the new ID
        # were written first, switch_microphone() would hit its same-ID guard
        # and ACE would continue recording from the old device.
        requested_mic = updates.get('microphone')
        current_mic = self.app.config.get('microphone')
        if 'microphone' in updates and requested_mic != current_mic:
            updates.pop('microphone', None)
            updates.pop('microphone_name', None)
            self.app.switch_microphone(requested_mic)


        requested_output = updates.get('output_device')
        requested_output_name = updates.get('output_device_name')
        current_output = (
            self.app.config.get('output_device'),
            self.app.config.get('output_device_name'),
        )
        if 'output_device' in updates and (
            requested_output, requested_output_name
        ) != current_output:
            self.app.switch_output_device(requested_output, requested_output_name)
        was_sc_enabled = bool(
            self.app.config.get('smart_corrections', {}).get('enabled', False)
        )

        with self.app._config_lock:
            self.app.config.update(updates)
            self.app.save_config()

        if 'ui' in updates:
            theme.set_theme(updates['ui'].get('theme'), refresh=True)

        if {
            'listening_indicator_enabled',
            'listening_indicator_position',
            'listening_indicator_custom_position',
        }.intersection(updates):
            apply_indicator = getattr(
                self.app, 'apply_listening_indicator_settings', None
            )
            if callable(apply_indicator):
                apply_indicator()

        # Mouse 4/5 bindings (main hotkey, command-mode button) live in one
        # Win32 hook; moving a binding between mouse and keyboard reinstalls it.
        if {'hotkey', 'command_mode'}.intersection(updates):
            refresh_mouse_hook = getattr(self.app, 'refresh_mouse_hook', None)
            if callable(refresh_mouse_hook):
                refresh_mouse_hook()

        now_sc_enabled = bool(
            self.app.config.get('smart_corrections', {}).get('enabled', False)
        )
        if now_sc_enabled and not was_sc_enabled:
            try:
                smart_corrections.warm_up(self.app)
            except Exception as e:
                logger.debug(f"[SETTINGS] Smart Corrections warm_up call failed: {e}")

        self.close()
