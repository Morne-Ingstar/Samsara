"""Injection safety helpers (queue 50, ARC audit5b).

Two things a keyboard-unable user cannot check for themselves:

1. WINDOW INTEGRITY. Windows UIPI silently drops keystrokes a process sends to
   a window whose process runs at a HIGHER integrity level (an elevated/admin
   window: Task Manager, an admin terminal, an installer). SendInput still
   reports every event as accepted, and Ctrl+V is dropped the same way, so
   nothing downstream can tell. window_integrity() asks BEFORE injecting.

2. PACED DELETION. "scratch that" removes what was just typed. Sending
   Shift+Left pairs relies on the target reading the Shift state in step with
   each Left, which slow or remote targets do not; see delete_backwards().

Pure ctypes and the standard library: importable from anywhere (including a
low-integrity test process) without pulling in the app. Never raises.
"""
from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

OK = "ok"             # target at our integrity level or lower: injection lands
HIGHER = "higher"     # target above ours: UIPI will silently drop injection
UNKNOWN = "unknown"   # could not tell -- must NOT be treated as OK

RID_NAMES = {0x0000: "untrusted", 0x1000: "low", 0x2000: "medium", 0x3000: "high", 0x4000: "system"}

_TOKEN_QUERY = 0x0008
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_INTEGRITY_LEVEL = 25
_WM_GETTEXTLENGTH = 0x000E
_SMTO_ABORTIFHUNG = 0x0002
_ERROR_ACCESS_DENIED = 5
_PROBE_TIMEOUT_MS = 150


@dataclass(frozen=True)
class IntegrityVerdict:
    state: str                    # OK | HIGHER | UNKNOWN
    own_rid: Optional[int]
    target_rid: Optional[int]
    method: str                   # "token" | "uipi_probe" | "same_process" | "none"
    detail: str
    elapsed_ms: float

    @property
    def blocked(self) -> bool:
        return self.state == HIGHER

    @property
    def confirmed_ok(self) -> bool:
        return self.state == OK

    def describe(self) -> str:
        own = RID_NAMES.get(self.own_rid, self.own_rid)
        target = RID_NAMES.get(self.target_rid, self.target_rid)
        return (f"state={self.state} own={own} target={target} method={self.method} "
                f"({self.detail}; {self.elapsed_ms:.2f} ms)")


class _Win32:
    def __init__(self):
        from ctypes import wintypes
        self.wt = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        H, DW, BOOL = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(DW)]
        self.user32.GetWindowThreadProcessId.restype = DW
        self.user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                                    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        self.user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        self.kernel32.OpenProcess.argtypes = [DW, BOOL, DW]
        self.kernel32.OpenProcess.restype = H
        self.kernel32.GetCurrentProcess.restype = H
        self.kernel32.GetCurrentProcessId.restype = DW
        self.kernel32.CloseHandle.argtypes = [H]
        self.advapi32.OpenProcessToken.argtypes = [H, DW, ctypes.POINTER(H)]
        self.advapi32.OpenProcessToken.restype = BOOL
        self.advapi32.GetTokenInformation.argtypes = [H, ctypes.c_int, ctypes.c_void_p, DW, ctypes.POINTER(DW)]
        self.advapi32.GetTokenInformation.restype = BOOL
        self.advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        self.advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        self.advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, DW]
        self.advapi32.GetSidSubAuthority.restype = ctypes.POINTER(DW)


@lru_cache(maxsize=1)
def _win32() -> Optional[_Win32]:
    if sys.platform != "win32":
        return None
    try:
        return _Win32()
    except Exception:
        return None


