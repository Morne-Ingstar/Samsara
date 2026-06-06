"""Show Numbers overlay — semantic voice clicking via numbered labels.

Commands:
    "show numbers"            -- draw numbered labels on clickable UI elements
    "hide numbers"            -- dismiss the overlay
    "refresh numbers"         -- re-enumerate and redraw without dismissing first
    "click N"                 -- left-click element N
    "click N twice"           -- double-click element N
    "click N right"           -- right-click element N
    "ctrl click N"            -- Ctrl+click element N
    "shift click N"           -- Shift+click element N
    "alt click N"             -- Alt+click element N
    "shift click N right"     -- Shift+right-click (modifiers combinable)
    "click thirty seven"      -- spoken numbers also accepted

Architecture:
    Fullscreen Qt widget (FramelessWindowHint, WA_TranslucentBackground,
    WindowTransparentForInput) renders numbered pill labels via QPainter.
    Physical mouse clicks pass through. Does not steal focus.

    Threading model:
      - UIA enumeration runs on the plugin worker thread (COM is thread-safe)
      - Qt window create/update runs on the Qt main thread via QTimer.singleShot
      - Foreground-window poll is a QTimer on the Qt main thread
      - Auto-dismiss uses threading.Timer (any thread) -> QTimer.singleShot -> Qt
"""

import logging
import re
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from samsara.plugin_commands import command
from samsara.ui import qt_runtime
from samsara.ui.numbers_overlay_qt import NumbersOverlayWindow, phys_to_logical, _COORD_DEBUG

logger = logging.getLogger(__name__)

try:
    import uiautomation as auto
    _UIA_AVAILABLE = True
except ImportError:
    auto = None
    _UIA_AVAILABLE = False
    logger.warning("[OVERLAY] uiautomation not available -- win32 fallback active")

# ---------------------------------------------------------------------------
# Module-level state  (one overlay at a time)
# ---------------------------------------------------------------------------

_state_lock     = threading.RLock()
_elements: list = []                             # index 0 -> label "1"
_dismiss_timer: "threading.Timer | None" = None
_overlay_window: "NumbersOverlayWindow | None" = None   # Qt thread only
_fg_timer: "QTimer | None" = None                       # Qt thread only
_fg_hwnd_at_show: int = 0

_enum_cache: "dict | None" = None   # {'hwnd': int, 't': float, 'elements': list}
_CACHE_TTL      = 10.0   # seconds
_AUTO_DISMISS_S = 30
_FG_POLL_MS     = 2000

# ---------------------------------------------------------------------------
# Pill geometry
# ---------------------------------------------------------------------------

_PILL_H = 22

def _pill_w(text: str) -> int:
    return 28 if len(text) == 1 else 36

# ---------------------------------------------------------------------------
# TTS helper (mirrors alarm_commands._speak)
# ---------------------------------------------------------------------------

def _speak(app, text: str) -> None:
    if hasattr(app, "audio_coordinator") and app.audio_coordinator:
        app.audio_coordinator.speak(text, category="agent_response", interruptible=False)
    elif hasattr(app, "tts_engine") and app.tts_engine:
        app.tts_engine.speak(text)
    else:
        print(f"[OVERLAY] {text}")

# ---------------------------------------------------------------------------
# Clickable types accepted by the filter
# ---------------------------------------------------------------------------

_CLICKABLE_TYPES = frozenset({
    'ButtonControl',
    'HyperlinkControl',
    'MenuItemControl',
    'TabItemControl',
    'CheckBoxControl',
    'RadioButtonControl',
    'ListItemControl',
    'ComboBoxControl',
    'EditControl',
    'SplitButtonControl',
    'TreeItemControl',
})

# ---------------------------------------------------------------------------
# UIA helpers
# ---------------------------------------------------------------------------

def _rect_intersects(uia_rect, screen_rect) -> bool:
    """True if uia_rect overlaps the (l, t, r, b) screen_rect tuple."""
    l2, t2, r2, b2 = screen_rect
    return not (
        uia_rect.right <= l2
        or uia_rect.left >= r2
        or uia_rect.bottom <= t2
        or uia_rect.top >= b2
    )


