"""Child-process host owning ALL Core Audio COM for session ducking.

WHY THIS PROCESS EXISTS (tribunal arc_20260803_163412, tier-2 ruling)
--------------------------------------------------------------------
In-process COM timeouts were rejected as fake isolation: a Core Audio call
that wedges cannot be abandoned from inside the process. The thread stays
stuck inside the RPC runtime holding its locks, so "timing out" only means
the caller stops waiting -- the damage (a wedged thread, held RPC locks,
an eventually-poisoned process) is already done and is unrecoverable
without a restart. Moving every ISimpleAudioVolume / MMDevice call into a
CHILD process makes the isolation real: a wedged child is killed and
respawned, and the parent (the user's hands: hotkeys, injection, Qt) never
notices.

THREADING CONTRACT (two Microsoft-documented rules, satisfied by construction)
-----------------------------------------------------------------------------
CoInitializeEx(NULL, COINIT_MULTITHREADED) is called ONCE, on the single
thread that then performs every COM call for this process's lifetime:

  1. WASAPI session management (IAudioSessionManager2 and friends) is
     documented as requiring the multithreaded apartment; using it from an
     STA/UI thread is unsupported and is a known source of hangs and
     re-entrancy surprises. The child has no UI and no message loop, so MTA
     is correct here by construction rather than by convention.

  2. ISimpleAudioVolume (like any COM interface obtained on a thread) must
     be Released on the same apartment that created it. In the parent this
     was only true by accident -- enumeration, volume writes, the sweep
     timer, and atexit restore could each land on a different thread. Here
     one thread creates, uses, and releases every pointer, so the rule
     cannot be violated.

PROTOCOL
--------
Newline-delimited JSON on stdin; exactly one newline-delimited JSON reply
per command on stdout, in order. Every reply echoes the request's `id` and
carries `elapsed_ms`. This process is deliberately single-threaded and
sequential: it is ALLOWED to hang. Bounding that hang is the parent's job
(see samsara/audio_ducking.py's transport), not this process's.

  {"id":1,"op":"ping"}                        -> {"id":1,"ok":true,"elapsed_ms":0}
  {"id":2,"op":"list_sessions"}               -> {"id":2,"ok":true,"sessions":[
                                                   {"sid":"...","pid":123,
                                                    "process_name":"spotify.exe"}],...}
  {"id":3,"op":"get_volume","sid":"..."}      -> {"id":3,"ok":true,"level":0.5,...}
  {"id":4,"op":"set_volume","sid":"...",
          "level":0.1}                        -> {"id":4,"ok":true,"prev_level":0.5,...}
  {"id":5,"op":"set_many","items":[
          {"sid":"...","level":0.1}]}         -> {"id":5,"ok":true,"results":[...],...}
  {"id":6,"op":"shutdown"}                    -> {"id":6,"ok":true,...} then exit

Failures are reported as {"ok": false, "err": "..."} -- this process never
raises out of the command loop, because a traceback on stdout would corrupt
the protocol stream.

SESSION IDS
-----------
`sid` is assigned by THIS process and is stable for its lifetime: repeated
list_sessions calls return the same sid for the same underlying audio
session, so the parent's per-session tracking survives sweeps. sids are
prefixed with this process's pid, so a respawned child can never hand back
an id the parent might confuse with one from the dead child.
"""
from __future__ import annotations

import ctypes
import itertools
import json
import os
import sys
import time
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
from typing import Any, Iterable

# Env sentinel: a frozen build re-executes its own exe for the child (there
# is no separate interpreter to run `-m samsara.ducking_host` with), so the
# app entry point checks this variable and diverts into main() below.
HOST_ENV_SENTINEL = "SAMSARA_DUCKING_HOST"


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


class ISimpleAudioVolume(Structure):
    pass


class ISimpleAudioVolumeVtbl(Structure):
    # Documented ISimpleAudioVolume vtable order (after IUnknown 0-2):
    # SetMasterVolume=3, GetMasterVolume=4, SetMute=5, GetMute=6.
    _fields_ = [
        (
            "QueryInterface",
            WINFUNCTYPE(
                ctypes.c_long, POINTER(ISimpleAudioVolume), POINTER(GUID), POINTER(c_void_p)
            ),
        ),
        ("AddRef", WINFUNCTYPE(ctypes.c_ulong, POINTER(ISimpleAudioVolume))),
        ("Release", WINFUNCTYPE(ctypes.c_ulong, POINTER(ISimpleAudioVolume))),
        (
            "SetMasterVolume",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(ISimpleAudioVolume),
                c_float,
                POINTER(GUID),
            ),
        ),
        (
            "GetMasterVolume",
            WINFUNCTYPE(ctypes.c_long, POINTER(ISimpleAudioVolume), POINTER(c_float)),
        ),
        (
            "SetMute",
            WINFUNCTYPE(
                ctypes.c_long,
                POINTER(ISimpleAudioVolume),
                ctypes.c_int,
                POINTER(GUID),
            ),
        ),
        (
            "GetMute",
            WINFUNCTYPE(ctypes.c_long, POINTER(ISimpleAudioVolume), ctypes.POINTER(ctypes.c_int)),
        ),
    ]


