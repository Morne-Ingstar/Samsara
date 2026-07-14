"""Show Numbers overlay — semantic voice clicking via numbered labels.

Commands:
    "show numbers" / "show"   -- draw numbered labels on clickable UI elements.
                                 If the overlay is already showing, "show"/
                                 "show numbers" re-enumerates and redraws
                                 (folds refresh into the same word) rather
                                 than reusing a stale cached list.
    "hide numbers" / "hide"   -- dismiss the overlay
    "refresh numbers" / "refresh" -- re-enumerate and redraw without dismissing first
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

DOM (browser-extension) path -- see samsara/browser_bridge.py and
browser_extension/ -- used instead of UIA whenever Brave is foregrounded and
the extension is connected: native UIA sees Brave's tabs/bookmarks and the
actual webpage as the same kind of control, so on a real page the 99-result
cap is consumed before webpage elements are ever reached. The DOM path
renders numbered labels inside the webpage itself (a content script, no Qt
window at all) and structurally cannot enumerate tabs/bookmarks, since a
content script's `document` is only ever the page's own DOM. Falls back to
the UIA path above -- visibly (a distinct log line and an overlay caption),
never silently -- when the extension isn't installed, isn't connected, or
the active tab is a restricted page.

    Threading model (DOM path):
      - browser_bridge's WS server thread and per-connection handler
        threads never touch Qt at all
      - DOM session state (_dom_active/_dom_hint_count) has its own lock
        (_dom_lock), separate from UIA's _state_lock/_elements -- only one
        of the two is ever populated at a time
      - An unsolicited in-page Escape reaches Python via a browser_bridge
        connection thread calling _on_dom_dismissed()
"""

import logging
import re
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from samsara import browser_bridge
from samsara.plugin_commands import command
from samsara.runtime import thread_registry
from samsara.ui import qt_runtime
from samsara.ui.numbers_overlay_qt import (
    NumbersOverlayWindow, phys_to_logical, _COORD_DEBUG,
    _ensure_dpi_thread_context, screen_for_hwnd,
)

logger = logging.getLogger(__name__)

_uia_mod = None
_UIA_AVAILABLE = None  # None = not yet attempted; True/False = result

# ---------------------------------------------------------------------------
# Module-level state  (one overlay at a time)
# ---------------------------------------------------------------------------

_state_lock     = threading.RLock()
_elements: list = []                             # index 0 -> label "1"
_dismiss_timer: "threading.Timer | None" = None
_overlay_window: "NumbersOverlayWindow | None" = None   # Qt thread only
_fg_timer: "QTimer | None" = None                       # Qt thread only
_fg_hwnd_at_show: int = 0                               # HWND of target window
_overlay_hwnd:    int = 0                               # HWND of overlay itself
_overlay_screen_name: str = ''                          # Qt thread only

_enum_cache: "dict | None" = None   # {'hwnd': int, 't': float, 'elements': list}
_CACHE_TTL      = 10.0   # seconds
_AUTO_DISMISS_S = 30
_FG_POLL_MS     = 2000

# ---------------------------------------------------------------------------
# DOM (browser-extension) session state -- deliberately a SEPARATE lock and
# variable set from _state_lock/_elements above, not a repurposing of them.
# A DOM "element" is a (requestId, number) pair to relay over the bridge, not
# a UIA control object with .Click()/.BoundingRectangle -- mixing the two
# into one list would require every UIA-only call site to branch anyway.
# Only one of _elements / _dom_active is ever populated at a time: starting
# one session clears the other (see _try_show_dom_numbers / _draw_overlay).
# Reachable from three different thread identities: the plugin-command
# worker thread (handle_show_numbers/handle_click), the Qt thread (never --
# the DOM path renders entirely in-page, no Qt object touches this lock),
# and a browser_bridge connection-handler thread (an unsolicited "dismissed"
# from the content script's in-page Escape listener, via the on_dismissed
# callback below).
# ---------------------------------------------------------------------------
_dom_lock       = threading.RLock()
_dom_active     = False
_dom_hint_count = 0
_BRAVE_PROCESS_NAMES = frozenset({'brave.exe'})
_bridge_started = False

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
    except Exception as e:
        logger.debug(f"_is_useful_clickable: {e}")
    return True


