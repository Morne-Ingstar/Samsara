"""Show Numbers overlay — semantic voice clicking via numbered labels.

Commands:
    "show numbers"       — draw numbered labels on clickable UI elements
    "hide numbers"       — dismiss the overlay
    "click N"            — left-click element N
    "click N twice"      — double-click element N
    "click N right"      — right-click element N
    "click thirty seven" — spoken numbers also accepted

Architecture:
    Single fullscreen transparent tk.Toplevel with one Canvas — NOT one
    Toplevel per element.  The window carries WS_EX_TRANSPARENT so the
    user's physical mouse clicks pass straight through to the app below.

    Threading model:
      - UIA enumeration runs on the plugin worker thread (COM is thread-safe)
      - ALL tkinter operations are marshalled to the main thread via
        app._schedule_ui(), never called directly from a worker thread
"""

import logging
import re
import threading
import tkinter as tk

from samsara.plugin_commands import command

logger = logging.getLogger(__name__)

try:
    import uiautomation as auto
    _UIA_AVAILABLE = True
except ImportError:
    auto = None
    _UIA_AVAILABLE = False
    logger.warning("[OVERLAY] uiautomation not available — win32 fallback active")

# ---------------------------------------------------------------------------
# Module-level state  (one overlay set at a time)
# ---------------------------------------------------------------------------

_state_lock = threading.RLock()
_overlay_window: "tk.Toplevel | None" = None
_overlay_canvas: "tk.Canvas | None" = None
_elements: list = []            # index 0 corresponds to label "1"
_dismiss_timer: "threading.Timer | None" = None
_fg_check_after_id = None       # root.after id for foreground poll
_fg_hwnd_at_show: int = 0       # foreground hwnd when overlay was shown

