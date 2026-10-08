"""Resolve a wake-profile target window without application state."""

import logging

logger = logging.getLogger("Samsara")


def _resolve_target_window(process_name, exclude_pids=None):
    """Find the first visible (or minimized) top-level window whose owning
    process matches *process_name* (case-insensitive executable name).

    Returns (hwnd, title) or None.
    exclude_pids: set of int PIDs to skip (Samsara's own PID, terminal PIDs).

    Uses psutil for fast PID-by-name lookup, then EnumWindows to find a
    window owned by one of those PIDs — same Win32 pattern as window_switcher.
    """
    import ctypes
    import ctypes.wintypes as _wt
    try:
        import psutil as _ps
    except ImportError:
        logger.debug("[WAKE-TARGET] psutil not available — cannot resolve target window")
        return None

    exclude = exclude_pids or set()
    target_pids = set()
    try:
        for proc in _ps.process_iter(['pid', 'name']):
            name = proc.info.get('name') or ''
            if name.lower() == process_name.lower() and proc.info['pid'] not in exclude:
                target_pids.add(proc.info['pid'])
    except Exception as exc:
        logger.exception(f"[WAKE-TARGET] process enumeration error: {exc}")
        return None

    if not target_pids:
        return None

    _user32 = ctypes.windll.user32
    found = []

    def _enum_cb(hwnd, _):
        visible   = bool(_user32.IsWindowVisible(hwnd))
        minimized = bool(_user32.IsIconic(hwnd))
        if not visible and not minimized:
            return True
        pid = _wt.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in target_pids:
            return True
        title_len = _user32.GetWindowTextLengthW(hwnd)
        if title_len == 0:
            return True
        buf = ctypes.create_unicode_buffer(title_len + 1)
        _user32.GetWindowTextW(hwnd, buf, title_len + 1)
        title = buf.value
        if title:
            found.append((hwnd, title))
        return True

    _WNDPROC = ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    _user32.EnumWindows(_WNDPROC(_enum_cb), 0)
    return found[0] if found else None
