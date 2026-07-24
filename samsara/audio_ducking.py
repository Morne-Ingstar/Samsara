"""Session audio ducking engine for WASAPI session control."""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Protocol
from ctypes import (
    POINTER,
    Structure,
    WINFUNCTYPE,
    byref,
    c_float,
    c_int,
    c_void_p,
    c_wchar_p,
    cast,
)
from ctypes import wintypes

logger = logging.getLogger("Samsara")


# 2026-07-24 amendment: cut from 2.0s so a session that appears mid-duck
# (e.g. a newly launched Electron media player) gets caught with less
# audible delay. If 500ms still proves insufficient for slow-to-register
# Electron-based players, the real fix is switching from this poll sweep
# to event-driven session discovery (IAudioSessionNotification /
# IAudioSessionManager2::RegisterSessionNotification) -- tracked as a
# follow-up, not attempted here.
SWEEP_INTERVAL_SECONDS = 0.5

# Float tolerance for "is the current volume still what we last set it
# to" comparisons (relative-attenuation conditional restore, see stop()).
# WASAPI volumes are float32 under the hood; a few ULPs of round-trip
# drift through get/set is normal and must not look like a user change.
_VOLUME_EPSILON = 1e-3


def _floats_close(a: float, b: float, epsilon: float = _VOLUME_EPSILON) -> bool:
    return abs(a - b) <= epsilon


class _SessionHandle(Protocol):
    session_id: str
    pid: int

    def get_master_volume(self) -> float: ...

    def set_master_volume(self, level: float) -> None: ...

    def close(self) -> None: ...


@dataclass
class _TrackedSession:
    session_id: str
    pid: int
    handle: Any
    original_volume: float
    applied_volume: float