_AUTO_DISMISS_S = 30
_FG_POLL_MS = 2000

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
    # Manual overlap check — IsOffscreen lies for scrolled/hidden/tab content
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

    Safe to call from a worker thread — UIA is COM, not tkinter.
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
                results.append({
                    'control': ctrl,
                    'rect': (r.left, r.top, r.right, r.bottom),
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
        import time
        import win32api, win32con
        x, y = self._center()
        win32api.SetCursorPos((x, y))
        if left:
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            if double:
                time.sleep(0.05)
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
                    results.append({
                        'control': _Win32Control(hwnd, rect),
                        'rect': rect,
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
# Click execution
# ---------------------------------------------------------------------------

def _click_with_validation(element, modifier: str) -> bool:
    """Validate element is still alive, then click. Returns True on success."""
    try:
        rect = element.BoundingRectangle
        if rect.width() <= 0 or rect.height() <= 0:
            logger.warning("[OVERLAY] Element no longer valid — UI changed")
            return False
        if not element.IsEnabled:
            logger.warning("[OVERLAY] Element disabled — UI changed")
            return False
    except Exception:
        logger.warning("[OVERLAY] Element handle stale — UI changed")
        return False

    return _perform_click(element, modifier)


def _perform_click(element, modifier: str) -> bool:
    """UIA-first click, then Win32 fallback."""
    try:
        if modifier == 'double':
            element.DoubleClick(simulateMove=False)
        elif modifier == 'right':
            element.RightClick(simulateMove=False)
        else:
            element.Click(simulateMove=False)
        return True
    except Exception as e:
        logger.info("[OVERLAY] UIA click failed (%s), falling back to mouse", e)

    try:
        import win32api, win32con, time
        rect = element.BoundingRectangle
        x = (rect.left + rect.right) // 2
        y = (rect.top + rect.bottom) // 2
        win32api.SetCursorPos((x, y))
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
        return True
    except Exception as e:
        logger.error("[OVERLAY] Win32 fallback click failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Overlay rendering — ALL functions below MUST run on the main UI thread
# ---------------------------------------------------------------------------

def _make_click_through(hwnd: int) -> None:
    """Apply WS_EX_LAYERED|WS_EX_TRANSPARENT so the overlay is click-through."""
    try:
        import win32gui, win32con
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        ex |= win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)
    except Exception as e:
        logger.warning("[OVERLAY] Could not make click-through: %s", e)


def _draw_overlays_on_ui_thread(app, elements: list) -> None:
    """Create the single fullscreen canvas and draw all labels.

    MUST be called via app._schedule_ui() — never directly from a worker thread.
    """
    global _overlay_window, _overlay_canvas, _elements, _fg_hwnd_at_show

    _destroy_overlay_on_ui_thread(app)   # clear any prior overlay

    if not elements:
        return

    sw = app.root.winfo_screenwidth()
    sh = app.root.winfo_screenheight()

    win = tk.Toplevel(app.root)
    win.overrideredirect(True)
    win.attributes('-topmost', True)
    win.attributes('-alpha', 0.95)
    win.configure(bg='magenta')
    win.geometry(f"{sw}x{sh}+0+0")
    win.attributes('-transparentcolor', 'magenta')
    win.update_idletasks()

    _make_click_through(win.winfo_id())

    canvas = tk.Canvas(win, width=sw, height=sh, bg='magenta',
                       highlightthickness=0, bd=0)
    canvas.pack(fill='both', expand=True)

    for i, elem in enumerate(elements, 1):
        x, y = elem['rect'][0], elem['rect'][1]
        bw, bh = 28, 22
        canvas.create_rectangle(
            x, y, x + bw, y + bh,
            fill='#ff9500', outline='#1a1a1a', width=1,
        )
        canvas.create_text(
            x + bw // 2, y + bh // 2,
            text=str(i),
            font=('Segoe UI', 11, 'bold'),
            fill='white',
        )

    with _state_lock:
        _overlay_window = win
        _overlay_canvas = canvas
        _elements[:] = [e['control'] for e in elements]

    # Record foreground hwnd for the change-detection poll
    try:
        import win32gui
        _fg_hwnd_at_show = win32gui.GetForegroundWindow()
    except Exception:
        _fg_hwnd_at_show = 0

    _schedule_dismiss_timer(app)
    _start_fg_poll(app)
    print(f"[OVERLAY] Showing {len(elements)} numbered clickables")


def _destroy_overlay_on_ui_thread(app=None) -> None:
    """Tear down the overlay window and reset all state.

    MUST be called via app._schedule_ui() — never directly from a worker thread.
    """
    global _overlay_window, _overlay_canvas, _fg_check_after_id

    _cancel_dismiss_timer()

    # Cancel foreground poll
    if _fg_check_after_id is not None and app is not None:
        try:
            app.root.after_cancel(_fg_check_after_id)
        except Exception:
            pass
        _fg_check_after_id = None

    if _overlay_window is not None:
        try:
            _overlay_window.destroy()
        except Exception:
            pass
        _overlay_window = None
        _overlay_canvas = None

    with _state_lock:
        _elements.clear()


# ---------------------------------------------------------------------------
# Auto-dismiss: timeout + foreground-window change
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
        app._schedule_ui(_destroy_overlay_on_ui_thread, app)
        print("[OVERLAY] Auto-dismissed after timeout")

    t = threading.Timer(seconds, _fire)
    t.daemon = True
    t.start()
    _dismiss_timer = t


def _start_fg_poll(app) -> None:
    """Schedule a recurring main-thread check for foreground window change."""

    def _poll():
        global _fg_check_after_id
        if _overlay_window is None:
            _fg_check_after_id = None
            return
        try:
            import win32gui
            cur = win32gui.GetForegroundWindow()
            if _fg_hwnd_at_show and cur != _fg_hwnd_at_show:
                _destroy_overlay_on_ui_thread(app)
                print("[OVERLAY] Auto-dismissed: foreground window changed")
                return
        except Exception:
            pass
        _fg_check_after_id = app.root.after(_FG_POLL_MS, _poll)

    global _fg_check_after_id
    _fg_check_after_id = app.root.after(_FG_POLL_MS, _poll)


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


# Keep the old name as an alias for the existing tests
def _parse_click_target(text: str):
    """Legacy parser: returns (number | None, modifier_str).

    modifier is 'single' | 'double' | 'right'.
    """
    text = re.sub(r'^(click|tap|press|select)\s+', '', text.strip().lower())

    modifier = 'single'
    if re.search(r'\b(twice|double)\b', text):
        modifier = 'double'
        text = re.sub(r'\b(twice|double)\b', '', text)
    elif re.search(r'\bright\b', text):
        modifier = 'right'
        text = re.sub(r'\bright\b', '', text)

    return _parse_spoken_number(text), modifier


# ---------------------------------------------------------------------------
# Voice commands
# ---------------------------------------------------------------------------

@command("show numbers",
         aliases=["show clickable", "show labels", "label things"],
         pack="accessibility")
def handle_show_numbers(app, remainder):
    """Enumerate clickable elements then draw overlays on the UI thread."""
    elements = _enumerate_foreground_clickables()
    if not elements:
        print("[OVERLAY] No clickable elements found in foreground window")
        return True
    app._schedule_ui(_draw_overlays_on_ui_thread, app, elements)
    return True


@command("hide numbers",
         aliases=["dismiss numbers", "hide labels", "clear labels"],
         pack="accessibility")
def handle_hide_numbers(app, remainder):
    app._schedule_ui(_destroy_overlay_on_ui_thread, app)
    return True


@command("click",
         aliases=["tap", "press"],
         pack="accessibility")
def handle_click(app, remainder):
    """Usage: 'click 7', 'click thirty seven', 'click 7 twice', 'click 7 right'."""
    if not remainder or not remainder.strip():
        return True

    text = remainder.strip().lower()

    modifier = 'single'
    if re.search(r'\b(twice|double)\b', text):
        modifier = 'double'
        text = re.sub(r'\b(twice|double)\b', '', text)
    elif re.search(r'\bright\b', text):
        modifier = 'right'
        text = re.sub(r'\bright\b', '', text)

    # Strip click-verb residue
    text = re.sub(r'^(click|tap|press|select)\s+', '', text.strip())

    number = _parse_spoken_number(text)
    if number is None:
        print(f"[OVERLAY] Couldn't parse number from: {remainder!r}")
        return True

    with _state_lock:
        if not _elements:
            print("[OVERLAY] No overlay active — use 'show numbers' first")
            return True
        if number < 1 or number > len(_elements):
            print(f"[OVERLAY] No element labeled {number} (have {len(_elements)})")
            return True
        element = _elements[number - 1]

    if not _click_with_validation(element, modifier):
        print(f"[OVERLAY] Element {number} no longer valid — try 'show numbers' again")
        return True

    app._schedule_ui(_destroy_overlay_on_ui_thread, app)
    return True


def enumerate_clickable_elements() -> list:
    """Public alias for _enumerate_foreground_clickables."""
    return _enumerate_foreground_clickables()
