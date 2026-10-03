"""Fail-closed, exact-HWND discovery for Interact; no window effects here."""

from __future__ import annotations

import hashlib
import math
import ntpath
import os
from dataclasses import dataclass

from samsara.app_index import MATCH_FLOOR, score_name_match
from samsara.subjects import WindowIdentity

_TOOLWINDOW = 0x80
_SHELL_CLASSES = {"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd", "#32770"}


@dataclass(frozen=True, slots=True)
class WindowSnapshot:
    hwnd: int
    root: int
    owner: int
    visible: bool
    cloaked: bool
    exstyle: int
    title: str
    window_class: str
    pid: int
    process_name: str
    process_start_time: float
    integrity: str


@dataclass(frozen=True, slots=True)
class TargetResult:
    identity: WindowIdentity | None = None
    title: str = ""
    point: tuple[int, int] | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.identity is not None and self.reason is None


@dataclass(frozen=True, slots=True)
class CandidateResult:
    candidates: tuple[TargetResult, ...] = ()
    reason: str | None = None


def title_fingerprint(title: str) -> str:
    return hashlib.sha256(title.strip().encode("utf-8")).hexdigest()


class Win32Native:
    """Small native facade, instantiated only when real capture is requested."""

    def windows(self) -> tuple[int, ...]:
        import win32gui

        found = []
        win32gui.EnumWindows(lambda hwnd, _: found.append(int(hwnd)) or True, None)
        return tuple(found)

    def sample_pointer(self) -> tuple[int, int, int]:
        """Get physical pointer and GA_ROOT in one per-monitor DPI context."""
        import ctypes
        from ctypes import wintypes as wt

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        user32.GetCursorPos.restype = wt.BOOL
        user32.WindowFromPoint.argtypes = [wt.POINT]
        user32.WindowFromPoint.restype = wt.HWND
        user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        user32.GetAncestor.restype = wt.HWND
        previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            raise OSError("per-monitor DPI context unavailable")
        try:
            point = wt.POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                raise OSError("pointer unavailable")
            hit = user32.WindowFromPoint(point)
            root = user32.GetAncestor(hit, 2) if hit else 0  # GA_ROOT, not GA_ROOTOWNER
            return int(point.x), int(point.y), int(root or 0)
        finally:
            user32.SetThreadDpiAwarenessContext(previous)

    def window(self, hwnd: int) -> WindowSnapshot | None:
        import ctypes
        from ctypes import wintypes as wt

        import psutil
        import win32con
        import win32gui
        import win32process

        if not win32gui.IsWindow(hwnd):
            return None
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        user32.GetAncestor.restype = wt.HWND
        root = int(user32.GetAncestor(hwnd, 2) or 0)
        owner = int(win32gui.GetWindow(hwnd, win32con.GW_OWNER) or 0)
        cloak = ctypes.c_int()
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwm.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
        dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        if dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloak), ctypes.sizeof(cloak)) != 0:
            raise OSError("window cloak status unavailable")
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            process = psutil.Process(pid)
            image, started = process.exe(), process.create_time()
        except (psutil.Error, OSError):
            image, started = "", 0.0
        from samsara.injection_safety import window_integrity

        return WindowSnapshot(
            hwnd=int(hwnd), root=root, owner=owner,
            visible=bool(win32gui.IsWindowVisible(hwnd)), cloaked=bool(cloak.value),
            exstyle=int(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)),
            title=win32gui.GetWindowText(hwnd), window_class=win32gui.GetClassName(hwnd),
            pid=int(pid), process_name=image, process_start_time=float(started),
            integrity=window_integrity(hwnd).state,
        )


