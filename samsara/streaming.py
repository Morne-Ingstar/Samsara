"""Streaming dictation: live partial transcription with overlay.

Architecture (ACE-04B):

  - Audio capture: DictationSessionConsumer drain thread fills _streaming_frames
    from the ACE engine ring. activate_streaming() starts the drain thread;
    stop_streaming() stops it and returns all accumulated audio.
  - StreamingSession owns the lifecycle: spawn worker, manage overlay,
    finalize on hotkey release.
  - StreamingWorker (daemon thread) calls snapshot_streaming_audio() every
    ~1.5s for non-destructive partial snapshots, runs Whisper at beam_size=1,
    posts overlay updates via Qt signals.
  - On stop_event the worker runs a final beam_size=5 pass with full
    Grammar-Lite cleanup, then posts paste + overlay close to the Qt thread.

Latency budget (for 'hold' streaming):

  pre-buffer skipped + 1.0s first chunk + ~0.3s transcribe = ~1.3s
  to first partial. Subsequent partials at ~1.5s cadence.

Locks:

  - app.model_lock (existing): serializes Whisper calls. Acquired
    non-blocking for partials -- if held, skip and try next interval.
  - _streaming_lock in DictationSessionConsumer: protects _streaming_frames
    for concurrent access between the drain thread and snapshot_streaming_audio().
"""

import ctypes
import functools
import html
import re
import sys
import threading
import time

import numpy as np
import pyautogui

try:
    import pyperclip
except ImportError:
    pyperclip = None

from samsara.cleanup import clean_text
from samsara.log import get_logger
from samsara.runtime import thread_registry
from samsara.session_modes import (
    SessionMode,
    is_dictate_commit,
    is_recover_draft,
    is_scratch_that,
    match_ava_invocation,
    match_literal_payload,
    match_switch_word,
)
from samsara.smart_corrections import smart_correct
from samsara import config_defaults


class _ThemeProxy:
    """`theme.TOKEN`, without importing Qt when this module is imported.

    samsara.ui.theme pulls PySide6 at import, and streaming.py deliberately
    does not -- every Qt import in here is inside the function that needs it,
    so the module can be imported during boot before the application exists.
    Forwarding each attribute also means the token is read at the moment it is
    used, which is what makes a palette switch reach this overlay at all."""

    def __getattr__(self, name):
        from samsara.ui import theme as _theme  # noqa: PLC0415
        return getattr(_theme, name)


theme = _ThemeProxy()
from samsara import diagnostics
from samsara import languages as _languages

logger = get_logger(__name__)


# Queue 136.  Presets are useful by voice; a drag uses the same normalized
# center + named-screen representation as ListeningIndicator, serialized in
# this one schema-backed setting as ``custom|screen|cx|cy``.
PREVIEW_POSITION_DEFAULT = "bottom-center"
PREVIEW_POSITION_PRESETS = frozenset({
    "top-left", "top-center", "top-right", "center-left", "center",
    "center-right", "bottom-left", "bottom-center", "bottom-right",
})
_CUSTOM_REACHABLE_MARGIN = 24


def _preview_position(value):
    """Validated preset/custom preview placement from config."""
    if value in PREVIEW_POSITION_PRESETS:
        return value
    if isinstance(value, str) and value.startswith("custom|"):
        parts = value.split("|", 3)
        if len(parts) == 4:
            try:
                cx, cy = float(parts[2]), float(parts[3])
            except (TypeError, ValueError):
                pass
            else:
                if all(np.isfinite(v) for v in (cx, cy)):
                    return ("custom", parts[1], min(max(cx, 0.0), 1.0), min(max(cy, 0.0), 1.0))
    return PREVIEW_POSITION_DEFAULT


def reset_preview_placement(app) -> bool:
    """Restore the preview to its documented default and move a live one.

    This is deliberately the one reset path used by settings, tray, and the
    voice command. A live DictatePreviewSession is optional: persisting the
    default still makes the next session recoverable.
    """
    config = getattr(app, "config", None)
    if not isinstance(config, dict):
        return False
    command_mode = dict(config.get("command_mode") or {})
    command_mode["preview_position"] = PREVIEW_POSITION_DEFAULT
    update = getattr(app, "update_config_and_save", None)
    if callable(update):
        update({"command_mode": command_mode})
    else:
        config["command_mode"] = command_mode

    session = getattr(app, "_dictate_preview", None)
    overlay = getattr(session, "_overlay", None)
    move = getattr(overlay, "move_draft", None)
    if callable(move):
        move(PREVIEW_POSITION_DEFAULT)
    return True


# ---- Modifier-release plumbing (Windows) -----------------------------------
#
# In streaming direct-paste mode the user is physically holding the hotkey
# (e.g. Ctrl+Shift). Sending Backspace while those modifiers are held turns
# into Ctrl+Shift+Backspace, which most apps interpret as "delete previous
# word" or "do nothing" rather than a single-character delete. Same problem
# for the synthesized Ctrl+V inside _paste_preserving_clipboard.
#
# Before the backspace+paste sequence we send key-up events for any held
# Ctrl/Shift keys so the target app sees clean events; after, we re-press
# only the ones that were actually held when we started. State-aware
# restore matters because the FINAL paste runs after the user has already
# released the hotkey -- re-pressing keys that aren't held would inject a
# phantom down event with no matching up.

if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.windll.user32

    _INPUT_KEYBOARD = 1
    _KEYEVENTF_KEYUP = 0x0002
    _VK_LCONTROL = 0xA2
    _VK_RCONTROL = 0xA3
    _VK_LSHIFT = 0xA0
    _VK_RSHIFT = 0xA1
    _MODIFIER_VKS = (_VK_LCONTROL, _VK_RCONTROL, _VK_LSHIFT, _VK_RSHIFT)

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD),
                    ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _MOUSEINPUT(ctypes.Structure):
        # Declared so the INPUT union has the correct size on x64.
        _fields_ = [("dx", wintypes.LONG),
                    ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

    def _send_key_event(vk, key_up):
        try:
            inp = _INPUT()
            inp.type = _INPUT_KEYBOARD
            inp.u.ki = _KEYBDINPUT(
                wVk=vk, wScan=0,
                dwFlags=_KEYEVENTF_KEYUP if key_up else 0,
                time=0, dwExtraInfo=None)
            _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        except Exception as e:
            logger.debug(f"[STREAM] SendInput vk={vk:#x} failed: {e}")

    def _release_held_modifiers():
        """Release any currently-held Ctrl/Shift keys. Returns the list of
        VKs that were held so the caller can press them back afterwards."""
        held = []
        try:
            for vk in _MODIFIER_VKS:
                if _user32.GetAsyncKeyState(vk) & 0x8000:
                    held.append(vk)
            for vk in held:
                _send_key_event(vk, key_up=True)
        except Exception as e:
            logger.debug(f"[STREAM] release_held_modifiers failed: {e}")
        return held

    def _press_modifiers(vks):
        if not vks:
            return
        try:
            for vk in vks:
                _send_key_event(vk, key_up=False)
        except Exception as e:
            logger.debug(f"[STREAM] press_modifiers failed: {e}")

else:
    def _release_held_modifiers():
        return []

    def _press_modifiers(vks):
        pass


MOD_GUARD_SETTLE_S = 0.01


FIRST_CHUNK_S = 0.7
CHUNK_INTERVAL_S = 1.0
MAX_DURATION_S = 120.0
MIN_PARTIAL_AUDIO_S = 0.5

OVERLAY_W = 500
OVERLAY_MIN_H = 60
OVERLAY_MAX_H = 200
TASKBAR_RESERVE = 50
OVERLAY_GAP_ABOVE_TASKBAR = 80


def bg_color():
    return f"{theme.BG1}"
FONT_FAMILY = "Segoe UI"
FONT_SIZE = 14
DIM_FONT_SIZE = 11
ALPHA = 0.92
DIM_ALPHA = 0.65
# DictatePreviewSession.set_transcript: muted color for the still-live
# partial line, so it reads as visually distinct from settled/finalized
# text without needing a second widget or a border-color swap per tick.

# ---- Idle fade (queue 75) ----------------------------------------------------
# The hands-free DICTATE preview is the app's one continuous "alive and
# listening" signal, so idle means faint and click-through, never gone -- a
# hidden preview looks exactly like a crashed one. "Fully hidden" (opacity 0)
# exists only as an explicit Settings choice; the listening indicator is
# force-shown for the whole hands-free session (dictation.py enter_command_mode)
# and keeps showing state either way.
#
# Delay default 5 s: in the owner's logs (848 pauses between hands-free
# utterances, 2026-09) the median pause is 1.8 s and 75% are under 5.0 s, so a
# mid-thought pause almost never fades the box; the 90th percentile is 20 s,
# which is real idle. Opacity 0.25: still plainly a box on the dark desktop,
# too faint to pull the eye.
IDLE_DELAY_S_DEFAULT = 5.0
IDLE_DELAY_S_MIN = 1.0
IDLE_DELAY_S_MAX = 60.0
IDLE_OPACITY_DEFAULT = 0.25
IDLE_OPACITY_MAX = 0.9
IDLE_DELAY_KEY = "command_mode.preview_idle_delay_s"
IDLE_OPACITY_KEY = "command_mode.preview_idle_opacity"

# Queue 104: idle stays click-through -- the box lives over other apps, and a
# faint box that ate clicks would be the worse bug. What was missing was a way
# back IN. Reaching for "Clear draft" on an idle box put the click straight
# through into the app underneath, and the only remaining way to wake the box
# was to speak -- which dictated the utterance ("ah") into the very draft the
# owner was trying to clear. The cursor arriving over the box now wakes it,
# polled on the tick below; see cursor_screen_pos() for why nothing
# event-driven can work here.

#: The ONE timer of an idle-enabled preview: activity poll, fade steps,
#: the "Listening..." dots and hint rotation all run on its tick (splash_qt's
#: single frame-timer pattern). Qt thread only.
IDLE_TICK_MS = 50
IDLE_FADE_MS = 400
ELLIPSIS_STEP_MS = 400
HINT_FIRST_AFTER_MS = 2000      # after the box has finished fading
HINT_ROTATE_MS = 8000
LISTENING_WORD = "Listening"
LISTENING_TEXT = LISTENING_WORD + "..."

# ---------------------------------------------------------------------------
# Queue 85: the dictate preview is scrollable, has a clear button, and every
# word is clickable. All of it exists only when `idle` is set (the hands-free
# DICTATE preview); hold-to-stream keeps the plain, taller-than-nothing label.
# ---------------------------------------------------------------------------

#: The box may grow taller than the hold-to-stream one: reviewing a draft is
#: the point. Beyond this it scrolls.
DICTATE_OVERLAY_MAX_H = 320
#: How close to the bottom still counts as "following the newest text".
AUTO_FOLLOW_SLACK_PX = 4
#: One "scroll the draft up/down" step.
SCROLL_STEP_PX = 90


def control_bg():
    return f"{theme.BG2}"
CLEAR_BUTTON_TEXT = "Clear draft"
#: Shown while a clicked word waits for its spoken replacement.
CORRECTION_PROMPT = 'Choose a replacement for "{word}", or say it'
HINT_FONT_SIZE = 11

#: Which commands an idle hint may teach. Only ids: the spoken phrase and what
#: it does are read from commands_catalog.json when the preview starts, and an
#: id the catalog does not have produces no hint -- a hint can never teach a
#: phrase that no longer works.
IDLE_HINT_COMMAND_IDS = (
    "builtin.scratch_that",
    "builtin.scratch_everything",
    "builtin.submit",
    "builtin.new_line",
    "verbatim_toggle.literal_on",
)


class IdleSettings:
    """Idle fade settings for one preview, read once from config."""

    __slots__ = ("delay_s", "opacity")

    def __init__(self, delay_s: float = IDLE_DELAY_S_DEFAULT,
                 opacity: float = IDLE_OPACITY_DEFAULT):
        self.delay_s = delay_s
        self.opacity = opacity

    @property
    def fully_hidden(self) -> bool:
        return self.opacity <= 0.0

    @classmethod
    def from_config(cls, config) -> "IdleSettings":
        """Out-of-range or unreadable values fall back to the defaults rather
        than to something that could hide the box by accident."""
        section = (config or {}).get("command_mode", {}) if isinstance(config, dict) else {}
        if not isinstance(section, dict):
            section = {}

        def _number(key, default, low, high):
            try:
                value = float(section.get(key, default))
            except (TypeError, ValueError):
                return default
            if value != value or not (low <= value <= high):   # NaN or out of range
                return default
            return value

        return cls(
            delay_s=_number("preview_idle_delay_s", IDLE_DELAY_S_DEFAULT,
                            IDLE_DELAY_S_MIN, IDLE_DELAY_S_MAX),
            opacity=_number("preview_idle_opacity", IDLE_OPACITY_DEFAULT, 0.0, IDLE_OPACITY_MAX),
        )


def idle_hints_from_catalog(records) -> list:
    """Hint lines for IDLE_HINT_COMMAND_IDS, text taken from catalog records
    (canonical phrase + description). Missing ids are skipped; [] when the
    catalog is unavailable."""
    from samsara.command_catalog import canonical_phrase
    by_id = {r.get("canonical_id"): r for r in (records or []) if isinstance(r, dict)}
    hints = []
    for command_id in IDLE_HINT_COMMAND_IDS:
        record = by_id.get(command_id)
        if record is None:
            continue
        try:
            phrase = canonical_phrase(record)
        except Exception:
            continue
        description = str(record.get("description") or "").strip().rstrip(".")
        if not phrase or not description or description.lower() == phrase.lower():
            continue
        hints.append(f'Say "{phrase}" - {description[0].lower() + description[1:]}')
    return hints


def load_idle_hints() -> list:
    try:
        from samsara.command_catalog import load_catalog_json
        return idle_hints_from_catalog(load_catalog_json())
    except Exception as exc:
        logger.debug(f"[DICTATE-PREVIEW] idle hints unavailable: {exc}")
        return []


if sys.platform == "win32":
    _GWL_EXSTYLE = -20
    _WS_EX_TRANSPARENT = 0x00000020
    _WS_EX_LAYERED = 0x00080000
    # Private prototypes: never set argtypes on the shared windll.user32
    # function objects other modules call with their own conventions.
    _GetWindowLongPtrW = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_int)(
        ("GetWindowLongPtrW", ctypes.windll.user32))
    _SetWindowLongPtrW = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t)(
        ("SetWindowLongPtrW", ctypes.windll.user32))


def set_window_click_through(widget, on: bool) -> None:
    """Qt thread only. Make a shown top-level pass mouse input to whatever is
    underneath (on) or take it again (off).

    WA_TransparentForMouseEvents alone only stops Qt handling the click:
    Windows still delivers it to this window, so the app underneath never gets
    it. WS_EX_TRANSPARENT on the native window is what makes the OS hit test
    skip it. Toggled in place, so the box is never re-shown (no flicker, no
    activation); measured to survive setWindowOpacity (reports/75)."""
    from PySide6.QtCore import Qt
    widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, on)
    if sys.platform == "win32":
        hwnd = int(widget.winId())
        style = _GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
        style = (style | _WS_EX_TRANSPARENT | _WS_EX_LAYERED) if on else (style & ~_WS_EX_TRANSPARENT)
        _SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, style)
    else:
        visible = widget.isVisible()
        widget.setWindowFlag(Qt.WindowType.WindowTransparentForInput, on)
        if visible:
            widget.show()


