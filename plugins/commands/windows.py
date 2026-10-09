"""Window management plugin.

Move windows between monitors by voice.

"Jarvis, bring Chrome here"              - move Chrome to monitor under cursor
"Jarvis, bring everything here"          - move all valid windows to cursor's monitor
"Jarvis, send Stremio to TV"             - move Stremio to TV monitor
"Jarvis, send Chrome to monitor 2"       - move Chrome to specific monitor
"Jarvis, put Warp on the left screen"    - left/right/middle/top/bottom by physical position
"Jarvis, put Warp on the left screen and Claude on the right"
                                         - two placements in one sentence, in order
"Jarvis, movie mode"                     - Stremio to TV fullscreen, optional Hyperion dim
"Jarvis, move mouse to TV"               - teleport cursor to TV monitor center
"Jarvis, save layout as work"            - save current window arrangement
"Jarvis, restore layout work"            - restore a saved arrangement
"Jarvis, list layouts"                   - print saved layout names
"Jarvis, delete layout work"             - remove a saved layout
"Jarvis, find lost windows"              - detect off-screen / orphaned windows
"Jarvis, rescue lost windows"            - move all lost windows to cursor's monitor
"Jarvis, find Chrome"                    - report state of all Chrome windows
"""

import ctypes
import ctypes.wintypes
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil
import win32api
import win32con
import win32gui
import win32process

from samsara.command_registry import DispatchResult, DispatchState
from samsara.plugin_commands import command

logger = logging.getLogger(__name__)

DWMWA_CLOAKED = 14
WS_EX_TOOLWINDOW = 0x00000080
HWND_TOP = 0
SWP_SHOWWINDOW = 0x0040

IGNORE_PROCESSES = {
    'explorer.exe',
    'dwm.exe',
    'searchapp.exe',
    'searchhost.exe',
    'shellexperiencehost.exe',
    'startmenuexperiencehost.exe',
    'obs64.exe',
    'steam.exe',
    'steamwebhelper.exe',
}

APP_ALIASES = {
    'chrome': 'chrome.exe',
    'firefox': 'firefox.exe',
    'edge': 'msedge.exe',
    'brave': 'brave.exe',
    'stremio': 'stremio.exe',
    'discord': 'Discord.exe',
    'spotify': 'Spotify.exe',
    'vscode': 'Code.exe',
    'vs code': 'Code.exe',
    'code': 'Code.exe',
    'notepad': 'notepad.exe',
    'word': 'WINWORD.EXE',
    'excel': 'EXCEL.EXE',
    'vlc': 'vlc.exe',
    'obs': 'obs64.exe',
    'terminal': 'WindowsTerminal.exe',
    'warp': 'warp.exe',
    'powershell': 'powershell.exe',
}

_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_bool,
    ctypes.c_size_t,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.wintypes.RECT),
    ctypes.c_size_t,
)


# ---------------------------------------------------------------------------
# Monitor enumeration
# ---------------------------------------------------------------------------

def get_monitors():
    monitors = []

    def _cb(hmon, hdc, lprect, lparam):
        try:
            info = win32api.GetMonitorInfo(hmon)
            work = info['Work']
            monitors.append({
                'handle': hmon,
                'rect': work,
                'width': work[2] - work[0],
                'height': work[3] - work[1],
                'primary': bool(info['Flags'] & 1),
                'device': info['Device'],
            })
        except Exception as e:
            logger.debug("GetMonitorInfo failed for hmon %s: %s", hmon, e)
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_cb), 0)
    monitors.sort(key=lambda m: m['rect'][0])
    for i, m in enumerate(monitors):
        m['index'] = i + 1

    primary = next((m['device'] for m in monitors if m['primary']), 'none')
    tv_candidate = monitors[-1]['device'] if monitors else 'none'
    logger.info(
        "Monitors: %d total, primary=%s, rightmost=%s",
        len(monitors), primary, tv_candidate,
    )
    return monitors


def get_monitor_under_cursor(monitors=None):
    if monitors is None:
        monitors = get_monitors()
    try:
        x, y = win32api.GetCursorPos()
    except Exception:
        return next((m for m in monitors if m['primary']), monitors[0] if monitors else None)

    for m in monitors:
        l, t, r, b = m['rect']
        if l <= x < r and t <= y < b:
            return m

    return next((m for m in monitors if m['primary']), monitors[0] if monitors else None)


def get_tv_monitor(app=None, monitors=None):
    if monitors is None:
        monitors = get_monitors()
    if not monitors:
        return None

    tv_device = None
    if app is not None:
        tv_device = app.config.get('window_manager', {}).get('tv_device', None)

    if tv_device:
        match = next((m for m in monitors if m['device'] == tv_device), None)
        if match:
            return match
        logger.warning(
            "Configured tv_device '%s' not found, falling back to rightmost monitor", tv_device
        )
    else:
        logger.info(
            "tv_device not configured; using rightmost monitor (%s). "
            "Add 'window_manager.tv_device' to config for stability.",
            monitors[-1]['device'],
        )

    return monitors[-1]


def get_monitor_by_index(index, monitors=None):
    if monitors is None:
        monitors = get_monitors()
    return next((m for m in monitors if m['index'] == index), None)


# ---------------------------------------------------------------------------
# Window filtering
# ---------------------------------------------------------------------------

def _is_cloaked(hwnd):
    try:
        val = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)
        )
        if val.value != 0:
            logger.debug("hwnd %s cloaked", hwnd)
            return True
        return False
    except Exception:
        return False


