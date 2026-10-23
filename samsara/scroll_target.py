"""Put voice wheel input over the foreground app without clicking it."""

import ctypes
from ctypes import wintypes
from functools import lru_cache


GA_ROOT = 2
MOUSE_BUTTONS = (0x01, 0x02, 0x04, 0x05, 0x06)


@lru_cache(maxsize=1)
def _user32():
    api = ctypes.WinDLL("user32", use_last_error=True)
    signatures = {
        "GetForegroundWindow": ([], wintypes.HWND),
        "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        "GetCursorPos": ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
        "WindowFromPoint": ([wintypes.POINT], wintypes.HWND),
        "GetClientRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        "ClientToScreen": ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
        "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_short),
        "SetCursorPos": ([ctypes.c_int, ctypes.c_int], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes = args
        function.restype = result
    return api


def prepare_scroll_target(config, *, api=None) -> bool:
    """Return whether to send the wheel; leave a moved pointer at its target.

    An existing pointer inside the foreground top-level keeps its pane. While
    any mouse button is held, preserve pointer-based scrolling without moving.
    Failed targeting cancels this wheel instead of scrolling an unrelated app.
    """
    if not config.get("scroll", {}).get("target_foreground", True):
        return True
    try:
        api = api if api is not None else _user32()

        def held():
            return any(api.GetAsyncKeyState(key) & 0x8000 for key in MOUSE_BUTTONS)

        if held():
            return True
        foreground = api.GetForegroundWindow()
        root = api.GetAncestor(foreground, GA_ROOT) if foreground else None
        if not root:
            return False
        point = wintypes.POINT()
        if not api.GetCursorPos(ctypes.byref(point)):
            return False
        under = api.WindowFromPoint(point)
        if under and api.GetAncestor(under, GA_ROOT) == root:
            return True
        rect = wintypes.RECT()
        if not api.GetClientRect(foreground, ctypes.byref(rect)):
            return False
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return False
        point = wintypes.POINT((rect.left + rect.right) // 2,
                              (rect.top + rect.bottom) // 2)
        if not api.ClientToScreen(foreground, ctypes.byref(point)):
            return False
        under = api.WindowFromPoint(point)
        if not under or api.GetAncestor(under, GA_ROOT) != root:
            return False
        if api.GetForegroundWindow() != foreground:
            return False
        if held():
            return True
        return bool(api.SetCursorPos(point.x, point.y))
    except OSError:
        return False