def window_is_click_through(widget) -> bool:
    """What the OS will do with a click on this window (tests, diagnostics)."""
    from PySide6.QtCore import Qt
    if sys.platform == "win32":
        return bool(_GetWindowLongPtrW(int(widget.winId()), _GWL_EXSTYLE) & _WS_EX_TRANSPARENT)
    return bool(widget.windowFlags() & Qt.WindowType.WindowTransparentForInput)


def cursor_screen_pos():
    """Queue 104. Where the pointer IS, in Qt global coordinates.

    This is the whole trick, so it is worth being explicit about why the
    obvious alternatives cannot work. An idle preview has WS_EX_TRANSPARENT
    set (set_window_click_through above), which makes the OS hit test skip
    the window entirely: Windows never decides the pointer is "over" it, so
    it sends no WM_MOUSEMOVE/WM_NCHITTEST/WM_MOUSELEAVE for it, so Qt
    synthesises no enter, leave, move or hover event. enterEvent(),
    QEvent.HoverEnter, setMouseTracking(), an eventFilter -- every one of
    them is downstream of a hit test that has already excluded us, and would
    silently never fire. That is exactly the state in which the preview's
    buttons need to become reachable again.

    QCursor.pos() is not downstream of any of that. It is a device-state
    query -- GetCursorPos on Windows -- that asks where the pointer is, not
    which window is under it. No window need be hit-testable, focused,
    activated or even visible for it to answer. Polling it and doing our own
    rectangle test is hit-testing the preview ourselves, which is the only
    way to hit-test a window the OS has been told to ignore.

    Costs one user32 call. Qt global coordinates are device-independent, the
    same space QWidget.frameGeometry() reports, so no devicePixelRatio
    scaling belongs here (unlike the native WindowFromPoint calls in the
    queue 75 tests, which take physical pixels)."""
    from PySide6.QtGui import QCursor
    return QCursor.pos()

PARTIAL_BEAM = 1
FINAL_BEAM = 5
NO_SPEECH_THRESHOLD = 0.6
LOG_PROB_THRESHOLD = -1.0

# Direct-paste mode: replace the previous partial via Ctrl+Z undo,
# then paste the new text. Relies on the target app having a per-paste
# undo entry (true for Notepad, RichEdit, browsers, IDEs we tested).
# Tunable settles -- bumping these helps slow apps but adds latency.
UNDO_SETTLE_S = 0.05
PASTE_SETTLE_S = 0.02


# ---------------------------------------------------------------------------
# Qt overlay
# ---------------------------------------------------------------------------

