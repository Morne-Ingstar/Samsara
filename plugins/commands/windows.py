"""Window management plugin.

Move windows between monitors by voice.

"Jarvis, bring Chrome here"              - move Chrome to monitor under cursor
"Jarvis, bring everything here"          - move all valid windows to cursor's monitor
"Jarvis, send Stremio to TV"             - move Stremio to TV monitor
"Jarvis, send Chrome to monitor 2"       - move Chrome to specific monitor
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
from datetime import datetime
from pathlib import Path

import psutil
import win32api
import win32con
import win32gui
import win32process

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
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return results


# ---------------------------------------------------------------------------
# Window moving
# ---------------------------------------------------------------------------

def move_window_to_monitor(hwnd, monitor):
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception as e:
        logger.warning("GetWindowRect failed for hwnd %s: %s", hwnd, e)
        return

    w = min(rect[2] - rect[0], monitor['width'])
    h = min(rect[3] - rect[1], monitor['height'])
    ml, mt = monitor['rect'][0], monitor['rect'][1]
    x = ml + (monitor['width'] - w) // 2
    y = mt + (monitor['height'] - h) // 2

    title = win32gui.GetWindowText(hwnd)
    logger.info("Moving '%s' to monitor %s (%d,%d)", title, monitor['index'], x, y)
    win32gui.SetWindowPos(hwnd, HWND_TOP, x, y, w, h, SWP_SHOWWINDOW)


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


def _parse_destination(dest_text, app):
    monitors = get_monitors()
    t = _strip_leading_the(dest_text.lower().strip())

    if 'tv' in t:
        return get_tv_monitor(app, monitors)
    if 'here' in t:
        return get_monitor_under_cursor(monitors)
    for word in t.split():
        if word.isdigit():
            m = get_monitor_by_index(int(word), monitors)
            if m:
                return m
    return None


def _parse_send_remainder(remainder):
    """Split 'chrome to tv' -> ('chrome', 'tv'), 'this to monitor 2' -> (None, 'monitor 2')."""
    r = remainder.lower().strip()
    r = _strip_leading_the(r)

    for sep in (' to the ', ' to ', ' on the ', ' on '):
        if sep in r:
            app_part, dest_part = r.split(sep, 1)
            app_part = app_part.strip()
            app_name = None if app_part in ('', 'this') else app_part
            return app_name, dest_part.strip()

    return None, r


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

@command("bring", aliases=["bring back", "get", "grab", "fetch"], pack="window-management")
def handle_bring(app, remainder):
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


@command("send", aliases=["move", "put", "throw"], pack="window-management")
def handle_send(app, remainder):
    logger.info("send: remainder='%s'", remainder)
    extra_ignore = _get_extra_ignore(app)

    app_name, dest_text = _parse_send_remainder(remainder)
    if not dest_text:
        logger.warning("send: could not parse destination from '%s'", remainder)
        return False

    target = _parse_destination(dest_text, app)
    if target is None:
        logger.warning("send: unknown destination '%s'", dest_text)
        return False

    logger.info("Destination: monitor %s", target['index'])

    if app_name is None:
        hwnd = win32gui.GetForegroundWindow()
        if hwnd:
            move_window_to_monitor(hwnd, target)
        return True

    windows = find_windows_by_app(app_name, extra_ignore)
    if not windows:
        print(f"[WINDOWS] No windows found for: {app_name}")
        return True
    for hwnd in windows:
        move_window_to_monitor(hwnd, target)
    return True


@command("movie mode", aliases=["movie time", "couch mode", "tv mode"], pack="window-management")
def handle_movie_mode(app, remainder):
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
    except Exception:
        pass

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
        except OSError:
            pass
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
         pack="window-management")
def handle_save_layout(app, remainder):
    name = _extract_layout_name(remainder)
    if not name:
        print("[LAYOUTS] No valid name provided")
        return True
    _save_current_layout(name)
    return True


@command("restore layout",
         aliases=["load layout", "restore window layout"],
         pack="window-management")
def handle_restore_layout(app, remainder):
    name = _extract_layout_name(remainder)
    if not name:
        print("[LAYOUTS] No valid name provided")
        return True
    _restore_layout(name)
    return True


@command("list layouts",
         aliases=["show layouts", "what layouts"],
         pack="window-management")
def handle_list_layouts(app, remainder):
    layouts = _load_all_layouts()
    if not layouts:
        print("[LAYOUTS] No saved layouts yet")
    else:
        print(f"[LAYOUTS] Available: {', '.join(sorted(layouts.keys()))}")
    return True


@command("delete layout",
         aliases=["forget layout", "remove layout"],
         pack="window-management")
def handle_delete_layout(app, remainder):
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
         pack="window-management")
def handle_find_lost_windows(app, remainder):
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
         pack="window-management")
def handle_rescue_lost(app, remainder):
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
         pack="window-management")
def handle_find_specific(app, remainder):
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
], pack="window-management")
def handle_cursor(app, remainder):
    logger.info("cursor: remainder='%s'", remainder)
    monitors = get_monitors()

    r = _strip_leading_the(remainder.lower().strip())

    if 'tv' in r:
        target = get_tv_monitor(app, monitors)
        if target:
            teleport_cursor(target)
        return True

    if 'here' in r:
        target = get_monitor_under_cursor(monitors)
        if target:
            teleport_cursor(target)
        return True

    for word in r.split():
        if word.isdigit():
            target = get_monitor_by_index(int(word), monitors)
            if target:
                teleport_cursor(target)
            return True

    if r:
        windows = find_windows_by_app(r)
        if windows:
            try:
                rect = win32gui.GetWindowRect(windows[0])
                cx = (rect[0] + rect[2]) // 2
                cy = (rect[1] + rect[3]) // 2
                win32api.SetCursorPos((cx, cy))
                logger.info("Cursor -> '%s' window center (%d,%d)", r, cx, cy)
            except Exception as e:
                logger.warning("Cursor teleport to app failed: %s", e)

    return True
