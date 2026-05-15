"""Text marker plugin.

Voice-driven text selection without click-dragging.

  "mark here"       - anchor the selection start at the current cursor position
  "select to here"  - shift-click at the current cursor position to extend the
                      selection from the anchor to here

Typical workflow:
  1. Click at the start of the desired selection.
  2. Say "mark here".
  3. Scroll/move to the end of what you want selected.
  4. Say "select to here" — text between the two points is highlighted.
"""

import ctypes
import ctypes.wintypes as wintypes
import sys

from samsara.plugin_commands import command

# ── Module-level anchor state ─────────────────────────────────────────────────

_marker_set: bool = False
_marker_pos: tuple | None = None   # (x, y) in screen pixels

# ── Win32 SendInput structures ────────────────────────────────────────────────

user32 = ctypes.windll.user32

INPUT_MOUSE    = 0
INPUT_KEYBOARD = 1

VK_SHIFT = 0x10

KEYEVENTF_KEYUP       = 0x0002
MOUSEEVENTF_LEFTDOWN  = 0x0002
MOUSEEVENTF_LEFTUP    = 0x0004
MOUSEEVENTF_ABSOLUTE  = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000   # required for correct absolute coords on multi-monitor

SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   wintypes.DWORD),
        ("dwFlags",     wintypes.DWORD),
        ("time",        wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         wintypes.WORD),
        ("wScan",       wintypes.WORD),
        ("dwFlags",     wintypes.DWORD),
        ("time",        wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("_input", _INPUT_UNION),
    ]


def _get_cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _to_absolute(x: int, y: int) -> tuple[int, int]:
    """Convert pixel coords to the 0-65535 normalised range SendInput expects.

    Uses SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN so the mapping covers the
    entire virtual desktop on multi-monitor setups, not just the primary screen.
    """
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    vw = vw if vw > 0 else 1
    vh = vh if vh > 0 else 1
    ax = int(x * 65535 / vw)
    ay = int(y * 65535 / vh)
    return ax, ay


def _left_click(x: int, y: int) -> None:
    """Send a plain left click at absolute screen position (x, y)."""
    ax, ay = _to_absolute(x, y)
    flags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

    inputs = (INPUT * 2)()

    inputs[0].type = INPUT_MOUSE
    inputs[0]._input.mi.dx = ax
    inputs[0]._input.mi.dy = ay
    inputs[0]._input.mi.dwFlags = flags | MOUSEEVENTF_LEFTDOWN

    inputs[1].type = INPUT_MOUSE
    inputs[1]._input.mi.dx = ax
    inputs[1]._input.mi.dy = ay
    inputs[1]._input.mi.dwFlags = flags | MOUSEEVENTF_LEFTUP

    user32.SendInput(2, inputs, ctypes.sizeof(INPUT))


def _shift_click(x: int, y: int) -> None:
    """Send Shift+LeftClick at absolute screen position (x, y).

    Input sequence sent as a single SendInput call so the OS treats them
    atomically: Shift down, mouse down, mouse up, Shift up.
    """
    ax, ay = _to_absolute(x, y)
    flags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

    inputs = (INPUT * 4)()

    # 1. Shift key down
    inputs[0].type = INPUT_KEYBOARD
    inputs[0]._input.ki.wVk = VK_SHIFT
    inputs[0]._input.ki.dwFlags = 0

    # 2. Left mouse button down
    inputs[1].type = INPUT_MOUSE
    inputs[1]._input.mi.dx = ax
    inputs[1]._input.mi.dy = ay
    inputs[1]._input.mi.dwFlags = flags | MOUSEEVENTF_LEFTDOWN

    # 3. Left mouse button up
    inputs[2].type = INPUT_MOUSE
    inputs[2]._input.mi.dx = ax
    inputs[2]._input.mi.dy = ay
    inputs[2]._input.mi.dwFlags = flags | MOUSEEVENTF_LEFTUP

    # 4. Shift key up
    inputs[3].type = INPUT_KEYBOARD
    inputs[3]._input.ki.wVk = VK_SHIFT
    inputs[3]._input.ki.dwFlags = KEYEVENTF_KEYUP

    user32.SendInput(4, inputs, ctypes.sizeof(INPUT))


# ── Commands ──────────────────────────────────────────────────────────────────

@command(
    "mark here",
    aliases=["set marker", "start selection"],
    pack="text-editing",
)
def mark_here(app, remainder):
    """Anchor the selection start at the current mouse position."""
    global _marker_set, _marker_pos

    x, y = _get_cursor_pos()
    _left_click(x, y)
    _marker_pos = (x, y)
    _marker_set = True
    print(f"[MARKER] Anchor set at ({x}, {y})")
    return True


@command(
    "select to here",
    aliases=["end selection", "select to mark", "grab to here"],
    pack="text-editing",
)
def select_to_here(app, remainder):
    """Extend the selection from the anchor to the current mouse position."""
    global _marker_set, _marker_pos

    if not _marker_set:
        print("[MARKER] No anchor set — say 'mark here' first")
        return False

    x, y = _get_cursor_pos()
    _shift_click(x, y)
    _marker_set = False
    _marker_pos = None
    print(f"[MARKER] Selected to ({x}, {y}) — anchor cleared")
    return True