class _StreamingWidget:
    """Internal Qt widget — created on the samsara-qt thread."""

    def __init__(self, dim: bool, idle: "IdleSettings | None" = None,
                 activity_probe=None, hints=(), preview_position=PREVIEW_POSITION_DEFAULT,
                 on_placement_committed=None):
        from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel, QApplication,
                                       QFrame, QPushButton, QScrollArea)
        from PySide6.QtCore import Qt, QTimer, QElapsedTimer, Signal, Slot
        from PySide6.QtGui import QFont

        # Inline QWidget subclass so we can define Signals
        class _W(QWidget):
            _update_sig = Signal(str, str, str)
            _flash_sig  = Signal(object)
            _close_sig  = Signal()

            def __init__(self, dim, parent=None):
                super().__init__(
                    parent,
                    Qt.WindowType.FramelessWindowHint |
                    Qt.WindowType.WindowStaysOnTopHint |
                    Qt.WindowType.Tool |
                    # WS_EX_NOACTIVATE on Windows. Tool alone still lets the
                    # widget take focus/activation on show() -- required so
                    # this overlay never steals foreground from whatever the
                    # user is dictating into (both the hold-to-stream feature
                    # and DictatePreviewSession's session-lane preview depend
                    # on this; the latter's session focus lock assumes the
                    # target window stays foreground for the whole session).
                    Qt.WindowType.WindowDoesNotAcceptFocus,
                )
                self._dim = dim
                self._fade_alpha = DIM_ALPHA if dim else ALPHA
                self._on_complete = None

                self._fade_timer = QTimer(self)
                self._fade_timer.setInterval(40)
                self._fade_timer.timeout.connect(self._fade_step)

                lay = QHBoxLayout(self)
                lay.setContentsMargins(0, 0, 0, 0)
                lay.setSpacing(0)

                self._border = QWidget()
                self._border.setFixedWidth(4)
                self._border.setStyleSheet(f"background:{theme.ACCENT};")
                lay.addWidget(self._border)

                content = QWidget()
                content.setStyleSheet(f"background:{bg_color()};")
                cLay = QVBoxLayout(content)
                cLay.setContentsMargins(8, 10, 12, 10)
                fs = DIM_FONT_SIZE if dim else FONT_SIZE
                init = "Listening (direct paste)..." if dim else "Listening..."
                self._label = QLabel(init)
                self._label.setWordWrap(True)
                self._label.setStyleSheet(
                    f"color:{theme.TEXT_PRIMARY};font-size:{fs}px;"
                    f"font-family:'{FONT_FAMILY}';background:transparent;"
                )
                self._label.setMinimumWidth(OVERLAY_W - 40)
                self._label.setMaximumWidth(OVERLAY_W - 40)
                self._label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                # Queue 85: the transcript lives in a scroll area so a draft
                # longer than the box can be read back instead of being pushed
                # out of sight. LinksAccessibleByMouse is what makes each word
                # clickable (and gives the hand cursor); the label is never
                # selectable, so a click is unambiguous.
                self._label.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
                self._label.linkActivated.connect(self._on_link)
                self._scroll = QScrollArea()
                self._scroll.setWidget(self._label)
                self._scroll.setWidgetResizable(True)
                self._scroll.setFrameShape(QFrame.Shape.NoFrame)
                self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
                self._scroll.setStyleSheet(
                    "QScrollArea{background:transparent;border:none;}"
                    "QScrollArea > QWidget > QWidget{background:transparent;}"
                    "QScrollBar:vertical{background:transparent;width:8px;margin:0;}"
                    f"QScrollBar::handle:vertical{{background:{theme.BORDER};"
                    "border-radius:4px;min-height:24px;}"
                    "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
                    "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
                )
                cLay.addWidget(self._scroll, stretch=1)
                # Auto-follow: stay pinned to the newest words while the view
                # is at the bottom, and stop following the moment the user
                # scrolls up to read. Driven by the scrollbar's own signals so
                # a wheel, a drag and a voice scroll all behave the same.
                bar = self._scroll.verticalScrollBar()
                # Queue 90: set BEFORE the connects -- a signal that arrives
                # during construction must never find these missing.
                self._follow = True
                #: The scroll range the current position was decided against.
                #: A value change that comes with a different maximum is the
                #: content resizing, not the user scrolling (see
                #: _on_scroll_value).
                self._known_max = 0
                bar.valueChanged.connect(self._on_scroll_value)
                bar.rangeChanged.connect(self._on_scroll_range)
                # Idle hint: its own row BELOW the transcript label, so it
                # never sits where dictated text appears; hidden whenever the
                # box is active.
                self._hint = QLabel("")
                self._hint.setWordWrap(True)
                self._hint.setStyleSheet(
                    f"color:{theme.TEXT_SECONDARY};font-size:{HINT_FONT_SIZE}px;"
                    f"font-family:'{FONT_FAMILY}';background:transparent;"
                )
                self._hint.setMinimumWidth(OVERLAY_W - 40)
                self._hint.setMaximumWidth(OVERLAY_W - 40)
                self._hint.hide()
                cLay.addWidget(self._hint)

                # Queue 85: the correction prompt ("say the replacement for
                # X"), its own row so it never overwrites the transcript.
                self._prompt = QLabel("")
                self._prompt.setWordWrap(True)
                self._prompt.setStyleSheet(
                    f"color:{theme.WARNING};font-size:{HINT_FONT_SIZE}px;"
                    f"font-family:'{FONT_FAMILY}';background:transparent;"
                )
                self._prompt.setMinimumWidth(OVERLAY_W - 40)
                self._prompt.setMaximumWidth(OVERLAY_W - 40)
                self._prompt.hide()
                cLay.addWidget(self._prompt)

                # Queue 161: a click opens a tap-first choice list.  It is a
                # child of the preview rather than a separate window, so it
                # cannot steal focus from the document being dictated into.
                self._choices = QWidget()
                self._choices.setStyleSheet(
                    f"background:{control_bg()};border:1px solid {theme.ACCENT};border-radius:6px;"
                )
                choice_lay = QVBoxLayout(self._choices)
                choice_lay.setContentsMargins(8, 6, 8, 6)
                choice_lay.setSpacing(4)
                self._choice_notice = QLabel("")
                self._choice_notice.setWordWrap(True)
                self._choice_notice.setStyleSheet(
                    f"color:{theme.TEXT_SECONDARY};font-size:{HINT_FONT_SIZE}px;"
                    f"font-family:'{FONT_FAMILY}';background:transparent;border:none;"
                )
                choice_lay.addWidget(self._choice_notice)
                self._choice_buttons = []
                self._choice_lay = choice_lay
                self._choices.hide()
                cLay.addWidget(self._choices)

                # Controls. Their visibility IS the "you can click me" signal:
                # they appear only while the box is interactive, and go away
                # with the fade that makes it click-through.
                self._controls = QWidget()
                self._controls.setStyleSheet("background:transparent;")
                bLay = QHBoxLayout(self._controls)
                bLay.setContentsMargins(0, 4, 0, 0)
                bLay.setSpacing(8)
                self._clear_btn = QPushButton(CLEAR_BUTTON_TEXT)
                self._clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                self._clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                self._clear_btn.setStyleSheet(
                    f"QPushButton{{background:{control_bg()};color:{theme.TEXT_PRIMARY};"
                    f"border:1px solid {theme.BORDER};border-radius:5px;"
                    f"padding:3px 10px;font-size:{HINT_FONT_SIZE}px;"
                    f"font-family:'{FONT_FAMILY}';}}"
                    f"QPushButton:hover{{border-color:{theme.ACCENT};}}"
                )
                self._clear_btn.clicked.connect(self._on_clear_clicked)
                bLay.addWidget(self._clear_btn)
                bLay.addStretch(1)
                self._controls.hide()
                cLay.addWidget(self._controls)
                lay.addWidget(content, stretch=1)

                self.setFixedWidth(OVERLAY_W)
                self.setWindowOpacity(DIM_ALPHA if dim else ALPHA)

                self._update_sig.connect(self._on_update)
                self._flash_sig.connect(self._on_flash)
                self._close_sig.connect(self._on_close)

                # Queue 75 idle state. None of this exists for hold-to-stream
                # (idle is None): that box only lives while a key is held.
                self._idle = None
                self._activity_probe = None
                self._hints = []
                self._life_timer = None
                self._clock = QElapsedTimer()
                self._clock.start()
                self._reduced_motion = False
                self._faded = False
                self._click_through = False
                self._fade_from = self._fade_to = self._active_alpha = DIM_ALPHA if dim else ALPHA
                self._fade_started_ms = None
                self._last_active_ms = 0
                self._idle_started_ms = 0
                self._last_content = None
                self._placeholder_prefix = ""      # label shows the Listening placeholder
                self._dots_shown = 3
                self._hint_index = -1
                self._hint_shown_ms = None
                # Queue 85. Callbacks are set by enable_interaction and dropped
                # by stop_life, so the widget holds no session reference at
                # close (queue 60's rule).
                self._on_word_clicked = None
                self._on_clear = None
                self._on_choice = None
                self._on_choice_dismiss = None
                self._interactive = False
                self._has_draft = False
                # Dragging is available only while the existing interactive
                # state is visible; idle remains genuinely click-through.
                self._dragging = False
                self._drag_offset = None
                self._preview_position = _preview_position(preview_position)
                self._on_placement_committed = on_placement_committed

            # ---- queue 85: scrolling, clear, click-to-correct ---------------

            def enable_interaction(self, on_word_clicked, on_clear):
                self._on_word_clicked = on_word_clicked
                self._on_clear = on_clear

            def _on_scroll_value(self, value):
                """The user (or a voice scroll) moved the view: follow the
                newest text only while the bottom is in sight.

                Queue 90: a value change that arrives with a DIFFERENT maximum
                is the draft growing or shrinking under the view -- Qt moves
                the range and clamps the value itself -- and must never be read
                as the user having scrolled. Only a move within an unchanged
                range is a deliberate scroll."""
                bar = self._scroll.verticalScrollBar()
                if bar.maximum() != self._known_max:
                    self._known_max = bar.maximum()
                    if self._follow:
                        bar.setValue(bar.maximum())
                    return
                self._follow = value >= bar.maximum() - AUTO_FOLLOW_SLACK_PX

            def _on_scroll_range(self, _minimum, maximum):
                """The transcript got taller or shorter. That is never the user
                scrolling, so the stored intent survives it: pin to the bottom
                if we were following, and leave the view exactly where the user
                put it if we were not."""
                self._known_max = maximum
                if self._follow:
                    self._scroll.verticalScrollBar().setValue(maximum)

            def scroll_draft(self, where: str) -> None:
                bar = self._scroll.verticalScrollBar()
                if where == "top":
                    bar.setValue(bar.minimum())
                elif where == "bottom":
                    bar.setValue(bar.maximum())
                elif where == "up":
                    bar.setValue(bar.value() - SCROLL_STEP_PX)
                else:
                    bar.setValue(bar.value() + SCROLL_STEP_PX)
                self._follow = bar.value() >= bar.maximum() - AUTO_FOLLOW_SLACK_PX

            def set_preview_position(self, position):
                if position not in PREVIEW_POSITION_PRESETS:
                    return
                self._preview_position = position
                self._position()
                callback = self._on_placement_committed
                if callback is not None:
                    callback(position)

            def _placement_screen(self):
                placement = self._preview_position
                if isinstance(placement, tuple):
                    name = placement[1]
                    for screen in QApplication.screens():
                        if screen.name() == name:
                            return screen
                    # A named monitor can disappear after undocking. Its
                    # fractions have no useful meaning on another display;
                    # use the primary screen's documented default instead.
                    self._preview_position = PREVIEW_POSITION_DEFAULT
                    callback = self._on_placement_committed
                    if callback is not None:
                        callback(PREVIEW_POSITION_DEFAULT)
                return QApplication.primaryScreen()

            @staticmethod
            def _clamp_position(x, y, w, h, geom):
                return (max(geom.left(), min(x, geom.right() - w + 1)),
                        max(geom.top(), min(y, geom.bottom() - h + 1)))

            @classmethod
            def _clamp_custom_position(cls, x, y, w, h, geom):
                """Keep custom drags visibly inside the usable desktop."""
                margin = min(
                    _CUSTOM_REACHABLE_MARGIN,
                    max((geom.width() - w) // 2, 0),
                    max((geom.height() - h) // 2, 0),
                )
                safe_geom = geom.adjusted(margin, margin, -margin, -margin)
                return cls._clamp_position(x, y, w, h, safe_geom)

            def _commit_drag_position(self):
                screen = QApplication.screenAt(self.mapToGlobal(self.rect().center()))
                screen = screen or QApplication.primaryScreen()
                if screen is None:
                    return
                geom = screen.availableGeometry()
                x, y = self._clamp_custom_position(
                    self.x(), self.y(), self.width(), self.height(), geom)
                self.move(x, y)
                cx = min(max(((x + self.width() / 2) - geom.x()) / max(geom.width(), 1), 0.0), 1.0)
                cy = min(max(((y + self.height() / 2) - geom.y()) / max(geom.height(), 1), 0.0), 1.0)
                self._preview_position = ("custom", screen.name(), cx, cy)
                callback = self._on_placement_committed
                if callback is not None:
                    callback(f"custom|{screen.name()}|{cx:.6f}|{cy:.6f}")

            def mousePressEvent(self, event):
                if self._interactive and event.button() == Qt.MouseButton.LeftButton:
                    self._dragging = True
                    self._drag_offset = event.globalPosition().toPoint() - self.pos()
                    event.accept()
                    return
                super().mousePressEvent(event)

            def mouseMoveEvent(self, event):
                if self._interactive and self._dragging:
                    self.move(event.globalPosition().toPoint() - self._drag_offset)
                    event.accept()
                    return
                super().mouseMoveEvent(event)

            def mouseReleaseEvent(self, event):
                if self._interactive and self._dragging and event.button() == Qt.MouseButton.LeftButton:
                    self._dragging = False
                    self._commit_drag_position()
                    event.accept()
                    return
                super().mouseReleaseEvent(event)

            def set_prompt(self, text: str) -> None:
                text = text or ""
                if text:
                    self._prompt.setText(text)
                    if not self._prompt.isVisible():
                        self._prompt.show()
                elif self._prompt.isVisible():
                    self._prompt.hide()
                self._position()

            def set_correction_choice_callbacks(self, on_choice, on_dismiss) -> None:
                self._on_choice = on_choice
                self._on_choice_dismiss = on_dismiss

            def show_correction_choices(self, word: str, candidates, no_match: bool = False) -> None:
                """Show large, focusless choices; the original is always safe."""
                while self._choice_buttons:
                    button = self._choice_buttons.pop()
                    self._choice_lay.removeWidget(button)
                    button.deleteLater()
                notice = ("Choose what you meant:" if not no_match else
                          "No similar word is in your vocabulary yet. You can keep the original or say a replacement.")
                self._choice_notice.setText(notice)
                for candidate in candidates:
                    button = QPushButton(candidate)
                    button.setCursor(Qt.CursorShape.PointingHandCursor)
                    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                    button.setMinimumHeight(36)
                    button.setStyleSheet(
                        f"QPushButton{{background:{theme.BG1};color:{theme.TEXT_PRIMARY};"
                        f"border:1px solid {theme.BORDER};border-radius:5px;padding:5px 10px;"
                        f"font-size:{HINT_FONT_SIZE + 1}px;font-family:'{FONT_FAMILY}';text-align:left;}}"
                        f"QPushButton:hover{{border-color:{theme.ACCENT};}}"
                    )
                    button.clicked.connect(lambda _checked=False, choice=candidate: self._choose_correction(choice))
                    self._choice_lay.addWidget(button)
                    self._choice_buttons.append(button)
                dismiss = QPushButton("Dismiss — change nothing")
                dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
                dismiss.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                dismiss.setMinimumHeight(32)
                dismiss.setStyleSheet(
                    f"QPushButton{{background:transparent;color:{theme.TEXT_SECONDARY};border:none;"
                    f"padding:4px;font-size:{HINT_FONT_SIZE}px;font-family:'{FONT_FAMILY}';text-align:left;}}"
                    f"QPushButton:hover{{color:{theme.TEXT_PRIMARY};}}"
                )
                dismiss.clicked.connect(self._dismiss_correction_choices)
                self._choice_lay.addWidget(dismiss)
                self._choice_buttons.append(dismiss)
                self._choices.show()
                self._position()

            def hide_correction_choices(self) -> None:
                if self._choices.isVisible():
                    self._choices.hide()
                    self._position()

            def _choose_correction(self, choice: str) -> None:
                callback = self._on_choice
                self.hide_correction_choices()
                if callback is not None:
                    callback(choice)

            def _dismiss_correction_choices(self) -> None:
                callback = self._on_choice_dismiss
                self.hide_correction_choices()
                if callback is not None:
                    callback()

            def _refresh_controls(self) -> None:
                """The controls are visible exactly when the box takes clicks:
                that is the user-visible difference between interactive and
                click-through."""
                if self._idle is None:
                    return
                show = self._interactive and self._has_draft
                if show != self._controls.isVisible():
                    self._controls.setVisible(show)
                    self._position()

            def _on_link(self, href: str) -> None:
                if self._on_word_clicked is None or not href.startswith(WORD_LINK_PREFIX):
                    return
                try:
                    index = int(href[len(WORD_LINK_PREFIX):])
                except ValueError:
                    return
                try:
                    self._on_word_clicked(index)
                except Exception as exc:
                    logger.warning(f"[DICTATE-PREVIEW] word click failed: {exc}")

            def _on_clear_clicked(self) -> None:
                if self._on_clear is None:
                    return
                try:
                    self._on_clear()
                except Exception as exc:
                    logger.warning(f"[DICTATE-PREVIEW] clear button failed: {exc}")

            # ---- idle fade (queue 75), Qt thread only -----------------------

            def enable_idle(self, idle, activity_probe, hints):
                from samsara.ui.splash_qt import _system_reduced_motion
                self._idle = idle
                self._activity_probe = activity_probe
                self._hints = list(hints or ())
                self._reduced_motion = _system_reduced_motion()
                self._life_timer = QTimer(self)     # child: dies with the widget
                self._life_timer.setInterval(IDLE_TICK_MS)
                self._life_timer.timeout.connect(self._life_tick)

            def stop_life(self):
                """Stop the idle timer and drop every outside reference. Called
                by StreamingOverlayQt._dispose_on_qt before hide/deleteLater."""
                if self._life_timer is not None:
                    self._life_timer.stop()
                self._activity_probe = None
                self._hints = []
                # Queue 85: the interaction callbacks reference the preview
                # session; drop them here for the same reason as the probe.
                self._on_word_clicked = None
                self._on_clear = None
                self._on_choice = None
                self._on_choice_dismiss = None

            def _now(self):
                return self._clock.elapsed()

            def _set_opacity(self, value):
                self.setWindowOpacity(max(0.0, min(1.0, value)))

            def _set_click_through(self, on):
                if on == self._click_through:
                    return
                try:
                    set_window_click_through(self, on)
                    self._click_through = on
                except Exception as exc:
                    logger.debug(f"[DICTATE-PREVIEW] click-through {on} failed: {exc}")

            def _hide_hint(self):
                self._hint_shown_ms = None
                if self._hint.isVisible():
                    self._hint.hide()
                    self._position()

            def mark_active(self):
                """Speech, new text or the cursor arriving over the box: full
                opacity in ONE step (the first words are the ones the user
                wants to read), clickable, no hint.

                Queue 104 reuses this unchanged as the hover wake path, on
                purpose -- a box woken by the mouse must be in exactly the
                state a box woken by speech is in, or "Clear draft" would be
                visible in one and not the other."""
                if self._idle is None:
                    return
                self._last_active_ms = self._now()
                self._hide_hint()
                if self._faded or self._fade_started_ms is not None:
                    self._faded = False
                    self._fade_started_ms = None
                    self._set_opacity(self._active_alpha)
                    self._set_click_through(False)
                    self._interactive = True
                    self._refresh_controls()

            def _begin_idle(self, now):
                self._faded = True
                self._idle_started_ms = now
                self._set_click_through(True)
                # Click-through and "click a word" cannot both be true: the
                # controls disappear with the fade, so what you see matches
                # what the window will accept.
                self._interactive = False
                self._refresh_controls()
                target = self._idle.opacity
                if self._reduced_motion:
                    self._set_opacity(target)
                else:
                    self._fade_from, self._fade_to = self.windowOpacity(), target
                    self._fade_started_ms = now

            def _speech_now(self):
                probe = self._activity_probe
                if probe is None:
                    return False
                try:
                    return bool(probe())
                except Exception as exc:
                    logger.debug(f"[DICTATE-PREVIEW] activity probe failed: {exc}")
                    return False

            def _cursor_inside(self):
                """Queue 104. True while the pointer is over the box's screen
                rect -- the preview hit-testing itself, because the OS has
                been told not to (see cursor_screen_pos above).

                Deliberately NOT true when the idle opacity is 0. A fully
                hidden preview is still a rectangle Windows will hit-test the
                moment WS_EX_TRANSPARENT comes off, so waking it on hover
                would materialise an invisible 460 px box under the cursor
                and start it eating the clicks it had been letting through --
                a worse defect than the one being fixed, and invisible to the
                user trying to diagnose it. "Hide the preview completely" is
                an explicit Settings choice; it keeps the pre-104 behaviour,
                where speech is the way back. Same bail-out as _tick_hint.

                Reads frameGeometry(), not geometry(): the frame rect is what
                the OS hit-tests. They are equal for this frameless window,
                so this is a statement of intent rather than a correction.
                """
                if self._idle is None or self._idle.fully_hidden:
                    return False
                try:
                    return self.frameGeometry().contains(cursor_screen_pos())
                except Exception as exc:
                    logger.debug(f"[DICTATE-PREVIEW] cursor probe failed: {exc}")
                    return False

            def _life_tick(self):
                # Queue 104: the isVisible() gate is also what stops the
                # cursor poll running for a hidden box. It is not an
                # optimisation there -- a closed/flashed-out preview that
                # still had a live tick would otherwise keep querying the
                # pointer against a stale rect.
                if self._idle is None or not self.isVisible():
                    return
                now = self._now()
                # Queue 104: hover is activity, and treated as activity on
                # EVERY tick it holds, not just the entering one. That is what
                # makes the box stay awake for as long as the pointer is on it
                # (_last_active_ms keeps moving) and makes the idle countdown
                # restart from the moment the pointer leaves (the last tick
                # inside was the last bump) -- with no enter/leave state of
                # our own to get out of step with the real pointer.
                # `or` short-circuits: no cursor query while speech is live.
                if self._speech_now() or self._cursor_inside():
                    self.mark_active()
                if self._fade_started_ms is not None:
                    fraction = min(1.0, (now - self._fade_started_ms) / IDLE_FADE_MS)
                    self._set_opacity(self._fade_from + (self._fade_to - self._fade_from) * fraction)
                    if fraction >= 1.0:
                        self._fade_started_ms = None
                        self._idle_started_ms = now
                elif not self._faded and now - self._last_active_ms >= self._idle.delay_s * 1000:
                    self._begin_idle(now)
                self._tick_dots(now)
                self._tick_hint(now)

            def _tick_dots(self, now):
                if self._placeholder_prefix is None or self._reduced_motion:
                    return
                dots = 1 + (now // ELLIPSIS_STEP_MS) % 3
                if dots != self._dots_shown:
                    self._dots_shown = dots
                    self._label.setText(self._placeholder_prefix + LISTENING_WORD + "." * dots)

            def _tick_hint(self, now):
                if (not self._faded or self._fade_started_ms is not None or not self._hints
                        or self._idle.fully_hidden):
                    return
                due = (now - self._idle_started_ms >= HINT_FIRST_AFTER_MS if self._hint_shown_ms is None
                       else now - self._hint_shown_ms >= HINT_ROTATE_MS)
                if not due:
                    return
                self._hint_index = (self._hint_index + 1) % len(self._hints)
                self._hint.setText(self._hints[self._hint_index])
                self._hint_shown_ms = now
                if not self._hint.isVisible():
                    self._hint.show()
                    self._position()

            def show_overlay(self):
                self._border.setStyleSheet(f"background:{theme.ACCENT};")
                self._fade_timer.stop()
                self.setWindowOpacity(DIM_ALPHA if self._dim else ALPHA)
                self._fade_alpha = DIM_ALPHA if self._dim else ALPHA
                self._position()
                self.show()
                self.raise_()
                if self._life_timer is not None:
                    self._faded = False
                    self._fade_started_ms = None
                    self._last_active_ms = self._now()
                    self._interactive = True
                    self._refresh_controls()
                    self._life_timer.start()

            def _transcript_height(self) -> int:
                """The height the transcript text actually needs at the box's
                width, measured with the label's own minimum out of the way.

                Queue 90, the cause of BOTH reported defects. Qt's
                QLabel::heightForWidth() returns
                `sizeForWidth(w).expandedTo(minimumSize())`, so feeding its
                answer straight back into setMinimumHeight() -- as queue 85 did
                -- turns the minimum into a RATCHET: every later measurement
                reads back max(text height, previous minimum) and the label can
                only ever get taller. Proof, on a one-line label 460 px wide:
                heightForWidth is 16 with the minimum at 0, 418 with the
                minimum at 418, and 16 again once the minimum is cleared.

                What the owner saw: the box grew to 320 px and stayed there
                after a commit or a clear ("once it goes up it doesn't shrink
                back down"), and the label kept its high-water height with the
                short new draft sitting at the TOP of it -- so following the
                bottom scrolled to the bottom of blank space and the words
                being spoken were above the visible area ("all the things I'm
                saying I can't see, because they're at the top of the window").
                """
                label = self._label
                if label.minimumHeight():
                    label.setMinimumHeight(0)
                return label.heightForWidth(OVERLAY_W - 40)

            def _position(self):
                screen = self._placement_screen()
                if screen is None:
                    return
                scr = screen.availableGeometry()
                hint_h = self._transcript_height()
                if self._idle is not None:
                    # A word-wrapped QLabel's sizeHint ignores the wrapped
                    # height, and QScrollArea sizes its widget by that hint --
                    # so without this the scroll range stays ~0 however long
                    # the draft gets, and the text is simply clipped.
                    self._label.setMinimumHeight(hint_h)
                if self._hint.isVisible():
                    hint_h += self._hint.heightForWidth(OVERLAY_W - 40) + 6
                if self._prompt.isVisible():
                    hint_h += self._prompt.heightForWidth(OVERLAY_W - 40) + 6
                if self._choices.isVisible():
                    hint_h += self._choices.sizeHint().height() + 6
                if self._controls.isVisible():
                    hint_h += self._controls.sizeHint().height() + 4
                # Queue 85: the dictate preview may grow taller before it
                # starts scrolling -- reading the draft back is the point.
                max_h = OVERLAY_MAX_H if self._idle is None else DICTATE_OVERLAY_MAX_H
                req_h  = max(OVERLAY_MIN_H,
                             min(max_h, hint_h + 20))
                self.setFixedHeight(req_h)
                placement = self._preview_position
                if isinstance(placement, tuple):
                    _custom, _screen_name, cx, cy = placement
                    x = round(scr.x() + cx * scr.width() - OVERLAY_W / 2)
                    y = round(scr.y() + cy * scr.height() - req_h / 2)
                else:
                    vertical, horizontal = placement.split("-", 1) if "-" in placement else ("center", placement)
                    x = (scr.left() if horizontal == "left" else
                         scr.right() - OVERLAY_W + 1 if horizontal == "right" else
                         scr.left() + (scr.width() - OVERLAY_W) // 2)
                    y = (scr.top() if vertical == "top" else
                         scr.bottom() - req_h + 1 if vertical == "bottom" else
                         scr.top() + (scr.height() - req_h) // 2)
                    if placement == PREVIEW_POSITION_DEFAULT:
                        y -= TASKBAR_RESERVE + OVERLAY_GAP_ABOVE_TASKBAR
                clamp = (self._clamp_custom_position if isinstance(placement, tuple)
                         else self._clamp_position)
                x, y = clamp(x, y, OVERLAY_W, req_h, scr)
                self.move(x, y)

            def _on_update(self, text, state, text_format="auto"):
                if self._idle is not None:
                    # Queue 90: following is NOT re-derived from the scrollbar
                    # here any more. New text always changes the label's height,
                    # and the layout can move the range before this runs --
                    # reading "value < maximum" at that moment says "the user
                    # scrolled up" when all that happened is that the draft got
                    # taller, and following would never switch back on by
                    # itself. The intent lives in self._follow and is changed
                    # only by a real scroll (_on_scroll_value / scroll_draft).
                    if state == StreamingOverlayQt.STATE_PLACEHOLDER:
                        # A commit or a clear: there is no draft left to be
                        # scrolled up inside, so whatever is said next is
                        # followed again.
                        self._follow = True
                        self._placeholder_prefix = text[:-len(LISTENING_TEXT)] if text.endswith(LISTENING_TEXT) else text
                        self._dots_shown = 3
                    else:
                        self._placeholder_prefix = None
                        if text != self._last_content:
                            # Before the text is set: the hint is gone and the
                            # box is at full opacity when the words appear.
                            self.mark_active()
                        self._last_content = text
                # Queue 55: the preview transcript is escaped HTML. Left on
                # AutoText, Qt's mightBeRichText() guessed PLAIN for a line
                # with no tag, and the escaped apostrophe showed literally
                # as "it&#x27;s". HTML callers now say so explicitly.
                self._label.setTextFormat({
                    "rich": Qt.TextFormat.RichText,
                    "plain": Qt.TextFormat.PlainText,
                }.get(text_format, Qt.TextFormat.AutoText))
                self._label.setText(text)
                if self._idle is not None:
                    # Only a real transcript gets the clear button: the
                    # "Listening..." placeholder is not a draft.
                    self._has_draft = state != StreamingOverlayQt.STATE_PLACEHOLDER and bool(text)
                    self._refresh_controls()
                if state == "done":
                    self._border.setStyleSheet(f"background:{theme.SUCCESS};")
                elif state:
                    self._border.setStyleSheet(f"background:{theme.ACCENT};")
                self._position()

            def _on_flash(self, on_complete):
                self._on_complete = on_complete
                self._border.setStyleSheet(f"background:{theme.SUCCESS};")
                self._fade_alpha = DIM_ALPHA if self._dim else ALPHA
                self._fade_timer.start()

            def _on_close(self):
                self._fade_timer.stop()
                self.stop_life()
                self.hide()
                cb, self._on_complete = self._on_complete, None
                if cb:
                    cb()

            def _fade_step(self):
                self._fade_alpha -= 0.08
                if self._fade_alpha <= 0.05:
                    self._fade_timer.stop()
                    self.hide()
                    cb, self._on_complete = self._on_complete, None
                    if cb:
                        cb()
                    return
                self.setWindowOpacity(max(0.0, self._fade_alpha))

        self._w = _W(dim)
        if idle is not None:
            self._w.enable_idle(idle, activity_probe, hints)

    def show_overlay(self):      self._w.show_overlay()
    def stop_life(self):         self._w.stop_life()
    def update(self, text, st, text_format="auto"):
        self._w._update_sig.emit(text, st or "", text_format)
    def flash(self, cb):         self._w._flash_sig.emit(cb)
    def close(self):             self._w._close_sig.emit()
    # Queue 85. Called on the Qt thread (StreamingOverlayQt posts them).
    def enable_interaction(self, on_word_clicked, on_clear):
        self._w.enable_interaction(on_word_clicked, on_clear)
    def set_correction_choice_callbacks(self, on_choice, on_dismiss):
        # Queue 161 installs these during construction. Keep the wrapper
        # surface complete so entering Hands-Free cannot crash.
        self._w.set_correction_choice_callbacks(on_choice, on_dismiss)
    def set_prompt(self, text):  self._w.set_prompt(text)
    def scroll_draft(self, where): self._w.scroll_draft(where)


def _safe_str(value) -> str:
    """Defensive UTF-8 boundary for preview text (DEFECT 3, 2026-09-11
    live-use report: a curly apostrophe rendered as several garbage
    characters in the preview while the final injected text -- which goes
    through a completely separate paste/clipboard path, never this one --
    was correct). Nothing upstream of set_transcript/on_utterance_final
    contractually promises `str`; if a `bytes` value ever reached here
    (Whisper segment text, a corrections-lookup replacement, anything),
    Python's default str(bytes) repr renders literal "\\xe2\\x80\\x99"-
    style escapes as visible garbage instead of the character they encode
    -- exactly this symptom. Decoding explicitly as UTF-8 here closes that
    class of bug at the render boundary regardless of which upstream step
    it came from."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


#: One "word" of the draft, for click-to-correct. Letters, digits, apostrophes
#: and inner hyphens; surrounding punctuation is never part of the target, so
#: clicking "Ingstar," corrects "Ingstar".
_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)
#: href scheme for a clickable word: "w:<index into word_targets()>".
WORD_LINK_PREFIX = "w:"


def word_targets(finalized_lines) -> list:
    """[(word, occurrence)] for every clickable word of the joined draft, in
    render order. `occurrence` is how many identical words came before it, so
    the session can replace the RIGHT "that" rather than the first one.

    The renderer below walks the fragments with the same regex in the same
    order, so index i here is exactly the word behind href "w:i". Sharing this
    function is what keeps the map and the anchors from drifting apart."""
    targets, counts = [], {}
    for line in finalized_lines or []:
        for match in _WORD_RE.finditer(_safe_str(line) or ""):
            word = match.group(0)
            occurrence = counts.get(word, 0)
            counts[word] = occurrence + 1
            targets.append((word, occurrence))
    return targets


def _join_dictate_fragments(finalized_lines, link_words: bool = False) -> str:
    """Join DictatePreviewSession's finalized fragments into one HTML
    string for StreamingOverlayQt.set_transcript -- see that method's
    DEFECT 2 note. Same-thought fragments join with a single space; an
    explicit embedded newline (a "new line"/"new paragraph" formatting
    token already substituted into a fragment's text) becomes a <br> and
    never gets an extra leading space glued onto whatever follows it.
    """
    joined = ""
    index = 0
    for t in finalized_lines:
        if not t:
            continue
        text = _safe_str(t)
        if link_words:
            # Queue 85: every word is an anchor, so a click identifies one
            # word. Escaping is unchanged -- each piece still goes through
            # html.escape (queue 55: the apostrophe that rendered as "&#x27;"
            # was an AutoText guess, not the escaping), and the anchor carries
            # an explicit colour because Qt would otherwise paint links blue
            # and underline the whole transcript.
            parts, pos = [], 0
            for match in _WORD_RE.finditer(text):
                parts.append(html.escape(text[pos:match.start()]))
                parts.append(
                    f'<a href="{WORD_LINK_PREFIX}{index}" style="color:{theme.TEXT_PRIMARY};'
                    f'text-decoration:none;">{html.escape(match.group(0))}</a>'
                )
                index += 1
                pos = match.end()
            parts.append(html.escape(text[pos:]))
            escaped = "".join(parts)
        else:
            escaped = html.escape(text)
        escaped = escaped.replace("\r\n", "<br>").replace("\n", "<br>")
        if not joined or joined.endswith("<br>"):
            joined += escaped
        else:
            joined += " " + escaped
    return joined


#: Strong references to every live overlay widget. Added and removed ONLY on
#: the Qt thread (StreamingOverlayQt._show_on_qt / _dispose_on_qt), so the
#: Python wrapper of a top-level QWidget can never reach refcount zero -- and
#: have Shiboken delete the C++ widget -- on any other thread. See
#: StreamingOverlayQt's docstring (queue 60).
_LIVE_WIDGETS: "set[_StreamingWidget]" = set()


class StreamingOverlayQt:
    """Thread-safe Qt drop-in for StreamingOverlay.

    Public API is identical and may be called from any thread. Every widget
    operation -- create, update, flash, close, destroy -- is posted to the
    samsara-qt thread and runs there, in posting order.

    Queue 60 (2026-09-14, two process deaths): the DICTATE-lane preview is
    torn down by a mode switch on the hands-free utterance thread. close()
    used to emit a queued close signal and return; the caller then dropped
    the last reference to this object, and Shiboken destroyed the parentless
    top-level QWidget on THAT thread while the Qt thread was still dispatching
    the close/update events queued for it -- "Windows fatal exception: access
    violation" on the Qt thread. Now the only strong reference to a live
    widget is _LIVE_WIDGETS, released on the Qt thread after hide() and
    deleteLater(), so it does not matter which thread drops this object.
    """

    STATE_LISTENING  = "listening"
    STATE_PROCESSING = "processing"
    STATE_DONE       = "done"
    #: Nothing dictated yet: the label is the "Listening..." placeholder whose
    #: dots animate. Styled like STATE_LISTENING; not activity for the fade.
    STATE_PLACEHOLDER = "placeholder"

    def __init__(self, dim: bool = False, idle: "IdleSettings | None" = None,
                 activity_probe=None, preview_position=PREVIEW_POSITION_DEFAULT,
                 on_placement_committed=None):
        self._dim    = dim
        self._widget: "_StreamingWidget | None" = None   # read/written on the Qt thread only
        # Queue 75: set before show(); read on the Qt thread when the widget is
        # created. The probe is called on the Qt thread and must only read
        # plain Python state (never a Qt object of another thread).
        self._idle = idle
        self._activity_probe = activity_probe
        self.idle_hints: list = []
        # Queue 85: set before show(); attached to the widget on the Qt thread.
        # Both are called ON the Qt thread when the user clicks.
        self._on_word_clicked = None
        self._on_clear = None
        self._on_choice = None
        self._on_choice_dismiss = None
        self._preview_position = _preview_position(preview_position)
        self._on_placement_committed = on_placement_committed

    def set_interaction_callbacks(self, on_word_clicked, on_clear) -> None:
        self._on_word_clicked = on_word_clicked
        self._on_clear = on_clear

    def set_correction_choice_callbacks(self, on_choice, on_dismiss) -> None:
        self._on_choice = on_choice
        self._on_choice_dismiss = on_dismiss

    def _new_widget(self) -> "_StreamingWidget":
        widget = _StreamingWidget(self._dim, self._idle, self._activity_probe, self.idle_hints,
                                  preview_position=self._preview_position,
                                  on_placement_committed=self._on_placement_committed)
        if self._on_word_clicked is not None or self._on_clear is not None:
            widget.enable_interaction(self._on_word_clicked, self._on_clear)
        if self._on_choice is not None or self._on_choice_dismiss is not None:
            widget.set_correction_choice_callbacks(self._on_choice, self._on_choice_dismiss)
        return widget

    @staticmethod
    def _post(fn) -> bool:
        """Run fn on the Qt thread (FIFO with every other post). False when
        there is no QApplication (headless tests): nothing to render."""
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QTimer
        qt_app = QApplication.instance()
        if qt_app is None:
            return False
        QTimer.singleShot(0, qt_app, fn)
        return True

    def _show_on_qt(self):
        if self._widget is None:
            self._widget = self._new_widget()
            _LIVE_WIDGETS.add(self._widget)
        self._widget.show_overlay()

    def _dispose_on_qt(self):
        """Qt thread only. hide() + deleteLater(); the strong reference in
        _LIVE_WIDGETS is dropped from the widget's own `destroyed` signal,
        i.e. only once Qt has deleted it. Dropping it here instead could free
        the Python wrapper -- and with it the C++ widget -- synchronously,
        which is fatal when this runs from inside the widget's own slot (a
        fade's completion callback does)."""
        widget, self._widget = self._widget, None
        if widget is None:
            return
        try:
            # Queue 75: the idle timer is a child of the widget, created and
            # started on this thread. Stopped here, before hide/deleteLater, so
            # no tick can run between the close and the deletion, and the
            # activity probe (a reference to the session and app) is dropped
            # now rather than when Qt finally deletes the widget.
            widget.stop_life()
            widget._w.destroyed.connect(functools.partial(_LIVE_WIDGETS.discard, widget))
            widget._w.hide()
            widget._w.deleteLater()
        except Exception as exc:
            logger.warning(f"[STREAM] overlay dispose failed (widget kept alive, hidden if possible): {exc}")

    def _ensure(self):
        """Create the widget on the Qt thread if not already done."""
        def _make():
            if self._widget is None:
                self._widget = self._new_widget()
                _LIVE_WIDGETS.add(self._widget)
        self._post(_make)

    def show(self):
        self._post(self._show_on_qt)

    def update_text(self, text, state=None, *, rich=False):
        """rich=True: `text` is escaped HTML and is rendered as rich text
        unconditionally. Otherwise Qt's AutoText detection applies, as before
        (hold-to-stream partials pass raw transcript text)."""
        text, state, fmt = text or "", state or "", "rich" if rich else "auto"

        def _update():
            if self._widget is not None:
                self._widget.update(text, state, fmt)
        self._post(_update)

    def set_literal_badge(self, on: bool) -> None:
        """Show/hide the "literal" badge while the VERBATIM profile is forced
        by the spoken toggle (see dictation.py's set_verbatim_forced). Stored
        here rather than passed through every call so an in-flight tick
        renders the same badge state as the commit that set it."""
        self._literal_badge = bool(on)

    def _badge_html(self) -> str:
        if not getattr(self, "_literal_badge", False):
            return ""
        return (
            f'<span style="color:{theme.TEXT_SECONDARY};font-style:normal;">'
            f'[literal]</span> '
        )

    def set_prompt(self, text: str) -> None:
        """Queue 85: the correction prompt row ("" hides it)."""
        def _apply():
            if self._widget is not None:
                self._widget.set_prompt(text)
        self._post(_apply)

    def show_correction_choices(self, word: str, candidates, no_match: bool = False) -> None:
        def _apply():
            if self._widget is not None:
                self._widget.show_correction_choices(word, candidates, no_match)
        self._post(_apply)

    def hide_correction_choices(self) -> None:
        def _apply():
            if self._widget is not None:
                self._widget.hide_correction_choices()
        self._post(_apply)

    def scroll_draft(self, where: str) -> None:
        """Queue 85: "top" | "bottom" | "up" | "down". Any thread."""
        def _apply():
            if self._widget is not None:
                self._widget.scroll_draft(where)
        self._post(_apply)

    def move_draft(self, position: str) -> None:
        """Place the preview at a named screen position without activation."""
        if position not in PREVIEW_POSITION_PRESETS:
            return
        self._preview_position = position
        def _apply():
            if self._widget is not None:
                self._widget.set_preview_position(position)
        self._post(_apply)

    def set_transcript(self, finalized_lines, partial, link_words: bool = False):
        """DictatePreviewSession's rolling-transcript renderer: settled
        finalized utterances (plain text) with the current live partial
        appended in a visually distinct (dimmer, italic) style. Minimal
        addition on top of the existing update() plumbing -- no new Qt
        signal/slot pair, just the HTML this builds, always sent with
        rich=True. (Queue 55: it used to rely on QLabel's AutoText guess,
        which read a single finalized line with no <br>/<span> as PLAIN
        text and displayed its escaped apostrophe as "&#x27;".)

        Text is HTML-escaped since it originates from Whisper transcription
        (untrusted-ish free text) -- a spoken "less than 5" or similar must
        never be interpreted as a tag.

        DEFECT 2 (2026-09-11 live-use report): finalized_lines are
        sub-second-pause SEGMENTS of one not-yet-committed dictation (see
        DictatePreviewSession.on_utterance_final), not separate paragraphs
        -- joining every one with <br> rendered ordinary slow speech as a
        column of single words. Joined with a single space instead; a line
        break now appears only where a fragment's own text carries an
        explicit newline/paragraph formatting token (see
        samsara/formatting_tokens.py's "new line"/"new paragraph" -> \\n),
        never merely because it's a separate accumulated fragment.
        """
        finalized_html = _join_dictate_fragments(finalized_lines, link_words=link_words)
        if partial:
            partial_html = (
                f'<span style="color:{theme.TEXT_SECONDARY};font-style:italic;">'
                f'{html.escape(_safe_str(partial))}</span>'
            )
            combined = f"{finalized_html}<br>{partial_html}" if finalized_html else partial_html
        else:
            combined = finalized_html
        badge = self._badge_html()
        if combined:
            self.update_text(badge + combined, self.STATE_LISTENING, rich=True)
        else:
            self.update_text(badge + LISTENING_TEXT, self.STATE_PLACEHOLDER, rich=True)

    def set_paused(self, finalized_lines):
        """hands_free.suspend_on_hold (2026-09-11): the toggle-DICTATE
        preview while a hotkey hold suspends it -- same settled transcript
        as set_transcript, but the trailing status reads "Paused (hold)"
        instead of a live partial, since nothing is being read or decoded
        for the hold's duration. Returns to set_transcript's normal
        "Listening..." once the hold releases."""
        finalized_html = _join_dictate_fragments(finalized_lines)
        paused_html = (
            f'<span style="color:{theme.TEXT_SECONDARY};font-style:italic;">'
            f'Paused (hold)</span>'
        )
        combined = f"{finalized_html}<br>{paused_html}" if finalized_html else paused_html
        self.update_text(combined, self.STATE_LISTENING, rich=True)

    def flash_done_and_fade(self, on_complete):
        """Flash, fade, then dispose the widget (on the Qt thread) and call
        on_complete. A hold-to-stream session never calls close() after a
        fade, so the fade's end is where its widget is released."""
        def _complete():
            self._dispose_on_qt()
            if on_complete:
                on_complete()

        def _flash():
            if self._widget is not None:
                self._widget.flash(_complete)
            else:
                _complete()
        if not self._post(_flash) and on_complete:
            on_complete()

    def close(self):
        """Hide and destroy the widget on the Qt thread. Idempotent, any thread."""
        self._post(self._dispose_on_qt)


class StreamingWorker(threading.Thread):
    """Daemon thread: runs partial Whisper passes, then a final pass on stop."""

    def __init__(self, session):
        super().__init__(daemon=True, name="streaming-worker")
        self._session = session
        self._stop_event = session.stop_event
        self._cancel_event = session.cancel_event

    def run(self):
        try:
            if getattr(self._session, '_cpu_final_only', False):
                # CPU policy: do not spend a second decode pass on partials.
                self._stop_event.wait()
            else:
                self._loop_partials()
        except Exception as e:
            logger.exception(f"[STREAM] Worker partial loop crashed: {e}")
        if self._cancel_event.is_set():
            self._session.on_cancelled()
            return
        try:
            self._final_pass()
        except Exception as e:
            logger.exception(f"[STREAM] Final pass crashed: {e}")
            self._session.on_final(None)

    def _loop_partials(self):
        first = True
        while not self._stop_event.is_set():
            wait_s = FIRST_CHUNK_S if first else CHUNK_INTERVAL_S
            first = False
            if self._stop_event.wait(timeout=wait_s):
                return
            if time.time() - self._session.start_time > MAX_DURATION_S:
                self._session.on_timeout()
                return
            text = self._transcribe_partial()
            # Cancellation can arrive while Whisper owns model_lock. Never
            # publish that stale partial after cancel() has hidden the overlay
            # and released the accumulator for another recording.
            if self._stop_event.is_set() or self._cancel_event.is_set():
                return
            if text:
                logger.debug(f"[STREAM] Partial: {text}")
                self._session.on_partial(text)

    def _transcribe_partial(self):
        app = self._session.app
        lock = app.model_lock
        if not lock.acquire(blocking=False):
            return None
        try:
            audio = self._snapshot_audio()
            if audio is None:
                return None
            params = self._partial_params()
            segments, _ = app.model.transcribe(audio, **params)
            text = "".join(seg.text for seg in segments).strip()
            return self._partial_cleanup(text)
        except Exception as e:
            logger.exception(f"[STREAM] Partial transcribe failed: {e}")
            return None
        finally:
            lock.release()

    def _final_pass(self):
        app = self._session.app
        try:
            # Acquire final audio by stopping the accumulator.  stop_streaming()
            # stops the drain thread and returns all frames atomically -- no race
            # with dictation.py's stop_recording(), which no longer calls it.
            # Done OUTSIDE model_lock: join can take up to 2s on slow threads.
            consumer = getattr(app, '_dictation_consumer', None)
            if consumer is not None and hasattr(consumer, 'stop_streaming'):
                audio = self._session._stop_capture()
            else:
                audio = self._snapshot_audio()   # non-ACE fallback

            if audio is None or audio.size == 0:
                self._session.on_final(None)
                return

            with app.model_lock:
                params = self._final_params()
                t0 = time.time()
                segments, info = app.model.transcribe(audio, **params)
                detected_lang = getattr(info, 'language', None)
                seg_list = list(segments)
                text = "".join(seg.text for seg in seg_list).strip()
                duration_s = len(audio) / app.model_rate
                elapsed_ms = int((time.time() - t0) * 1000)

            try:
                diag_sig = diagnostics.segment_signals(seg_list)
            except Exception as e:
                logger.debug(f"[STREAM] segment signal extraction failed: {e}")
                diag_sig = {}

            if not text:
                self._session.on_final(None, duration_s=duration_s,
                                       elapsed_ms=elapsed_ms)
                return

            diag_corr_start = time.perf_counter()

            try:
                text = app.voice_training_window.apply_corrections(text)
            except Exception as e:
                logger.debug(f"[STREAM] apply_corrections failed: {e}")

            try:
                text = app.process_transcription(text)
            except Exception as e:
                logger.debug(f"[STREAM] process_transcription failed: {e}")

            raw = text
            _cmode = 'verbatim' if getattr(app, '_skip_cleanup', False) else app.config.get('cleanup_mode', 'clean')
            cleaned = clean_text(text, mode=_cmode)
            t_corrections_ms = int((time.perf_counter() - diag_corr_start) * 1000)

            # Smart Corrections (optional LLM cleanup pass) -- streaming
            # gate (off by default; latency-sensitive path). Never blocks
            # output on failure (see smart_correct docs).
            t_smart_ms = -1
            smart_changed = False
            if app.config.get('smart_corrections', {}).get('modes', {}).get('streaming', False):
                diag_smart_start = time.perf_counter()
                text_before_smart = cleaned
                try:
                    cleaned = smart_correct(cleaned, app)
                except Exception as e:
                    logger.debug(f"[STREAM] smart_correct failed: {e}")
                t_smart_ms = int((time.perf_counter() - diag_smart_start) * 1000)
                smart_changed = (cleaned != text_before_smart)

            if app.config.get('add_trailing_space', True):
                cleaned = cleaned + " "

            # Inline formatting tokens ("new line" -> \n, etc.) -- after
            # smart_correct, before delivery/history. Applies to the FINAL
            # streamed text only (see DictationApp._apply_formatting_tokens)
            # -- partials (on_partial/_direct_paste_partial, above) never
            # pass through this method and are deliberately left as raw
            # transcription text.
            cleaned = app._apply_formatting_tokens(cleaned)

            # Diagnostics record -- total measured from transcribe start to
            # just before handoff to on_final (the streaming equivalent of
            # "just before paste").
            try:
                diagnostics.record(diagnostics.DiagRecord(
                    mode="streaming",
                    audio_s=duration_s,
                    model_name=app.config.get('model_size', config_defaults.DEFAULTS['model_size']),
                    device=getattr(app, 'device_type', 'unknown'),
                    compute_type=app.config.get('compute_type', config_defaults.DEFAULTS['compute_type']),
                    t_transcribe_ms=elapsed_ms,
                    t_corrections_ms=t_corrections_ms,
                    t_smart_ms=t_smart_ms,
                    t_total_ms=int((time.time() - t0) * 1000),
                    text=cleaned,
                    smart_changed=smart_changed,
                    language=_languages.describe_diagnostics_language(
                        app.config.get('language', 'en'), detected_lang,
                    ),
                    **diag_sig,
                ), app=app)
            except Exception as e:
                logger.debug(f"[STREAM] diagnostics record failed: {e}")

            self._session.on_final(cleaned, raw_text=raw,
                                   duration_s=duration_s,
                                   elapsed_ms=elapsed_ms)
        except Exception as e:
            logger.exception(f"[STREAM] Final pass error: {e}")
            self._session.on_final(None)

    def _snapshot_audio(self):
        app = self._session.app

        # ACE-04B: use the DictationSessionConsumer's streaming accumulator.
        consumer = getattr(app, '_dictation_consumer', None)
        if consumer is None or not hasattr(consumer, 'snapshot_streaming_audio'):
            return None
        audio = consumer.snapshot_streaming_audio()
        if audio is None or audio.size == 0:
            return None
        if audio.size / app.model_rate < MIN_PARTIAL_AUDIO_S:
            return None
        return audio  # already at model_rate (16kHz) — no resample needed

    def _partial_params(self):
        app = self._session.app
        try:
            prompt = app.voice_training_window.get_initial_prompt()
        except Exception as e:
            logger.debug(f"[STREAM] get_initial_prompt failed: {e}")
            prompt = None
        return {
            'language': _languages.resolve_transcribe_language(app),
            'initial_prompt': prompt,
            'beam_size': PARTIAL_BEAM,
            'vad_filter': False,
            'no_speech_threshold': NO_SPEECH_THRESHOLD,
            'log_prob_threshold': LOG_PROB_THRESHOLD,
            'condition_on_previous_text': False,
            'without_timestamps': True,
            'word_timestamps': False,
            'temperature': 0.0,
        }

    def _final_params(self):
        app = self._session.app
        try:
            params = app.get_transcription_params()
        except Exception as e:
            logger.debug(f"[STREAM] get_transcription_params failed: {e}")
            params = {
                'language': _languages.resolve_transcribe_language(app),
                'initial_prompt': None,
            }
        params = dict(params)
        params['beam_size'] = FINAL_BEAM
        params['vad_filter'] = False
        return params

    def _partial_cleanup(self, text):
        if not text:
            return text
        # Capitalize the first character only -- no filler removal so
        # words don't disappear mid-sentence in the overlay.
        for i, ch in enumerate(text):
            if ch.isalpha():
                return text[:i] + ch.upper() + text[i + 1:]
        return text


class _LiveSurfaceStreamingOverlay:
    """Legacy overlay-shaped adapter; it creates no second top-level window."""

    def __init__(self, partials) -> None:
        self._partials = partials

    def show(self):
        return None

    def update_text(self, text, state=None):
        if state == StreamingOverlayQt.STATE_PROCESSING:
            self._partials.partial(text)
        else:
            self._partials.final(text)

    def close(self):
        self._partials.stop()

    def delivery_finished(self, text):
        self._partials.final(text, delivered=True)

    def flash_done_and_fade(self, on_complete):
        self._partials.stop()
        if on_complete:
            on_complete()


class StreamingSession:
    """Owns one streaming dictation: overlay + worker + state machine.

    States: IDLE -> RECORDING -> STREAMING -> FINALIZING -> PASTING -> IDLE
    Construct in 'hold' mode when config['streaming_mode'] is True. The
    app calls start() right after the audio stream is up, finalize() on
    hotkey release, cancel() on ESC.
    """

    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_STREAMING = "streaming"
    STATE_FINALIZING = "finalizing"
    STATE_PASTING = "pasting"
    STATE_DONE = "done"

    def __init__(self, app):
        self.app = app
        self.start_time = time.time()
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self._state = self.STATE_RECORDING
        self._state_lock = threading.Lock()
        self._parking_enabled = bool((app.config.get('ui', {}) or {}).get(
            'live_surface', {}).get('enabled', False))
        # Surface-enabled holds must stay retractable until their final
        # delivery decision.  Direct partial paste defeats Pause.
        self._direct_paste = (not self._parking_enabled and bool(
            app.config.get('streaming_direct_paste', False)))
        # Whether the last direct-paste operation actually pasted
        # something. Drives the Ctrl+Z undo before each new paste so we
        # replace only our most recent partial, never pre-existing
        # content. Reset to False at session start and after the final
        # replacement.
        self._last_pasted = False
        # Foreground window captured at session creation. We bail out
        # of direct paste if focus changes mid-stream so we don't type
        # into a different app.
        self._target_hwnd = None
        if sys.platform == "win32":
            try:
                self._target_hwnd = _user32.GetForegroundWindow()
            except Exception as e:
                logger.debug(f"[STREAM] GetForegroundWindow failed: {e}")
                self._target_hwnd = None
        # Serializes select+paste between the worker thread (partials),
        # the main Qt thread (final), and the cancel undo thread.
        self._paste_lock = threading.Lock()
        self._live_partials = None
        self._cpu_final_only = False
        controller = getattr(app, 'live_surface', None)
        if controller is not None and (app.config.get('ui', {}) or {}).get(
                'live_surface', {}).get('enabled', False):
            from samsara.live_surface.model import Lane
            from samsara.live_surface.partials import CapturePartials, cpu_final_only
            capture_id = controller.begin_capture(Lane.HOLD)
            self._cpu_final_only = cpu_final_only(app)
            self._live_partials = CapturePartials(
                controller, Lane.HOLD, capture_id, self._cpu_final_only,
            )
            self._overlay = _LiveSurfaceStreamingOverlay(self._live_partials)
        else:
            self._overlay = StreamingOverlayQt(dim=self._direct_paste)
        self._worker = StreamingWorker(self)
        self._last_partial = ""
        self._capture_cleanup_lock = threading.Lock()
        self._capture_cleaned = False
        self._finished_notified = False

    # ---- Public lifecycle (call from main thread) -----------------------

    def start(self):
        """Begin partial loop. Audio stream must already be running."""
        self._overlay.show()
        self._worker.start()
        thread_registry.register(self._worker, "streaming-worker")
        with self._state_lock:
            self._state = self.STATE_STREAMING

    def finalize(self):
        """Hotkey released: worker will run final pass + paste."""
        with self._state_lock:
            if self._state in (self.STATE_FINALIZING, self.STATE_PASTING,
                               self.STATE_DONE):
                return
            self._state = self.STATE_FINALIZING
        self.stop_event.set()

    def cancel(self):
        """Dismiss overlay, no paste, no history. In direct-paste mode,
        also delete whatever partials were typed into the focused app."""
        with self._state_lock:
            if self._state in (self.STATE_DONE,):
                return
            self._state = self.STATE_DONE
        self.cancel_event.set()
        self.stop_event.set()
        # A cancelled worker skips its final pass, so it would otherwise
        # never stop DictationSessionConsumer's streaming accumulator.
        self._discard_capture()
        if self._direct_paste and self._last_pasted:
            thread_registry.spawn("streaming-cancel-undo", self._undo_direct_paste,
                             daemon=True)
        self._overlay.close()
        self._notify_finished()

    # ---- Worker -> session callbacks (called from worker thread) --------

    def on_partial(self, text):
        self._last_partial = text
        self._overlay.update_text(text, StreamingOverlayQt.STATE_PROCESSING)
        if self._direct_paste and text:
            # Runs on the worker thread -- pyautogui + clipboard ops are
            # blocking and must not run on the Tk main thread.
            self._direct_paste_partial(text)

    def on_timeout(self):
        logger.info("[STREAM] Max duration reached -- auto-finalizing")
        self.app._schedule_ui(self._on_timeout_main)

    def on_cancelled(self):
        self._discard_capture()
        self._overlay.close()
        self._notify_finished()

    def on_final(self, final_text, raw_text=None, duration_s=0.0,
                 elapsed_ms=0):
        if self.cancel_event.is_set():
            self.on_cancelled()
            return
        self.app._schedule_ui(self._deliver_final, final_text, raw_text,
                              duration_s, elapsed_ms)

    # ---- Main-thread handlers -------------------------------------------

    def _on_timeout_main(self):
        try:
            if self.app.recording:
                self.app.stop_recording()
        except Exception as e:
            logger.exception(f"[STREAM] Auto-stop after timeout failed: {e}")

    def _deliver_final(self, text, raw_text, duration_s, elapsed_ms):
        with self._state_lock:
            self._state = self.STATE_PASTING
        if not text or not text.strip():
            logger.info("[STREAM] No speech detected")
            if duration_s > MIN_PARTIAL_AUDIO_S:
                try:
                    self.app._log_history(
                        raw_text="",
                        display_text="(no speech detected)",
                        duration_ms=int(duration_s * 1000),
                        mode="streaming",
                        status="empty",
                    )
                except Exception as e:
                    logger.debug(f"[STREAM] _log_history (empty) failed: {e}")
            # In direct-paste mode, the user has partial text typed into
            # the target app -- erase it on no-speech so we leave a clean
            # slate. Off-thread to avoid blocking Tk.
            if self._direct_paste and self._last_pasted:
                thread_registry.spawn("streaming-empty-undo", self._undo_direct_paste,
                                 daemon=True)
            self._overlay.update_text("(no speech)",
                                      StreamingOverlayQt.STATE_DONE)
            self._overlay.flash_done_and_fade(self._mark_done)
            return

        if self._parking_enabled:
            original = self._target_hwnd
            current = None
            if sys.platform == "win32":
                try:
                    current = _user32.GetForegroundWindow()
                except Exception:
                    current = None
            focus_changed = original is None or current != original
            literal_rule = getattr(self.app, "_verbatim_rule", None)
            literal = bool(literal_rule and literal_rule())
            # Without explicit silence-boundary proof, retain the complete
            # final for review.  Do not strip a phrase from prose and paste
            # the remainder on a guess.
            voice_pause = (not literal and text.strip().lower().endswith("pause draft"))
            requested = bool(getattr(self.app, "_hold_park_requested", False))
            manager = getattr(self.app, "_session_mode_manager", None)
            pending = bool(getattr(getattr(manager, "draft_document", None), "text", ""))
            if requested or pending or focus_changed or voice_pause:
                try:
                    manager = self.app._ensure_session_mode_manager()
                    if focus_changed or voice_pause:
                        manager.pause_draft(source=(
                            "focus changed" if focus_changed else "voice review"))
                    manager.stage_parked_hold(text, source=(
                        "pause" if requested else "focus changed" if focus_changed else
                        "voice review" if voice_pause else "append"))
                    # Settle the capture first, then project the entire parked
                    # document. A one-hold preview must not overwrite it.
                    self._overlay.update_text(text.rstrip(), StreamingOverlayQt.STATE_DONE)
                    controller = getattr(self.app, "live_surface", None)
                    if controller is not None:
                        controller.sync_draft(manager)
                    self._overlay.flash_done_and_fade(self._mark_done)
                    return
                except Exception as exc:
                    # Fail closed: a staging error must never turn a parked
                    # result into an external paste.
                    logger.exception("[STREAM] parked final could not be staged: %s", exc)
                    self.cancel_event.set()
                    self._overlay.close()
                    self._notify_finished()
                    return

        # Update overlay to show final text.
        self._overlay.update_text(text.rstrip(),
                                  StreamingOverlayQt.STATE_DONE)
        logger.info(f"[OK] {text}")
        try:
            self.app.play_sound("success")
        except Exception as e:
            logger.debug(f"[STREAM] play_sound('success') failed: {e}")
        try:
            self.app.add_to_history(text.strip(), is_command=False)
        except Exception as e:
            logger.exception(f"[STREAM] add_to_history failed: {e}")
        try:
            self.app._log_history(
                raw_text=raw_text if raw_text is not None else text,
                display_text=text.strip(),
                duration_ms=int(duration_s * 1000),
                mode="streaming",
                status="success",
            )
        except Exception as e:
            logger.debug(f"[STREAM] _log_history (success) failed: {e}")
        try:
            self.app._notify_main_window(text.strip())
        except Exception as e:
            logger.debug(f"[STREAM] _notify_main_window failed: {e}")

        if self.app.config.get('auto_paste', True):
            if self._parking_enabled:
                try:
                    delivered = self.app._paste_preserving_clipboard(text) is not False
                except Exception:
                    logger.exception("[STREAM] Final insertion failed; retaining draft")
                    delivered = False
                if delivered:
                    if isinstance(self._overlay, _LiveSurfaceStreamingOverlay):
                        self._overlay.delivery_finished(text.rstrip())
                else:
                    manager = self.app._ensure_session_mode_manager()
                    manager.stage_parked_hold(text, source="failed delivery")
                    controller = getattr(self.app, "live_surface", None)
                    if controller is not None:
                        controller.sync_draft(manager)
            elif self._direct_paste:
                thread_registry.spawn("streaming-final-paste", self._direct_paste_final,
                                 args=(text,),
                                 daemon=True)
            else:
                self._paste_with_retry(text)

        self._overlay.flash_done_and_fade(self._mark_done)

    def _paste_with_retry(self, text):
        try:
            self.app._paste_preserving_clipboard(text)
            return
        except Exception as e:
            logger.exception(f"[STREAM] Paste failed once: {e} -- retrying in 100ms")
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        QTimer.singleShot(100, QApplication.instance(),
                          lambda: self._paste_retry_then_clipboard(text))

    def _paste_retry_then_clipboard(self, text):
        try:
            self.app._paste_preserving_clipboard(text)
        except Exception as e:
            logger.exception(f"[STREAM] Paste retry failed: {e}")
            if pyperclip is not None:
                try:
                    pyperclip.copy(text)
                    logger.info("[STREAM] Text copied to clipboard -- paste manually")
                except Exception as e:
                    logger.exception(f"[STREAM] Clipboard copy fallback failed: {e}")

    def _mark_done(self):
        with self._state_lock:
            self._state = self.STATE_DONE
        self._notify_finished()

    def _discard_capture(self):
        """Stop and discard the shared streaming accumulator exactly once."""
        self._stop_capture()

    def _stop_capture(self):
        """Stop the accumulator once and return its audio to the final pass."""
        with self._capture_cleanup_lock:
            if self._capture_cleaned:
                return None
            self._capture_cleaned = True
        consumer = getattr(self.app, '_dictation_consumer', None)
        if consumer is not None and hasattr(consumer, 'stop_streaming'):
            try:
                return consumer.stop_streaming()
            except Exception as e:
                logger.exception(f"[STREAM] Capture cleanup failed: {e}")
        return None

    def _notify_finished(self):
        """Release app-level ownership once across repeated close paths."""
        with self._capture_cleanup_lock:
            if self._finished_notified:
                return
            self._finished_notified = True
        callback = getattr(self.app, '_on_streaming_session_finished', None)
        if callback is not None:
            callback(self)

    # ---- Direct-paste helpers (off the Tk main thread) ------------------

    def _direct_paste_partial(self, text):
        """Replace the previously pasted partial with `text` via Ctrl+Z
        undo + Ctrl+V paste. Runs on the worker thread between
        transcribe iterations.

        The CapsLock streaming hotkey does not produce held Ctrl/Shift,
        so the synthesized Ctrl+Z and Ctrl+V chords land cleanly. The
        target app's per-paste undo entry (Notepad / RichEdit / IDEs)
        is what makes this safe -- pre-existing content stays intact
        because we only undo our own paste."""
        text = text.replace('\n', ' ').replace('\r', ' ')
        with self._paste_lock:
            if self.cancel_event.is_set():
                return
            if not self._focus_unchanged():
                self._last_pasted = False
                self.cancel_event.set()
                logger.warning("[STREAM] Focus changed -- aborting direct paste")
                return
            try:
                if self._last_pasted:
                    pyautogui.hotkey('ctrl', 'z')
                    time.sleep(UNDO_SETTLE_S)
                self.app._paste_preserving_clipboard(text)
                self._last_pasted = True
                if PASTE_SETTLE_S:
                    time.sleep(PASTE_SETTLE_S)
                logger.debug(f"[STREAM] Direct paste: {len(text.split())} words")
            except Exception as e:
                logger.exception(f"[STREAM] direct partial paste failed: {e}")

    def _direct_paste_final(self, text):
        """Replace the last partial with the cleaned final text.

        Daemon thread, runs after the user released the hotkey. We
        still release any lingering Ctrl/Shift via SendInput as a
        belt-and-braces guard against a brief release-mid-flight race;
        the safety net is harmless when nothing is held. We never
        re-press the modifiers afterwards -- a synthetic down without a
        matching up corrupts OS state."""
        sanitized = text.replace('\n', ' ').replace('\r', ' ')
        with self._paste_lock:
            _release_held_modifiers()
            try:
                if self._last_pasted:
                    pyautogui.hotkey('ctrl', 'z')
                    time.sleep(UNDO_SETTLE_S)
                self.app._paste_preserving_clipboard(sanitized)
                self._last_pasted = False
                if PASTE_SETTLE_S:
                    time.sleep(PASTE_SETTLE_S)
                logger.debug(f"[STREAM] Direct paste (final): "
                             f"{len(sanitized.split())} words")
            except Exception as e:
                logger.exception(f"[STREAM] direct final paste failed: {e}")
                # Fallback: leave the cleaned text on the clipboard so
                # the user can paste it manually.
                if pyperclip is not None:
                    try:
                        pyperclip.copy(sanitized)
                        logger.info("[STREAM] Text copied to clipboard "
                                    "-- paste manually")
                    except Exception as e:
                        logger.exception(f"[STREAM] Clipboard copy fallback failed: {e}")

    def _undo_direct_paste(self):
        """Cancel: undo the last partial paste so only our text is
        removed and pre-existing content stays intact."""
        with self._paste_lock:
            if not self._last_pasted:
                return
            _release_held_modifiers()
            try:
                pyautogui.hotkey('ctrl', 'z')
                self._last_pasted = False
                logger.info("[STREAM] Direct paste (undo): cleared")
            except Exception as e:
                logger.exception(f"[STREAM] direct paste undo failed: {e}")

    def _focus_unchanged(self):
        """True if the focused window matches the one captured at
        session start. Always True on non-Windows or if we couldn't
        capture the handle -- focus tracking is best-effort."""
        if sys.platform != "win32" or self._target_hwnd is None:
            return True
        try:
            return _user32.GetForegroundWindow() == self._target_hwnd
        except Exception as e:
            logger.debug(f"[STREAM] GetForegroundWindow check failed: {e}")
            return True


# ---------------------------------------------------------------------------
# Toggle-DICTATE streaming preview (session_streaming_preview)
# ---------------------------------------------------------------------------
#
# A PREVIEW-ONLY layer for the hands-free toggle session's DICTATE lane --
# distinct from StreamingSession above (hold-to-stream hotkey feature, its
# own audio source via DictationSessionConsumer.snapshot_streaming_audio()).
# This reuses StreamingOverlayQt for display and the same tail-window /
# non-blocking-model-lock partial-decode shape as StreamingWorker, but:
#
#   - Audio source is WakeConsumer.snapshot_dictate_preview_audio() (the
#     toggle session's own in-progress utterance buffer), not the streaming
#     accumulator.
#   - Injection is COMPLETELY separate and unchanged: dictation.py's
#     _handle_command_mode_utterance still does the one authoritative decode
#     per utterance and pastes on the silence boundary through the existing
#     FIFO worker. Partials computed here are NEVER injected, pasted, or
#     touch the scratch-that stack -- overlay text only.
#   - Params force include_vocabulary=False + language='en', mirroring
#     _handle_command_mode_utterance's own DICTATE/AVA override (c98c677 /
#     the 2026-07-18 AVA-language-forcing follow-up) -- free-form prose,
#     never matched against the command registry.
#   - Display is a PERSISTENT ROLLING TRANSCRIPT, not a per-utterance flash.
#     Unlike StreamingSession (ends on hotkey release -- clear-on-done is
#     correct there), this session CONTINUES across utterances. Clearing on
#     every silence boundary made a natural mid-thought pause erase the
#     words just spoken -- punishing the user for pausing, actively bad for
#     the accessibility case this whole session mode exists for. See
#     on_utterance_final below.

# Rolling-transcript cap: how many recent FINALIZED utterances stay visible
# before the oldest ages out (dropped from the top, never a full wipe).
# Utterance-COUNT, not wall-clock/audio-duration -- deterministic and
# trivial to test, and a toggle-DICTATE utterance is naturally one
# spoken thought (silence-bounded), so "last few thoughts" reads more
# naturally here than "last N seconds" would.
#: Queue 85: the box scrolls now, so the transcript no longer has to throw the
#: user's earlier sentences away to stay on screen ("if you say more than two
#: sentences it starts pushing sentences up and out of the box"). This is only
#: a runaway guard: every fragment here belongs to ONE staged thought, and the
#: list is cleared at each commit.
DICTATE_PREVIEW_TRANSCRIPT_MAX_UTTERANCES = 120


class _LiveSurfaceDictateOverlay:
    """Presentation-compatible, windowless target for ``DictatePreviewSession``.

    It deliberately implements only display methods.  The shared widget owns
    its own intent signals, while the existing session remains the source of
    GPU partial cadence and the authoritative final path.
    """

    def __init__(self, controller, partials):
        self._controller = controller
        self._partials = partials
        self.idle_hints = []

    def show(self):
        self._controller.start()

    def close(self):
        self._partials.stop()

    def set_transcript(self, lines, partial, link_words=True):
        text = "\n".join([str(line) for line in lines if line] + ([partial] if partial else []))
        self._partials.partial(text)

    def set_interaction_callbacks(self, *_args):
        pass

    def set_correction_choice_callbacks(self, *_args):
        pass

    def set_literal_badge(self, _on):
        pass

    def set_prompt(self, _text):
        pass

    def show_correction_choices(self, *_args):
        pass

    def hide_correction_choices(self):
        pass

    def move_draft(self, _placement):
        pass

    def scroll_draft(self, where):
        self._controller.scroll_draft(where)


class DictatePreviewSession:
    """Owns one toggle-session DICTATE-lane preview: overlay + tick thread.

    Constructed fresh each time the session enters DICTATE, torn down on
    lane switch-away or session end -- see dictation.py's
    _ensure_streaming_preview/_release_streaming_preview. Best-effort and
    fully skippable: any failure here must never affect the authoritative
    per-utterance final decode/paste path.
    """

    def __init__(self, app):
        self.app = app
        self._stop_event = threading.Event()
        self._closed = False
        # Queue 75: faint + click-through when nobody is speaking. Read once
        # per DICTATE entry, so a Settings change applies from the next entry.
        self.idle = IdleSettings.from_config(getattr(app, "config", None))
        command_mode = getattr(app, "config", {}).get("command_mode", {})
        saved_placement = command_mode.get("preview_position", PREVIEW_POSITION_DEFAULT)
        self._live_partials = None
        controller = getattr(app, "live_surface", None)
        live_enabled = bool((getattr(app, "config", {}).get("ui", {}) or {}).get(
            "live_surface", {}).get("enabled", False))
        if controller is not None and live_enabled:
            # Keep this session's existing GPU partial ticker and its
            # authoritative final callback, but do not build a second Qt
            # window.  The small adapter below forwards its presentation
            # calls into the one shared surface.
            from samsara.live_surface.model import Lane
            from samsara.live_surface.partials import CapturePartials, cpu_final_only
            self._live_partials = CapturePartials(
                controller, Lane.HANDS_FREE_DICTATE,
                controller.begin_capture(Lane.HANDS_FREE_DICTATE),
                cpu_only=cpu_final_only(app),
            )
            self._overlay = _LiveSurfaceDictateOverlay(controller, self._live_partials)
        else:
            self._overlay = StreamingOverlayQt(dim=False, idle=self.idle,
                                               activity_probe=self._speech_active,
                                               preview_position=saved_placement,
                                               on_placement_committed=self._save_preview_placement)
        # Session-scoped rolling transcript -- see module comment above and
        # on_utterance_final below. A fresh DictatePreviewSession is
        # constructed on every DICTATE re-entry (dictation.py's
        # _ensure_streaming_preview never reuses one across entries), so
        # this is empty for every new session by construction; also
        # reset explicitly in start() so "re-entering DICTATE starts a
        # fresh empty transcript" holds even if that construction contract
        # ever changes.
        self._finalized: list = []
        # Bumped on every on_utterance_final call (a real utterance/commit
        # boundary). _loop captures this before its own slow decode and
        # skips rendering if it's gone stale by the time decode finishes --
        # see _loop's comment below (DEFECT 2026-09-11: a partial that was
        # still mid-decode when a commit landed used to render its
        # now-stale text over the just-cleared transcript).
        self._generation = 0
        # Queue 85: [(word, occurrence)] for the text currently on screen, in
        # render order. Rebuilt by _render from the SAME helper the renderer
        # uses, so href "w:i" and self._words[i] are the same word.
        self._words: list = []
        # True between a word click and the utterance that answers it: that
        # utterance is a replacement, never a new dictation fragment.
        self._correction_armed = False
        self._overlay.set_interaction_callbacks(self._on_word_clicked, self._on_clear_clicked)
        set_choices = getattr(self._overlay, "set_correction_choice_callbacks", None)
        if callable(set_choices):
            set_choices(self._on_candidate_chosen, self._on_candidates_dismissed)

    # ---- Public lifecycle (call from the session/mode-change thread) ----

    def start(self):
        self._finalized = []
        self._generation = 0
        self._words = []
        manager = self._manager()
        if manager is not None:
            try:
                manager.set_correction_capture_fn(self._capture_correction)
                manager.set_draft_scroll_fn(self.scroll_draft)
                manager.set_correction_undo_fn(self._undo_last_correction)
            except Exception as exc:
                logger.debug(f"[DICTATE-PREVIEW] correction/scroll wiring unavailable: {exc}")
        self._show_parked_draft(manager)
        # Read here, on the caller's thread, never on the Qt thread.
        self._overlay.idle_hints = load_idle_hints()
        self._overlay.show()
        # spawn() registers AND starts -- do not call register() again
        # (that would double-enter this thread under a second, -2-suffixed
        # name; see thread_registry.spawn's docstring).
        # CPU-only machines retain the final callback below, but never add a
        # second decoding loop merely to show partial words.
        live_partials = getattr(self, "_live_partials", None)
        if not (live_partials and live_partials.cpu_only):
            thread_registry.spawn("dictate-preview", self._loop, daemon=True)

    def set_literal_badge(self, on: bool) -> None:
        """Forwarded from dictation.py's set_verbatim_forced so the preview
        box shows a "[literal]" badge while the VERBATIM profile is forced by
        the spoken toggle. Best-effort like everything else in this class."""
        try:
            self._overlay.set_literal_badge(on)
            self._render("")
        except Exception as exc:
            logger.debug(f"[DICTATE-PREVIEW] literal badge failed: {exc}")

    def stop(self):
        """Idempotent. Stops the tick thread and closes the overlay."""
        self._closed = True
        self._stop_event.set()
        self._overlay.close()

    def _speech_active(self) -> bool:
        """Activity probe for the idle fade, called on the Qt thread every
        IDLE_TICK_MS. Reads only plain attributes: WakeConsumer sets
        app.is_speaking at the Silero speech onset (well before the first
        partial exists), so the box is back at full opacity as the user starts
        talking, not a second later when text arrives."""
        if self._closed:
            return False
        return bool(getattr(self.app, "is_speaking", False))

    # ---- Queue 85: scroll, clear, click a word to correct it -------------

    def _manager(self):
        """The session's mode manager, or None. Best-effort like the rest of
        this class: the preview must never break the dictation path."""
        try:
            return self.app._ensure_session_mode_manager()
        except Exception as exc:
            logger.debug(f"[DICTATE-PREVIEW] session manager unavailable: {exc}")
            return None

    def _render(self, partial: str = "") -> None:
        """The ONE place the transcript is drawn. Rebuilds the clickable-word
        map from the same fragments in the same order as the renderer, so a
        click always resolves to the word the user actually clicked."""
        lines = list(self._finalized)
        self._words = word_targets(lines)
        self._overlay.set_transcript(lines, partial, link_words=True)

    def scroll_draft(self, where: str = "up") -> None:
        """Move the transcript view or place its box by its named voice preset.
        Safe from any thread (the overlay posts to the Qt thread)."""
        try:
            if where.startswith("move:"):
                self._overlay.move_draft(where.removeprefix("move:"))
            else:
                self._overlay.scroll_draft(where)
        except Exception as exc:
            logger.debug(f"[DICTATE-PREVIEW] scroll {where} failed: {exc}")

    def _save_preview_placement(self, placement: str) -> None:
        """Persist the widget's normalized drag or named voice placement.

        The Qt widget only emits a value; config mutation stays with the app,
        mirroring ListeningIndicator's placement_committed boundary.
        """
        if not isinstance(placement, str):
            return
        config = getattr(self.app, "config", None)
        if not isinstance(config, dict):
            return
        command_mode = dict(config.get("command_mode", {}) or {})
        command_mode["preview_position"] = placement
        updater = getattr(self.app, "update_config_and_save", None)
        try:
            if callable(updater):
                updater({"command_mode": command_mode})
            else:
                config["command_mode"] = command_mode
                save = getattr(self.app, "save_config", None)
                if callable(save):
                    save()
        except Exception as exc:
            logger.debug("[DICTATE-PREVIEW] placement save failed: %s", exc)

    def _on_word_clicked(self, index: int) -> None:
        """A word in the preview was clicked (Qt thread). Arms the correction;
        the NEXT utterance is its replacement. Nothing is changed yet."""
        if self._closed or index < 0 or index >= len(self._words):
            return
        word, occurrence = self._words[index]
        expected = sum(1 for w, _o in self._words if w == word)
        manager = self._manager()
        if manager is None:
            return
        try:
            result = manager.request_word_correction(
                word, occurrence, expected_count=expected, source="preview_click")
        except Exception as exc:
            logger.warning(f"[DICTATE-PREVIEW] could not arm a correction: {exc}")
            return
        if result.get("ok"):
            self._correction_armed = True
            self._set_prompt(CORRECTION_PROMPT.format(word=word))
            candidates = self._correction_candidates(word)
            self._show_correction_choices(word, [word] + candidates, no_match=not candidates)
        else:
            logger.info("[DICTATE-PREVIEW] correction refused: %s", result.get("reason"))

    def _correction_candidates(self, word: str) -> list[str]:
        """Only suggest this person's taught words/corrections, never a
        generic dictionary.  Re-decode alternatives were empirically identical
        on a real clip, so the stored clip is intentionally not decoded here."""
        try:
            from samsara.correction_queue import get_queue, phonetic_neighbours  # noqa: PLC0415
            from samsara.phonetic_wash import get_user_corrections  # noqa: PLC0415
            training = getattr(self.app, "voice_training_window", None)
            terms = list(getattr(training, "custom_vocab", None) or [])
            corrections = getattr(training, "corrections_dict", None) or {}
            if isinstance(corrections, dict):
                terms.extend(corrections.keys())
                terms.extend(corrections.values())
            user_corrections = get_user_corrections() or {}
            terms.extend(user_corrections.keys())
            terms.extend(user_corrections.values())
            terms.extend(get_queue().correction_terms())
            return phonetic_neighbours(word, terms)
        except Exception as exc:
            logger.debug("[DICTATE-PREVIEW] candidate lookup unavailable: %s", exc)
            return []

    def _show_correction_choices(self, word: str, candidates: list[str], no_match: bool) -> None:
        try:
            self._overlay.show_correction_choices(word, candidates, no_match)
        except Exception as exc:
            logger.debug("[DICTATE-PREVIEW] correction choices unavailable: %s", exc)

    def _hide_correction_choices(self) -> None:
        try:
            self._overlay.hide_correction_choices()
        except Exception as exc:
            logger.debug("[DICTATE-PREVIEW] correction choices hide unavailable: %s", exc)

    def _on_candidate_chosen(self, candidate: str) -> None:
        manager = self._manager()
        if manager is None:
            return
        outcome = manager.choose_word_correction(candidate)
        self._correction_armed = manager.pending_word_correction() is not None
        self._resync_from_draft(manager)
        self._refresh_prompt()
        self._hide_correction_choices()
        self._render("")
        logger.info("[DICTATE-PREVIEW] correction choice %r -> %s", candidate,
                    getattr(outcome, "kind", outcome))

    def _on_candidates_dismissed(self) -> None:
        manager = self._manager()
        if manager is not None:
            manager.cancel_word_correction("dismissed")
        self._correction_armed = False
        self._set_prompt("")
        self._hide_correction_choices()

    def _on_clear_clicked(self) -> None:
        """The Clear draft button (Qt thread). Goes through the session's own
        confirm_clear_draft -- the exact path the spoken "scratch everything"
        -> "yes" takes -- so the button and the phrase cannot diverge, and
        queue 84's recovery slot covers the button too."""
        if self._closed:
            return
        manager = self._manager()
        if manager is None:
            return
        try:
            outcome = manager.confirm_clear_draft("clear button")
        except Exception as exc:
            logger.warning(f"[DICTATE-PREVIEW] clear draft failed: {exc}")
            return
        logger.info("[DICTATE-PREVIEW] clear button -> %s", getattr(outcome, "kind", outcome))
        self._finalized = []
        self._set_prompt("")
        self._render("")

    def _resync_from_draft(self, manager=None) -> None:
        """Replace the preview's own fragments with the session's staged draft.
        Used after a word correction: the session edited the draft in place, so
        re-reading it is both simpler and truer than patching the copy here."""
        manager = manager if manager is not None else self._manager()
        buffer = ""
        if manager is not None:
            try:
                buffer = manager.dictate_pending_buffer or ""
            except Exception as exc:
                logger.debug(f"[DICTATE-PREVIEW] draft unreadable: {exc}")
                return
        self._finalized = [buffer] if buffer.strip() else []

    def _show_parked_draft(self, manager=None) -> None:
        """Restore a session-exit draft before accepting new DICTATE text.

        Session exit is deliberately non-destructive: the manager retains the
        staged draft, then its next DICTATE reset restores it.  The preview
        must make that restored state explicit so the next utterance never
        appears to append to an invisible old draft.
        """
        self._resync_from_draft(manager)
        if self._finalized:
            words = len(self._finalized[0].split())
            self._set_prompt(f"{words} words waiting — say finish to paste, or clear")
            self._render("")

    def _undo_last_correction(self) -> dict:
        """"forget that correction": removes an accepted dictionary entry, or
        the newest un-reviewed capture when nothing has been accepted."""
        try:
            from samsara.correction_queue import undo_last_correction  # noqa: PLC0415
            return undo_last_correction(self.app)
        except Exception as exc:
            logger.warning(f"[DICTATE-PREVIEW] correction undo failed: {exc}")
            return {"undone": False, "wrong": "", "right": "", "where": "error"}

    def _refresh_prompt(self) -> None:
        """Keep the "say the replacement" row in step with the session: it
        disappears as soon as the correction is applied, cancelled or expired."""
        manager = self._manager()
        pending = None
        if manager is not None:
            try:
                pending = manager.pending_word_correction()
            except Exception as exc:
                logger.debug(f"[DICTATE-PREVIEW] pending correction unreadable: {exc}")
        self._set_prompt(CORRECTION_PROMPT.format(word=pending["word"]) if pending else "")
        if not pending:
            self._hide_correction_choices()

    def _set_prompt(self, text: str) -> None:
        """Best-effort, like every other overlay call in this class: the
        prompt is a hint, and never worth breaking a dictation utterance for."""
        try:
            self._overlay.set_prompt(text)
        except Exception as exc:
            logger.debug(f"[DICTATE-PREVIEW] prompt unavailable: {exc}")

    def _capture_correction(self, wrong: str, right: str, context: str = "", audio=None):
        """Queue an applied correction FOR REVIEW. Never writes the
        dictionary: a wrong entry there would silently rewrite that word in
        every future dictation, so a human accepts it in the correction
        window first (samsara/correction_queue.py explains the classes)."""
        try:
            from samsara.correction_queue import get_queue, save_audio  # noqa: PLC0415
            # The clip is the utterance Whisper got wrong -- saved only when
            # the word belongs to exactly one staged chunk (see
            # replace_draft_word), and only for a pair that may ever be stored.
            clip = None
            if audio is not None:
                clip = save_audio(audio, wrong)
            result = get_queue().capture(wrong, right, source="preview_click",
                                         context=context, audio=clip)
        except Exception as exc:
            logger.warning(f"[DICTATE-PREVIEW] correction capture failed: {exc}")
            return None
        if not result.get("stored"):
            logger.info('[DICTATE-PREVIEW] "%s" -> "%s" fixed in the draft only (%s)',
                        wrong, right, result.get("reason"))
        return result

    def on_utterance_final(
        self, final_text: str = "", scratch_success: bool = False,
        dictate_committed: bool = False, draft_recovered: bool = False,
    ) -> None:
        """Called from dictation.py after a DICTATE-lane utterance's
        authoritative final decode/dispatch completes, with that utterance's
        actual final text (the same string dispatch_utterance/injection
        used -- NOT a re-decode), whether dispatch_utterance's OWN outcome
        was a successful scratch-that (outcome.kind == "scratch_success"),
        whether it was a successful buffered-DICTATE commit
        (outcome.kind == "dictate_committed"), and whether it put an aborted
        draft back (outcome.kind == "dictate_draft_recovered", queue 88) --
        all the real dispatch outcome, not re-derived from text; see
        _is_control_phrase's
        docstring for why text alone can't tell us this. Appends dictation
        content to the rolling transcript rather than clearing on every
        utterance: the session continues past a pause, so wiping the
        overlay there would erase the words just spoken every time the user
        pauses. The overlay stays open and visible for the whole DICTATE
        lane -- no flash/fade/close on this path (flash_done_and_fade
        remains intact for StreamingSession's own per-recording use).

        Control phrases (scratch that / end / and / a switch word / an Ava
        invocation / an exit phrase) are NOT dictation -- they must never
        show up as transcript lines. Handled here rather than by the caller
        filtering `final_text` before calling, so a refused control word
        (e.g. scratch-that with a stale focus lock -- see
        SessionModeManager._do_scratch_that) is still correctly suppressed
        from the transcript even though nothing was actually undone.

        dictate_committed=True (2026-07-19 dogfooding fix): a successful
        "end"/"and" commit has just pasted the ENTIRE staged thought into
        the target -- every line currently in self._finalized was staged
        text belonging to that now-delivered thought. Leaving them visible
        after a successful commit made the overlay look like the paste
        never happened. Clear here, once, after the (already-suppressed,
        since "end"/"and" are control phrases) append/pop above -- the
        overlay itself stays open for the next buffer, only its finalized
        transcript resets. A REFUSED or FAILED commit (dictate_commit_
        refused / dictate_commit_blocked_focus_lock / dictate_commit_failed)
        must retain the lines -- nothing was actually delivered, so the
        caller passes dictate_committed=False for those outcomes.

        DEFECT 1 (2026-09-11 live-use report): dictate_committed only ever
        reflected outcome.kind == "dictate_committed" -- the explicit
        spoken "end"/"and" or the local commit-key path. But
        SessionModeManager._do_switch also auto-commits a pending DICTATE
        buffer when the user's utterance switches AWAY from DICTATE (e.g.
        "command mode", an Ava invocation) WHILE a thought is staged
        (session_modes.py's _do_switch, ~line 980) -- that commit
        genuinely pastes the thought, but the outcome bubbling back up is
        "mode_switch" (or whatever the switch's own dispatch produced),
        never "dictate_committed", so this caller-supplied flag alone
        cannot see it and the just-delivered lines used to stay stuck in
        the box. Fixed below by ALSO checking the session manager's own
        source of truth (dictate_pending_buffer) after the append/pop
        above: whenever it comes up empty, every line in self._finalized
        has necessarily already been delivered (committed) or never
        existed, so clearing is always correct regardless of which outcome
        kind got us there.
        """
        if self._closed:
            return
        self._generation += 1
        final_text = (final_text or "").strip()
        # getattr: tests/test_streaming_preview_box.py builds this class with
        # __new__ and sets only the fields it needs, so a new attribute must
        # never be assumed present.
        if getattr(self, "_correction_armed", False):
            # Queue 85: a word was clicked, so this utterance was its
            # replacement (or a refusal/cancel of it) -- never a new dictation
            # fragment. The session edited the draft itself; re-read it rather
            # than guess what changed.
            manager = self._manager()
            still_armed = False
            if manager is not None:
                try:
                    still_armed = manager.pending_word_correction() is not None
                except Exception as exc:
                    logger.debug(f"[DICTATE-PREVIEW] pending correction unreadable: {exc}")
            self._correction_armed = still_armed
            if dictate_committed or self._dictate_buffer_is_empty():
                self._finalized = []
            else:
                self._resync_from_draft(manager)
            self._refresh_prompt()
            self._render("")
            return
        if draft_recovered:
            # Queue 88 (2026-09-15 16:19 live incident): "bring back my draft"
            # after a cancel restored the session's staged buffer -- the log
            # and the eventual commit both prove the 53 characters really came
            # back -- but this box is rebuilt empty on every session entry
            # (_release_streaming_preview / _ensure_streaming_preview) and its
            # _finalized list is only ever appended to by THIS method. So the
            # owner heard "Draft back, 10 words", saw a success chip, and
            # watched an empty box: a command reporting success while nothing
            # visible happened. Re-read the session's own draft, exactly as
            # the queue 85 word-correction path above does -- the recovery
            # rewrites the whole buffer, so patching this copy could not be
            # right anyway.
            self._resync_from_draft()
            self._refresh_prompt()
            self._render("")
            return
        if scratch_success:
            # dispatch_utterance's own outcome, not re-derived from text --
            # the real undo already happened, so mirror it by popping the
            # just-finalized line (scratch-that undoes the PREVIOUS
            # utterance, not itself) rather than merely suppressing this
            # one. Nothing to pop is a safe no-op, same as the real
            # _do_scratch_that's own empty-stack case.
            if self._finalized:
                self._finalized.pop()
        elif final_text and not self._is_control_phrase(final_text):
            self._finalized.append(final_text)
            overflow = len(self._finalized) - DICTATE_PREVIEW_TRANSCRIPT_MAX_UTTERANCES
            if overflow > 0:
                del self._finalized[:overflow]
        # Any other control phrase (scratch-that that was REFUSED, commit,
        # switch, Ava invocation, exit phrase) falls through here: not
        # appended, not popped -- correctly a no-op on the transcript,
        # since the real dispatch either did nothing dictation-shaped or
        # (for a refused scratch) genuinely left the prior content in place.
        if dictate_committed or self._dictate_buffer_is_empty():
            self._finalized = []
        #
        # Partial cleared to "": this utterance's partial is now stale --
        # the next tick will produce a fresh one for the NEXT utterance.
        # A copy, not the live list: on_utterance_final runs on the cmd-utt
        # thread while the tick thread (_loop) may be mid-render of a
        # previous snapshot -- handing out the same list object risks the
        # overlay observing an in-place mutation (append/pop/clear below)
        # partway through, making a transcript line appear to vanish.
        self._refresh_prompt()
        self._render("")

    def _dictate_buffer_is_empty(self) -> bool:
        """Robust, caller-independent commit signal: true whenever the
        session's OWN staged-DICTATE buffer is currently empty. Used
        alongside (not instead of) the dictate_committed flag above -- see
        on_utterance_final's DEFECT 1 note -- so a commit that happens as a
        SIDE EFFECT of some other outcome (a mode switch, an Ava
        invocation) still clears the transcript, without on_utterance_final
        having to enumerate every outcome.kind that can trigger one.

        Gated on buffer_dictate_until_commit: this session (dictation.py's
        _ensure_session_mode_manager) always runs buffered, but the
        "pending buffer" concept simply does not exist for a manager built
        without it (e.g. a non-buffered duck-typed manager in a focused
        unit test) -- an always-empty buffer there must NOT be
        misread as "everything was just committed".

        Fails closed (False -- do not clear) on any error, matching this
        module's best-effort/never-affects-the-real-path philosophy."""
        try:
            manager = self.app._ensure_session_mode_manager()
            if not manager.buffer_dictate_until_commit:
                return False
            return not manager.dictate_pending_buffer
        except Exception as e:
            logger.debug(f"[DICTATE-PREVIEW] pending-buffer check unavailable: {e}")
            return False

    def _is_control_phrase(self, text: str) -> bool:
        """True when `text` is a recognized session CONTROL phrase rather
        than dictation content -- reuses the SAME authoritative matchers
        dispatch_utterance() itself checks (session_modes.is_scratch_that /
        is_dictate_commit / match_switch_word / match_ava_invocation, plus
        the session's own configured abort/exit-phrase matcher), so this
        predicate can never drift from what the final path actually treats
        as control. Display-only: never used for dispatch, injection, or
        the scratch-that stack -- see module docstring above.

        "literal <phrase>" needs no special-case exclusion: every one of
        these matchers requires exact/whole-utterance equality (post
        normalize_utterance's filler-word stripping, which does not strip
        "literal"), so "literal scratch that" normalizes to "literal
        scratch that" and never equals "scratch that" -- match_literal_
        payload's own escape hatch falls out of this for free.

        Does NOT re-run passes_switch_anti_hallucination_gate/passes_
        dictate_commit_gate (those need segment-level signals this
        lightweight partial decode doesn't have) -- a control phrase
        spoken on low-confidence audio that the real gate would have
        rejected (and treated as ordinary text) could still be suppressed
        here. Display-only edge case, not the bug this predicate exists
        to fix (confidently-spoken control phrases showing up as
        dictation), and self-correcting: the REAL dispatch outcome always
        wins for what actually happens to the injected text.
        """
        if not text:
            return False
        try:
            manager = self.app._ensure_session_mode_manager()
        except Exception as e:
            logger.debug(f"[DICTATE-PREVIEW] control-phrase check unavailable: {e}")
            return False
        try:
            return bool(
                is_scratch_that(text)
                or is_dictate_commit(text)
                # Queue 88: "bring back my draft" is a control phrase. When
                # there IS a draft, on_utterance_final resyncs the box from
                # the session and this never matters; when there is NOT, the
                # words used to be appended as if the user had dictated them,
                # under a chip that says "nothing to bring back".
                or is_recover_draft(text)
                or match_switch_word(text) is not None
                or match_ava_invocation(text, manager._ava_invocations)
                or manager._matches_abort_phrase(text)
                or self._is_reserved_hands_free_command(manager, text)
            )
        except Exception as e:
            logger.debug(f"[DICTATE-PREVIEW] control-phrase check failed: {e}")
            return False

    @staticmethod
    def _is_reserved_hands_free_command(manager, text: str) -> bool:
        """True when `text` is one of the combined hands-free lane's reserved
        commands ("scroll down", "submit", "click 3", ...).

        DEFECT 1 residue (2026-09-11): these are commands, not dictation, but
        they were the one control route this predicate did not cover, so
        _dispatch_hands_free_command's phrase was appended to the visible
        transcript as if the user had dictated it. For a COMMIT-policy
        command ("submit") that was invisible -- the commit empties the
        pending buffer, so on_utterance_final's clear wiped the bogus line on
        the same call. For a PRESERVE-policy one ("scroll down", "show
        numbers", "page up") nothing clears, so the command's own words sat
        in the box indefinitely, above dictation the user really had spoken.
        A COMMIT command whose commit is BLOCKED (hands_free_command_blocked)
        leaves the same residue for the same reason.

        Mirrors dispatch_utterance's own guard exactly (session_modes.py's
        combined-lane block): the probe only applies in buffered DICTATE, and
        an explicit "literal <phrase>" escape hatch is dictation, not a
        command -- checked first there and here, so "literal scroll down"
        stays visible text. The probe is documented side-effect-free
        (dictation.py's _probe_hands_free_command: "Classify one utterance
        without executing it"), so calling it from this display-only
        predicate cannot execute anything."""
        if not (getattr(manager, 'buffer_dictate_until_commit', False)
                and manager.mode is SessionMode.DICTATE):
            return False
        if match_literal_payload(text) is not None:
            return False
        probe = getattr(manager, '_hands_free_command_probe_fn', None)
        if probe is None:
            return False
        return probe(text) is not None

    # ---- Tick loop (own daemon thread) -----------------------------------

    def _suspended_for_hold(self) -> bool:
        """hands_free.suspend_on_hold (default true, 2026-09-11): true
        while a hotkey hold is in progress -- the tick loop must stop
        reading/decoding entirely for the duration (WakeConsumer's own
        _process_frame independently stops accumulating into
        _utterance_frames; this is this session's OWN read of the ring via
        snapshot_dictate_preview_audio, which has no such gate on its
        own). Fails open (False -- keep ticking) on any error, matching
        this module's best-effort philosophy; the setting defaults on, so
        a lookup failure should not silently disable suspension, but
        neither should it ever crash the tick loop."""
        try:
            if not getattr(self.app, '_hotkey_recording', False):
                return False
            return bool(self.app.config.get('hands_free', {}).get('suspend_on_hold', True))
        except Exception as e:
            logger.debug(f"[DICTATE-PREVIEW] suspend-on-hold check failed: {e}")
            return False

    def _loop(self):
        first = True
        was_suspended = False
        while not self._stop_event.is_set():
            wait_s = FIRST_CHUNK_S if first else CHUNK_INTERVAL_S
            first = False
            if self._stop_event.wait(timeout=wait_s):
                return
            if self._suspended_for_hold():
                was_suspended = self._enter_hold_pause(was_suspended)
                continue   # no reading/decoding at all while suspended
            if was_suspended:
                was_suspended = False
                self._render("")
            # DEFECT 1 (2026-09-11): captured BEFORE the (slow, model-lock-
            # bound) decode below. If a commit lands on the cmd-utt thread
            # while this tick is mid-decode, on_utterance_final bumps
            # _generation; the partial this tick just produced belongs to
            # audio from BEFORE that commit, so rendering it now would
            # paint stale text right back over the transcript
            # on_utterance_final just correctly cleared/updated.
            generation = self._generation
            text = self._transcribe_partial()
            if self._stop_event.is_set() or self._closed:
                return
            # A hold can start WHILE the decode above is running -- and this
            # loop is single-threaded, so it cannot notice the suspension (or
            # bump _generation) until that decode returns. `text` was decoded
            # from audio captured BEFORE the hold, which WakeConsumer has by
            # now already discarded (discard_stale_wake_utterance, called at
            # the instant _hotkey_recording flips True), so emitting it here
            # would paint pre-hold words over a box that must read "Paused
            # (hold)" -- the exact half-utterance the suspension contract
            # says to drop. Re-check after the decode and discard it.
            if self._suspended_for_hold():
                was_suspended = self._enter_hold_pause(was_suspended)
                continue
            if generation != self._generation:
                continue
            # A partial that IS a recognized control phrase is never shown
            # as transcript text -- hold the prior transcript (no new
            # set_transcript call this tick) rather than flashing "Scratch
            # that." as if it were dictated content, same reasoning as
            # on_utterance_final's suppression below.
            if text and not self._is_control_phrase(text):
                # Copy for the same reason as on_utterance_final's call above.
                self._render(text)

    def _enter_hold_pause(self, was_suspended: bool) -> bool:
        """Render the "Paused (hold)" state once per hold; returns the new
        was_suspended flag. Bumping _generation here is what stops any
        partial still in flight from painting over the paused box once its
        decode finally returns."""
        if not was_suspended:
            self._generation += 1
            self._overlay.set_paused(list(self._finalized))
        return True

    def _transcribe_partial(self):
        app = self.app
        lock = app.model_lock
        # Never queue behind a final decode or a hotkey decode -- skip this
        # tick entirely rather than block the model lock.
        if not lock.acquire(blocking=False):
            return None
        try:
            audio = self._snapshot_audio()
            if audio is None:
                return None
            params = self._partial_params()
            segments, info = app.model.transcribe(audio, **params)
            text = "".join(seg.text for seg in segments).strip()
            # Partials are provisional: validate before display, but only the
            # final accepted decode learns languages or plays refusal feedback.
            return app._filter_dictation_language(text, info, remember=False, feedback=False)
        except Exception as e:
            logger.exception(f"[DICTATE-PREVIEW] Partial transcribe failed: {e}")
            return None
        finally:
            lock.release()

    def _snapshot_audio(self):
        app = self.app
        consumer = getattr(app, "_wake_consumer", None)
        if consumer is None or not hasattr(consumer, "snapshot_dictate_preview_audio"):
            return None
        audio = consumer.snapshot_dictate_preview_audio()
        if audio is None or audio.size == 0:
            return None
        # WakeConsumer's ring rate (samsara.audio_engine.frame.SAMPLE_RATE)
        # and app.model_rate (samsara.constants.MODEL_SAMPLE_RATE) are both
        # [LOCKED] at 16000 -- no resample step needed here, unlike
        # _handle_command_mode_utterance's generic src_rate parameter
        # (shared across callers that are NOT always at the ring rate).
        if audio.size / app.model_rate < MIN_PARTIAL_AUDIO_S:
            return None
        return audio

    def _partial_params(self):
        app = self.app
        try:
            # include_vocabulary=False: same free-form-prose rule
            # _handle_command_mode_utterance applies to this lane (see
            # module docstring above). Per-call-site override, NOT a change
            # to get_transcription_params' shared base_params.
            params = dict(app.get_transcription_params(include_vocabulary=False))
        except Exception as e:
            logger.debug(f"[DICTATE-PREVIEW] get_transcription_params failed: {e}")
            params = {"language": "en", "initial_prompt": None}
        params.update({
            "language": "en",
            "beam_size": PARTIAL_BEAM,
            "vad_filter": False,
            "no_speech_threshold": NO_SPEECH_THRESHOLD,
            "log_prob_threshold": LOG_PROB_THRESHOLD,
            "condition_on_previous_text": False,
            "without_timestamps": True,
            "word_timestamps": False,
            "temperature": 0.0,
        })
        return params