ISimpleAudioVolume._fields_ = [("lpVtbl", POINTER(ISimpleAudioVolumeVtbl))]


def _vtbl(iface: c_void_p, index: int, restype: Any, *argtypes: Any):
    table = cast(iface, POINTER(POINTER(c_void_p))).contents
    func_ptr = table[index]
    return WINFUNCTYPE(restype, c_void_p, *argtypes)(func_ptr)


def _release(iface: c_void_p) -> None:
    if not iface:
        return
    _vtbl(iface, 2, ctypes.c_ulong)(iface)


def _query_interface(interface: c_void_p, iid: GUID) -> c_void_p:
    out = c_void_p()
    hr = _vtbl(interface, 0, ctypes.c_long, POINTER(GUID), POINTER(c_void_p))(
        interface,
        byref(iid),
        byref(out),
    )
    if hr < 0 or not out:
        raise OSError(f"QueryInterface failed: 0x{hr & 0xFFFFFFFF:08x}")
    return out


class _CtypesSessionHandle:
    """Adapter around an ISimpleAudioVolume pointer.

    Created, used, and released entirely on this process's single COM
    thread -- see the module docstring's threading contract, rule 2.
    """

    def __init__(self, session_id: str, pid: int, volume_iface: c_void_p) -> None:
        self.session_id = session_id
        self.pid = pid
        self._volume_iface = volume_iface

    def get_master_volume(self) -> float:
        func = _vtbl(self._volume_iface, 4, ctypes.c_long, POINTER(c_float))
        value = c_float()
        hr = func(self._volume_iface, byref(value))
        if hr < 0:
            raise OSError(
                f"ISimpleAudioVolume.GetMasterVolume failed (0x{hr & 0xFFFFFFFF:08x})"
            )
        return float(value.value)

    def set_master_volume(self, level: float) -> None:
        func = _vtbl(self._volume_iface, 3, ctypes.c_long, c_float, POINTER(GUID))
        level = max(0.0, min(1.0, float(level)))
        hr = func(self._volume_iface, c_float(level), None)
        if hr < 0:
            raise OSError(
                f"ISimpleAudioVolume.SetMasterVolume failed (0x{hr & 0xFFFFFFFF:08x})"
            )

    def close(self) -> None:
        _release(self._volume_iface)


def _get_session_enumerator() -> c_void_p:
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
        POINTER(c_void_p),
    )(session_mgr, byref(session_enum))

    _release(session_mgr)
    _release(device)
    _release(enumerator)
    if hr < 0:
        raise OSError(f"GetSessionEnumerator failed: 0x{hr & 0xFFFFFFFF:08x}")

    return session_enum