def _token_rid(api: _Win32, process_handle) -> "tuple[Optional[int], str]":
    """Integrity RID of a process token, or (None, why)."""
    token = api.wt.HANDLE()
    if not api.advapi32.OpenProcessToken(process_handle, _TOKEN_QUERY, ctypes.byref(token)):
        return None, f"OpenProcessToken error {ctypes.get_last_error()}"
    try:
        buf = ctypes.create_string_buffer(256)
        needed = api.wt.DWORD(0)
        if not api.advapi32.GetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, buf, 256, ctypes.byref(needed)):
            return None, f"GetTokenInformation error {ctypes.get_last_error()}"
        # TOKEN_MANDATORY_LABEL starts with SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes }
        sid = ctypes.c_void_p.from_buffer(buf).value
        count = api.advapi32.GetSidSubAuthorityCount(sid)[0]
        return int(api.advapi32.GetSidSubAuthority(sid, count - 1)[0]), ""
    finally:
        api.kernel32.CloseHandle(token)


@lru_cache(maxsize=1)
def own_integrity_rid() -> Optional[int]:
    api = _win32()
    if api is None:
        return None
    try:
        rid, _why = _token_rid(api, api.kernel32.GetCurrentProcess())
        return rid
    except Exception:
        return None


def window_integrity(hwnd: Optional[int] = None) -> IntegrityVerdict:
    """Can keystrokes sent to `hwnd` (default: the foreground window) land?

    1. Read the target process's token integrity level and compare it with
       ours (authoritative when the token is readable).
    2. If it is not readable -- the case for many elevated processes seen from
       a standard one -- probe UIPI directly: WM_GETTEXTLENGTH is one of the
       messages UIPI refuses from a lower-integrity sender, failing with
       ERROR_ACCESS_DENIED. A refusal means HIGHER; an answer means the
       message boundary is open, so OK.
    3. Anything else (no window, probe timed out, API missing) is UNKNOWN.
    """
    started = time.perf_counter()

    def verdict(state, method, detail, own=None, target=None):
        return IntegrityVerdict(state, own, target, method, detail, (time.perf_counter() - started) * 1000)

    api = _win32()
    if api is None:
        return verdict(UNKNOWN, "none", "integrity check unavailable on this platform")
    try:
        own = own_integrity_rid()
        if hwnd is None:
            hwnd = api.user32.GetForegroundWindow()
        if not hwnd:
            return verdict(UNKNOWN, "none", "no foreground window", own)
        pid = api.wt.DWORD(0)
        api.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return verdict(UNKNOWN, "none", "window has no process", own)
        if pid.value == api.kernel32.GetCurrentProcessId():
            return verdict(OK, "same_process", "our own window", own, own)

        why = "own integrity level unreadable"
        if own is not None:
            process = api.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if process:
                try:
                    target, why = _token_rid(api, process)
                finally:
                    api.kernel32.CloseHandle(process)
                if target is not None:
                    return verdict(HIGHER if target > own else OK, "token",
                                   f"pid {pid.value}", own, target)
            else:
                why = f"OpenProcess error {ctypes.get_last_error()}"

        result = ctypes.c_size_t(0)
        ctypes.set_last_error(0)
        answered = api.user32.SendMessageTimeoutW(hwnd, _WM_GETTEXTLENGTH, 0, 0, _SMTO_ABORTIFHUNG,
                                                  _PROBE_TIMEOUT_MS, ctypes.byref(result))
        error = ctypes.get_last_error()
        if answered:
            return verdict(OK, "uipi_probe", f"token unreadable ({why}); window answered", own)
        if error == _ERROR_ACCESS_DENIED:
            return verdict(HIGHER, "uipi_probe", f"token unreadable ({why}); UIPI refused the probe", own)
        return verdict(UNKNOWN, "uipi_probe", f"token unreadable ({why}); probe failed with error {error}", own)
    except Exception as exc:
        return verdict(UNKNOWN, "none", f"integrity check failed: {exc!r}")


# ---------------------------------------------------------------------------
# Paced deletion for "scratch that"
# ---------------------------------------------------------------------------

