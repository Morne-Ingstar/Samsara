"""Show Numbers overlay — label clickable elements for voice clicking.

Commands:
    "show numbers"            — overlay numbered badges on clickable UI elements
    "hide numbers"            — dismiss all overlays
    "click N"                 — left-click element N (also: "click N twice", "right click N")
"""

import logging
import re
import threading
import tkinter as tk
from typing import List, Optional

from samsara.plugin_commands import command

logger = logging.getLogger(__name__)

try:
    import uiautomation as auto
    _UIA_AVAILABLE = True
except ImportError:
    auto = None
    _UIA_AVAILABLE = False
    logger.warning("[OVERLAY] uiautomation not available — will use win32 fallback")

# ---------------------------------------------------------------------------
# Module-level state (one set of overlays at a time)
# ---------------------------------------------------------------------------

_current_overlays: List[tk.Toplevel] = []
_current_elements: List = []        # parallel list: one control per overlay
_overlay_lock = threading.Lock()
_dismiss_timer: Optional[threading.Timer] = None
_AUTO_DISMISS_S = 30

# ---------------------------------------------------------------------------
# UI element enumeration
# ---------------------------------------------------------------------------

_CLICKABLE_TYPES = frozenset({
    'ButtonControl',
    'HyperlinkControl',
    'MenuItemControl',
    'TabItemControl',
    'CheckBoxControl',
    'RadioButtonControl',
    'ListItemControl',
    'TreeItemControl',
    'ComboBoxControl',
    'EditControl',
})


def enumerate_clickable_elements() -> list:
    """Return list of dicts with 'control', 'rect', 'control_type', 'name'.

    Uses Windows UI Automation when available, falls back to EnumChildWindows.
    """
    if _UIA_AVAILABLE:
        return _enumerate_uia()
    return _enumerate_win32_fallback()


def _enumerate_uia() -> list:
    fg = auto.GetForegroundControl()
    if fg is None:
        return []

    clickables = []

    def _walk(control, depth=0):
        if depth > 12:
            return
        try:
            if control.IsOffscreen:
                return
        except Exception:
            pass
        if control.ControlTypeName in _CLICKABLE_TYPES:
            try:
                rect = control.BoundingRectangle
                if rect.width() > 0 and rect.height() > 0:
                    clickables.append({
                        'control': control,
                        'rect': (rect.left, rect.top, rect.right, rect.bottom),
                        'control_type': control.ControlTypeName,
                        'name': control.Name or '',
                    })
            except Exception:
                pass
        try:
            for child in control.GetChildren():
                _walk(child, depth + 1)
        except Exception:
            pass

    _walk(fg)
    return clickables


class _Win32Rect:
    """Minimal BoundingRectangle-compatible adapter for win32 fallback."""
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _Win32Control:
    """Win32 HWND wrapper with a uiautomation-compatible interface."""

    def __init__(self, hwnd, rect):
        self._hwnd = hwnd
        l, t, r, b = rect
        self.BoundingRectangle = _Win32Rect(l, t, r, b)

    def _center(self):
        br = self.BoundingRectangle
        return (br.left + br.right) // 2, (br.top + br.bottom) // 2

    def Click(self, simulateMove=True):
        self._mouse(left=True)

    def DoubleClick(self, simulateMove=True):
        import time
        self._mouse(left=True)
        time.sleep(0.05)
        self._mouse(left=True)

    def RightClick(self, simulateMove=True):
        self._mouse(left=False)

    def _mouse(self, left: bool):
        import win32api, win32con
        x, y = self._center()
        win32api.SetCursorPos((x, y))
        if left:
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        else:
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)


def _enumerate_win32_fallback() -> list:
    try:
        import win32gui
    except ImportError:
        logger.error("[OVERLAY] Neither uiautomation nor win32gui is available")
        return []

    fg_hwnd = win32gui.GetForegroundWindow()
    if not fg_hwnd:
        return []

    _CLICKABLE_CLASSES = {
        'button', 'edit', 'combobox', 'listbox', 'syslink',
        'syslistview32', 'systreeview32', 'systabcontrol32',
    }
    results = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        cls = win32gui.GetClassName(hwnd).lower()
        for cc in _CLICKABLE_CLASSES:
            if cc in cls:
                try:
                    rect = win32gui.GetWindowRect(hwnd)
                    l, t, r, b = rect
                    if r - l > 0 and b - t > 0:
                        results.append({
                            'control': _Win32Control(hwnd, rect),
                            'rect': rect,
                            'control_type': cls,
                            'name': win32gui.GetWindowText(hwnd),
                        })
                except Exception:
                    pass
                break
        return True

    try:
        win32gui.EnumChildWindows(fg_hwnd, _cb, None)
    except Exception as e:
        logger.error("[OVERLAY] EnumChildWindows failed: %s", e)
    return results


# ---------------------------------------------------------------------------
# Number overlay widget
# ---------------------------------------------------------------------------

class NumberOverlay(tk.Toplevel):
    """Frameless, always-on-top numbered badge at the top-left of an element."""

    _BG = '#ff9500'

    def __init__(self, parent, number: int, x: int, y: int):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes('-topmost', True)
        self.attributes('-alpha', 0.9)
        self.configure(bg=self._BG)
        tk.Label(
            self,
            text=str(number),
            font=('Segoe UI', 12, 'bold'),
            bg=self._BG, fg='white',
            padx=6, pady=1,
        ).pack()
        self.geometry(f'+{x}+{y}')


