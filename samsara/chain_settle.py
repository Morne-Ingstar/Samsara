"""Bounded, read-only desktop observation between command-chain effects."""
from __future__ import annotations

import ntpath
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Window:
    hwnd: int
    pid: int
    exe: str
    rect: tuple


@dataclass
class Target:
    desktop: object
    exes: frozenset[str]
    existing: frozenset[int] = frozenset()
    hwnd: int | None = None
    new_window: bool = False
    moving: bool = False


class Desktop:
    """Read only top-level windows; cache process names for this one step."""

    def __init__(self):
        self._exes = {}

    def window(self, hwnd):
        import psutil
        import win32gui
        import win32process

        try:
            if (not hwnd or not win32gui.IsWindowVisible(hwnd)
                    or win32gui.GetAncestor(hwnd, 2) != hwnd
                    or not win32gui.GetWindowText(hwnd)):
                return None
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid not in self._exes:
                self._exes[pid] = psutil.Process(pid).name().casefold()
            return Window(hwnd, pid, self._exes[pid], tuple(win32gui.GetWindowRect(hwnd)))
        except (OSError, psutil.Error):
            return None

    def foreground(self):
        from .handlers import _get_foreground_hwnd
        return self.window(_get_foreground_hwnd())

    def windows(self):
        import win32gui

        found = []
        win32gui.EnumWindows(lambda hwnd, _: found.append(hwnd) or True, None)
        return tuple(w for hwnd in found if (w := self.window(hwnd)) is not None)


def _exe(value):
    return ntpath.basename(str(value).strip().strip('"')).casefold()


def prepare(entry, args, values=None, *, desktop=None):
    """Capture the expected identity BEFORE an effect, only for relevant steps.

    Open's plugin focuses an existing window; a true launch must yield a new
    HWND of the expected process. Titles alone never satisfy a launch wait.
    """
    phrase = entry.phrase.casefold()
    verb = phrase.split()[0] if phrase else ''
    data = entry.data or {}
    moving = verb in {'snap', 'dock', 'maximize', 'restore', 'move'}
    launching = data.get('type') == 'launch' or verb in {'open', 'launch'}
    focusing = verb in {'focus', 'activate', 'bring', 'switch'}
    if not (moving or launching or focusing):
        return None
    desktop = desktop if desktop is not None else Desktop()
    values = values or {}
    name = str(values.get('app_name') or args.get('app_name') or
               (args.get('remainder') if not moving else '') or '').strip()
    if not name and launching and phrase.startswith('open '):
        name = phrase[5:]
    try:
        from plugins.commands.app_verbs import LAUNCH_OVERRIDES, resolve_window
        from plugins.commands.windows import APP_ALIASES

        match = resolve_window(name) if name else None
        if moving:
            window = desktop.window(match[0]) if match else desktop.foreground()
            return Target(desktop, frozenset({window.exe}) if window else frozenset(),
                          hwnd=window.hwnd if window else None, moving=True)
        if match and data.get('type') != 'launch':
            return Target(desktop, frozenset({_exe(match[2])}), hwnd=match[0])
        executable = _exe(data.get('target', ''))
        override = LAUNCH_OVERRIDES.get(name.casefold(), {})
        exes = {_exe(exe) for exe in override.get('exe_candidates', ())}
        if match:
            exes.add(_exe(match[2]))
        if name.casefold() in APP_ALIASES:
            exes.add(_exe(APP_ALIASES[name.casefold()]))
        if executable.endswith('.exe'):
            exes.add(executable)
        if not exes and name:
            exes.add(name.casefold() if name.casefold().endswith('.exe') else name.casefold() + '.exe')
        if str(data.get('target', '')).startswith('ms-settings:'):
            exes.add('systemsettings.exe')
        existing = frozenset(w.hwnd for w in desktop.windows() if w.exe in exes) if launching else frozenset()
        return Target(desktop, frozenset(exes), existing, new_window=launching)
    except Exception:
        # Unavailable identity observation takes the same bounded timeout path.
        return Target(desktop, frozenset(), moving=moving)


def wait(target, fresh, *, clock=None, sleep=None, timeout=3.0):
    """Poll at 25 ms; moves need 250 ms minimum and 150 ms stable geometry."""
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    started = clock()
    deadline = started + timeout
    stable_since = started
    previous = None
    while fresh():
        now = clock()
        try:
            current = target.desktop.foreground()
        except Exception:
            current = None
        matched = (current is not None and current.exe in target.exes
                   and (target.hwnd is None or current.hwnd == target.hwnd)
                   and (not target.new_window or current.hwnd not in target.existing))
        if matched:
            if not target.moving:
                return True
            identity = (current.hwnd, current.pid, current.rect)
            if identity != previous:
                stable_since = now
                previous = identity
            if now - started >= .25 and now - stable_since >= .15:
                return True
        else:
            previous = None
            stable_since = now
        if now >= deadline:
            return False
        sleep(min(.025, deadline - now))
    return False
