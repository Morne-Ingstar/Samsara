"""Home page of the Samsara hub window (queue 26).

Samsara controls the computer; dictation is one thing it does. Home makes
that obvious at a glance, top to bottom:

    1. STATE BLOCK    the live mark beside three facts read from RUNTIME
                      state (never from config alone): what is listening,
                      which lane, where the next utterance goes; one
                      instruction line generated from config and the
                      catalog; a Stop listening button on the tray's path.
    2. LAST ACTION    the newest entry of DictationApp._outcome_ring (the
                      ring _show_outcome_chip appends to), rendered by
                      kind, with only the actions that exist today.
    3. WHAT YOU CAN DO five cards from the live command catalog.
    4. IDENTITY STRIP words today, the infinity card, the creed.

Every actionable element is a QPushButton whose visible text equals its
accessible name (WCAG Label in Name), at least 44 px tall, in top-to-bottom
tab order. Nothing is hover-only.

Runtime sources (the "what will happen when I speak" facts):
    listening   DictationApp.recording / snoozed / wake_word_active /
                continuous_active / command_mode_active / toggle_active --
                the same flags _tray_mark reads (dictation.py) -- plus
                available_mics vs config['microphone'] for mic presence.
    lane        ava_command_session_active / ava_mode_active /
                command_mode_active / wake_word_active.
    target      SessionModeManager._dictate_target_hwnd (session_modes.py),
                resolved to the window title; else "any textbox".
"""
from __future__ import annotations

import datetime as _dt
from typing import Callable, Optional

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from samsara import command_catalog, config_defaults
from samsara.log import get_logger
from samsara.session_modes import CHIP_CHECK, CHIP_CROSS
from samsara.ui import theme
from samsara.ui.command_cheatsheet_qt import UNAVAILABLE_TEXT
from samsara.ui.tray_qt import MarkFrame, paint_mark

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Layout constants: one column, 16/24 grid, cards on BG1
# ---------------------------------------------------------------------------

CONTENT_MAX_W = 760
GRID = 16
GRID_LG = 24
STATE_MARK_PX = 60
MIN_TARGET = 44
OUTCOME_RING_MAX = 8
BASE_POINT_SIZE = 9.0   # Segoe UI 9 pt is Windows' 100% text size

CAPABILITY_TITLES = (
    "Control windows",
    "Run it hands-free",
    "Dictate anywhere",
    "Ask Ava",
    "Teach it your words",
    "Guides & help",
)
#: The guidance hub behind the "Guides & help" card (40): visible label ->
#: (app method or settings tab). Each is an existing surface.
GUIDES = (
    ("Command reference", "cheatsheet", None),
    ("Quick reference", "app", "open_quick_reference"),
    ("Tutorial", "app", "show_tutorial"),
    ("Help & support", "settings", "Help & Support"),
)
NOTHING_TO_CORRECT = "Nothing to correct yet"
ANY_TEXTBOX = "any textbox"
STOP_LISTENING = "Stop listening"
STOPPING = "Stopping"
LISTENING_OFF = "Listening is off"
PAUSE_HANDS_FREE = "Pause hands-free"
RESUME_HANDS_FREE = "Resume hands-free"
# How soon the page re-reads runtime state after a control is pressed, so
# the mark, the state line and the button itself change within 200 ms.
FEEDBACK_MS = 150

# One icon vocabulary for the hub's nav rows and Home's capability cards
# (38): plain glyphs from Segoe UI Symbol, built with chr() so this file
# stays ASCII, painted in the theme's text tokens by glyph_icon().
ICON_GLYPHS = {
    "home": chr(0x2302),        # house
    "history": chr(0x21BA),     # anticlockwise arrow
    "dictionary": chr(0x2261),  # three lines
    "settings": chr(0x2699),    # gear
    "windows": chr(0x25A3),     # square in square
    "hands_free": chr(0x25C9),  # fisheye
    "dictate": chr(0x270E),     # pencil
    "ava": chr(0x2726),         # four-pointed star
    "words": chr(0x2261),       # three lines (same as dictionary)
    "guides": chr(0x2139),      # information source
}
CARD_GLYPHS = ("windows", "hands_free", "dictate", "ava", "words", "guides")
WORDS_TODAY = "words today"
WORDS_REMAINING = "words remaining"
INFINITY = chr(0x221E)
# Glyphs via chr() so this file stays pure ASCII (non-ASCII literals have
# caused encoding trouble on this machine; see session_modes.py).
MIDDLE_DOT = chr(0xB7)
EM_DASH = chr(0x2014)
CREED = f"Free {MIDDLE_DOT} Open source {MIDDLE_DOT} Accessibility first"
# Interpunct-free form for callers that cannot show the middle dot.
CREED_ASCII = "Free - Open source - Accessibility first"
_CATALOG_UNAVAILABLE_SHORT = "command list unavailable"


def glyph_icon(key: str, colour: str, px: int = 18) -> QIcon:
    """A QIcon of one ICON_GLYPHS entry painted in `colour` (a theme token),
    so a button keeps its text as its accessible name and still shows an
    icon. Renders at 2x for crisp scaling."""
    glyph = ICON_GLYPHS.get(key, "")
    size = px * 2
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    font = QFont("Segoe UI Symbol")
    font.setPixelSize(int(size * 1.0))
    painter.setFont(font)
    painter.setPen(QColor(colour))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    pixmap.setDevicePixelRatio(2.0)
    return QIcon(pixmap)


def text_scale() -> float:
    """How much larger the user's text is than the Windows default (Settings
    > Accessibility > Text size changes QApplication's default font). Every
    px size on this page is multiplied by it, so a 20% larger system font
    gives 20% larger text here instead of clipping."""
    app = QApplication.instance()
    if app is None:
        return 1.0
    size = app.font().pointSizeF()
    if size <= 0:
        return 1.0
    return max(0.5, size / BASE_POINT_SIZE)


def _px(n: float) -> int:
    return max(1, round(n * text_scale()))


# ---------------------------------------------------------------------------
# Runtime state (pure functions over the app object -- tested with mocks)
# ---------------------------------------------------------------------------