def _get_foreground_control(auto):
    """Return the foreground UIA control across uiautomation API versions."""
    getter = getattr(auto, 'GetForegroundControl', None)
    if callable(getter):
        return getter()

    hwnd_getter = getattr(auto, 'GetForegroundWindow', None)
    hwnd = hwnd_getter() if callable(hwnd_getter) else 0
    if not hwnd:
        try:
            import win32gui
            hwnd = win32gui.GetForegroundWindow()
        except (ImportError, AttributeError):
            hwnd = 0

    from_handle = getattr(auto, 'ControlFromHandle', None)
    if hwnd and callable(from_handle):
        return from_handle(hwnd)
    logger.warning(
        "[OVERLAY] uiautomation has no foreground-control API; "
        "falling back to Win32 enumeration"
    )
    return None


# ---------------------------------------------------------------------------
# DOM (browser-extension) Show Numbers path
# ---------------------------------------------------------------------------

def _is_brave_foreground(fg_hwnd: int) -> bool:
    """True if the foreground window belongs to a Brave process."""
    if not fg_hwnd:
        return False
    try:
        import win32process
        import psutil
        _, pid = win32process.GetWindowThreadProcessId(fg_hwnd)
        name = psutil.Process(pid).name().lower()
        return name in _BRAVE_PROCESS_NAMES
    except Exception as e:
        logger.debug(f"_is_brave_foreground: {e}")
        return False


def _on_dom_dismissed() -> None:
    """Called from a browser_bridge connection-handler thread when the
    extension reports an unsolicited dismissal (in-page Escape). Clears
    Python-side DOM session state so a stale 'click N' after Escape fails
    gracefully instead of addressing a session that no longer exists in
    the page."""
    global _dom_active, _dom_hint_count
    with _dom_lock:
        _dom_active = False
        _dom_hint_count = 0
    print("[OVERLAY] DOM overlay dismissed (in-page Escape)")


def _ensure_bridge_started() -> "browser_bridge.BrowserBridge | None":
    """Lazily starts the local bridge server on first use (not at import
    time) -- most Samsara sessions never touch Brave, so there's no reason
    to bind a loopback port until "show numbers" is actually tried while
    Brave is foregrounded. Idempotent; returns None if the bridge failed
    to bind (port taken) so callers fall back to UIA for the session."""
    global _bridge_started
    bridge = browser_bridge.get_bridge()
    if not _bridge_started:
        bridge.set_on_dismissed(_on_dom_dismissed)
        if not bridge.start():
            return None
        _bridge_started = True
    return bridge


def _try_show_dom_numbers(app):
    """Attempts the DOM (browser-extension) Show Numbers path.

    Returns (handled, fallback_reason):
      - (True, None)   -- DOM overlay shown (or page legitimately has no
                           candidates -- both are a fully-handled outcome,
                           not a fallback).
      - (False, None)  -- not applicable (Brave isn't foregrounded); caller
                           should silently use UIA as always.
      - (False, reason)-- Brave IS foregrounded but the DOM path failed
                           (bridge down/timeout/no content script); caller
                           should fall back to UIA AND surface this
                           visibly (distinct log line + overlay caption),
                           per the "never silently do nothing" requirement.
    """
    global _dom_active, _dom_hint_count
    try:
        import win32gui
        fg_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        fg_hwnd = 0

    if not _is_brave_foreground(fg_hwnd):
        return False, None

    bridge = _ensure_bridge_started()
    if bridge is None:
        return False, "bridge_bind_failed"

    if not bridge.is_connected():
        return False, "extension_not_connected"

    hints = bridge.request_hints(timeout=0.8)
    if hints is None:
        reason = bridge.last_hints_unavailable_reason or "unknown"
        if reason == "no_candidates":
            # A real, connected DOM response: the page genuinely has no
            # actionable elements. This is a legitimate outcome, not a
            # failure -- do NOT fall back to UIA (that would just show
            # browser tabs/bookmarks again, the exact problem this feature
            # exists to avoid).
            _speak(app, "No clickable elements found on this page.")
            return True, None
        return False, reason

    with _state_lock:
        _elements.clear()
    with _dom_lock:
        _dom_active = True
        _dom_hint_count = len(hints)

    _cancel_dismiss_timer()
    _schedule_dismiss_timer(app)
    print(f"[OVERLAY] DOM overlay active -- {len(hints)} numbered clickables (webpage)")
    if hasattr(app, 'hints'):
        app.hints.increment('show_numbers_used')
    return True, None