class SessionDucker:
    """Duck and restore non-excluded WASAPI sessions with a context manager.

    `current_duck(pid)` returns the currently applied duck level for a PID, so
    callers can compose levels with `min(self.current_duck(pid), own_level)` and
    restore in reverse order when nested behavior is needed.

    RELATIVE ATTENUATION (2026-07-24 amendment): `duck_level` is a
    multiplicative FACTOR applied to each session's CURRENT volume at
    start() time (`applied = current * duck_level`), not an absolute
    level to jam every session to. This is what makes LIFO-layering two
    independent SessionDucker instances behave as a true nest (e.g. an
    "idle" ducker at 0.8 followed by a "capture" ducker at 0.15 while the
    idle one is still active correctly compounds to 0.12 of the true
    original, and releasing the capture ducker lands back at the idle
    ducker's own level, not full volume) -- see dictation.py's hands-free
    ducking for the concrete two-stage caller.

    RESTORE IS CONDITIONAL: stop() only restores a session to its
    pre-duck volume if that session's CURRENT volume still equals the
    value THIS instance applied. If the user (or the app itself) changed
    that session's volume while ducked, their change wins -- we leave it
    alone rather than clobbering it with a stale snapshot. See stop() and
    _floats_close().
    """

    def __init__(
        self,
        duck_level: float = 0.15,
        exclude_pids: set[int] | None = None,
    ) -> None:
        self.duck_level = max(0.0, min(1.0, float(duck_level)))
        self._base_excludes = set(exclude_pids or [])
        self._base_excludes.add(os.getpid())

        self._lock = threading.Lock()
        self._active = False
        self._tracked_by_id: dict[str, _TrackedSession] = {}
        self._tracking_order: list[str] = []
        self._pid_counts: dict[int, int] = {}
        self._sweep_timer: threading.Timer | None = None
        self._atexit_registered = False

    def __enter__(self) -> "SessionDucker":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any | None,
    ) -> None:
        self.stop()

    def current_duck(self, pid: int) -> float | None:
        """Return active duck level for `pid` or None when this instance is not ducking it."""
        with self._lock:
            return self.duck_level if self._pid_counts.get(int(pid), 0) else None

    def start(self) -> None:
        """Enumerate sessions and duck every non-excluded PID by `duck_level`
        (a multiplicative factor against each session's OWN current volume
        -- see the class docstring's "RELATIVE ATTENUATION").

        A lightweight re-sweep is scheduled every SWEEP_INTERVAL_SECONDS to
        catch sessions that appear while active. A no-op while already
        active (idempotent -- safe to call repeatedly, e.g. from a debounce
        path that reuses an already-ducking instance).
        """
        with self._lock:
            if self._active:
                return

        tracked: list[tuple[_SessionHandle, float]] = []
        try:
            discovered = list(_iter_audio_sessions())
            for session in discovered:
                if session.pid in self._base_excludes:
                    self._close_session(session)
                    continue
                level = float(session.get_master_volume())
                tracked.append((session, level))

            applied = self._set_sessions_to_duck(tracked)

            with self._lock:
                self._record_tracks_locked(applied)
                self._active = True
                self._schedule_sweep_locked()
                self._register_atexit_locked()
        except Exception as exc:
            for session, _ in tracked:
                self._close_session(session)
            logger.warning("SessionDucker.start(): ducking disabled: %s", exc)
            return

    def stop(self) -> None:
        """Restore stored volumes in reverse acquisition order -- CONDITIONALLY:
        a session is only restored if its CURRENT volume still equals the
        value THIS instance applied (see class docstring). If the user or
        the app itself changed that session's volume while ducked, their
        change wins; we leave it alone instead of overwriting it with a
        stale pre-duck snapshot.
        """
        with self._lock:
            if not self._active:
                return

            self._active = False
            tracked_entries = list(self._tracked_by_id.values())
            self._tracking_order = []
            self._tracked_by_id = {}
            self._pid_counts = {}

            timer = self._sweep_timer
            self._sweep_timer = None
            if timer is not None:
                timer.cancel()

        try:
            for entry in reversed(tracked_entries):
                try:
                    current = float(entry.handle.get_master_volume())
                    if _floats_close(current, entry.applied_volume):
                        entry.handle.set_master_volume(entry.original_volume)
                    else:
                        logger.debug(
                            "SessionDucker.stop(): session %s volume changed "
                            "mid-duck (applied=%.4f now=%.4f) -- leaving as-is",
                            entry.session_id, entry.applied_volume, current,
                        )
                except Exception as exc:
                    logger.warning(
                        "SessionDucker.stop(): session restore failed: %s", exc
                    )
                finally:
                    self._close_session(entry.handle)
        except Exception as exc:
            logger.warning(
                "SessionDucker.stop(): failed to restore all sessions: %s", exc
            )

    def _set_sessions_to_duck(
        self, items: list[tuple[_SessionHandle, float]]
    ) -> list[tuple[_SessionHandle, float, float]]:
        """Apply RELATIVE ducking (each session's OWN current volume times
        `duck_level`) to every discovered session. Roll back on partial
        failures. Returns (session, original_volume, applied_volume)
        triples for tracking -- applied_volume is what stop() later checks
        the live volume against before restoring."""
        changed: list[tuple[_SessionHandle, float, float]] = []
        try:
            for session, original in items:
                applied = max(0.0, min(1.0, original * self.duck_level))
                session.set_master_volume(applied)
                changed.append((session, original, applied))
        except Exception:
            for session, original, _applied in reversed(changed):
                try:
                    session.set_master_volume(original)
                except Exception:
                    logger.debug("SessionDucker: restore failed during duck rollout")
            raise
        return changed

    def _run_sweep(self) -> None:
        new_sessions: list[tuple[_SessionHandle, float]] = []
        try:
            with self._lock:
                if not self._active:
                    return
                sessions = list(_iter_audio_sessions())
                tracked_ids = set(self._tracked_by_id)

            for session in sessions:
                if session.pid in self._base_excludes:
                    self._close_session(session)
                    continue
                if session.session_id in tracked_ids:
                    self._close_session(session)
                    continue
                new_sessions.append((session, float(session.get_master_volume())))

            applied = self._set_sessions_to_duck(new_sessions)

            if applied:
                with self._lock:
                    self._record_tracks_locked(applied)

        except Exception as exc:
            logger.warning(
                "SessionDucker.sweep(): ducking sweep failed, unchanged: %s", exc
            )
            for session, _ in new_sessions:
                self._close_session(session)
        finally:
            with self._lock:
                if not self._active:
                    return
                self._schedule_sweep_locked()

    def _record_tracks_locked(
        self, items: list[tuple[_SessionHandle, float, float]]
    ) -> None:
        """Store mutable tracks to restore later. Call with `_lock` held."""
        for session, original, applied in items:
            tracked = _TrackedSession(
                session_id=session.session_id,
                pid=session.pid,
                handle=session,
                original_volume=original,
                applied_volume=applied,
            )
            self._tracked_by_id[session.session_id] = tracked
            self._tracking_order.append(session.session_id)
            self._pid_counts[session.pid] = self._pid_counts.get(session.pid, 0) + 1

    def _schedule_sweep_locked(self) -> None:
        if self._sweep_timer is not None:
            self._sweep_timer.cancel()
        timer = threading.Timer(SWEEP_INTERVAL_SECONDS, self._run_sweep)
        timer.daemon = True
        self._sweep_timer = timer
        timer.start()

    def _register_atexit_locked(self) -> None:
        if self._atexit_registered:
            return
        atexit.register(self.stop)
        self._atexit_registered = True

    @staticmethod
    def _close_session(session: _SessionHandle) -> None:
        try:
            session.close()
        except Exception:
            logger.debug("SessionDucker: failed to close a session handle")