def _is_useful_clickable(control, parent_rect_screen) -> bool:
    """True if control deserves a numbered label."""
    if control.ControlTypeName not in _CLICKABLE_TYPES:
        return False

    rect = control.BoundingRectangle
    w, h = rect.width(), rect.height()

    if w < 8 or h < 8:
        return False
    if w > 1500 or h > 1000:
        return False
    if not _rect_intersects(rect, parent_rect_screen):
        return False
    try:
        if not control.IsEnabled:
            return False
    except Exception:
        pass
    return True


def _enumerate_foreground_clickables() -> list:
    """Walk the foreground window's UIA subtree; return useful clickables.

    Safe to call from a worker thread -- UIA is COM, not Qt.
    """
    if not _UIA_AVAILABLE:
        return _enumerate_win32_fallback()

    fg = auto.GetForegroundControl()
    if fg is None:
        return []

    fg_rect = fg.BoundingRectangle
    fg_screen = (fg_rect.left, fg_rect.top, fg_rect.right, fg_rect.bottom)

    results = []

    def _walk(ctrl, depth=0):
        if depth > 12 or len(results) >= 99:
            return
        try:
            if _is_useful_clickable(ctrl, fg_screen):
                r = ctrl.BoundingRectangle
                if _COORD_DEBUG and len(results) < 5:
                    logger.debug(
                        "[DPI-COORD] elem %d raw UIA: left=%d top=%d right=%d bottom=%d "
                        "(%s '%s')",
                        len(results) + 1,
                        r.left, r.top, r.right, r.bottom,
                        ctrl.ControlTypeName, (ctrl.Name or '')[:30],
                    )
                lx1, ly1 = phys_to_logical(r.left, r.top)
                lx2, ly2 = phys_to_logical(r.right, r.bottom)
                if _COORD_DEBUG and len(results) < 5:
                    logger.debug(
                        "[DPI-COORD] elem %d after conversion: (%d,%d) -> (%d,%d)",
                        len(results) + 1, r.left, r.top, lx1, ly1,
                    )
                results.append({
                    'control': ctrl,
                    'rect': (lx1, ly1, lx2, ly2),
                    'name': ctrl.Name or '',
                    'type': ctrl.ControlTypeName,
                })
        except Exception:
            pass
        try:
            for child in ctrl.GetChildren():
                _walk(child, depth + 1)
        except Exception:
            pass

    _walk(fg)
    return results

# ---------------------------------------------------------------------------
# Win32 fallback (when uiautomation not installed)
# ---------------------------------------------------------------------------

class _Win32Rect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b

    def width(self):  return self.right - self.left
    def height(self): return self.bottom - self.top


class _Win32Control:
    def __init__(self, hwnd, rect):
        self._hwnd = hwnd
        l, t, r, b = rect
        self.BoundingRectangle = _Win32Rect(l, t, r, b)
        self.IsEnabled = True

    def _center(self):
        br = self.BoundingRectangle
        return (br.left + br.right) // 2, (br.top + br.bottom) // 2

    def Click(self, simulateMove=True):
        self._send(left=True, double=False)

    def DoubleClick(self, simulateMove=True):
        self._send(left=True, double=True)

    def RightClick(self, simulateMove=True):
        self._send(left=False, double=False)

    def _send(self, left: bool, double: bool):
        import time as _time
        import win32api, win32con
        x, y = self._center()
        win32api.SetCursorPos((x, y))
        if left:
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            if double:
                _time.sleep(0.05)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        else:
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)