def _iter_audio_sessions() -> Iterable[_CtypesSessionHandle]:
    """Yield one handle per live render session on the default endpoint.

    Identical enumeration to the pre-child-process implementation; the only
    change is that CoInitializeEx is no longer called here (main() owns it,
    once, for the process).
    """
    session_enum = _get_session_enumerator()
    try:
        count = c_int()
        hr = _vtbl(session_enum, 3, ctypes.c_long, POINTER(c_int))(
            session_enum, byref(count)
        )
        if hr < 0:
            raise OSError(
                f"IAudioSessionEnumerator.GetCount failed: 0x{hr & 0xFFFFFFFF:08x}"
            )

        for i in range(count.value):
            session_ctl = c_void_p()
            hr = _vtbl(
                session_enum,
                4,
                ctypes.c_long,
                c_int,
                POINTER(c_void_p),
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
                        POINTER(wintypes.DWORD),
                    )(
                        session_ctl2,
                        byref(pid),
                    )
                    if hr < 0:
                        continue

                    session_ptr = c_wchar_p()
                    hr = _vtbl(
                        session_ctl2, 13, ctypes.c_long, POINTER(c_wchar_p)
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


def _process_name(pid: int) -> str | None:
    """Best-effort process name for diagnostics. Never fatal: the parent
    uses sid/pid for all decisions, and the name is purely for humans
    reading a flight-recorder trail or an incident bundle."""
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:
        return None


class _SessionRegistry:
    """Owns every live ISimpleAudioVolume pointer, keyed by a stable sid.

    The Windows session INSTANCE identifier is the natural identity here
    (it distinguishes two concurrent sessions of the same process, which a
    plain pid cannot). Re-enumeration reuses the already-held pointer for a
    known instance and releases the duplicate the enumerator just handed
    back, so sids stay stable across sweeps and reference counts stay
    balanced. Vanished sessions are released and dropped.
    """

    def __init__(self) -> None:
        self._sid_by_instance: dict[str, str] = {}
        self._handles: dict[str, _CtypesSessionHandle] = {}
        self._counter = itertools.count(1)
        # pid prefix: a respawned child can never emit an sid that collides
        # with one the parent still remembers from the dead child.
        self._prefix = os.getpid()

    def refresh(self) -> list[dict]:
        seen_instances: set[str] = set()
        listed: list[dict] = []

        for handle in _iter_audio_sessions():
            instance = handle.session_id
            seen_instances.add(instance)
            sid = self._sid_by_instance.get(instance)
            if sid is not None and sid in self._handles:
                # Already tracked -- keep the pointer we own, release the
                # duplicate reference this enumeration produced.
                handle.close()
            else:
                sid = f"{self._prefix}-{next(self._counter)}"
                self._sid_by_instance[instance] = sid
                self._handles[sid] = handle
            tracked = self._handles[sid]
            listed.append(
                {
                    "sid": sid,
                    "pid": tracked.pid,
                    "process_name": _process_name(tracked.pid),
                    # 52: the Windows instance identifier outlives this child
                    # and the parent, so the parent's duck journal can match
                    # a session again after a hard kill.
                    "instance": instance,
                }
            )

        for instance, sid in list(self._sid_by_instance.items()):
            if instance in seen_instances:
                continue
            self._sid_by_instance.pop(instance, None)
            stale = self._handles.pop(sid, None)
            if stale is not None:
                try:
                    stale.close()
                except Exception:
                    pass

        return listed

    def get(self, sid: str) -> _CtypesSessionHandle | None:
        return self._handles.get(sid)

    def release_all(self) -> None:
        for handle in self._handles.values():
            try:
                handle.close()
            except Exception:
                pass
        self._handles.clear()
        self._sid_by_instance.clear()


def _handle_command(cmd: dict, registry: _SessionRegistry) -> dict:
    op = cmd.get("op")

    if op == "ping":
        return {"ok": True}

    if op == "list_sessions":
        return {"ok": True, "sessions": registry.refresh()}

    if op == "get_volume":
        handle = registry.get(str(cmd.get("sid")))
        if handle is None:
            return {"ok": False, "err": "unknown sid"}
        return {"ok": True, "level": handle.get_master_volume()}

    if op == "set_volume":
        handle = registry.get(str(cmd.get("sid")))
        if handle is None:
            return {"ok": False, "err": "unknown sid"}
        previous = handle.get_master_volume()
        handle.set_master_volume(float(cmd.get("level", 1.0)))
        return {"ok": True, "prev_level": previous}

    if op == "set_many":
        results = []
        for item in cmd.get("items") or []:
            sid = str(item.get("sid"))
            handle = registry.get(sid)
            if handle is None:
                results.append({"sid": sid, "ok": False, "err": "unknown sid"})
                continue
            try:
                previous = handle.get_master_volume()
                handle.set_master_volume(float(item.get("level", 1.0)))
                results.append({"sid": sid, "ok": True, "prev_level": previous})
            except Exception as exc:
                results.append({"sid": sid, "ok": False, "err": str(exc)})
        return {"ok": True, "results": results}

    if op == "shutdown":
        registry.release_all()
        return {"ok": True}

    return {"ok": False, "err": f"unknown op {op!r}"}


def main(stdin=None, stdout=None) -> int:
    """Run the command loop until shutdown or EOF.

    CoInitializeEx happens here, once, on this thread -- every COM call in
    this process then happens on this same thread (module docstring,
    threading contract). Nothing in this loop may raise: a traceback on
    stdout would desynchronize the parent's reply stream.
    """
    stream_in = stdin if stdin is not None else sys.stdin
    stream_out = stdout if stdout is not None else sys.stdout

    ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    registry = _SessionRegistry()

    while True:
        line = stream_in.readline()
        if not line:
            break  # EOF: parent went away
        line = line.strip()
        if not line:
            continue

        started = time.monotonic()
        try:
            cmd = json.loads(line)
            if not isinstance(cmd, dict):
                raise ValueError("command must be a JSON object")
        except Exception as exc:
            reply = {"id": None, "ok": False, "err": f"bad command: {exc}"}
            reply["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            stream_out.write(json.dumps(reply) + "\n")
            stream_out.flush()
            continue

        try:
            reply = _handle_command(cmd, registry)
        except Exception as exc:
            reply = {"ok": False, "err": str(exc)}

        reply["id"] = cmd.get("id")
        reply["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        stream_out.write(json.dumps(reply) + "\n")
        stream_out.flush()

        if cmd.get("op") == "shutdown":
            break

    return 0


if __name__ == "__main__":
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