class _CtypesSessionHandle:
    """Adapter around an ISimpleAudioVolume pointer."""

    def __init__(self, session_id: str, pid: int, volume_iface: c_void_p) -> None:
        self.session_id = session_id
        self.pid = pid
        self._volume_iface = volume_iface

    def get_master_volume(self) -> float:
        func = _vtbl(self._volume_iface, 4, c_float, c_void_p, POINTER(c_float))
        value = c_float()
        hr = func(self._volume_iface, byref(value))
        if hr < 0:
            raise OSError(
                f"ISimpleAudioVolume.GetMasterVolume failed (0x{hr & 0xFFFFFFFF:08x})"
            )
        return float(value.value)

    def set_master_volume(self, level: float) -> None:
        func = _vtbl(self._volume_iface, 3, ctypes.c_int, c_void_p, c_float, c_void_p)
        level = max(0.0, min(1.0, float(level)))
        hr = func(self._volume_iface, c_float(level), None)
        if hr < 0:
            raise OSError(
                f"ISimpleAudioVolume.SetMasterVolume failed (0x{hr & 0xFFFFFFFF:08x})"
            )

    def close(self) -> None:
        _release(self._volume_iface)


ole32 = ctypes.windll.ole32

ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoCreateInstance.argtypes = [
    c_void_p,
    c_void_p,
    wintypes.DWORD,
    c_void_p,
    POINTER(c_void_p),
]
ole32.CoCreateInstance.restype = ctypes.c_long
ole32.CLSIDFromString.argtypes = [c_wchar_p, c_void_p]
ole32.CLSIDFromString.restype = ctypes.c_long
ole32.CoTaskMemFree.argtypes = [c_void_p]
ole32.CoTaskMemFree.restype = None

CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0
E_RENDER = 0
E_CONSOLE = 0