def _enumerate_win32_fallback() -> list:
    try:
        import win32gui
    except ImportError:
        logger.error("[OVERLAY] Neither uiautomation nor win32gui available")
        return []

    fg_hwnd = win32gui.GetForegroundWindow()
    if not fg_hwnd:
        return []

    _CLASSES = {'button', 'edit', 'combobox', 'listbox', 'syslink',
                'syslistview32', 'systreeview32', 'systabcontrol32'}
    results = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        cls = win32gui.GetClassName(hwnd).lower()
        if any(c in cls for c in _CLASSES):
            try:
                rect = win32gui.GetWindowRect(hwnd)
                l, t, r, b = rect
                if r - l > 0 and b - t > 0:
                    ll, lt = phys_to_logical(l, t)
                    lr, lb = phys_to_logical(r, b)
                    results.append({
                        'control': _Win32Control(hwnd, rect),
                        'rect': (ll, lt, lr, lb),
                        'name': win32gui.GetWindowText(hwnd),
                        'type': cls,
                    })
            except Exception:
                pass
        return True

    try:
        win32gui.EnumChildWindows(fg_hwnd, _cb, None)
    except Exception as e:
        logger.error("[OVERLAY] EnumChildWindows failed: %s", e)
    return results

# ---------------------------------------------------------------------------
# Element enumeration cache
# ---------------------------------------------------------------------------

def _cached_enumerate() -> list:
    global _enum_cache
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
    except Exception:
        hwnd = 0

    now = time.time()
    if (
        _enum_cache is not None
        and _enum_cache['hwnd'] == hwnd
        and now - _enum_cache['t'] < _CACHE_TTL
    ):
        print("[OVERLAY] Using cached element list")
        return list(_enum_cache['elements'])

    elements = _enumerate_foreground_clickables()
    if hwnd and elements:
        _enum_cache = {'hwnd': hwnd, 't': now, 'elements': elements}
    return elements


def _invalidate_cache() -> None:
    global _enum_cache
    _enum_cache = None

# ---------------------------------------------------------------------------
# Collision avoidance
# ---------------------------------------------------------------------------

def _place_labels(elements: list) -> list:
    """Compute pill positions with downward nudging to avoid overlaps.

    Returns list of [screen_x, screen_y, pill_w, pill_h, text].
    """
    placed = []
    for i, e in enumerate(elements, 1):
        rx, ry = e['rect'][0], e['rect'][1]
        text   = str(i)
        pw, ph = _pill_w(text), _PILL_H

        for step in range(20):
            cy = ry + step * (ph + 2)
            collision = any(
                rx < px + ppw and rx + pw > px and cy < py + pph and cy + ph > py
                for px, py, ppw, pph, _ in placed
            )
            if not collision:
                placed.append([rx, cy, pw, ph, text])
                break
        else:
            placed.append([rx, ry, pw, ph, text])   # accept overlap after 20 attempts
    return placed

# ---------------------------------------------------------------------------
# Click execution
# ---------------------------------------------------------------------------

_KEY_VK_MAP = {'shift': 0x10, 'ctrl': 0x11, 'alt': 0x12}  # VK_SHIFT/CONTROL/MENU


def _apply_modifier_keys(keys: frozenset, fn) -> None:
    """Hold keyboard modifier keys, call fn(), release them. No-op if keys empty."""
    if not keys:
        fn()
        return
    import win32api
    KEYEVENTF_KEYUP = 0x0002
    pressed = [_KEY_VK_MAP[k] for k in sorted(keys) if k in _KEY_VK_MAP]
    for vk in pressed:
        win32api.keybd_event(vk, 0, 0, 0)
    try:
        fn()
    finally:
        for vk in reversed(pressed):
            win32api.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _click_with_validation(element, modifier: str, keys: frozenset = frozenset()) -> bool:
    """Validate element is still alive, then click. Returns True on success."""
    try:
        rect = element.BoundingRectangle
        if rect.width() <= 0 or rect.height() <= 0:
            logger.warning("[OVERLAY] Element no longer valid -- UI changed")
            return False
        if not element.IsEnabled:
            logger.warning("[OVERLAY] Element disabled -- UI changed")
            return False
    except Exception:
        logger.warning("[OVERLAY] Element handle stale -- UI changed")
        return False

    return _perform_click(element, modifier, keys)