def is_valid_app_window(hwnd, extra_ignore=None):
    if not win32gui.IsWindowVisible(hwnd):
        return False
    if not win32gui.GetWindowText(hwnd):
        return False
    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if ex_style & WS_EX_TOOLWINDOW:
        logger.debug("hwnd %s toolwindow, skipping", hwnd)
        return False
    if _is_cloaked(hwnd):
        return False
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc_name = psutil.Process(pid).name().lower()
        blocked = {p.lower() for p in IGNORE_PROCESSES}
        if extra_ignore:
            blocked.update(p.lower() for p in extra_ignore)
        if proc_name in blocked:
            logger.debug("hwnd %s process %s blocked", hwnd, proc_name)
            return False
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Window finding
# ---------------------------------------------------------------------------

def find_windows_by_app(app_name, extra_ignore=None):
    name_lower = app_name.lower().strip()
    target_exe = APP_ALIASES.get(name_lower)
    results = []

    def _cb(hwnd, _):
        if not is_valid_app_window(hwnd, extra_ignore):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc_name = psutil.Process(pid).name().lower()
        except Exception:
            return True

        if target_exe:
            if proc_name == target_exe.lower():
                results.append(hwnd)
        else:
            if name_lower in proc_name:
                results.append(hwnd)
                return True
            if name_lower in win32gui.GetWindowText(hwnd).lower():
                results.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    logger.debug("find_windows_by_app('%s'): %d windows", app_name, len(results))
    return results


def get_all_movable_windows(extra_ignore=None):
    results = []

    def _cb(hwnd, _):
        if not is_valid_app_window(hwnd, extra_ignore):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            title = win32gui.GetWindowText(hwnd)
            results.append((hwnd, title, pid))
        except Exception as e:
            logger.debug(f"_cb: {e}")
        return True

    win32gui.EnumWindows(_cb, None)
    return results


# ---------------------------------------------------------------------------
# Window moving
# ---------------------------------------------------------------------------

def raise_window(hwnd: int, *, activate: bool, preflight=None) -> bool:
    """Restore if minimized, then steal foreground using the full incantation.

    Steps:
      1. Restore if minimised.
      2. Relax the foreground lock timeout to 0 (best-effort).
      3. Dual AttachThreadInput: both the current-foreground thread AND the
         calling thread attach to the target thread's input queue — this is
         what makes the steal work from background/audio threads.
      4. ShowWindow, BringWindowToTop, TOPMOST flip, SetForegroundWindow,
         SetActiveWindow, SetFocus.
      5. Detach inputs.
      6. Verify via GetForegroundWindow() == hwnd after a 50 ms settle.

    Returns True if the window is actually in the foreground afterwards,
    False if Windows still refused (caller should proceed anyway and log it).
    Backward-safe: all existing callers ignore the return value.

    With ``activate=False`` this performs only the non-activating raise
    (restore, show, bring-to-top, and the temporary TOPMOST flip), leaving
    foreground ownership untouched. This is for layout restores, where
    activating each window would thrash the desktop; successful completion of
    that non-activating sequence returns True.
    """
    try:
        user32 = ctypes.windll.user32

        def checked(fn, *args):
            if preflight is not None and not preflight():
                raise RuntimeError("bound window changed")
            return fn(*args)

        # 1. Restore if minimised.
        if checked(user32.IsIconic, hwnd):
            checked(user32.ShowWindow, hwnd, win32con.SW_RESTORE)

        _SWP_NOMOVE = 0x0002
        _SWP_NOSIZE = 0x0001
        _SWP_NOACTIVATE = 0x0010
        _SWP_FLAGS = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE

        if not activate:
            checked(user32.ShowWindow, hwnd, win32con.SW_SHOW)
            checked(user32.BringWindowToTop, hwnd)
            # TOPMOST flip brings the window forward without permanently
            # pinning it as always-on-top or stealing foreground ownership.
            checked(user32.SetWindowPos, hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, _SWP_FLAGS)
            checked(user32.SetWindowPos, hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, _SWP_FLAGS)
            return True

        # 2. Capture current foreground and thread IDs.
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
        our_tid = ctypes.windll.kernel32.GetCurrentThreadId()

        # 3. Relax foreground lock timeout to 0 ms (best-effort; needs UIPI access).
        try:
            timeout = ctypes.c_ulong(0)
            user32.SystemParametersInfoW(0x2001, 0, ctypes.byref(timeout), 0x0002)
        except Exception as exc:
            logger.debug("raise_window: %s", exc)

        # 4. Attach both the foreground-owner thread AND our calling thread to
        #    the target's input queue, then perform the full steal sequence.
        attached_fg = bool(
            fg_tid and fg_tid != tgt_tid and
            user32.AttachThreadInput(fg_tid, our_tid, True)
        )
        attached_us = bool(
            our_tid and our_tid != tgt_tid and
            user32.AttachThreadInput(our_tid, tgt_tid, True)
        )
        try:
            checked(user32.ShowWindow, hwnd, win32con.SW_SHOW)
            checked(user32.BringWindowToTop, hwnd)
            # TOPMOST flip brings the window above everything without
            # permanently pinning it as always-on-top.
            checked(user32.SetWindowPos, hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, _SWP_FLAGS)
            checked(user32.SetWindowPos, hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, _SWP_FLAGS)
            checked(user32.SetForegroundWindow, hwnd)
            checked(user32.SetActiveWindow, hwnd)
            checked(user32.SetFocus, hwnd)
        finally:
            if attached_us:
                user32.AttachThreadInput(our_tid, tgt_tid, False)
            if attached_fg:
                user32.AttachThreadInput(fg_tid, our_tid, False)

        # 5. Authoritative verification: trust GetForegroundWindow, not the
        #    return value of SetForegroundWindow (which lies under lock).
        time.sleep(0.05)
        return user32.GetForegroundWindow() == hwnd

    except Exception as exc:
        logger.debug("raise_window failed for hwnd %s: %s", hwnd, exc)
        return False