class GUID(Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


def _guid(raw: str) -> GUID:
    value = GUID()
    hr = ole32.CLSIDFromString(c_wchar_p(raw), byref(value))
    if hr < 0:
        raise OSError(f"CLSIDFromString({raw!r}) failed: 0x{hr & 0xFFFFFFFF:08x}")
    return value


CLSID_MMDeviceEnumerator = _guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = _guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioSessionManager2 = _guid("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
IID_IAudioSessionControl2 = _guid("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
IID_ISimpleAudioVolume = _guid("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")


class IUnknownVtbl(Structure):
    pass


class IUnknown(Structure):
    _fields_ = [("lpVtbl", POINTER(IUnknownVtbl))]


IUnknownVtbl._fields_ = [
    (
        "QueryInterface",
        WINFUNCTYPE(ctypes.c_long, POINTER(IUnknown), POINTER(GUID), POINTER(c_void_p)),
    ),
    ("AddRef", WINFUNCTYPE(ctypes.c_ulong, POINTER(IUnknown))),
    ("Release", WINFUNCTYPE(ctypes.c_ulong, POINTER(IUnknown))),
]


class IMMDevice(Structure):
    pass


class IMMDeviceVtbl(Structure):
    _fields_ = [
        (
            "QueryInterface",
            WINFUNCTYPE(
                ctypes.c_long, POINTER(IMMDevice), POINTER(GUID), POINTER(c_void_p)
            ),
        ),
        ("AddRef", WINFUNCTYPE(ctypes.c_ulong, POINTER(IMMDevice))),
        ("Release", WINFUNCTYPE(ctypes.c_ulong, POINTER(IMMDevice))),
        (
            "Activate",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(IMMDevice),
                POINTER(GUID),
                wintypes.DWORD,
                c_void_p,
                POINTER(c_void_p),
            ),
        ),
    ]


IMMDevice._fields_ = [("lpVtbl", POINTER(IMMDeviceVtbl))]


class IMMDeviceEnumerator(Structure):
    pass


class IMMDeviceEnumeratorVtbl(Structure):
    _fields_ = [
        (
            "QueryInterface",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(IMMDeviceEnumerator),
                POINTER(GUID),
                POINTER(c_void_p),
            ),
        ),
        ("AddRef", WINFUNCTYPE(ctypes.c_ulong, POINTER(IMMDeviceEnumerator))),
        ("Release", WINFUNCTYPE(ctypes.c_ulong, POINTER(IMMDeviceEnumerator))),
        (
            "EnumAudioEndpoints",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(IMMDeviceEnumerator),
                c_int,
                wintypes.DWORD,
                POINTER(c_void_p),
            ),
        ),
        (
            "GetDefaultAudioEndpoint",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(IMMDeviceEnumerator),
                c_int,
                c_int,
                POINTER(POINTER(IMMDevice)),
            ),
        ),
    ]


IMMDeviceEnumerator._fields_ = [("lpVtbl", POINTER(IMMDeviceEnumeratorVtbl))]


class IAudioSessionControl(Structure):
    pass


class IAudioSessionControlVtbl(Structure):
    _fields_ = [
        (
            "QueryInterface",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(IAudioSessionControl),
                POINTER(GUID),
                POINTER(c_void_p),
            ),
        ),
        ("AddRef", WINFUNCTYPE(ctypes.c_ulong, POINTER(IAudioSessionControl))),
        ("Release", WINFUNCTYPE(ctypes.c_ulong, POINTER(IAudioSessionControl))),
    ]


IAudioSessionControl._fields_ = [("lpVtbl", POINTER(IAudioSessionControlVtbl))]


class IAudioSessionControl2(Structure):
    pass


class IAudioSessionControl2Vtbl(Structure):
    _fields_ = [
        (
            "QueryInterface",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(IAudioSessionControl2),
                POINTER(GUID),
                POINTER(c_void_p),
            ),
        ),
        ("AddRef", WINFUNCTYPE(ctypes.c_ulong, POINTER(IAudioSessionControl2))),
        ("Release", WINFUNCTYPE(ctypes.c_ulong, POINTER(IAudioSessionControl2))),
        (
            "GetState",
            WINFUNCTYPE(ctypes.c_long, POINTER(IAudioSessionControl2), POINTER(c_int)),
        ),
        (
            "GetDisplayName",
            WINFUNCTYPE(
                ctypes.c_long, POINTER(IAudioSessionControl2), POINTER(c_wchar_p)
            ),
        ),
        (
            "SetDisplayName",
            WINFUNCTYPE(
                ctypes.c_long, POINTER(IAudioSessionControl2), c_wchar_p, POINTER(GUID)
            ),
        ),
        (
            "GetIconPath",
            WINFUNCTYPE(
                ctypes.c_long, POINTER(IAudioSessionControl2), POINTER(c_wchar_p)
            ),
        ),
        (
            "SetIconPath",
            WINFUNCTYPE(
                ctypes.c_long, POINTER(IAudioSessionControl2), c_wchar_p, POINTER(GUID)
            ),
        ),
        (
            "GetGroupingParam",
            WINFUNCTYPE(ctypes.c_long, POINTER(IAudioSessionControl2), POINTER(GUID)),
        ),
        (
            "SetGroupingParam",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(IAudioSessionControl2),
                POINTER(GUID),
                POINTER(GUID),
            ),
        ),
        (
            "RegisterAudioSessionNotification",
            WINFUNCTYPE(ctypes.c_long, POINTER(IAudioSessionControl2), c_void_p),
        ),
        (
            "UnregisterAudioSessionNotification",
            WINFUNCTYPE(ctypes.c_long, POINTER(IAudioSessionControl2), c_void_p),
        ),
        (
            "GetSessionIdentifier",
            WINFUNCTYPE(
                ctypes.c_long, POINTER(IAudioSessionControl2), POINTER(c_wchar_p)
            ),
        ),
        (
            "GetSessionInstanceIdentifier",
            WINFUNCTYPE(
                ctypes.c_long, POINTER(IAudioSessionControl2), POINTER(c_wchar_p)
            ),
        ),
        (
            "GetProcessId",
            WINFUNCTYPE(
                ctypes.c_long, POINTER(IAudioSessionControl2), POINTER(wintypes.DWORD)
            ),
        ),
    ]