def _enumerate_foreground_clickables() -> list:
    """Walk the foreground window's UIA subtree; return useful clickables.

    Safe to call from a worker thread -- UIA is COM, not Qt.
    """
    global _uia_mod, _UIA_AVAILABLE
    if _UIA_AVAILABLE is None:
        try:
            import uiautomation as _m
            _uia_mod = _m
            _UIA_AVAILABLE = True
        except ImportError:
            _UIA_AVAILABLE = False
            logger.warning("[OVERLAY] uiautomation not available -- win32 fallback active")

    if not _UIA_AVAILABLE:
        return _enumerate_win32_fallback()

    auto = _uia_mod
    fg = _get_foreground_control(auto)
    if fg is None:
        return _enumerate_win32_fallback()

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
        except Exception as e:
            logger.debug(f"_walk: {e}")
        try:
            for child in ctrl.GetChildren():
                _walk(child, depth + 1)
        except Exception as e:
            logger.debug(f"_walk: {e}")

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

    try:
        fg_hwnd = win32gui.GetForegroundWindow()
    except AttributeError:
        logger.error("[OVERLAY] win32gui foreground-window API unavailable")
        return []
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
            except Exception as e:
                logger.debug(f"_cb: {e}")
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

def _show_overlay_qt(labels: list, fg_hwnd: int, app, caption: str = "") -> None:
    """Create or update the overlay window. Qt main thread only."""
    global _overlay_window, _fg_timer, _fg_hwnd_at_show, _overlay_hwnd, _overlay_screen_name

    print(f"[OVERLAY] _show_overlay_qt called on Qt thread — {len(labels)} label(s)")
    _fg_hwnd_at_show = fg_hwnd

    active_screen = screen_for_hwnd(fg_hwnd)
    if active_screen is None:
        print("[OVERLAY] ERROR: no screen found for hwnd, aborting")
        return
    active_name = active_screen.name()

    geo = active_screen.geometry()
    logger.info(
        "[OVERLAY] render labels=%d screen=%s geo=(%d,%d %dx%d) dpr=%.2f first=%r",
        len(labels), active_name,
        geo.x(), geo.y(), geo.width(), geo.height(),
        active_screen.devicePixelRatio(),
        labels[0] if labels else None,
    )

    # Same screen -> reuse the existing window (no HWND recreate), regardless
    # of whether it's currently hidden. HIDE-not-destroy (see _hide_overlay_qt)
    # means a previously-dismissed overlay on the same screen is still alive
    # here, just hidden -- matching status_overlay/task_overlay/reminder_toast's
    # reuse pattern instead of leaking a fresh HWND on every show/hide cycle.
    same_screen = (
        _overlay_window is not None
        and active_name == _overlay_screen_name
    )

    if same_screen:
        _overlay_window.update_labels(labels, caption)
    else:
        if _overlay_window is not None:
            _overlay_window.hide()
        _ensure_dpi_thread_context()
        _overlay_window = NumbersOverlayWindow(labels, active_screen)
        _overlay_window._caption = caption
        _overlay_screen_name = active_name

    # Explicit show()+raise() every time (not just on first construction) --
    # matching status_overlay.show()/task_overlay.show()'s re-show path.
    # Without this, a same-screen re-show only updated label content and
    # relied on whatever z-order the window already had, so if another
    # always-on-top window (reminder toast, status/task overlay) had been
    # created or re-shown since, the numbers overlay stayed rendered but
    # invisible underneath it.
    _overlay_window.show()
    _overlay_window.raise_()

    # Capture the overlay's own HWND so _fg_poll_qt can exclude it.
    # winId() is only valid after show(); int() converts from shiboken.VoidPtr.
    try:
        _overlay_hwnd = int(_overlay_window.winId())
    except Exception:
        _overlay_hwnd = 0

    # (Re-)start foreground poll
    _stop_fg_timer_qt()
    _fg_timer = QTimer()
    _fg_timer.setInterval(_FG_POLL_MS)
    _fg_timer.timeout.connect(lambda: _fg_poll_qt(app))
    _fg_timer.start()