def _perform_click(element, modifier: str, keys: frozenset = frozenset()) -> bool:
    """UIA-first click, then Win32 fallback. keys holds modifier key names to hold."""
    try:
        if modifier == 'double':
            _apply_modifier_keys(keys, lambda: element.DoubleClick(simulateMove=False))
        elif modifier == 'right':
            _apply_modifier_keys(keys, lambda: element.RightClick(simulateMove=False))
        else:
            _apply_modifier_keys(keys, lambda: element.Click(simulateMove=False))
        return True
    except Exception as e:
        logger.info("[OVERLAY] UIA click failed (%s), falling back to mouse", e)

    try:
        import win32api, win32con
        rect = element.BoundingRectangle
        x = (rect.left + rect.right) // 2
        y = (rect.top + rect.bottom) // 2
        win32api.SetCursorPos((x, y))

        def _do_mouse():
            if modifier == 'right':
                win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
                win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            else:
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                if modifier == 'double':
                    time.sleep(0.05)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

        _apply_modifier_keys(keys, _do_mouse)
        return True
    except Exception as e:
        logger.error("[OVERLAY] Win32 fallback click failed: %s", e)
        return False

# ---------------------------------------------------------------------------
# Qt overlay -- all functions below must run on the Qt main thread
# unless otherwise noted
# ---------------------------------------------------------------------------

def _show_overlay_qt(labels: list, fg_hwnd: int, app) -> None:
    """Create or update the overlay window. Qt main thread only."""
    global _overlay_window, _fg_timer, _fg_hwnd_at_show

    print(f"[OVERLAY] _show_overlay_qt called on Qt thread — {len(labels)} label(s)")
    _fg_hwnd_at_show = fg_hwnd

    if _overlay_window is not None and not _overlay_window.isHidden():
        _overlay_window.update_labels(labels)
    else:
        if _overlay_window is not None:
            _overlay_window.close()
        _overlay_window = NumbersOverlayWindow(labels)
        _overlay_window.showNoActivate() if hasattr(_overlay_window, 'showNoActivate') else _overlay_window.show()

    # (Re-)start foreground poll
    _stop_fg_timer_qt()
    _fg_timer = QTimer()
    _fg_timer.setInterval(_FG_POLL_MS)
    _fg_timer.timeout.connect(lambda: _fg_poll_qt(app))
    _fg_timer.start()


def _hide_overlay_qt() -> None:
    """Close the overlay window and stop the fg timer. Qt main thread only."""
    global _overlay_window
    _stop_fg_timer_qt()
    if _overlay_window is not None:
        _overlay_window.close()
        _overlay_window = None
    with _state_lock:
        _elements.clear()


def _stop_fg_timer_qt() -> None:
    """Stop and release the foreground poll timer. Qt main thread only."""
    global _fg_timer
    if _fg_timer is not None:
        _fg_timer.stop()
        _fg_timer.deleteLater()
        _fg_timer = None


def _fg_poll_qt(app) -> None:
    """Periodic foreground-HWND check. Qt main thread only."""
    if _overlay_window is None or _overlay_window.isHidden():
        _stop_fg_timer_qt()
        return
    try:
        import win32gui
        cur = win32gui.GetForegroundWindow()
        if _fg_hwnd_at_show and cur != _fg_hwnd_at_show:
            print("[OVERLAY] Auto-dismissed: foreground window changed")
            _destroy_overlay(app)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Auto-dismiss + overlay lifecycle (thread-safe)
# ---------------------------------------------------------------------------

def _cancel_dismiss_timer() -> None:
    global _dismiss_timer
    if _dismiss_timer is not None:
        try:
            _dismiss_timer.cancel()
        except Exception:
            pass
        _dismiss_timer = None


def _schedule_dismiss_timer(app, seconds: int = _AUTO_DISMISS_S) -> None:
    _cancel_dismiss_timer()
    global _dismiss_timer

    def _fire():
        print("[OVERLAY] Auto-dismissed after timeout")
        _destroy_overlay(app)

    t = threading.Timer(seconds, _fire)
    t.daemon = True
    t.start()
    _dismiss_timer = t