IAudioSessionControl2._fields_ = [("lpVtbl", POINTER(IAudioSessionControl2Vtbl))]


class IAudioSessionEnumerator(Structure):
    pass


class IAudioSessionEnumeratorVtbl(Structure):
    _fields_ = [
        (
            "QueryInterface",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(IAudioSessionEnumerator),
                POINTER(GUID),
                POINTER(c_void_p),
            ),
        ),
        ("AddRef", WINFUNCTYPE(ctypes.c_ulong, POINTER(IAudioSessionEnumerator))),
        ("Release", WINFUNCTYPE(ctypes.c_ulong, POINTER(IAudioSessionEnumerator))),
        (
            "GetCount",
            WINFUNCTYPE(
                ctypes.c_long, POINTER(IAudioSessionEnumerator), POINTER(c_int)
            ),
        ),
        (
            "GetSession",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(IAudioSessionEnumerator),
                c_int,
                POINTER(POINTER(IAudioSessionControl)),
            ),
        ),
    ]


IAudioSessionEnumerator._fields_ = [("lpVtbl", POINTER(IAudioSessionEnumeratorVtbl))]


class IAudioSessionManager2(Structure):
    pass


class IAudioSessionManager2Vtbl(Structure):
    _fields_ = [
        (
            "QueryInterface",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(IAudioSessionManager2),
                POINTER(GUID),
                POINTER(c_void_p),
            ),
        ),
        ("AddRef", WINFUNCTYPE(ctypes.c_ulong, POINTER(IAudioSessionManager2))),
        ("Release", WINFUNCTYPE(ctypes.c_ulong, POINTER(IAudioSessionManager2))),
        (
            "GetAudioSessionControl",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(IAudioSessionManager2),
                POINTER(GUID),
                wintypes.DWORD,
                POINTER(c_void_p),
            ),
        ),
        (
            "GetSimpleAudioVolume",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(IAudioSessionManager2),
                POINTER(GUID),
                wintypes.DWORD,
                POINTER(c_void_p),
            ),
        ),
        (
            "GetSessionEnumerator",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(IAudioSessionManager2),
                POINTER(POINTER(IAudioSessionEnumerator)),
            ),
        ),
    ]


IAudioSessionManager2._fields_ = [("lpVtbl", POINTER(IAudioSessionManager2Vtbl))]


def _vtbl(iface: c_void_p, index: int, restype: Any, *argtypes: Any):
    table = cast(iface, POINTER(POINTER(c_void_p))).contents
    func_ptr = table[index]
    return WINFUNCTYPE(restype, c_void_p, *argtypes)(func_ptr)


def _release(iface: c_void_p) -> None:
    if not iface:
        return
    _vtbl(iface, 2, ctypes.c_ulong, c_void_p)(iface)


def _query_interface(interface: c_void_p, iid: GUID) -> c_void_p:
    out = c_void_p()
    hr = _vtbl(interface, 0, ctypes.c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))(
        interface,
        byref(iid),
        byref(out),
    )
    if hr < 0 or not out:
        raise OSError(f"QueryInterface failed: 0x{hr & 0xFFFFFFFF:08x}")
    return out