# ---------------------------------------------------------------------------
# Click execution
# ---------------------------------------------------------------------------

def _do_click(element, modifier: str) -> None:
    try:
        if modifier == 'double':
            element.DoubleClick(simulateMove=False)
        elif modifier == 'right':
            element.RightClick(simulateMove=False)
        else:
            element.Click(simulateMove=False)
    except Exception as e:
        logger.warning("[OVERLAY] UIA click failed (%s), falling back to mouse", e)
        br = element.BoundingRectangle
        x = (br.left + br.right) // 2
        y = (br.top + br.bottom) // 2
        _simulate_click(x, y, modifier)


def _simulate_click(x: int, y: int, modifier: str) -> None:
    import win32api, win32con
    import time
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


# ---------------------------------------------------------------------------
# Overlay lifecycle
# ---------------------------------------------------------------------------

def _cancel_dismiss_timer() -> None:
    global _dismiss_timer
    if _dismiss_timer is not None:
        _dismiss_timer.cancel()
        _dismiss_timer = None


def _schedule_auto_dismiss(app, timeout_s: int = _AUTO_DISMISS_S) -> None:
    global _dismiss_timer
    _cancel_dismiss_timer()
    _dismiss_timer = threading.Timer(timeout_s, lambda: _hide_overlays(app))
    _dismiss_timer.daemon = True
    _dismiss_timer.start()


def _hide_overlays(app=None) -> None:
    """Destroy all overlays and clear module state. Thread-safe."""
    _cancel_dismiss_timer()

    def _do_destroy():
        with _overlay_lock:
            for overlay in _current_overlays:
                try:
                    overlay.destroy()
                except Exception:
                    pass
            _current_overlays.clear()
            _current_elements.clear()

    if app is not None and hasattr(app, 'root') and app.root is not None:
        try:
            app.root.after(0, _do_destroy)
        except RuntimeError:
            _do_destroy()
    else:
        _do_destroy()


def _show_overlays(app) -> None:
    """Enumerate clickable elements, then create overlays on the UI thread."""
    _hide_overlays(app)

    try:
        elements = enumerate_clickable_elements()
    except Exception as e:
        logger.error("[OVERLAY] Enumeration failed: %s", e)
        print("[OVERLAY] UI enumeration failed — is uiautomation installed?")
        return

    if not elements:
        print("[OVERLAY] No clickable elements found")
        return

    elements = elements[:99]
    logger.info("[OVERLAY] Found %d clickable element(s)", len(elements))

    has_root = (app is not None
                and hasattr(app, 'root')
                and app.root is not None)

    def _create_all():
        with _overlay_lock:
            _current_elements.clear()
            _current_overlays.clear()
            for i, elem in enumerate(elements, 1):
                _current_elements.append(elem['control'])
                if has_root:
                    x, y = elem['rect'][0], elem['rect'][1]
                    try:
                        overlay = NumberOverlay(app.root, i, x, y)
                        _current_overlays.append(overlay)
                    except Exception as exc:
                        logger.warning("[OVERLAY] Overlay %d failed: %s", i, exc)
        if has_root:
            _schedule_auto_dismiss(app)
        print(f"[OVERLAY] Showing {len(elements)} numbered element(s)")

    if has_root:
        try:
            app.root.after(0, _create_all)
        except RuntimeError:
            _create_all()
    else:
        _create_all()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_WORD_TO_NUM = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19, 'twenty': 20,
}


def _parse_click_target(text: str):
    """Parse 'click N' voice remainder into (number, modifier).

    modifier is 'single', 'double', or 'right'.
    Returns (None, 'single') if no number can be found.
    """
    text = re.sub(r'^(click|tap|press|select)\s+', '', text.strip().lower())

    modifier = 'single'
    if re.search(r'\b(twice|double)\b', text):
        modifier = 'double'
        text = re.sub(r'\b(twice|double)\b', '', text)
    elif re.search(r'\bright\b', text):
        modifier = 'right'
        text = re.sub(r'\bright\b', '', text)

    m = re.search(r'\d+', text)
    if m:
        return int(m.group()), modifier

    for word, num in _WORD_TO_NUM.items():
        if re.search(rf'\b{word}\b', text):
            return num, modifier

    return None, modifier


# ---------------------------------------------------------------------------
# Voice commands
# ---------------------------------------------------------------------------

@command("show numbers",
         aliases=["show clickable", "show labels"],
         pack="core")
def handle_show_numbers(app, remainder):
    _show_overlays(app)
    return True


@command("hide numbers",
         aliases=["dismiss numbers", "hide labels"],
         pack="core")
def handle_hide_numbers(app, remainder):
    _hide_overlays(app)
    return True


@command("click",
         aliases=[],
         pack="core")
def handle_click(app, remainder):
    if not remainder or not remainder.strip():
        return True

    number, modifier = _parse_click_target(remainder.strip().lower())

    if number is None:
        print(f"[OVERLAY] Couldn't parse number from: {remainder!r}")
        return True

    with _overlay_lock:
        if not _current_elements:
            print("[OVERLAY] No overlay active — use 'show numbers' first")
            return True
        if number < 1 or number > len(_current_elements):
            print(f"[OVERLAY] No element labeled {number} "
                  f"(have {len(_current_elements)})")
            return True
        element = _current_elements[number - 1]

    _do_click(element, modifier)
    _hide_overlays(app)
    return True