def move_window_to_monitor(hwnd, monitor, *, preflight=None):
    def ready():
        return preflight is None or preflight()
    try:
        if not ready():
            return False
        if win32gui.IsIconic(hwnd):
            if not ready():
                return False
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        if not ready():
            return False
        rect = win32gui.GetWindowRect(hwnd)
    except Exception as e:
        logger.warning("GetWindowRect failed for hwnd %s: %s", hwnd, e)
        return False

    w = min(rect[2] - rect[0], monitor['width'])
    h = min(rect[3] - rect[1], monitor['height'])
    ml, mt = monitor['rect'][0], monitor['rect'][1]
    x = ml + (monitor['width'] - w) // 2
    y = mt + (monitor['height'] - h) // 2

    if not ready():
        return False
    logger.info("Moving hwnd %s to monitor %s (%d,%d)", hwnd, monitor['index'], x, y)
    try:
        win32gui.SetWindowPos(hwnd, HWND_TOP, x, y, w, h, SWP_SHOWWINDOW)
    except Exception:
        return False
    raised = (raise_window(hwnd, activate=True, preflight=preflight) if preflight is not None
              else raise_window(hwnd, activate=True))
    if not raised:
        logger.debug("Moved hwnd %s to monitor %s but Windows refused to raise it", hwnd, monitor['index'])
        return False
    try:
        return ready() and win32gui.GetWindowRect(hwnd) == (x, y, x + w, y + h)
    except Exception:
        return False


def maximize_window_on_monitor(hwnd, monitor):
    move_window_to_monitor(hwnd, monitor)
    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)


# ---------------------------------------------------------------------------
# Cursor teleportation
# ---------------------------------------------------------------------------

def teleport_cursor(monitor):
    l, t, r, b = monitor['rect']
    cx, cy = (l + r) // 2, (t + b) // 2
    win32api.SetCursorPos((cx, cy))
    logger.info("Cursor -> monitor %s center (%d,%d)", monitor['index'], cx, cy)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _get_extra_ignore(app):
    if app is None:
        return []
    return app.config.get('window_manager', {}).get('ignore_processes', [])


def _strip_leading_the(s):
    return s[4:] if s.startswith('the ') else s


class UnresolvedDestination:
    """Explicit "I could not resolve this screen" marker returned by
    _parse_destination -- falsy, carries the reason, never mistaken for a
    monitor dict. The handlers turn it into a REJECTED result with
    'refused: unknown screen ...' so a bad destination is never a silent
    no-op."""

    __slots__ = ('text', 'reason')

    def __init__(self, text, reason):
        self.text = text
        self.reason = reason

    def __bool__(self):
        return False

    def __repr__(self):
        return f"UnresolvedDestination({self.text!r}: {self.reason})"


_SCREEN_NOUNS = ('screen', 'monitor', 'display')
_POSITION_WORDS = {
    'left': 'left', 'right': 'right',
    'middle': 'middle', 'center': 'middle', 'centre': 'middle', 'central': 'middle',
    'top': 'top', 'upper': 'top', 'bottom': 'bottom', 'lower': 'bottom',
}
_ORDINALS = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5, 'sixth': 6}
_DESTINATION_WORDS = frozenset(_POSITION_WORDS) | frozenset(_ORDINALS) | {'tv', 'here', 'other', 'main', 'primary'}


def _center(m):
    l, t, r, b = m['rect']
    return (l + r) / 2.0, (t + b) / 2.0