def _cfg(app) -> dict:
    cfg = getattr(app, 'config', None)
    return cfg if isinstance(cfg, dict) else {}


def _cfg_get(cfg: dict, key: str):
    try:
        return config_defaults.cfg_get(cfg, key)
    except KeyError:
        node = cfg
        for part in key.split('.'):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node


def listening_state(app) -> tuple:
    """(text, live). `live` is True while something is capturing or armed
    right now -- the condition under which Stop listening does anything."""
    if getattr(app, 'recording', False):
        return ("Recording", True)
    if getattr(app, 'snoozed', False):
        return ("Snoozed", False)
    if getattr(app, 'wake_word_active', False):
        return ("Wake word armed", True)
    if getattr(app, 'continuous_active', False):
        return ("Continuous", True)
    if (getattr(app, 'command_mode_active', False)
            or getattr(app, 'ava_command_session_active', False)
            or getattr(app, 'toggle_active', False)):
        return ("Listening", True)
    mode = str(_cfg_get(_cfg(app), 'mode') or 'hold')
    if mode == 'hold':
        return ("Hold key", False)
    if mode == 'toggle':
        return ("Toggle key", False)
    return ("Off", False)


def capturing(app) -> bool:
    """True while something is actually capturing speech: a hold/toggle
    recording, continuous mode, or a hands-free (command / Ava) session. An
    armed wake listener is ambient, not a capture."""
    return bool(getattr(app, 'recording', False)
                or getattr(app, 'continuous_active', False)
                or getattr(app, 'toggle_active', False)
                or getattr(app, 'command_mode_active', False)
                or getattr(app, 'ava_command_session_active', False))


def mic_present(app) -> bool:
    """False when the configured microphone is not among the devices the app
    last enumerated (available_mics), or when it enumerated none. An app
    that never enumerated (no attribute) makes no claim -> True."""
    mics = getattr(app, 'available_mics', None)
    if mics is None:
        return True
    ids = {m.get('id') for m in mics if isinstance(m, dict)}
    mic_id = _cfg(app).get('microphone')
    if mic_id is None:
        return bool(ids)
    return mic_id in ids


def lane_text(app) -> str:
    if (getattr(app, 'ava_command_session_active', False)
            or getattr(app, 'ava_mode_active', False)
            or getattr(app, 'command_mode_active', False)):
        return "hands-free session"
    if getattr(app, 'wake_word_active', False):
        return "command"
    return "dictate"


