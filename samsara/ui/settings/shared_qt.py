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
    QAbstractButton, QAbstractSpinBox, QPlainTextEdit,
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
            f" font-size: {theme.TYPE_BODY}px;"
            " font-family: 'Consolas', 'Courier New', monospace;"
            f" font-weight: {theme.WEIGHT_SCALE['regular']};"
            " padding: 6px 14px;"
            "}"
            "QPushButton:hover {"
            f" background-color: {theme.BG1};"
            "}"
            "QPushButton:focus {"
            f" border: 2px solid {theme.ACCENT};"
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
            f" font-size: {theme.TYPE_BODY}px;"
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
            f"font-size: {theme.TYPE_BODY}px;",
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
    margin: 5px 0;
    padding: 0 20px;
    min-height: {theme.TYPE_BODY + 19}px;
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
    /* TYPE_BODY + 8 content + 8 padding + 2 border + 10 margin = 44 DIP. */
    margin: 5px 0;
    background-color: ${{BG2}};
    border: 1px solid {theme.wash(0.14)};
    border-radius: 6px;
    padding: 4px 12px;
    color: ${{TEXT_PRIMARY}};
    min-width: 200px;
    min-height: {theme.TYPE_BODY + 8}px;
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
    margin: 5px 0;
    color: ${{TEXT_PRIMARY}};
    spacing: 8px;
    /* Same cascade cause as the QLabel fix above (9b7f00f): QCheckBox is a
       QWidget with no background-color of its own, so it otherwise picks
       up the QMainWindow, QWidget rule's BG0 as an opaque bar across
       the row -- the ::indicator sub-control below already paints its own
       background correctly and is untouched. */
    background-color: transparent;
    min-height: {theme.TYPE_BODY + 19}px;
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
    margin: 5px 0;
    background-color: ${{ACCENT}};
    color: ${{TEXT_ON_ACCENT}};
    border: none;
    border-radius: 6px;
    padding: 6px 24px;
    font-weight: 600;
    font-size: {theme.TYPE_BODY}px;
    min-height: {theme.TYPE_BODY + 8}px;
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
    margin: 5px 0;
    background-color: ${{BG2}};
    border: 1px solid {theme.wash(0.14)};
    border-radius: 6px;
    padding: 4px 10px;
    color: ${{TEXT_PRIMARY}};
    min-width: 80px;
    min-height: {theme.TYPE_BODY + 8}px;
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
    margin: 5px 0;
    background-color: ${{BG2}};
    border: 1px solid {theme.wash(0.14)};
    border-radius: 6px;
    padding: 4px 12px;
    color: ${{TEXT_PRIMARY}};
    font-size: {theme.TYPE_BODY}px;
    min-height: {theme.TYPE_BODY + 8}px;
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


# Shared imports retained for the page control factories.
__all__ = [name for name in globals() if not name.startswith('__')]