def _draw_overlay(app, elements: list) -> None:
    """Build label list and show the Qt overlay. Safe to call from any thread."""
    global _fg_hwnd_at_show

    labels = _place_labels(elements)

    if _COORD_DEBUG:
        for lbl in labels[:5]:
            logger.debug(
                "[DPI-COORD] pill: screen_x=%d screen_y=%d w=%d h=%d label=%s",
                lbl[0], lbl[1], lbl[2], lbl[3], lbl[4],
            )

    with _state_lock:
        _elements[:] = [e['control'] for e in elements]

    try:
        import win32gui
        fg_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        fg_hwnd = 0

    _cancel_dismiss_timer()
    qt_runtime.post(lambda: _show_overlay_qt(labels, fg_hwnd, app))
    _schedule_dismiss_timer(app)
    print(f"[OVERLAY] Showing {len(elements)} numbered clickables")


def _destroy_overlay(app=None) -> None:
    """Thread-safe dismiss: cancel timers and schedule Qt cleanup."""
    _cancel_dismiss_timer()
    qt_runtime.post(_hide_overlay_qt)


def _destroy_overlay_completely() -> None:
    """Full teardown -- call on app quit."""
    _destroy_overlay()

# ---------------------------------------------------------------------------
# Spoken number parsing
# ---------------------------------------------------------------------------

_WORD_TO_NUM = {
    'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40,
    'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90,
}


def _parse_spoken_number(text: str) -> "int | None":
    """Parse a spoken number 0-99 from text. Returns None if not found.

    Handles:
      - Digits:   "7", "37"
      - Words:    "seven", "twelve"
      - Compound: "thirty seven", "ninety nine"
    """
    text = text.lower().strip()
    if not text:
        return None

    m = re.search(r'\b(\d{1,2})\b', text)
    if m:
        n = int(m.group(1))
        return n if 0 <= n <= 99 else None

    total = 0
    found = False
    for token in text.split():
        if token in _WORD_TO_NUM:
            total += _WORD_TO_NUM[token]
            found = True
    return total if found and 0 <= total <= 99 else None


def _parse_click_target(text: str):
    """Legacy parser: returns (number | None, modifier_str, keys_frozenset).

    modifier is 'single' | 'double' | 'right'.
    keys is a frozenset of 'shift' | 'ctrl' | 'alt'.
    """
    text = re.sub(r'^(click|tap|press|select)\s+', '', text.strip().lower())

    keys: set = set()
    for kw in ('shift', 'ctrl', 'alt'):
        if re.search(r'\b' + kw + r'\b', text):
            keys.add(kw)
            text = re.sub(r'\b' + kw + r'\b', '', text)

    modifier = 'single'
    if re.search(r'\b(twice|double)\b', text):
        modifier = 'double'
        text = re.sub(r'\b(twice|double)\b', '', text)
    elif re.search(r'\bright\b', text):
        modifier = 'right'
        text = re.sub(r'\bright\b', '', text)

    return _parse_spoken_number(text), modifier, frozenset(keys)

# ---------------------------------------------------------------------------
# Voice commands
# ---------------------------------------------------------------------------

@command("show overlay test",
         aliases=["overlay test", "show numbers debug"],
         pack="accessibility")