def _window_title(hwnd) -> str:
    """Title of a top-level window handle ('' when unreadable). Module-level
    so tests can substitute it."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(int(hwnd))
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(int(hwnd), buf, length + 1)
        return buf.value
    except Exception as exc:
        logger.debug(f"_window_title: {exc}")
        return ""


def target_text(app) -> str:
    """Where the next utterance goes: the focus-locked window's title while
    a hands-free session holds one, else "any textbox"."""
    session = (getattr(app, 'command_mode_active', False)
               or getattr(app, 'ava_command_session_active', False))
    manager = getattr(app, '_session_mode_manager', None)
    hwnd = getattr(manager, '_dictate_target_hwnd', None)
    if session and hwnd:
        title = _window_title(hwnd)
        if title:
            return title
    return ANY_TEXTBOX


def hotkey_label(hotkey) -> str:
    """'ctrl+shift' -> 'Ctrl+Shift'."""
    parts = [p.strip() for p in str(hotkey or '').split('+') if p.strip()]
    return "+".join(p[:1].upper() + p[1:] for p in parts)


def help_phrase(records) -> Optional[str]:
    """The catalog's spoken help command ("what can i say"), or None when
    the catalog is absent or has no such command. Never hardcoded."""
    for record in records or []:
        cid = str(record.get('canonical_id', ''))
        if cid.endswith('.what_can_i_say'):
            return command_catalog.canonical_phrase(record)
    for record in records or []:
        aliases = [str(a) for a in record.get('aliases', [])]
        if any('quick reference' in a for a in aliases):
            return command_catalog.canonical_phrase(record)
    return None


def instruction_line(cfg: dict, help_cmd: Optional[str]) -> str:
    """One line generated from live config: the hotkey, the wake word when
    enabled, and the catalog's help phrase when the catalog has one."""
    mode = str(_cfg_get(cfg, 'mode') or 'hold')
    hotkey = hotkey_label(_cfg_get(cfg, 'hotkey') or config_defaults.DEFAULTS['hotkey'])
    if mode == 'hold':
        parts = [f"Hold {hotkey} and speak."]
    elif mode == 'toggle':
        parts = [f"Press {hotkey} to start and again to stop."]
    else:
        parts = ["Speak any time."]
    if _cfg_get(cfg, 'wake_word_enabled'):
        phrase = _cfg_get(cfg, 'wake_word_config.phrase')
        if phrase:
            parts.append(f"Say {phrase} for hands-free.")
    if help_cmd:
        parts.append(f"Say {help_cmd} for commands.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Outcomes (the ring _show_outcome_chip appends to)
# ---------------------------------------------------------------------------

def outcome_ring(app) -> list:
    ring = getattr(app, '_outcome_ring', None)
    if not ring:
        return []
    return list(ring)[-OUTCOME_RING_MAX:]


def classify_outcome(label: str, kind: str) -> str:
    """'typed' | 'miss' | 'command' | 'other'."""
    label = str(label or '')
    if label == "MISS":
        return "miss"
    if label == "typed" and kind == "success":
        return "typed"
    if label.startswith(CHIP_CHECK):
        return "command"
    return "other"


def outcome_reason(label: str, kind: str) -> Optional[str]:
    """The reason an outcome carries in its label, or None. MISS carries
    none; 'refused: why' / 'cancelled: why' and error chips do."""
    label = str(label or '')
    if label == "MISS":
        return None
    for prefix in ("refused: ", "cancelled: "):
        if label.startswith(prefix):
            return label[len(prefix):]
    if kind == "error" and label.startswith(CHIP_CROSS + " "):
        return label[len(CHIP_CROSS) + 1:]
    return None


def last_typed_text(app) -> str:
    """The most recent typed text: the history store's newest dictation
    entry (what the correction window pre-fills with too)."""
    store = getattr(app, 'history_store', None)
    if store is None:
        return ""
    try:
        rows = store.query(type_filter='dictation', limit=1)
    except Exception as exc:
        logger.debug(f"last_typed_text: {exc}")
        return ""
    if not rows:
        return ""
    try:
        return str(rows[0]['display_text'] or '')
    except (KeyError, IndexError, TypeError):
        return ""


def render_outcome(app, outcome) -> str:
    """The card's one line for an outcome tuple (label, kind, ts)."""
    label, kind = str(outcome[0]), str(outcome[1])
    cls = classify_outcome(label, kind)
    if cls == "typed":
        text = last_typed_text(app).replace('\n', ' ').strip()
        return text or label
    if cls == "miss":
        heard = str(getattr(app, '_last_miss_text', '') or '').strip()
        return f"{CHIP_CROSS} {heard or label}"
    return label


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def catalog_records(app) -> Optional[list]:
    """Live catalog records the way the cheat sheet builds them (registry
    rows first, commands_catalog.json second), or None when unavailable."""
    rows = None
    matcher = getattr(getattr(app, 'command_executor', None), '_matcher', None)
    if matcher is not None and hasattr(matcher, 'list_commands'):
        try:
            rows = matcher.list_commands()
        except Exception as exc:
            logger.debug(f"catalog_records: {exc}")
            rows = None
    try:
        return command_catalog.guidance_catalog(rows)
    except Exception as exc:
        logger.warning(f"[HOME] command catalog unavailable: {exc}")
        return None


def matching_records(records, needle: str) -> list:
    """The records the cheat sheet's text filter would show for `needle`."""
    needle = needle.strip().lower()
    out = []
    for r in records or []:
        phrase = command_catalog.canonical_phrase(r).lower()
        aliases = [str(a).lower() for a in r.get('aliases', [])]
        if needle in phrase or any(needle in a for a in aliases):
            out.append(r)
    return out


def catalog_example(records) -> Optional[str]:
    picks = command_catalog.pick_examples(records, 1)
    return command_catalog.canonical_phrase(picks[0]) if picks else None


def _user_word_count() -> Optional[int]:
    try:
        from samsara import phonetic_wash
        return len(phonetic_wash.get_user_corrections() or {})
    except Exception as exc:
        logger.debug(f"_user_word_count: {exc}")
        return None


def ava_text(cfg: dict) -> str:
    local = bool(_cfg_get(cfg, 'ollama.enabled'))
    cloud = bool(_cfg_get(cfg, 'cloud_llm.enabled'))
    if local and cloud:
        return "local or your key"
    if local:
        return "local"
    if cloud:
        return "your key"
    return "off"


def capability_cards(app, records) -> list:
    """The five cards, in order. Each: {title, value, action, arg}.
    action is 'cheatsheet' (arg = filter text), 'settings' (arg = tab name)
    or 'page' (arg = hub page name)."""
    cfg = _cfg(app)
    if records is None:
        windows_value = _CATALOG_UNAVAILABLE_SHORT
    else:
        n = len(matching_records(records, "window"))
        windows_value = f"{n} commands"
    phrase = _cfg_get(cfg, 'wake_word_config.phrase') or config_defaults.DEFAULTS['wake_word_config.phrase']
    mode = str(_cfg_get(cfg, 'mode') or 'hold')
    hotkey = hotkey_label(_cfg_get(cfg, 'hotkey') or config_defaults.DEFAULTS['hotkey'])
    if mode == 'hold':
        dictate_value = f"hold {hotkey}"
    elif mode == 'toggle':
        dictate_value = f"press {hotkey}"
    else:
        dictate_value = "always on"
    words = _user_word_count()
    return [
        {"title": CAPABILITY_TITLES[0], "value": windows_value,
         "action": "cheatsheet", "arg": "window"},
        {"title": CAPABILITY_TITLES[1], "value": f"say {phrase}",
         "action": "settings", "arg": "Modes"},
        {"title": CAPABILITY_TITLES[2], "value": dictate_value,
         "action": "settings", "arg": "Modes"},
        {"title": CAPABILITY_TITLES[3], "value": ava_text(cfg),
         "action": "settings", "arg": "Ava / Cloud"},
        {"title": CAPABILITY_TITLES[4],
         "value": f"{words} words" if words is not None else "your dictionary",
         "action": "page", "arg": "Dictionary"},
        {"title": CAPABILITY_TITLES[5], "value": f"{len(GUIDES)} guides",
         "action": "guides", "arg": None},
    ]


# ---------------------------------------------------------------------------
# Words today
# ---------------------------------------------------------------------------

def local_midnight_iso(now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def words_today(app, now: Optional[_dt.datetime] = None) -> int:
    store = getattr(app, 'history_store', None)
    if store is None or not hasattr(store, 'words_typed_since'):
        return 0
    try:
        return int(store.words_typed_since(local_midnight_iso(now)))
    except Exception as exc:
        logger.debug(f"words_today: {exc}")
        return 0


# ---------------------------------------------------------------------------
# Actions on the app (each one an existing path)
# ---------------------------------------------------------------------------

def stop_capture(app) -> Optional[str]:
    """Stop the CURRENT capture through the app's own per-lane stop path
    (38): the one the hotkey release / session end use, never the snooze.
    Returns the name of the method called, or None when nothing was
    capturing. Order matters: a hands-free session owns its recording."""
    for flag, method in (
        ('ava_command_session_active', 'exit_ava_command_session'),
        ('command_mode_active', 'exit_command_mode'),
        ('continuous_active', 'stop_continuous_mode'),
        ('recording', 'stop_recording'),
        ('toggle_active', 'stop_recording'),
    ):
        if getattr(app, flag, False):
            fn = getattr(app, method, None)
            if not callable(fn):
                continue
            try:
                fn()
            except Exception as exc:
                logger.warning(f"[HOME] {method} failed: {exc}")
                return None
            return method
    return None


def pause_hands_free(app) -> bool:
    """The explicit pause: the tray's snooze until resumed. Only while the
    wake listener is armed and not already paused."""
    if getattr(app, 'snoozed', False) or not getattr(app, 'wake_word_active', False):
        return False
    fn = getattr(app, 'snooze_listening', None)
    if not callable(fn):
        return False
    try:
        fn(None)
    except Exception as exc:
        logger.warning(f"[HOME] pause hands-free failed: {exc}")
        return False
    return True


def resume_hands_free(app) -> bool:
    """Undo the pause: the app's resume_listening."""
    if not getattr(app, 'snoozed', False):
        return False
    fn = getattr(app, 'resume_listening', None)
    if not callable(fn):
        return False
    try:
        fn()
    except Exception as exc:
        logger.warning(f"[HOME] resume hands-free failed: {exc}")
        return False
    return True


def stop_listening(app) -> bool:
    """Kept for callers of the old name: stops the current capture. It
    never snoozes (that was the wiring the owner found dead-ended)."""
    return stop_capture(app) is not None


def _post_after(fn) -> None:
    """Run fn on the Qt thread AFTER anything already posted (qt_runtime's
    posts are FIFO on one thread), so a callback posted after a window's
    own init post sees the built window. Falls back to a direct call
    outside the runtime."""
    try:
        from samsara.ui import qt_runtime  # noqa: PLC0415
        qt_runtime.post(fn)
    except Exception:  # noqa: BLE001
        fn()


def open_cheatsheet_filtered(app, needle: str) -> bool:
    """Show the command reference with its text filter set to `needle` and
    the category on All (the sheet's own filter path). The sheet's window
    is read when the posted callback RUNS, not when this is called: on the
    first open the wrapper has no window yet (its init is itself a post),
    which is why the filter never applied on a first press (40)."""
    sheet = getattr(app, 'cheat_sheet', None)
    if sheet is None:
        logger.error("[HOME] Command reference: the app has no cheat_sheet")
        return False
    try:
        sheet.show()
    except Exception as exc:
        logger.warning(f"[HOME] Command reference show failed: {exc}")
        return False

    def _apply():
        window = getattr(sheet, '_window', None)
        if window is None:
            logger.error("[HOME] Command reference: window not built after its init post")
            return
        try:
            window._set_category("All")
            window._filter.setText(needle)
        except Exception as exc:
            logger.warning(f"[HOME] Command reference filter failed: {exc}")

    _post_after(_apply)
    return True


def settings_tab_ids() -> list:
    """The settings page registry the hub links into (the split window's
    _TAB_NAMES); empty when the settings module cannot be imported."""
    try:
        from samsara.ui.settings_qt import _TAB_NAMES  # noqa: PLC0415
        return list(_TAB_NAMES)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[HOME] settings page registry unavailable: {exc}")
        return []


def open_settings_tab(app, tab: str) -> bool:
    """Open Settings ON `tab`. The tab id is asserted against the page
    registry at call time (a missing id is logged loudly and nothing
    opens), and the page is selected in a callback posted AFTER the
    window's own init post, reading the window when it runs. Before (40),
    the window was read at call time: None on the first open, so Settings
    landed on page one -- the owner's "Ask Ava opens Settings at the top".
    """
    ids = settings_tab_ids()
    if tab not in ids:
        logger.error("[HOME] Settings page %r is not in the registry %r; not opening", tab, ids)
        return False
    fn = getattr(app, 'open_settings', None)
    if not callable(fn):
        logger.error("[HOME] the app has no open_settings")
        return False
    try:
        fn()
    except Exception as exc:
        logger.warning(f"[HOME] open_settings failed: {exc}")
        return False

    def _select():
        window = getattr(getattr(app, '_settings_qt', None), '_window', None)
        if window is None or not hasattr(window, 'show_tab'):
            logger.error("[HOME] Settings window not built after its init post; page %r not selected", tab)
            return
        try:
            window.show_tab(tab)
        except Exception as exc:
            logger.error(f"[HOME] Settings page {tab!r} could not be selected: {exc}")

    _post_after(_select)
    return True


def open_guide(app, label: str) -> bool:
    """Open one GUIDES entry by its visible label."""
    for name, kind, target in GUIDES:
        if name != label:
            continue
        if kind == "cheatsheet":
            return open_cheatsheet_filtered(app, "")
        if kind == "settings":
            return open_settings_tab(app, target)
        fn = getattr(app, target, None)
        if not callable(fn):
            logger.error(f"[HOME] guide {label!r}: the app has no {target}")
            return False
        try:
            fn()
        except Exception as exc:
            logger.warning(f"[HOME] guide {label!r} failed: {exc}")
            return False
        return True
    logger.error(f"[HOME] unknown guide {label!r}")
    return False


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

def _label(text: str, *, size: int, color: str = theme.TEXT_PRIMARY,
           weight: int = 400, wrap: bool = False, family: Optional[str] = None,
           spacing: Optional[str] = None) -> QLabel:
    lbl = QLabel(text)
    css = (f"color: {color}; font-size: {_px(size)}px; font-weight: {weight};"
           " background: transparent; border: none;")
    if family:
        css += f" font-family: {family};"
    if spacing:
        css += f" letter-spacing: {spacing};"
    lbl.setStyleSheet(css)
    lbl.setWordWrap(wrap)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    return lbl


def _button(text: str) -> QPushButton:
    """A visible-label button: accessible name == text, 44 px target."""
    btn = QPushButton(text)
    btn.setAccessibleName(text)
    btn.setMinimumHeight(MIN_TARGET)
    btn.setMinimumWidth(MIN_TARGET)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    btn.setStyleSheet(
        f"QPushButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY};"
        f" border: 1px solid {theme.BORDER}; border-radius: 6px;"
        f" padding: {_px(8)}px {_px(14)}px; font-size: {_px(13)}px; }}"
        f"QPushButton:hover {{ border-color: {theme.ACCENT}; }}"
        f"QPushButton:focus {{ border: 2px solid {theme.ACCENT}; }}"
        f"QPushButton:disabled {{ color: {theme.TEXT_DISABLED}; border-color: {theme.BORDER_FAINT}; }}"
    )
    return btn


def _card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("homeCard")
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    return frame


def _fact_row(name: str) -> tuple:
    """A 'NAME  value' row. Returns (row widget, value label)."""
    row = QWidget()
    row.setStyleSheet("background: transparent;")
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(_px(10))
    key = _label(name.upper(), size=11, color=theme.TEXT_SECONDARY, weight=700, spacing="0.06em")
    key.setMinimumWidth(_px(150))
    value = _label("", size=14, weight=500, wrap=True)
    value.setAccessibleName(name)
    lay.addWidget(key, alignment=Qt.AlignmentFlag.AlignTop)
    lay.addWidget(value, stretch=1)
    return row, value


class _StateMark(QWidget):
    """The mark at 60 px, fed the very same MarkFrame the header mark shows
    (set_frame is called by the owner whenever that frame changes)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame = MarkFrame("idle", "off")
        self.setFixedSize(STATE_MARK_PX, STATE_MARK_PX)
        self.setStyleSheet("background: transparent;")
        self.setAccessibleName("Samsara mark")

    @property
    def frame(self):
        return self._frame

    def set_frame(self, frame):
        if not isinstance(frame, MarkFrame):
            frame = MarkFrame("idle", "off")
        if frame != self._frame:
            self._frame = frame
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        f = self._frame
        # Brand presentation (38): ACCENT at rest with the eye present,
        # the tray's idle grey and eyeless ring retired on this surface.
        paint_mark(painter, QRectF(0, 0, self.width(), self.height()),
                   f.capture, f.eye, f.rotation, f.opacity, brand=True)
        painter.end()


class _CapabilityCard(QPushButton):
    """A card that IS the button: its text is the title (so the accessible
    name equals the visible label); the live value sits below it."""

    def __init__(self, title: str, value: str, parent=None, glyph: Optional[str] = None):
        super().__init__(title, parent)
        self.setAccessibleName(title)
        if glyph:
            self.setIcon(glyph_icon(glyph, theme.ACCENT))
            self.setIconSize(QSize(_px(18), _px(18)))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setMinimumHeight(max(MIN_TARGET, _px(84)))
        self.setStyleSheet(
            f"QPushButton {{ background: {theme.BG1}; color: {theme.TEXT_PRIMARY};"
            f" border: 1px solid {theme.BORDER}; border-radius: 8px; text-align: left;"
            f" padding: {_px(12)}px {_px(14)}px {_px(36)}px {_px(14)}px;"
            f" font-size: {_px(13)}px; font-weight: 600; }}"
            f"QPushButton:hover {{ border-color: {theme.ACCENT}; }}"
            f"QPushButton:focus {{ border: 2px solid {theme.ACCENT}; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(_px(14), _px(12), _px(14), _px(12))
        lay.addStretch()
        self._value = _label(value, size=13, color=theme.ACCENT, weight=500)
        self._value.setAccessibleName(f"{title} value")
        lay.addWidget(self._value, alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        # The grid may not squeeze the card below what its title and value need.
        self.setMinimumWidth(self.sizeHint().width())

    @property
    def value_text(self) -> str:
        return self._value.text()

    def set_value(self, value: str):
        self._value.setText(value)
        self.setMinimumWidth(self.sizeHint().width())


class HomePage(QWidget):
    """The hub's landing page. `open_page(name)` is the hub's own navigation
    (used by the "Teach it your words" card to show Dictionary)."""

    def __init__(self, app, open_page: Optional[Callable[[str], None]] = None,
                 parent=None):
        super().__init__(parent)
        self._app = app
        self._open_page = open_page or (lambda name: None)
        self._records = catalog_records(app)
        self._help = help_phrase(self._records)
        self._last_outcome = None
        self._build()
        self.refresh()

    # ---- Build ----------------------------------------------------------

    def _build(self):
        self.setStyleSheet(
            f"QFrame#homeCard {{ background-color: {theme.BG1};"
            f" border: 1px solid {theme.BORDER}; border-radius: 8px; }}"
            f"QFrame#homeDivider {{ background-color: {theme.BORDER_FAINT};"
            f" border: none; max-height: 1px; min-height: 1px; }}"
            f"QScrollArea {{ border: none; background: transparent; }}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self._scroll)

        host = QWidget()
        host.setStyleSheet("background: transparent;")
        host_lay = QHBoxLayout(host)
        host_lay.setContentsMargins(GRID_LG, GRID_LG, GRID_LG, GRID_LG)
        host_lay.setSpacing(0)
        self._content = QWidget()
        self._content.setObjectName("homeContent")
        self._content.setStyleSheet("background: transparent;")
        self._content.setMaximumWidth(CONTENT_MAX_W)
        self._content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        host_lay.addStretch(0)
        host_lay.addWidget(self._content, stretch=1)
        host_lay.addStretch(0)
        self._scroll.setWidget(host)

        col = QVBoxLayout(self._content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(GRID_LG)

        col.addWidget(self._build_state_block())
        col.addWidget(self._build_last_action())
        col.addWidget(self._build_capabilities())
        col.addWidget(self._build_identity_strip())
        col.addStretch(1)

        # Tab order: top to bottom, in the order the widgets were built.
        chain = [self._stop_btn, self._pause_btn, self._undo_btn, self._teach_btn, self._why_btn,
                 *self._cards, *self._guide_btns.values()]
        for a, b in zip(chain, chain[1:]):
            QWidget.setTabOrder(a, b)
        self._tab_chain = chain

    def _build_state_block(self) -> QWidget:
        card = _card()
        card.setAccessibleName("State")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(GRID_LG, GRID_LG, GRID_LG, GRID_LG)
        lay.setSpacing(GRID)

        top = QHBoxLayout()
        top.setSpacing(GRID_LG)
        self._mark = _StateMark()
        top.addWidget(self._mark, alignment=Qt.AlignmentFlag.AlignTop)

        facts = QVBoxLayout()
        facts.setSpacing(_px(8))
        row, self._listening_val = _fact_row("Listening")
        facts.addWidget(row)
        row, self._lane_val = _fact_row("Lane")
        facts.addWidget(row)
        row, self._target_val = _fact_row("Next utterance goes to")
        facts.addWidget(row)
        top.addLayout(facts, stretch=1)
        lay.addLayout(top)

        self._instruction = _label("", size=13, color=theme.TEXT_SECONDARY, wrap=True)
        self._instruction.setAccessibleName("Instruction")
        lay.addWidget(self._instruction)

        # Rotating line slot: the owner fills it later. Hidden by default.
        self._tagline = _label("", size=13, color=theme.TEXT_SECONDARY, wrap=True)
        self._tagline.setObjectName("home.tagline")
        self._tagline.setAccessibleName("home.tagline")
        self._tagline.setVisible(False)
        lay.addWidget(self._tagline)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(GRID)
        # Stops the CURRENT capture (38). Disabled, never dead, when nothing
        # is capturing: the state line beside it says what is going on.
        self._stop_btn = _button(STOP_LISTENING)
        self._stop_btn.clicked.connect(self._on_stop)
        btn_row.addWidget(self._stop_btn)
        # The explicit pause is its own control; while paused it reads
        # "Resume hands-free" so the state is always visible and exitable.
        self._pause_btn = _button(PAUSE_HANDS_FREE)
        self._pause_btn.clicked.connect(self._on_pause)
        btn_row.addWidget(self._pause_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)
        return card

    def _build_last_action(self) -> QWidget:
        card = _card()
        card.setAccessibleName("Last action")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(GRID_LG, GRID_LG, GRID_LG, GRID_LG)
        lay.setSpacing(GRID)
        lay.addWidget(_label("LAST ACTION", size=11, color=theme.TEXT_SECONDARY,
                             weight=700, spacing="0.06em"))
        self._action_text = _label("", size=15, weight=500)
        self._action_text.setAccessibleName("Last action text")
        self._action_text.setProperty("elided", True)
        self._action_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self._action_text)
        self._reason = _label("", size=13, color=theme.TEXT_SECONDARY, wrap=True)
        self._reason.setAccessibleName("Reason")
        self._reason.setVisible(False)
        lay.addWidget(self._reason)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(GRID)
        self._undo_btn = _button("Undo")
        self._undo_btn.clicked.connect(self._on_undo)
        self._teach_btn = _button("Teach a word")
        self._teach_btn.clicked.connect(self._on_teach)
        self._why_btn = _button("Why?")
        self._why_btn.clicked.connect(self._on_why)
        for b in (self._undo_btn, self._teach_btn, self._why_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)
        # Why "Teach a word" is disabled, when it is: a visible reason on its
        # own line, not a window that opens on nothing (40).
        self._teach_note = _label("", size=12, color=theme.TEXT_SECONDARY, wrap=True)
        self._teach_note.setAccessibleName("Teach a word note")
        self._teach_note.setVisible(False)
        lay.addWidget(self._teach_note)
        return card

    def _build_capabilities(self) -> QWidget:
        box = QWidget()
        box.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(GRID)
        lay.addWidget(_label("WHAT YOU CAN DO", size=11, color=theme.TEXT_SECONDARY,
                             weight=700, spacing="0.06em"))
        self._catalog_note = _label(UNAVAILABLE_TEXT, size=12, color=theme.TEXT_SECONDARY, wrap=True)
        self._catalog_note.setTextFormat(Qt.TextFormat.RichText)
        self._catalog_note.setOpenExternalLinks(True)
        self._catalog_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._catalog_note.setAccessibleName("Command list unavailable")
        self._catalog_note.setVisible(self._records is None)
        lay.addWidget(self._catalog_note)

        self._cards_grid = QGridLayout()
        self._cards_grid.setHorizontalSpacing(GRID)
        self._cards_grid.setVerticalSpacing(GRID)
        self._cards = []
        for spec, glyph in zip(capability_cards(self._app, self._records), CARD_GLYPHS):
            card = _CapabilityCard(spec["title"], spec["value"], glyph=glyph)
            card.clicked.connect(lambda _=False, s=spec: self._on_card(s))
            self._cards.append(card)
        self._cards_cols = 0
        self._relayout_cards(CONTENT_MAX_W)
        lay.addLayout(self._cards_grid)

        # Guides & help (40): the row beneath the cards, opened by the card,
        # one button per guidance surface. Visible labels are the accessible
        # names; hidden until the card is pressed.
        self._guides_row = QWidget()
        self._guides_row.setStyleSheet("background: transparent;")
        self._guides_row.setAccessibleName("Guides")
        glay = QHBoxLayout(self._guides_row)
        glay.setContentsMargins(0, 0, 0, 0)
        glay.setSpacing(GRID)
        self._guide_btns = {}
        for label, _kind, _target in GUIDES:
            btn = _button(label)
            btn.clicked.connect(lambda _=False, l=label: self._on_guide(l))
            glay.addWidget(btn)
            self._guide_btns[label] = btn
        glay.addStretch(1)
        self._guides_row.setVisible(False)
        lay.addWidget(self._guides_row)
        return box

    def _relayout_cards(self, width: int):
        """As many columns (at most 3) as the widest card allows at this
        width -- so a wide font or a large scale factor wraps to two or one
        column instead of clipping a title."""
        widest = max((c.minimumWidth() for c in self._cards), default=1)
        cols = max(1, min(3, (width + GRID) // (widest + GRID)))
        if cols == self._cards_cols:
            return
        self._cards_cols = cols
        grid = self._cards_grid
        for card in self._cards:
            grid.removeWidget(card)
        for c in range(3):
            grid.setColumnStretch(c, 0)
        for i, card in enumerate(self._cards):
            grid.addWidget(card, i // cols, i % cols)
        for c in range(cols):
            grid.setColumnStretch(c, 1)

    def _build_identity_strip(self) -> QWidget:
        box = QWidget()
        box.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(GRID)
        divider = QFrame()
        divider.setObjectName("homeDivider")
        divider.setFrameShape(QFrame.Shape.NoFrame)
        lay.addWidget(divider)

        self._identity_grid = QGridLayout()
        self._identity_grid.setHorizontalSpacing(GRID)
        self._identity_grid.setVerticalSpacing(GRID)

        self._words_card, self._words_val = self._stat_card(WORDS_TODAY, "0", theme.TEXT_PRIMARY)
        self._infinity_card, _inf = self._stat_card(WORDS_REMAINING, INFINITY, theme.ACCENT)
        # Same size: the larger of the two preferred sizes, never smaller
        # than 150 x 78 (the figure changes width as the day goes on).
        self._words_val.setText("00000")
        hints = [self._words_card.sizeHint(), self._infinity_card.sizeHint()]
        # +4: the styled 1 px frame border on each side is outside the
        # layout's contents rect, and the hint rounds.
        w = max(_px(150), *(h.width() + 4 for h in hints))
        h = max(_px(78), *(h.height() + 4 for h in hints))
        for card in (self._words_card, self._infinity_card):
            card.setFixedSize(w, h)
        self._words_val.setText("0")

        self._creed = _label(CREED, size=14, color=theme.TEXT_SECONDARY, weight=400,
                             family=theme.FONT_FAMILY_DISPLAY, spacing="0.12em")
        self._creed.setAccessibleName("Creed")
        self._creed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._creed.setWordWrap(True)
        # Stretches and wraps rather than demanding its unwrapped width, so
        # a wide face or a large scale factor folds the creed onto two lines.
        self._creed.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._identity_rows = 0
        self._relayout_identity(CONTENT_MAX_W)
        lay.addLayout(self._identity_grid)
        return box

    def _relayout_identity(self, width: int):
        """Cards left, creed right on one row when it fits; otherwise the
        creed takes its own row beneath the cards, still right-aligned."""
        need = (self._words_card.width() + self._infinity_card.width() + 2 * GRID
                + self._creed.minimumSizeHint().width())
        rows = 1 if need <= width else 2
        if rows == self._identity_rows:
            return
        self._identity_rows = rows
        grid = self._identity_grid
        for w in (self._words_card, self._infinity_card, self._creed):
            grid.removeWidget(w)
        grid.addWidget(self._words_card, 0, 0)
        grid.addWidget(self._infinity_card, 0, 1)
        grid.setColumnStretch(2, 1)
        if rows == 1:
            grid.addWidget(self._creed, 0, 2)
        else:
            grid.addWidget(self._creed, 1, 0, 1, 3)

    def _stat_card(self, label: str, value: str, color: str) -> tuple:
        card = _card()
        card.setAccessibleName(label)
        card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(GRID, _px(12), GRID, _px(12))
        lay.setSpacing(_px(2))
        val = _label(value, size=22, color=color, weight=600)
        val.setAccessibleName(f"{label} value")
        lay.addWidget(val)
        lay.addWidget(_label(label, size=11, color=theme.TEXT_SECONDARY, weight=600))
        return card, val

    # ---- Refresh (runtime) ---------------------------------------------

    def set_mark_frame(self, frame):
        self._mark.set_frame(frame)

    def refresh(self):
        app = self._app
        text, live = listening_state(app)
        if not mic_present(app):
            self._listening_val.setText(f"{text} {EM_DASH} microphone not found")
            self._listening_val.setStyleSheet(
                f"color: {theme.ERROR}; font-size: {_px(14)}px; font-weight: 500;"
                " background: transparent; border: none;")
        else:
            self._listening_val.setText(text)
            self._listening_val.setStyleSheet(
                f"color: {theme.TEXT_PRIMARY}; font-size: {_px(14)}px; font-weight: 500;"
                " background: transparent; border: none;")
        self._lane_val.setText(lane_text(app))
        self._target_val.setText(target_text(app))
        self._instruction.setText(instruction_line(_cfg(app), self._help))

        self._refresh_controls()

        self._refresh_last_action()
        self._words_val.setText(str(words_today(app)))

    def _refresh_controls(self):
        app = self._app
        active = capturing(app)
        stopping = getattr(self, '_stopping', False) and active
        stop_text = STOPPING if stopping else STOP_LISTENING
        if self._stop_btn.text() != stop_text:
            self._stop_btn.setText(stop_text)
            self._stop_btn.setAccessibleName(stop_text)
        self._stop_btn.setEnabled(active and not stopping)
        if not active:
            self._stopping = False
        snoozed = bool(getattr(app, 'snoozed', False))
        armed = bool(getattr(app, 'wake_word_active', False))
        pause_text = RESUME_HANDS_FREE if snoozed else PAUSE_HANDS_FREE
        if self._pause_btn.text() != pause_text:
            self._pause_btn.setText(pause_text)
            self._pause_btn.setAccessibleName(pause_text)
        self._pause_btn.setEnabled(snoozed or armed)

    def _feedback(self):
        """Re-read runtime state now and again shortly after, so a press
        changes the mark, the state line and the button within 200 ms even
        when the app's stop path finishes asynchronously."""
        self.refresh()
        QTimer.singleShot(FEEDBACK_MS, self.refresh)
        QTimer.singleShot(FEEDBACK_MS * 4, self.refresh)

    def _refresh_teach(self):
        has_text = bool(last_typed_text(self._app).strip())
        self._teach_btn.setEnabled(has_text)
        self._teach_note.setText("" if has_text else NOTHING_TO_CORRECT)
        self._teach_note.setVisible(not has_text)

    def _refresh_last_action(self):
        self._refresh_teach()
        ring = outcome_ring(self._app)
        outcome = ring[-1] if ring else None
        if outcome != self._last_outcome:
            self._reason.setVisible(False)
        self._last_outcome = outcome
        if outcome is None:
            example = catalog_example(self._records)
            self._action_text.setText(
                f"Nothing yet. Try: {example}" if example else "Nothing yet.")
            self._action_text.setToolTip("")
            self._undo_btn.setEnabled(False)
            self._why_btn.setEnabled(False)
            return
        full = render_outcome(self._app, outcome)
        self._action_full = full
        self._elide(full)
        cls = classify_outcome(outcome[0], outcome[1])
        self._undo_btn.setEnabled(cls == "typed")
        self._why_btn.setEnabled(outcome_reason(outcome[0], outcome[1]) is not None)

    def _elide(self, full: str):
        fm = self._action_text.fontMetrics()
        width = max(self._action_text.width(), 200)
        self._action_text.setText(fm.elidedText(full, Qt.TextElideMode.ElideRight, width))
        self._action_text.setToolTip("")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # Columns follow the room the viewport gives, not the content's own
        # width (which the grid would otherwise push out sideways).
        avail = max(1, min(CONTENT_MAX_W, self._scroll.viewport().width() - 2 * GRID_LG))
        self._relayout_cards(avail)
        self._relayout_identity(avail)
        full = getattr(self, '_action_full', None)
        if full and self._last_outcome is not None:
            self._elide(full)

    # ---- Actions ----------------------------------------------------------

    def _on_stop(self):
        if not capturing(self._app):
            return
        self._stopping = True
        self._stop_btn.setText(STOPPING)
        self._stop_btn.setAccessibleName(STOPPING)
        self._stop_btn.setEnabled(False)
        stop_capture(self._app)
        self._feedback()

    def _on_pause(self):
        if getattr(self._app, 'snoozed', False):
            resume_hands_free(self._app)
        else:
            pause_hands_free(self._app)
        self._feedback()

    def _on_undo(self):
        fn = getattr(self._app, '_handle_unified_scratch_that', None)
        if callable(fn):
            try:
                fn()
            except Exception as exc:
                logger.warning(f"[HOME] undo failed: {exc}")
        self.refresh()

    def _on_teach(self):
        if not last_typed_text(self._app).strip():
            self._refresh_teach()          # says so on the card; opens nothing
            return
        fn = getattr(self._app, 'open_correction_capture', None)
        if not callable(fn):
            logger.error("[HOME] the app has no open_correction_capture")
            return
        try:
            fn()
        except Exception as exc:
            logger.warning(f"[HOME] teach a word failed: {exc}")

    def _on_why(self):
        if self._last_outcome is None:
            return
        reason = outcome_reason(self._last_outcome[0], self._last_outcome[1])
        if reason:
            self._reason.setText(reason)
            self._reason.setVisible(True)

    def _on_card(self, spec: dict):
        action, arg = spec.get("action"), spec.get("arg")
        if action == "cheatsheet":
            open_cheatsheet_filtered(self._app, arg)
        elif action == "settings":
            open_settings_tab(self._app, arg)
        elif action == "page":
            self._open_page(arg)
        elif action == "guides":
            self.show_guides(not self._guides_row.isVisibleTo(self))
        else:
            logger.error(f"[HOME] card action {action!r} has no destination")

    def show_guides(self, visible: bool = True):
        self._guides_row.setVisible(visible)
        if visible:
            first = next(iter(self._guide_btns.values()), None)
            if first is not None:
                first.setFocus()

    def _on_guide(self, label: str):
        open_guide(self._app, label)

    def destinations(self) -> dict:
        """Every actionable control on this page -> its concrete destination
        (the table tests/test_home_qt.py checks). kinds: 'app' calls an app
        method; 'settings' selects a registry page; 'cheatsheet' shows the
        command reference; 'page' switches the hub page; 'inline' changes
        this page; 'guides' opens the guides row."""
        table = {
            STOP_LISTENING: ("app", "exit_ava_command_session | exit_command_mode | stop_continuous_mode | stop_recording"),
            PAUSE_HANDS_FREE: ("app", "snooze_listening"),
            RESUME_HANDS_FREE: ("app", "resume_listening"),
            "Undo": ("app", "_handle_unified_scratch_that"),
            "Teach a word": ("app", "open_correction_capture"),
            "Why?": ("inline", "Reason"),
        }
        for spec in capability_cards(self._app, self._records):
            table[spec["title"]] = (spec["action"], spec["arg"])
        for label, kind, target in GUIDES:
            table[label] = (kind, target)
        return table

    # ---- Introspection (tests, screenshots) ------------------------------

    @property
    def cards(self) -> list:
        return list(self._cards)

    @property
    def tab_chain(self) -> list:
        """The declared tab order, visible controls only (a hidden guides
        row is skipped by Qt's focus traversal too)."""
        return [w for w in self._tab_chain if w.isVisibleTo(self)]

    @property
    def state_texts(self) -> dict:
        return {
            "Listening": self._listening_val.text(),
            "Lane": self._lane_val.text(),
            "Next utterance goes to": self._target_val.text(),
            "Instruction": self._instruction.text(),
        }

    @property
    def last_action_text(self) -> str:
        return self._action_text.text()

    @property
    def content_widget(self) -> QWidget:
        return self._content


def find_by_accessible_name(root: QWidget, name: str) -> Optional[QWidget]:
    """The first descendant (or root) whose accessible name is `name` -- the
    way a voice or screen-reader user reaches a control."""
    if root.accessibleName() == name:
        return root
    for w in root.findChildren(QWidget):
        if w.accessibleName() == name:
            return w
    return None


def overflowing_widgets(root: QWidget) -> list:
    """Visible descendants whose preferred size does not fit the rect the
    layout gave them. Word-wrapped labels are judged by heightForWidth;
    labels marked elided (property 'elided') are judged by height only.
    Empty when nothing clips -- the scaling gate."""
    bad = []
    for w in root.findChildren(QWidget):
        if not w.isVisibleTo(root) or w.width() <= 0 or w.height() <= 0:
            continue
        if not isinstance(w, (QLabel, QPushButton)):
            continue
        hint = w.sizeHint()
        name = w.accessibleName() or w.objectName() or type(w).__name__
        if isinstance(w, QLabel) and w.wordWrap():
            need_h = w.heightForWidth(w.width())
            if need_h > w.height() + 1:
                bad.append(f"{name}: needs h={need_h} has h={w.height()}")
            continue
        if isinstance(w, QLabel) and w.property("elided"):
            if hint.height() > w.height() + 1:
                bad.append(f"{name}: needs h={hint.height()} has h={w.height()}")
            continue
        if hint.width() > w.width() + 1 or hint.height() > w.height() + 1:
            bad.append(f"{name}: needs {hint.width()}x{hint.height()}"
                       f" has {w.width()}x{w.height()}")
    return bad