def _hide_overlay_qt() -> None:
    """Hide the overlay window and stop the fg timer. Qt main thread only.

    HIDE-not-destroy -- same reusable-singleton pattern as status_overlay.py /
    task_overlay.py / reminder_toast.py. The window (and its HWND/screen
    identity) stays alive so the NEXT "show numbers" on the same screen can
    reuse it via _show_overlay_qt's same_screen branch instead of
    reconstructing (and leaking the previous, now-orphaned HWND) every
    single show/hide cycle.
    """
    _stop_fg_timer_qt()
    if _overlay_window is not None:
        _overlay_window.hide()
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
        # Dismiss only when focus has moved to a window that is neither the
        # original target window nor the overlay itself. The overlay can briefly
        # hold focus on some monitors when first shown; treating it as a foreign
        # window would cause a false auto-dismiss within the first poll cycle.
        if _fg_hwnd_at_show and cur != _fg_hwnd_at_show and cur != _overlay_hwnd:
            print(f"[OVERLAY] Auto-dismissed: foreground window changed "
                  f"(target={_fg_hwnd_at_show:#x} overlay={_overlay_hwnd:#x} cur={cur:#x})")
            _destroy_overlay(app)
    except Exception as e:
        logger.debug(f"_fg_poll_qt: {e}")

# ---------------------------------------------------------------------------
# Auto-dismiss + overlay lifecycle (thread-safe)
# ---------------------------------------------------------------------------

def _cancel_dismiss_timer() -> None:
    global _dismiss_timer
    if _dismiss_timer is not None:
        try:
            _dismiss_timer.cancel()
        except Exception as e:
            logger.debug(f"_cancel_dismiss_timer: {e}")
        _dismiss_timer = None


def _schedule_dismiss_timer(app, seconds: int = _AUTO_DISMISS_S) -> None:
    _cancel_dismiss_timer()
    global _dismiss_timer

    def _fire():
        print("[OVERLAY] Auto-dismissed after timeout")
        _destroy_overlay(app)

    t = thread_registry.timer("show_numbers.auto_dismiss", seconds, _fire, daemon=True)
    _dismiss_timer = t


def _draw_overlay(app, elements: list, caption: str = "") -> None:
    """Build label list and show the Qt overlay. Safe to call from any thread.

    caption: non-empty only when this UIA overlay is being shown as a
    visible fallback from a failed DOM (browser-extension) attempt -- see
    _try_show_dom_numbers -- so the user can tell the two paths apart
    instead of the fallback happening invisibly.
    """
    _clear_dom_session()  # mutual exclusion: starting a UIA session ends any DOM one
    try:
        import win32gui
        fg_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        fg_hwnd = 0

    # Limit to elements whose center falls on the active monitor.
    # One monitor = one DPI scale = coherent coordinate space for the overlay.
    active_screen = screen_for_hwnd(fg_hwnd)
    if active_screen is not None:
        geo = active_screen.geometry()
        ax0, ay0 = geo.x(), geo.y()
        ax1, ay1 = ax0 + geo.width(), ay0 + geo.height()
        on_active = [
            e for e in elements
            if ax0 <= (e['rect'][0] + e['rect'][2]) // 2 < ax1
            and ay0 <= (e['rect'][1] + e['rect'][3]) // 2 < ay1
        ]
        if on_active:
            elements = on_active

    labels = _place_labels(elements)

    if _COORD_DEBUG:
        for lbl in labels[:5]:
            logger.debug(
                "[DPI-COORD] pill: screen_x=%d screen_y=%d w=%d h=%d label=%s",
                lbl[0], lbl[1], lbl[2], lbl[3], lbl[4],
            )

    with _state_lock:
        _elements[:] = [e['control'] for e in elements]

    _cancel_dismiss_timer()
    # Defensive start: mirrors ai_command_hud_qt.show_hud()/splash_qt.py's
    # own pattern. qt_runtime.post() silently drops the callback (logging a
    # warning only) whenever the runtime is not yet RUNNING -- if this is
    # ever the first Qt work requested in the process (e.g. a voice command
    # fires before/around the rest of boot, or the icon-setting block in
    # dictation.py's __init__ swallowed an exception before reaching its own
    # ensure_started() call), the overlay would silently never be built at
    # all. ensure_started() is idempotent -- a no-op when already RUNNING.
    qt_runtime.ensure_started()
    qt_runtime.post(lambda: _show_overlay_qt(labels, fg_hwnd, app, caption))
    _schedule_dismiss_timer(app)
    print(f"[OVERLAY] Showing {len(elements)} numbered clickables (active monitor)")


def _clear_dom_session() -> None:
    """Clears Python-side DOM session state and asks the extension to
    drop its in-page hints too. Safe to call even if no DOM session is
    active (no-op bridge.send_dismiss() when nothing is connected)."""
    global _dom_active, _dom_hint_count
    with _dom_lock:
        was_active = _dom_active
        _dom_active = False
        _dom_hint_count = 0
    if was_active:
        browser_bridge.get_bridge().send_dismiss()