def handle_show_overlay_test(app, remainder):
    """Phase-1 diagnostic: render 4 hardcoded labels, bypassing all UIA/enumeration code.

    Logs Qt event-loop state so the root cause of a blank overlay is visible.
    Keep this command in place — it costs nothing and confirms the renderer works
    independently of element enumeration.
    """
    import threading
    from PySide6.QtCore import QThread

    qt_app = QApplication.instance()
    print(f"[OVERLAY-TEST] QApplication.instance() = {qt_app}")
    if qt_app is not None:
        qt_thread = qt_app.thread()
        cur_thread = QThread.currentThread()
        on_qt_thread = qt_thread is cur_thread
        print(
            f"[OVERLAY-TEST] Qt-app thread id={int(qt_thread.currentThreadId())}  "
            f"current thread id={int(cur_thread.currentThreadId())}  "
            f"on-Qt-thread={on_qt_thread}"
        )
        print(
            f"[OVERLAY-TEST] Python thread: {threading.current_thread().name!r}"
        )
        if on_qt_thread:
            print("[OVERLAY-TEST] Called from Qt thread — singleShot will fire")
        else:
            print(
                "[OVERLAY-TEST] Called from worker thread — singleShot(0, qt_app, cb) "
                "routes to Qt thread via running event loop"
            )
    else:
        print("[OVERLAY-TEST] WARNING: No QApplication — Qt event loop not running!")

    # Hardcoded labels at fixed logical coordinates; bypasses UIA, placement,
    # DPI conversion, and the entire element enumeration stack.
    labels = [
        [100, 100, 40, 30, "1"],
        [200, 100, 40, 30, "2"],
        [300, 100, 40, 30, "3"],
        [400, 100, 40, 30, "4"],
    ]

    try:
        import win32gui
        fg_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        fg_hwnd = 0

    qt_runtime.post(lambda: _show_overlay_qt(labels, fg_hwnd, app))
    print("[OVERLAY-TEST] Queued 4 test labels — watch for [OVERLAY] _show_overlay_qt called")
    return True


@command("show numbers",
         aliases=["show clickable", "show labels", "label things"],
         pack="accessibility")
def handle_show_numbers(app, remainder):
    """Enumerate clickable elements then draw overlay."""
    elements = _cached_enumerate()
    if not elements:
        print("[OVERLAY] No clickable elements found in foreground window")
        return True
    _draw_overlay(app, elements)
    if hasattr(app, 'hints'):
        app.hints.increment('show_numbers_used')
    return True


@command("hide numbers",
         aliases=["dismiss numbers", "hide labels", "clear labels"],
         pack="accessibility")
def handle_hide_numbers(app, remainder):
    _destroy_overlay(app)
    return True


@command("refresh numbers",
         aliases=["update numbers"],
         pack="accessibility")
def handle_refresh_numbers(app, remainder):
    """Re-enumerate the foreground window and redraw without dismissing first."""
    _invalidate_cache()
    elements = _enumerate_foreground_clickables()
    if not elements:
        print("[OVERLAY] No clickable elements found -- overlay unchanged")
        return True
    _draw_overlay(app, elements)
    return True


@command("click",
         aliases=["tap", "press"],
         pack="accessibility")
def handle_click(app, remainder):
    """Usage: 'click 7', 'click thirty seven', 'click 7 twice', 'click 7 right',
              'ctrl click 7', 'shift click 7', 'alt click 7', 'shift click 7 right'."""
    if not remainder or not remainder.strip():
        return True

    text = remainder.strip().lower()

    keys: set = set()
    for kw in ('shift', 'ctrl', 'alt'):
        if re.search(r'\b' + kw + r'\b', text):
            keys.add(kw)
            text = re.sub(r'\b' + kw + r'\b', '', text)
    keys_frozen = frozenset(keys)

    modifier = 'single'
    if re.search(r'\b(twice|double)\b', text):
        modifier = 'double'
        text = re.sub(r'\b(twice|double)\b', '', text)
    elif re.search(r'\bright\b', text):
        modifier = 'right'
        text = re.sub(r'\bright\b', '', text)

    text = re.sub(r'^(click|tap|press|select)\s+', '', text.strip())

    number = _parse_spoken_number(text)
    if number is None:
        print(f"[OVERLAY] Couldn't parse number from: {remainder!r}")
        return True

    with _state_lock:
        if not _elements:
            print("[OVERLAY] No overlay active -- use 'show numbers' first")
            return True
        if number < 1 or number > len(_elements):
            msg = f"Element {number} is not available. There are {len(_elements)} elements."
            print(f"[OVERLAY] {msg}")
            _speak(app, msg)
            return True
        element = _elements[number - 1]

    if not _click_with_validation(element, modifier, keys_frozen):
        msg = f"Element {number} is no longer available."
        print(f"[OVERLAY] {msg}")
        _speak(app, msg)
        return True

    _destroy_overlay(app)
    return True


def enumerate_clickable_elements() -> list:
    """Public alias for _enumerate_foreground_clickables."""
    return _enumerate_foreground_clickables()