def _monitor_by_position(position, monitors, text=''):
    """left = smallest x, right = largest x, top/bottom likewise on y,
    middle = the one between on x -- by physical work-area geometry, never
    by index or device name."""
    if not monitors:
        return UnresolvedDestination(text, 'no monitors')
    if len(monitors) == 1:
        return UnresolvedDestination(text, 'only one screen')
    xs = sorted(monitors, key=lambda m: (_center(m)[0], _center(m)[1]))
    ys = sorted(monitors, key=lambda m: (_center(m)[1], _center(m)[0]))
    if position in ('left', 'right'):
        # Left/right only mean something when the extremes do not share a
        # column: two stacked screens overlap in x and have no left or right.
        if xs[0]['rect'][2] > xs[-1]['rect'][0]:
            return UnresolvedDestination(text, 'screens are stacked, none is left or right')
        return xs[0] if position == 'left' else xs[-1]
    if position in ('top', 'bottom'):
        # Side-by-side screens (even of different heights) overlap in y.
        if ys[0]['rect'][3] > ys[-1]['rect'][1]:
            return UnresolvedDestination(text, 'screens are side by side, none is top or bottom')
        return ys[0] if position == 'top' else ys[-1]
    if position == 'middle':
        if len(monitors) < 3:
            return UnresolvedDestination(text, f'no middle screen with {len(monitors)} monitors')
        if len(monitors) % 2 == 0:
            return UnresolvedDestination(text, f'no single middle screen with {len(monitors)} monitors')
        return xs[len(xs) // 2]
    return UnresolvedDestination(text, f'unknown position {position!r}')


def _parse_destination(dest_text, app, monitors=None, *, strict=False):
    """Resolve spoken destination text to a monitor dict, or an
    UnresolvedDestination (falsy) saying why.

        tv                       -> configured tv_device, else rightmost
        here                     -> the monitor under the cursor
        monitor N / screen N / N -> index N (monitors sorted by x)
        second screen, third...  -> ordinal index
        left/right/middle/centre/top/bottom [screen|monitor|display]
                                 -> by physical position (_monitor_by_position)
        the other screen         -> the monitor the cursor is NOT on (2 monitors)
        main / primary [screen]  -> the primary monitor

    Policy uses strict=True: failed live-state reads propagate, without the
    handler reader's best-effort fallback. Destination grammar is unchanged.
    """
    if monitors is None:
        monitors = _strict_monitors() if strict else get_monitors()
    t = _strip_leading_the((dest_text or '').lower().strip())
    words = t.split()
    if not words:
        return UnresolvedDestination(dest_text, 'no destination given')

    if 'tv' in words:
        m = get_tv_monitor(app, monitors)
        return m if m else UnresolvedDestination(dest_text, 'no tv monitor')
    if 'here' in words:
        m = _strict_cursor_monitor(monitors) if strict else get_monitor_under_cursor(monitors)
        return m if m else UnresolvedDestination(dest_text, 'no monitor under the cursor')
    for word in words:
        if word.isdigit():
            m = get_monitor_by_index(int(word), monitors)
            return m if m else UnresolvedDestination(dest_text, f'no monitor {word} (have {len(monitors)})')
        if word in _ORDINALS:
            m = get_monitor_by_index(_ORDINALS[word], monitors)
            return m if m else UnresolvedDestination(dest_text, f'no {word} monitor (have {len(monitors)})')
    if 'other' in words:
        if len(monitors) != 2:
            return UnresolvedDestination(dest_text, f"'other' is ambiguous with {len(monitors)} monitors")
        here = _strict_cursor_monitor(monitors) if strict else get_monitor_under_cursor(monitors)
        return next((m for m in monitors if m is not here), monitors[0])
    if 'main' in words or 'primary' in words:
        m = next((m for m in monitors if m.get('primary')), None)
        return m if m else UnresolvedDestination(dest_text, 'no primary monitor')
    for word in words:
        if word in _POSITION_WORDS:
            return _monitor_by_position(_POSITION_WORDS[word], monitors, dest_text)
    return UnresolvedDestination(dest_text, 'unknown screen')


def _is_destination_text(text):
    """Does this fragment read as a destination on its own ('left', 'the
    right screen', 'monitor 2', 'tv')? Pure grammar, no monitor lookup."""
    words = _strip_leading_the((text or '').lower().strip()).split()
    if not words:
        return False
    return all(w in _DESTINATION_WORDS or w in _SCREEN_NOUNS or w.isdigit() or w == 'the' for w in words) \
        and any(w in _DESTINATION_WORDS or w.isdigit() for w in words)


def _parse_send_remainder(remainder):
    """Split 'chrome to tv' -> ('chrome', 'tv'), 'this to monitor 2' -> (None, 'monitor 2'),
    'claude right' / 'warp left screen' -> ('claude', 'right') / ('warp', 'left screen')."""
    r = remainder.lower().strip()
    r = _strip_leading_the(r)

    for sep in (' to the ', ' to ', ' on the ', ' on ', ' onto the ', ' onto '):
        if sep in r:
            app_part, dest_part = r.split(sep, 1)
            app_part = app_part.strip()
            app_name = None if app_part in ('', 'this') else app_part
            return app_name, dest_part.strip()

    # "X left" / "X right screen": a trailing destination with no preposition.
    words = r.split()
    for cut in range(1, len(words)):
        tail = ' '.join(words[cut:])
        if _is_destination_text(tail) and not _is_destination_text(' '.join(words[:cut])):
            return ' '.join(words[:cut]), tail
    return None, r


def _parse_placements(remainder):
    """Split a compound 'X on the left and Y on the right' / 'X left, Y right'
    into [(app, destination), ...] in spoken order. A separator only splits
    when BOTH sides parse to a destination, so an app name containing
    'and' stays whole."""
    r = (remainder or '').strip()
    if not r:
        return []
    for sep in (', and ', ' and ', ', ', '; '):
        if sep not in r.lower():
            continue
        idx = r.lower().index(sep)
        left, right = r[:idx], r[idx + len(sep):]
        first, second = _parse_send_remainder(left), _parse_send_remainder(right)
        if first[1] and second[1] and _is_destination_text(first[1]) and _is_destination_text(second[1]):
            return [first] + _parse_placements(right)
    return [_parse_send_remainder(r)]


def _strict_monitors():
    """Read-only policy snapshot; detect enumeration/monitor-info failures
    that the handler's best-effort monitor reader otherwise tolerates."""
    monitors = get_monitors()
    handles = {int(handle) for handle, _hdc, _rect in win32api.EnumDisplayMonitors()}
    if not handles or {int(m['handle']) for m in monitors} != handles:
        raise ValueError('incomplete monitor enumeration')
    return monitors


def _strict_cursor_monitor(monitors):
    """Read the cursor without the handler reader's exception fallback."""
    x, y = win32api.GetCursorPos()  # a failed read propagates to validation
    for monitor in monitors:
        l, t, r, b = monitor['rect']
        if l <= x < r and t <= y < b:
            return monitor
    return next((m for m in monitors if m['primary']), monitors[0] if monitors else None)


def parse_send_slots(remainder):
    """Extract declared slots with the handler's parser, without any effects.
    Compound placements carry all destinations so policy checks every one
    before the handler can move the first window."""
    placements = _parse_placements(remainder)
    if not placements:
        return {}
    destinations = tuple(dest for _name, dest in placements)
    slots = {'monitor': destinations[0] if len(destinations) == 1 else destinations}
    names = tuple(name for name, _dest in placements if name is not None)
    if names:
        slots['app_name'] = names[0] if len(names) == 1 else names
    return slots


def _validate_monitor(value, *, app=None):
    destinations = (value,) if isinstance(value, str) else value
    if not isinstance(destinations, (list, tuple)) or not destinations:
        return False
    if not all(isinstance(dest, str) and dest.strip() for dest in destinations):
        return False
    monitors = _strict_monitors()
    return all(bool(_parse_destination(dest, app, monitors, strict=True))
               for dest in destinations)


def _validate_app_name(value, *, app=None):
    """Normalize each single name as the handler does, without placement
    splitting or window lookup. Window existence remains the handler's check."""
    names = (value,) if isinstance(value, str) else value
    if not isinstance(names, (list, tuple)) or not names:
        return False
    for name in names:
        if not isinstance(name, str) or not name.strip():
            return False
        normalized = _strip_leading_the(name.lower().strip())
        if normalized in ('', 'this'):
            return False
    return True


# Static registration by the module that owns both parsers. No transcript,
# config or model-provided field can install or choose a validator.
from samsara.arg_types import register_arg_type  # noqa: E402

register_arg_type('monitor', _validate_monitor)
register_arg_type('app_name', _validate_app_name)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

@command("bring", aliases=["bring back", "get", "grab", "fetch"], pack="window-management",
         risk_class="ui", param_schema={"app_name": {"type": "app_name", "required": False}})
def handle_bring(app, remainder):
    """Brings the app you name to the screen your pointer is on."""
    logger.info("bring: remainder='%s'", remainder)
    extra_ignore = _get_extra_ignore(app)
    target = get_monitor_under_cursor()

    r = remainder.lower().strip()
    for suffix in (' here', ' back here', ' to here'):
        if r.endswith(suffix):
            r = r[:-len(suffix)].strip()
            break

    if r in ('', 'everything', 'all', 'all windows', 'it all', 'them all', 'all of them'):
        windows = get_all_movable_windows(extra_ignore)
        for hwnd, _, __ in windows:
            move_window_to_monitor(hwnd, target)
        logger.info("Brought %d windows to monitor %s", len(windows), target['index'])
        return True

    logger.info("Bringing '%s' to monitor %s", r, target['index'])
    windows = find_windows_by_app(r, extra_ignore)
    if not windows:
        print(f"[WINDOWS] No windows found for: {r}")
        return True
    for hwnd in windows:
        move_window_to_monitor(hwnd, target)
    return True


def _windows_for(app_name, extra_ignore):
    """The window(s) a spoken app name refers to: the alias/process/title
    match this plugin has always used, then app_verbs' fuzzy live-window
    resolver (the same one 'focus warp' uses) as a fallback. EnumWindows
    hands windows back in z-order, top first, so [0] is the most recently
    active one."""
    hwnds = find_windows_by_app(app_name, extra_ignore)
    if hwnds:
        return hwnds, 'alias'
    try:
        from plugins.commands.app_verbs import resolve_window  # noqa: PLC0415
        match = resolve_window(app_name)
    except Exception as e:
        logger.debug("resolve_window(%r) unavailable: %s", app_name, e)
        match = None
    if match is not None:
        return [match[0]], 'resolved'
    return [], 'none'


def _place_one(app_name, dest_text, app, monitors, extra_ignore):
    """One placement through the single-window path. Returns a detail dict
    with 'ok' and either what moved or why not."""
    target = _parse_destination(dest_text, app, monitors)
    if not target:
        return {'ok': False, 'app': app_name, 'destination': dest_text,
                'reason': f"refused: unknown screen '{dest_text}' ({target.reason})"}
    detail = {'ok': True, 'app': app_name or 'foreground', 'destination': dest_text,
              'monitor': target['index']}
    if app_name is None:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return {'ok': False, 'app': 'foreground', 'destination': dest_text, 'reason': 'no foreground window'}
        move_window_to_monitor(hwnd, target)
        return detail
    hwnds, how = _windows_for(app_name, extra_ignore)
    if not hwnds:
        return {'ok': False, 'app': app_name, 'destination': dest_text,
                'reason': f"no window found for '{app_name}'"}
    hwnd = hwnds[0]
    move_window_to_monitor(hwnd, target)
    detail['hwnd'] = hwnd
    detail['resolved_by'] = how
    if len(hwnds) > 1:
        detail['note'] = f"{len(hwnds)} {app_name} windows; moved the most recently active one"
    return detail


@command("send", aliases=["move", "put", "throw"], pack="window-management",
         risk_class="ui", param_schema={"app_name": {"type": "app_name", "required": False}, "monitor": {"type": "monitor", "required": True}})
def handle_send(app, remainder):
    """Moves the app you name to the screen you name, such as the left one.

    A second placement that fails after the first succeeded is reported as
    FAILED with the detail naming it: partial completion is never called success
    and the first move is never undone.
    """
    logger.info("send: remainder='%s'", remainder)
    extra_ignore = _get_extra_ignore(app)

    placements = _parse_placements(remainder)
    if not placements or not any(dest for _app, dest in placements):
        logger.warning("send: could not parse destination from '%s'", remainder)
        return DispatchResult(DispatchState.REJECTED, 'send', 'send',
                              {'reason': f"refused: no destination in '{remainder}'"})

    monitors = get_monitors()
    done, outcome = [], None
    for app_name, dest_text in placements:
        outcome = _place_one(app_name, dest_text, app, monitors, extra_ignore)
        if not outcome['ok']:
            break
        logger.info("Placed %s on monitor %s", outcome['app'], outcome['monitor'])
        if outcome.get('note'):
            print(f"[WINDOWS] {outcome['note']}")
        done.append(outcome)

    if outcome is not None and outcome['ok']:
        return DispatchResult(DispatchState.COMPLETED, 'send', 'send', {'placements': done})

    if done:
        # Partial completion: say exactly what happened.
        msg = (f"partial: placed {', '.join(d['app'] for d in done)}; "
               f"could not place {outcome['app']} -- {outcome['reason']}")
        print(f"[WINDOWS] {msg}")
        logger.warning("send: %s", msg)
        return DispatchResult(DispatchState.FAILED, 'send', 'send',
                              {'partial': True, 'placements': done, 'failed': outcome, 'reason': msg})

    print(f"[WINDOWS] {outcome['reason']}")
    logger.warning("send: %s", outcome['reason'])
    state = DispatchState.REJECTED if outcome['reason'].startswith('refused') else DispatchState.FAILED
    return DispatchResult(state, 'send', 'send', {'placements': [], 'failed': outcome, 'reason': outcome['reason']})


@command("movie mode", aliases=["movie time", "couch mode", "tv mode"], pack="window-management",
         risk_class="ui")
def handle_movie_mode(app, remainder):
    """Puts video on the TV screen and clears the other screens."""
    extra_ignore = _get_extra_ignore(app)
    tv = get_tv_monitor(app)
    if tv is None:
        print("[WINDOWS] No TV monitor found")
        return True

    windows = find_windows_by_app('stremio', extra_ignore)
    if not windows:
        print("[WINDOWS] Stremio is not open")
        return True

    for hwnd in windows:
        maximize_window_on_monitor(hwnd, tv)
    logger.info("Movie mode: %d Stremio window(s) on monitor %s", len(windows), tv['index'])

    try:
        hl = sys.modules.get('samsara_plugin_hyperion_lights')
        if hl is not None and app is not None:
            hl._send(app, {
                "command": "color",
                "color": [255, 100, 30],
                "priority": 1,
                "origin": "Samsara",
            })
    except Exception as e:
        logger.debug(f"handle_movie_mode: {e}")

    return True


# ---------------------------------------------------------------------------
# Saved layouts: storage helpers
# ---------------------------------------------------------------------------

_RESERVED_NAMES = frozenset({'default', 'current', 'none'})


def _get_layouts_path():
    appdata = os.environ.get('APPDATA', str(Path.home() / 'AppData' / 'Roaming'))
    layouts_dir = Path(appdata) / 'Samsara'
    layouts_dir.mkdir(parents=True, exist_ok=True)
    return layouts_dir / 'window_layouts.json'


def _load_all_layouts():
    path = _get_layouts_path()
    if not path.exists():
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load layouts: %s", e)
        return {}


def _save_all_layouts(layouts):
    path = _get_layouts_path()
    tmp = path.with_suffix('.json.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(layouts, f, indent=2)
        os.replace(str(tmp), str(path))
    except Exception as e:
        logger.error("Failed to save layouts: %s", e)
        try:
            tmp.unlink()
        except OSError as e:
            logger.debug(f"_save_all_layouts: {e}")
        raise


def _extract_layout_name(remainder):
    name = remainder.strip().lower()
    for filler in ('as ', 'called ', 'named ', 'the '):
        if name.startswith(filler):
            name = name[len(filler):].strip()
            break
    if not name:
        return None
    if name in _RESERVED_NAMES:
        print(f"[LAYOUTS] '{name}' is a reserved name")
        return None
    if len(name) > 30:
        print(f"[LAYOUTS] Name too long (max 30 chars)")
        return None
    return name


def _save_current_layout(name):
    monitors = get_monitors()
    windows_list = []

    for hwnd, _title, pid in get_all_movable_windows():
        try:
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            continue

        # Determine which monitor contains the window's center point
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        monitor_index = 1
        for m in monitors:
            ml, mt, mr, mb = m['rect']
            if ml <= cx < mr and mt <= cy < mb:
                monitor_index = m['index']
                break

        try:
            placement = win32gui.GetWindowPlacement(hwnd)
            maximized = (placement[1] == win32con.SW_SHOWMAXIMIZED)
        except Exception:
            maximized = False

        try:
            app_name = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            app_name = 'unknown'

        windows_list.append({
            'app': app_name,
            'title_pattern': '',
            'monitor_index': monitor_index,
            'rect': list(rect),
            'maximized': maximized,
        })

    layouts = _load_all_layouts()
    layouts[name] = {
        'created': datetime.now().isoformat(timespec='seconds'),
        'windows': windows_list,
    }
    _save_all_layouts(layouts)
    print(f"[LAYOUTS] Saved '{name}' with {len(windows_list)} windows")


def _restore_layout(name):
    layouts = _load_all_layouts()
    if name not in layouts:
        print(f"[LAYOUTS] No layout named '{name}'")
        return

    monitors = get_monitors()
    entries = layouts[name]['windows']
    total = len(entries)
    restored = 0

    for entry in entries:
        app_name = entry['app']
        monitor_index = entry.get('monitor_index', 1)
        saved_rect = entry.get('rect', [100, 100, 900, 700])
        maximized = entry.get('maximized', False)

        target = get_monitor_by_index(monitor_index, monitors)
        if target is None:
            target = next((m for m in monitors if m['primary']),
                          monitors[0] if monitors else None)

        # Build the restore rect: use saved coords when the monitor is available;
        # center on the primary/fallback monitor otherwise.
        if get_monitor_by_index(monitor_index, monitors) is not None:
            l, t, r2, b = saved_rect
            restore_l, restore_t, restore_w, restore_h = l, t, r2 - l, b - t
        else:
            if target is None:
                continue
            w = min(saved_rect[2] - saved_rect[0], target['width'])
            h = min(saved_rect[3] - saved_rect[1], target['height'])
            restore_l = target['rect'][0] + (target['width'] - w) // 2
            restore_t = target['rect'][1] + (target['height'] - h) // 2
            restore_w, restore_h = w, h

        # Try finding the window — first by exact process name, then by stem
        hwnds = find_windows_by_app(app_name)
        if not hwnds:
            stem = app_name.rsplit('.', 1)[0] if '.' in app_name else app_name
            hwnds = find_windows_by_app(stem)
        if not hwnds:
            print(f"[LAYOUTS] Skipped: {app_name} not running")
            continue

        for hwnd in hwnds:
            try:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetWindowPos(
                    hwnd, HWND_TOP,
                    restore_l, restore_t, restore_w, restore_h,
                    SWP_SHOWWINDOW,
                )
                if maximized:
                    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
                if not raise_window(hwnd, activate=False):
                    logger.debug("Restored hwnd %s but Windows refused to raise it", hwnd)
                restored += 1
            except Exception as e:
                logger.warning("Restore failed for %s hwnd=%s: %s", app_name, hwnd, e)

    print(f"[LAYOUTS] Restored {restored} of {total} windows")


def _delete_layout(name):
    layouts = _load_all_layouts()
    if name not in layouts:
        print(f"[LAYOUTS] No layout named '{name}'")
        return
    del layouts[name]
    _save_all_layouts(layouts)
    print(f"[LAYOUTS] Deleted '{name}'")


# ---------------------------------------------------------------------------
# Lost window helpers
# ---------------------------------------------------------------------------

def _is_rect_on_any_monitor(rect, monitors=None):
    """True if rect has any overlap with any monitor's work area."""
    if monitors is None:
        monitors = get_monitors()
    left, top, right, bottom = rect
    for m in monitors:
        ml, mt, mr, mb = m['rect']
        if right > ml and left < mr and bottom > mt and top < mb:
            return True
    return False


def _detect_lost_windows():
    """Return list of dicts for windows whose rect is entirely off all monitors."""
    monitors = get_monitors()
    lost = []
    for hwnd, title, pid in get_all_movable_windows():
        try:
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            continue
        if not _is_rect_on_any_monitor(rect, monitors):
            try:
                app_name = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                app_name = 'unknown'
            lost.append({
                'hwnd': hwnd,
                'title': title,
                'app': app_name,
                'rect': rect,
            })
    return lost


# ---------------------------------------------------------------------------
# Saved layout commands
# ---------------------------------------------------------------------------

@command("save layout",
         aliases=["save window layout", "save this layout"],
         pack="window-management",
         risk_class="write", param_schema={"name": {"type": "text", "required": False}},
)
def handle_save_layout(app, remainder):
    """Saves where your windows are now, under the name you give it."""
    name = _extract_layout_name(remainder)
    if not name:
        print("[LAYOUTS] No valid name provided")
        return True
    _save_current_layout(name)
    return True


@command("restore layout",
         aliases=["load layout", "restore window layout"],
         pack="window-management",
         risk_class="ui", param_schema={"name": {"type": "text", "required": False}},
)
def handle_restore_layout(app, remainder):
    """Puts your windows back the way a saved layout had them."""
    name = _extract_layout_name(remainder)
    if not name:
        print("[LAYOUTS] No valid name provided")
        return True
    _restore_layout(name)
    return True


@command("list layouts",
         aliases=["show layouts", "what layouts"],
         pack="window-management",
         risk_class="read",
)
def handle_list_layouts(app, remainder):
    """Reads out the window layouts you have saved."""
    layouts = _load_all_layouts()
    if not layouts:
        print("[LAYOUTS] No saved layouts yet")
    else:
        print(f"[LAYOUTS] Available: {', '.join(sorted(layouts.keys()))}")
    return True


@command("delete layout",
         aliases=["forget layout", "remove layout"],
         pack="window-management",
         risk_class="destructive", param_schema={"name": {"type": "text", "required": False}},
)
def handle_delete_layout(app, remainder):
    """Deletes the saved window layout you name."""
    name = _extract_layout_name(remainder)
    if not name:
        print("[LAYOUTS] No valid name provided")
        return True
    _delete_layout(name)
    return True


# ---------------------------------------------------------------------------
# Lost window commands
# ---------------------------------------------------------------------------

@command("find lost windows",
         aliases=["lost windows", "where are my windows"],
         pack="window-management",
         risk_class="read",
)
def handle_find_lost_windows(app, remainder):
    """Reports any windows sitting off-screen where you cannot reach them."""
    lost = _detect_lost_windows()
    if not lost:
        print("[LOST] No lost windows detected")
    else:
        print(f"[LOST] Found {len(lost)} lost window(s):")
        for w in lost:
            print(f"  - {w['app']}: {w['title'][:60]}")
    return True


@command("rescue lost windows",
         aliases=["recover windows", "bring back lost windows"],
         pack="window-management",
         risk_class="ui",
)
def handle_rescue_lost(app, remainder):
    """Pulls windows that are off-screen back onto a screen you can see."""
    lost = _detect_lost_windows()
    if not lost:
        print("[LOST] No lost windows to rescue")
        return True
    target = get_monitor_under_cursor()
    for w in lost:
        move_window_to_monitor(w['hwnd'], target)
    print(f"[LOST] Rescued {len(lost)} window(s) to monitor {target['index']}")
    return True


@command("find window",
         pack="window-management",
         risk_class="ui", param_schema={"app_name": {"type": "app_name", "required": False}},
)
def handle_find_specific(app, remainder):
    """Finds the window for the app you name and brings it to the front."""
    if not remainder or not remainder.strip():
        print("[FIND] No app specified")
        return True
    app_name = remainder.strip()
    hwnds = find_windows_by_app(app_name)
    if not hwnds:
        print(f"[FIND] No {app_name} windows open")
        return True
    monitors = get_monitors()
    for hwnd in hwnds:
        try:
            rect = win32gui.GetWindowRect(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if win32gui.IsIconic(hwnd):
                status = "minimized"
            elif _is_rect_on_any_monitor(rect, monitors):
                status = "visible"
            else:
                status = "OFF-SCREEN"
            print(f"[FIND] {app_name}: {title[:60]} [{status}]")
        except Exception as e:
            logger.warning("find_specific hwnd=%s: %s", hwnd, e)
    return True


@command("cursor to", aliases=[
    "mouse to",
    "pointer to",
    "move cursor to",
    "move mouse to",
    "move the cursor to",
    "move the mouse to",
    "teleport cursor to",
    "teleport mouse to",
], pack="window-management",
    risk_class="ui", param_schema={"monitor": {"type": "monitor", "required": False}},
)
def handle_cursor(app, remainder):
    """Moves the mouse pointer to the screen or place you name."""
    logger.info("cursor: remainder='%s'", remainder)
    monitors = get_monitors()
    target = _parse_destination(remainder, app, monitors)
    if target:
        teleport_cursor(target)
        return True

    # Not a screen: an app window ("cursor to chrome") -- its centre.
    r = _strip_leading_the(remainder.lower().strip())
    windows = find_windows_by_app(r) if r else []
    if windows:
        try:
            rect = win32gui.GetWindowRect(windows[0])
            cx = (rect[0] + rect[2]) // 2
            cy = (rect[1] + rect[3]) // 2
            win32api.SetCursorPos((cx, cy))
            logger.info("Cursor -> '%s' window center (%d,%d)", r, cx, cy)
            return True
        except Exception as e:
            logger.warning("Cursor teleport to app failed: %s", e)
            return DispatchResult(DispatchState.FAILED, 'cursor to', 'cursor to', {'reason': str(e)})

    reason = f"refused: unknown screen or window '{remainder}' ({target.reason})"
    print(f"[WINDOWS] {reason}")
    return DispatchResult(DispatchState.REJECTED, 'cursor to', 'cursor to', {'reason': reason})


# ---------------------------------------------------------------------------
# Window snap (halves and quadrants)
# ---------------------------------------------------------------------------

_SNAP_DIRECTIONS = frozenset({
    'left', 'right', 'top', 'bottom',
    'top left', 'top right', 'bottom left', 'bottom right',
})


def _get_monitor_for_window(hwnd, monitors=None):
    """Return the monitor whose work area contains the center of hwnd."""
    if monitors is None:
        monitors = get_monitors()
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:
        return next((m for m in monitors if m['primary']), monitors[0] if monitors else None)
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    for m in monitors:
        ml, mt, mr, mb = m['rect']
        if ml <= cx < mr and mt <= cy < mb:
            return m
    return next((m for m in monitors if m['primary']), monitors[0] if monitors else None)


def _snap_rect(monitor, direction):
    """Return (x, y, w, h) for snapping a window to direction on monitor.

    Uses the work area so the snapped window never hides behind the taskbar.
    Odd pixel widths/heights are given to the right/bottom zone, consistent
    with Windows' own snapping behaviour.
    """
    l, t, r, b = monitor['rect']
    w = r - l
    h = b - t
    hw = w // 2
    hh = h // 2

    table = {
        'left':         (l,      t,      hw,     h),
        'right':        (l + hw, t,      w - hw, h),
        'top':          (l,      t,      w,      hh),
        'bottom':       (l,      t + hh, w,      h - hh),
        'top left':     (l,      t,      hw,     hh),
        'top right':    (l + hw, t,      w - hw, hh),
        'bottom left':  (l,      t + hh, hw,     h - hh),
        'bottom right': (l + hw, t + hh, w - hw, h - hh),
    }
    return table.get(direction)


def snap_window(hwnd, direction, *, preflight=None) -> bool:
    """Snap an exact HWND; a bound caller rechecks identity at each substep."""
    if direction not in _SNAP_DIRECTIONS:
        return False
    ready = lambda: preflight is None or preflight()
    if not hwnd or not ready():
        return False
    try:
        if win32gui.IsIconic(hwnd):
            if not ready():
                return False
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED:
            if not ready():
                return False
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        if not ready():
            return False
        monitors = get_monitors()
        if preflight is None:
            monitor = _get_monitor_for_window(hwnd, monitors)
        else:
            # A bound action cannot use the legacy primary-monitor fallback.
            rect = win32gui.GetWindowRect(hwnd)
            cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
            monitor = next((m for m in monitors if m['rect'][0] <= cx < m['rect'][2]
                            and m['rect'][1] <= cy < m['rect'][3]), None)
        if not monitor:
            return False
        x, y, w, h = _snap_rect(monitor, direction)
        if not ready():
            return False
        win32gui.SetWindowPos(hwnd, HWND_TOP, x, y, w, h, SWP_SHOWWINDOW)
        raised = (raise_window(hwnd, activate=True, preflight=preflight) if preflight is not None
                  else raise_window(hwnd, activate=True))
        return bool(raised and ready() and
                    win32gui.GetWindowRect(hwnd) == (x, y, x + w, y + h))
    except Exception as exc:
        logger.debug("snap_window failed for hwnd %s: %s", hwnd, exc)
        return False


@command("snap", aliases=["dock"], pack="window-management",
         risk_class="ui", param_schema={"side": {"type": "side", "required": False}})
def handle_snap(app, remainder):
    """Legacy foreground snap delegates to the exact-HWND helper."""
    direction = remainder.strip().lower()
    if direction not in _SNAP_DIRECTIONS:
        print(f"[SNAP] Unknown direction '{direction}'.")
        return True
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd or not snap_window(hwnd, direction):
        print("[SNAP] Window could not be snapped")
    return True
