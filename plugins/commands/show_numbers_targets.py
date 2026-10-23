"""Window metadata and target selection for the existing Show Numbers overlay.

The overlay module remains the owner of session state, command registration and
rendering. Helpers receive it explicitly so its existing provider seams apply.
"""
import re

from samsara.command_registry import DispatchState


def foreground_info(hwnd: int, logger) -> dict:
    """exe, window class, kind ('chromium' for Chrome_WidgetWin_* -- Chromium
    browsers and Electron apps -- else 'native'), physical rect, topmost."""
    info = {"hwnd": hwnd, "exe": "?", "cls": "?", "kind": "native", "rect": None, "topmost": None}
    if not hwnd:
        return info
    try:
        import win32gui
        info["cls"] = win32gui.GetClassName(hwnd)
        info["rect"] = tuple(win32gui.GetWindowRect(hwnd))
        GWL_EXSTYLE, WS_EX_TOPMOST = -20, 0x8
        info["topmost"] = bool(win32gui.GetWindowLong(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
    except Exception as e:
        logger.debug(f"_foreground_info: {e}")
    try:
        import psutil
        import win32process
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        info["exe"] = psutil.Process(pid).name().lower()
    except Exception as e:
        logger.debug(f"_foreground_info exe: {e}")
    if str(info["cls"]).startswith("Chrome_WidgetWin"):
        info["kind"] = "chromium"
    return info


def selection_number(text, overlay):
    """A whole bare label belongs to numbers only while its overlay is active."""
    if overlay.grid_active() or not overlay.is_overlay_active():
        return None
    from samsara.spoken_numbers import parse_spoken_number
    number = parse_spoken_number(text.strip().lower().rstrip('.!?'))
    return number if number is not None and 0 <= number <= 99 else None


def show_taskbar_numbers(app, overlay):
    """Discover primary-taskbar controls without activating their window."""
    from plugins.commands import click_by_name
    click_by_name.cancel()
    overlay._destroy_overlay(app)
    try:
        import win32gui
        owner = win32gui.GetForegroundWindow()
        target = win32gui.FindWindow('Shell_TrayWnd', None)
        if not owner or not target or not win32gui.IsWindowVisible(target):
            raise RuntimeError('taskbar unavailable')
        fg = overlay._foreground_info(target)
        fg['hwnd'] = owner  # The taskbar is targeted without activating it.
        elements, stats = overlay.get_service().overlay_elements(target_hwnd=target)
        if win32gui.GetForegroundWindow() != owner:
            return DispatchState.FAILED
        if not elements:
            raise RuntimeError('no taskbar controls')
    except Exception:
        overlay._speak(app, "No clickable taskbar controls found.")
        return DispatchState.FAILED
    overlay.logger.info("[SHOW_NUMBERS] taskbar enumerated %d controls %s", len(elements), stats)
    overlay._draw_overlay(app, elements, fg_info=fg)
    overlay._taskbar_mode = True
    return True