def _destroy_overlay(app=None) -> None:
    """Thread-safe dismiss: cancel timers, clear whichever session (UIA or
    DOM) is active. Always safe to call both halves -- clearing an
    inactive session is a no-op."""
    _cancel_dismiss_timer()
    _clear_dom_session()
    qt_runtime.ensure_started()
    qt_runtime.post(_hide_overlay_qt)


def _destroy_overlay_completely() -> None:
    """Full teardown -- call on app quit.

    browser_bridge.stop() is genuinely blocking (bounded, ~1.5s) and is
    called BEFORE the (fire-and-forget) Qt-post below: qt_runtime.post()
    schedules _hide_overlay_qt on the Qt thread and returns immediately
    without waiting for it, so placing the bridge shutdown after it would
    add real, easy-to-miss latency to app quit for no benefit -- the Qt
    hide is already going to happen asynchronously regardless of ordering.
    """
    browser_bridge.get_bridge().stop(timeout=1.5)
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

    # Defensive start -- same reasoning as _draw_overlay(): this diagnostic
    # exists specifically to confirm the renderer works independently of
    # element enumeration, so it must not depend on some OTHER code path
    # having already started qt_runtime first.
    qt_runtime.ensure_started()

    qt_app = QApplication.instance()
    print(f"[OVERLAY-TEST] QApplication.instance() = {qt_app}")
    if qt_app is not None:
        qt_thread = qt_app.thread()
        cur_thread = QThread.currentThread()
        on_qt_thread = qt_thread is cur_thread
        # QThread.currentThreadId() isn't exposed in this PySide6 build
        # (QThread.currentThread()/isCurrentThread() are); use Python's own
        # thread ident instead, which is always available.
        print(
            f"[OVERLAY-TEST] on-Qt-thread={on_qt_thread}  "
            f"calling Python thread ident={threading.get_ident()}"
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


def _show_grid_qt(fg_hwnd: int, app) -> None:
    """Draw diagnostic grid on the active monitor. Qt main thread only.

    Computes pill positions at the time of rendering from the actual active
    screen geometry so coordinates are always correct regardless of monitor
    layout or DPI scale.
    """
    global _overlay_window, _overlay_screen_name

    active_screen = screen_for_hwnd(fg_hwnd)
    if active_screen is None:
        active_screen = QApplication.primaryScreen()
    if active_screen is None:
        print("[OVERLAY-GRID] ERROR: no screen available")
        return

    geo = active_screen.geometry()
    x0, y0, w, h = geo.x(), geo.y(), geo.width(), geo.height()
    pw, ph = _pill_w("TL"), _PILL_H

    labels = [
        [x0 + 100,         y0 + 100,        pw, ph, "TL"],
        [x0 + w // 2,      y0 + 100,        pw, ph, "TC"],
        [x0 + w - 160,     y0 + 100,        pw, ph, "TR"],
        [x0 + 100,         y0 + h - 140,    pw, ph, "BL"],
        [x0 + w // 2,      y0 + h // 2,     pw, ph, "MM"],
    ]

    if _overlay_window is not None:
        _overlay_window.close()
    _ensure_dpi_thread_context()
    _overlay_window = NumbersOverlayWindow(labels, active_screen)
    _overlay_screen_name = active_screen.name()
    _overlay_window.show()

    print(
        f"[OVERLAY-GRID] Grid on {active_screen.name()} "
        f"{w}x{h} @{active_screen.devicePixelRatio():.1f}x:\n"
        f"  TL=({x0+100},{y0+100})  TC=({x0+w//2},{y0+100})"
        f"  TR=({x0+w-160},{y0+100})\n"
        f"  BL=({x0+100},{y0+h-140})  MM=({x0+w//2},{y0+h//2})"
    )


@command("overlay grid",
         aliases=["grid test", "overlay grid test"],
         pack="accessibility")
def handle_overlay_grid(app, remainder):
    """Diagnostic: draw 5 test pills spanning the active monitor.

    Positions (TL, TC, TR, BL, MM) are computed on the Qt thread from the
    actual screen geometry so coordinates are always relative to whatever
    monitor the foreground window is on.  Check the log for exact values.
    """
    try:
        import win32gui
        fg_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        fg_hwnd = 0

    qt_runtime.ensure_started()
    qt_runtime.post(lambda: _show_grid_qt(fg_hwnd, app))
    print("[OVERLAY-GRID] Grid test queued — check log for pill coordinates")
    return True


@command("show numbers",
         aliases=["show clickable", "show labels", "label things", "show"],
         pack="accessibility")
def handle_show_numbers(app, remainder):
    """Enumerate clickable elements then draw overlay.

    Tries the DOM (browser-extension) path first when Brave is the
    foreground window -- native UI Automation sees Brave's own tabs/
    bookmarks and the actual webpage as the same kind of thing, so on a
    real page with many tabs/bookmarks the 99-result UIA cap is consumed
    before webpage controls are ever reached. The DOM path structurally
    cannot enumerate tabs/bookmarks at all (a content script's `document`
    is only ever the webpage itself), so it's preferred whenever available.
    Falls back to UIA -- visibly, not silently -- when the extension isn't
    installed, isn't connected, or the active tab is a restricted page.

    If the (UIA) overlay is already showing, re-enumerate fresh (bypass the
    cache) instead of reusing whatever _cached_enumerate() last saw -- folds
    show + refresh into one word, since saying "show"/"show numbers" again
    while it's already up is almost always "the UI changed, refresh this,"
    not "show me the exact same labels again." This is the primary
    show -> observe -> click loop the short "show" alias is meant to serve.
    """
    handled, fallback_reason = _try_show_dom_numbers(app)
    if handled:
        return True

    caption = ""
    if fallback_reason is not None:
        logger.info(
            "[SHOW_NUMBERS] DOM bridge unavailable/timeout -- falling back to "
            "UIA (reason=%s)", fallback_reason,
        )
        caption = "Native fallback (browser extension unavailable)"

    if is_overlay_active():
        _invalidate_cache()
        elements = _enumerate_foreground_clickables()
    else:
        elements = _cached_enumerate()
    if not elements:
        message = "No clickable elements found in the foreground window."
        logger.warning("[OVERLAY] %s", message)
        _speak(app, message)
        return True
    _draw_overlay(app, elements, caption)
    if hasattr(app, 'hints'):
        app.hints.increment('show_numbers_used')
    return True


@command("hide numbers",
         aliases=["dismiss numbers", "hide labels", "clear labels", "hide"],
         pack="accessibility")
def handle_hide_numbers(app, remainder):
    _destroy_overlay(app)
    return True


@command("refresh numbers",
         aliases=["update numbers", "refresh"],
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

    with _dom_lock:
        dom_active = _dom_active
        dom_count = _dom_hint_count

    if dom_active:
        if number < 1 or number > dom_count:
            msg = f"Element {number} is not available. There are {dom_count} elements."
            print(f"[OVERLAY] {msg}")
            _speak(app, msg)
            return True
        action = {'single': 'click', 'double': 'doubleclick', 'right': 'rightclick'}.get(
            modifier, 'click'
        )
        dom_modifiers = {
            'ctrlKey': 'ctrl' in keys_frozen,
            'shiftKey': 'shift' in keys_frozen,
            'altKey': 'alt' in keys_frozen,
        }
        ok = browser_bridge.get_bridge().send_selection(number, action, modifiers=dom_modifiers)
        if not ok:
            msg = f"Element {number} is no longer available."
            print(f"[OVERLAY] {msg}")
            _speak(app, msg)
            return True
        # content.js already clears its own hints on a successful selection;
        # this only clears the Python-side flags (a redundant dismiss to an
        # already-cleared content script is a harmless no-op there).
        _clear_dom_session()
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


def is_overlay_active() -> bool:
    """True while a numbered overlay -- UIA/Qt OR DOM/in-page -- is
    currently shown.

    Safe to call from any thread: _elements is populated (any thread, under
    _state_lock) in _draw_overlay before the Qt window is posted, and cleared
    (Qt thread, under _state_lock) in _hide_overlay_qt; _dom_active is set/
    cleared (worker thread or a browser_bridge connection thread, under
    _dom_lock) in _try_show_dom_numbers/_clear_dom_session -- so this never
    reads Qt objects across threads, just the two lock-guarded flags other
    callers (e.g. reminder_toast) use to gate on overlay visibility of
    either kind.
    """
    with _state_lock:
        uia = bool(_elements)
    with _dom_lock:
        dom = _dom_active
    return uia or dom