#: Backspaces sent per SendInput batch, and the pause after each batch.
#: See delete_backwards() and reports/50 for the measurements behind them.
#:
#: Queue 99 asked whether a counted scratch ("scratch that three times") needs
#: different pacing. It does not, and the reason is that this pacing is per
#: CHARACTER, not per chunk: N chunks are N delete_backwards() calls, each
#: batching its own characters at DELETE_BATCH with DELETE_BATCH_PAUSE_S
#: between batches, so the keystroke rate inside a run is the same rate a
#: single long deletion already runs at and was measured at (40 characters in
#: 0.14 s on three targets, reports/50). The only place the pattern differs is
#: the seam between two chunks, where one call's last partial batch is not
#: followed by a pause -- and that seam is not bare: session_modes re-runs the
#: focus-lock and HWND checks per chunk and dictation.py's _remove_chars_fn
#: re-runs window_integrity() (a SendMessageTimeoutW probe) before each call,
#: which costs more wall-clock than the 30 ms pause it replaces. A counted
#: scratch is therefore paced no faster than one long one, and re-checks the
#: target more often than one long one would.
DELETE_BATCH = 8
DELETE_BATCH_PAUSE_S = 0.03


def build_backspace_events(n: int) -> list:
    """(vk, scan, flags) tuples for n Backspace presses -- no modifier keys.

    The hardware scan code (0x0E) is included: pyautogui sends scan code 0,
    which targets that translate by scan code (remote desktop clients) cannot
    map, and it sends arrows without KEYEVENTF_EXTENDEDKEY, which is why its
    Shift+Left selected nothing in the queue-50 measurements."""
    VK_BACK, SCAN_BACK, KEYEVENTF_KEYUP = 0x08, 0x0E, 0x0002
    events = []
    for _ in range(max(0, int(n))):
        events.append((VK_BACK, SCAN_BACK, 0))
        events.append((VK_BACK, SCAN_BACK, KEYEVENTF_KEYUP))
    return events


def delete_backwards(n: int, *, batch: int = DELETE_BATCH, pause_s: float = DELETE_BATCH_PAUSE_S,
                     still_target=None, send_input=None, sleep=time.sleep) -> "tuple[int, str]":
    """Delete the n characters before the caret with Backspace presses.

    Why Backspace, not Shift+Left then Delete: a Backspace carries no modifier
    state, so a target that samples Shift late (remote desktop, some web
    IDEs, a busy app) cannot turn the selection into a cursor move and delete
    the wrong span; a dropped event under-deletes instead of mis-deleting.
    Batches are paced so the target's input queue keeps up, and `still_target`
    (a no-argument callable) is re-checked before every batch so a focus
    change stops the deletion instead of continuing in another window.

    Returns (characters deleted, "done" | "focus_changed" | "send_failed").
    """
    if send_input is None:
        send_input = _send_key_events
    deleted = 0
    remaining = max(0, int(n))
    while remaining > 0:
        if still_target is not None and not still_target():
            return deleted, "focus_changed"
        count = min(batch, remaining)
        if not send_input(build_backspace_events(count)):
            return deleted, "send_failed"
        deleted += count
        remaining -= count
        if remaining > 0 and pause_s > 0:
            sleep(pause_s)
    return deleted, "done"


def _send_key_events(events: list) -> bool:
    """SendInput a list of (vk, scan, flags) keyboard events in one call."""
    if sys.platform != "win32" or not events:
        return not events
    try:
        from ctypes import wintypes
        ULONG_PTR = ctypes.c_size_t

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class MOUSEINPUT(ctypes.Structure):
            # Full union size matters: SendInput rejects a wrong cbSize (see
            # clipboard.type_text_unicode, 2026-08-02).
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        if ctypes.sizeof(INPUT) != (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28):
            return False
        arr = (INPUT * len(events))()
        for i, (vk, scan, flags) in enumerate(events):
            arr[i].type = 1
            arr[i].u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        sent = ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
        return sent == len(events)
    except Exception:
        return False