class WindowTargetAdapter:
    def __init__(self, native=None, *, own_pid: int | None = None, aliases=None):
        self.native = native if native is not None else Win32Native()
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self.aliases = aliases
        self._overlay_rects: tuple[tuple[int, int, int, int], ...] = ()

    def publish_overlay_rects(self, rects) -> None:
        """Qt caller publishes physical rectangles; capture only reads this tuple."""
        frozen = tuple(tuple(map(int, rect)) for rect in rects)
        if any(len(rect) != 4 or rect[2] <= rect[0] or rect[3] <= rect[1]
               for rect in frozen):
            raise ValueError("invalid overlay rectangle")
        self._overlay_rects = frozen

    def inspect(self, hwnd: int) -> TargetResult:
        if not hwnd:
            return TargetResult(reason="no_window")
        try:
            snap = self.native.window(hwnd)
        except Exception:
            return TargetResult(reason="api_unavailable")
        if snap is None:
            return TargetResult(reason="dead_hwnd")
        try:
            if snap.root != hwnd:
                return TargetResult(reason="not_root")
            if snap.owner:
                return TargetResult(reason="owned_window")
            if (not snap.visible or snap.cloaked or snap.exstyle & _TOOLWINDOW
                    or not snap.title.strip() or snap.window_class.casefold() in _SHELL_CLASSES):
                return TargetResult(reason="ineligible_window")
            if snap.pid == self.own_pid:
                return TargetResult(reason="own_pid")
            if snap.integrity != "ok":
                return TargetResult(reason="integrity_" + (snap.integrity or "unknown"))
            image = ntpath.normcase(ntpath.normpath(snap.process_name)) if snap.process_name else ""
            if (not snap.pid or not image or image == "."
                    or not math.isfinite(snap.process_start_time)
                    or snap.process_start_time <= 0):
                return TargetResult(reason="process_unavailable")
            identity = WindowIdentity(hwnd, snap.pid, image, snap.process_start_time,
                                      snap.window_class)
        except ValueError:
            return TargetResult(reason="process_unavailable")
        except Exception:
            return TargetResult(reason="api_unavailable")
        return TargetResult(identity=identity, title=snap.title)

    def capture_pointer(self) -> TargetResult:
        try:
            x, y, root = self.native.sample_pointer()
        except Exception:
            return TargetResult(reason="api_unavailable")
        point = (x, y)
        if any(left <= x < right and top <= y < bottom
               for left, top, right, bottom in self._overlay_rects):
            return TargetResult(point=point, reason="own_overlay")
        found = self.inspect(root)
        return TargetResult(found.identity, found.title, point, found.reason)

    def validate(self, identity: WindowIdentity, *, title_hash: str | None = None) -> TargetResult:
        if not isinstance(identity, WindowIdentity):
            return TargetResult(reason="invalid_identity")
        found = self.inspect(identity.hwnd)
        if not found.ok:
            return found
        current = found.identity
        if current.pid != identity.pid:
            return TargetResult(reason="pid_changed")
        if (current.process_name != identity.process_name
                or current.process_start_time != identity.process_start_time):
            return TargetResult(reason="process_changed")
        if current.window_class != identity.window_class:
            return TargetResult(reason="window_class_changed")
        if title_hash is not None and title_fingerprint(found.title) != title_hash:
            return TargetResult(reason="title_changed")
        return found

    def probe_same_hwnd(self, identity: WindowIdentity, *, title_hash: str | None = None) -> bool:
        return self.validate(identity, title_hash=title_hash).ok

    def enumerate_named(self, name: str) -> CandidateResult:
        if not name.strip():
            return CandidateResult(reason="empty_name")
        try:
            hwnds = self.native.windows()
        except Exception:
            return CandidateResult(reason="api_unavailable")
        aliases = self.aliases
        if aliases is None:
            try:
                from plugins.commands.windows import APP_ALIASES
                aliases = APP_ALIASES
            except Exception:
                return CandidateResult(reason="api_unavailable")
        alias_image = aliases.get(name.casefold().strip(), "").casefold()
        scored = []
        incomplete = False
        for hwnd in hwnds:
            found = self.inspect(hwnd)
            if not found.ok:
                if found.reason in ("api_unavailable", "process_unavailable", "integrity_unknown"):
                    incomplete = True
                continue
            stem = ntpath.basename(found.identity.process_name).rsplit(".", 1)[0]
            image = ntpath.basename(found.identity.process_name).casefold()
            score = max(score_name_match(name, found.title), score_name_match(name, stem))
            if alias_image and image == alias_image:
                score = 1.0
            if score >= MATCH_FLOOR:
                scored.append((score, found))
        if incomplete:
            return CandidateResult(reason="enumeration_incomplete")
        scored.sort(key=lambda item: (-item[0], item[1].identity.hwnd))
        return CandidateResult(tuple(found for _, found in scored),
                               None if scored else "no_match")