def _get_session_enumerator() -> c_void_p:
    ole32.CoInitializeEx(None, COINIT_MULTITHREADED)

    enumerator = c_void_p()
    hr = ole32.CoCreateInstance(
        byref(CLSID_MMDeviceEnumerator),
        None,
        CLSCTX_ALL,
        byref(IID_IMMDeviceEnumerator),
        byref(enumerator),
    )
    if hr < 0:
        raise OSError(
            f"CoCreateInstance(MMDeviceEnumerator) failed: 0x{hr & 0xFFFFFFFF:08x}"
        )

    device = c_void_p()
    hr = _vtbl(
        enumerator,
        4,
        ctypes.c_long,
        c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        POINTER(c_void_p),
    )(enumerator, E_RENDER, E_CONSOLE, byref(device))
    if hr < 0:
        _release(enumerator)
        raise OSError(f"GetDefaultAudioEndpoint failed: 0x{hr & 0xFFFFFFFF:08x}")

    session_mgr = c_void_p()
    hr = _vtbl(
        device,
        3,
        ctypes.c_long,
        c_void_p,
        POINTER(GUID),
        wintypes.DWORD,
        c_void_p,
        POINTER(c_void_p),
    )(
        device,
        byref(IID_IAudioSessionManager2),
        CLSCTX_ALL,
        None,
        byref(session_mgr),
    )
    if hr < 0:
        _release(device)
        _release(enumerator)
        raise OSError(
            f"IMMDevice.Activate(IAudioSessionManager2) failed: 0x{hr & 0xFFFFFFFF:08x}"
        )

    session_enum = c_void_p()
    hr = _vtbl(
        session_mgr,
        5,
        ctypes.c_long,
        c_void_p,
        POINTER(POINTER(IAudioSessionEnumerator)),
    )(session_mgr, byref(session_enum))

    _release(session_mgr)
    _release(device)
    _release(enumerator)
    if hr < 0:
        raise OSError(f"GetSessionEnumerator failed: 0x{hr & 0xFFFFFFFF:08x}")

    return session_enum


def _iter_audio_sessions() -> Iterable[_SessionHandle]:
    session_enum = _get_session_enumerator()
    try:
        count = c_int()
        hr = _vtbl(session_enum, 3, ctypes.c_long, c_void_p, POINTER(c_int))(
            session_enum, byref(count)
        )
        if hr < 0:
            raise OSError(
                f"IAudioSessionEnumerator.GetCount failed: 0x{hr & 0xFFFFFFFF:08x}"
            )

        for i in range(count.value):
            session_ctl = c_void_p()
            hr = _vtbl(
                session_enum, 4, ctypes.c_long, c_void_p, c_int, POINTER(c_void_p)
            )(
                session_enum,
                i,
                byref(session_ctl),
            )
            if hr < 0:
                continue
            try:
                session_ctl2 = c_void_p()
                try:
                    session_ctl2 = _query_interface(
                        session_ctl, IID_IAudioSessionControl2
                    )
                    pid = wintypes.DWORD()
                    hr = _vtbl(
                        session_ctl2,
                        14,
                        ctypes.c_long,
                        c_void_p,
                        POINTER(wintypes.DWORD),
                    )(
                        session_ctl2,
                        byref(pid),
                    )
                    if hr < 0:
                        continue

                    session_ptr = c_wchar_p()
                    hr = _vtbl(
                        session_ctl2, 13, ctypes.c_long, c_void_p, POINTER(c_wchar_p)
                    )(
                        session_ctl2,
                        byref(session_ptr),
                    )
                    if hr < 0 or not session_ptr.value:
                        continue
                    session_id = session_ptr.value
                    ole32.CoTaskMemFree(cast(session_ptr, c_void_p))

                    volume_ptr = c_void_p()
                    hr = _vtbl(
                        session_ctl2,
                        0,
                        ctypes.c_long,
                        c_void_p,
                        POINTER(GUID),
                        POINTER(c_void_p),
                    )(
                        session_ctl2,
                        byref(IID_ISimpleAudioVolume),
                        byref(volume_ptr),
                    )
                    if hr < 0 or not volume_ptr:
                        continue

                    yield _CtypesSessionHandle(session_id, int(pid.value), volume_ptr)
                finally:
                    _release(session_ctl2)
            finally:
                _release(session_ctl)
    finally:
        _release(session_enum)
