"""Audio ducking during dictation ("attenuation").

Lowers the volume of every OTHER application's audio session (music,
video, calls) while a dictation window is open, so the mic hears less
playback bleed -- then restores it when dictation ends. This attacks echo
at the source instead of subtracting it after capture, unlike the
homegrown NLMS adaptive filter in samsara/echo_cancel.py (which measured
only 3-8% echo reduction in practice and is off by default for that
reason -- see that module's docstring). Opt-in, off by default (see
config_schema.py's ducking.enabled/ducking.level).

Pure ctypes COM against the Windows Core Audio session APIs, in the exact
style of plugins/commands/volume.py (manual vtable dispatch, no pycaw, no
new dependencies) -- extended from single-endpoint master volume to
per-session enumeration:

    IMMDeviceEnumerator -> GetDefaultAudioEndpoint(eRender, eMultimedia)
    -> IMMDevice::Activate(IAudioSessionManager2)
    -> GetSessionEnumerator -> per-session IAudioSessionControl2
       (owning PID, system-sounds check, session instance id)
       + ISimpleAudioVolume (get/set volume), both obtained via
       QueryInterface on the same session control object.

Vtable layouts and IIDs cross-checked against pycaw's comtypes interface
definitions (F:\\envs\\sami\\Lib\\site-packages\\pycaw\\api\\{audiopolicy,
audioclient}\\__init__.py) during development -- pycaw itself is NOT a
dependency of this module or the project.

duck()/restore() are synchronous/blocking (not dispatched to a background
thread) deliberately: restore() is called from a `finally` block and an
atexit hook specifically to GUARANTEE the user's audio is never left
permanently quiet, and a fire-and-forget async restore could be cut off
mid-fade if the process exits immediately after. All sessions fade in
lockstep (one shared step loop, not one fade per session sequentially),
so total duck()/restore() latency is bounded at ~150ms regardless of how
many other apps are playing audio, not 150ms-per-session.
"""

import atexit
import ctypes
import logging
import os
import struct
import threading
import time
from ctypes import HRESULT, POINTER, byref, cast, c_float, c_void_p, c_wchar_p

logger = logging.getLogger("Samsara")

# ── Core Audio COM definitions (mirrors plugins/commands/volume.py) ──────────

CLSCTX_ALL = 23
E_RENDER = 0        # eRender
E_MULTIMEDIA = 1    # eMultimedia

_FADE_DURATION_S = 0.15
_FADE_STEPS = 6
_STEP_DELAY_S = _FADE_DURATION_S / _FADE_STEPS


def _guid_bytes(s):
    """Convert a '{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}' GUID string to the
    16-byte little-endian layout COM expects. Identical to volume.py's
    helper -- duplicated here rather than imported so this module stays
    fully self-contained (a plugins/ file is not meant to be imported as a
    library by samsara/ core)."""
    parts = s.strip('{}').split('-')
    return struct.pack('<IHH', int(parts[0], 16), int(parts[1], 16),
                        int(parts[2], 16)) + bytes.fromhex(parts[3] + parts[4])


_CLSID_MMDeviceEnumerator     = _guid_bytes('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
_IID_IMMDeviceEnumerator      = _guid_bytes('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
_IID_IAudioSessionManager2    = _guid_bytes('{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}')
_IID_IAudioSessionControl2    = _guid_bytes('{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}')
_IID_ISimpleAudioVolume       = _guid_bytes('{87CE5498-68D6-44E5-9215-6DA47EF883D8}')

_ole32 = ctypes.windll.ole32
_ole32.CoInitialize.argtypes = [c_void_p]
_ole32.CoInitialize.restype = HRESULT
_ole32.CoCreateInstance.argtypes = [
    c_void_p, c_void_p, ctypes.c_ulong, c_void_p, POINTER(c_void_p)
]
_ole32.CoCreateInstance.restype = HRESULT
_ole32.CoTaskMemFree.argtypes = [c_void_p]
_ole32.CoTaskMemFree.restype = None


def _get_vtable_func(iface_ptr, index, restype, *argtypes):
    """Get a function from a COM vtable by index. Identical helper to
    volume.py's -- see that file for why raw vtable dispatch is used
    instead of a COM framework."""
    vtable = cast(iface_ptr, POINTER(POINTER(c_void_p)))
    func_ptr = vtable[0][index]
    functype = ctypes.WINFUNCTYPE(restype, *argtypes)
    return functype(func_ptr)


def _release(iface_ptr):
    if iface_ptr:
        _get_vtable_func(iface_ptr, 2, ctypes.c_ulong, c_void_p)(iface_ptr)  # IUnknown::Release


def _query_interface(iface_ptr, iid_bytes):
    """IUnknown::QueryInterface -- vtable index 0 on every COM interface."""
    func = _get_vtable_func(
        iface_ptr, 0, HRESULT,
        c_void_p,           # this
        c_void_p,           # riid
        POINTER(c_void_p),  # ppvObject
    )
    out = c_void_p()
    hr = func(iface_ptr, iid_bytes, byref(out))
    if hr != 0 or not out:
        return None
    return out


_initialized = False
_warned_once = False


def _ensure_com_init():
    global _initialized
    if _initialized:
        return True
    hr = _ole32.CoInitialize(None)
    if hr < 0 and hr != 1:  # anything but S_OK or S_FALSE (already initialized)
        return False
    _initialized = True
    return True


def _warn_once(msg):
    global _warned_once
    if not _warned_once:
        logger.warning(f"[DUCKING] {msg} -- ducking disabled for this session")
        _warned_once = True


# ── Session enumeration ─────────────────────────────────────────────────────

def _get_session_enumerator():
    """Full pipeline: device enumerator -> default render device ->
    IAudioSessionManager2 -> IAudioSessionEnumerator. Returns None (and
    releases anything already obtained) on any failure. Caller owns
    releasing the returned enumerator."""
    enumerator = c_void_p()
    hr = _ole32.CoCreateInstance(
        _CLSID_MMDeviceEnumerator, None, CLSCTX_ALL,
        _IID_IMMDeviceEnumerator, byref(enumerator),
    )
    if hr != 0:
        return None
    try:
        get_default = _get_vtable_func(
            enumerator, 4, HRESULT,
            c_void_p, ctypes.c_uint, ctypes.c_uint, POINTER(c_void_p),
        )
        device = c_void_p()
        hr = get_default(enumerator, E_RENDER, E_MULTIMEDIA, byref(device))
        if hr != 0:
            return None
        try:
            activate = _get_vtable_func(
                device, 3, HRESULT,
                c_void_p, c_void_p, ctypes.c_ulong, c_void_p, POINTER(c_void_p),
            )
            session_mgr = c_void_p()
            hr = activate(device, _IID_IAudioSessionManager2, CLSCTX_ALL, None,
                           byref(session_mgr))
            if hr != 0:
                return None
            try:
                # IAudioSessionManager2::GetSessionEnumerator -- vtable index 5
                # (IAudioSessionManager base has 3=GetAudioSessionControl,
                # 4=GetSimpleAudioVolume; Manager2 adds this at 5).
                get_enum = _get_vtable_func(
                    session_mgr, 5, HRESULT, c_void_p, POINTER(c_void_p),
                )
                session_enum = c_void_p()
                hr = get_enum(session_mgr, byref(session_enum))
                if hr != 0:
                    return None
                return session_enum
            finally:
                _release(session_mgr)
        finally:
            _release(device)
    finally:
        _release(enumerator)


def _iter_sessions(session_enum):
    """Yield (instance_id: str, pid: int, is_system_sounds: bool,
    simple_volume_iface: c_void_p) for every session on session_enum.
    Caller must _release() each yielded simple_volume_iface when done with
    it. instance_id/pid extraction failures skip that session silently
    (never raise -- one bad session must not abort enumeration)."""
    # IAudioSessionEnumerator::GetCount -- vtable index 3
    get_count = _get_vtable_func(session_enum, 3, HRESULT, c_void_p, POINTER(ctypes.c_int))
    count = ctypes.c_int()
    if get_count(session_enum, byref(count)) != 0:
        return
    # IAudioSessionEnumerator::GetSession -- vtable index 4
    get_session = _get_vtable_func(
        session_enum, 4, HRESULT, c_void_p, ctypes.c_int, POINTER(c_void_p),
    )
    for i in range(count.value):
        ctl = c_void_p()
        if get_session(session_enum, i, byref(ctl)) != 0 or not ctl:
            continue
        try:
            ctl2 = _query_interface(ctl, _IID_IAudioSessionControl2)
            if not ctl2:
                continue
            try:
                # IAudioSessionControl2 vtable (base IAudioSessionControl is
                # 0-11; Control2 adds 12=GetSessionIdentifier,
                # 13=GetSessionInstanceIdentifier, 14=GetProcessId,
                # 15=IsSystemSoundsSession, 16=SetDuckingPreferences).
                get_instance_id = _get_vtable_func(
                    ctl2, 13, HRESULT, c_void_p, POINTER(c_wchar_p),
                )
                instance_id_ptr = c_wchar_p()
                if get_instance_id(ctl2, byref(instance_id_ptr)) != 0 or not instance_id_ptr.value:
                    continue
                instance_id = instance_id_ptr.value
                _ole32.CoTaskMemFree(cast(instance_id_ptr, c_void_p))

                get_pid = _get_vtable_func(
                    ctl2, 14, HRESULT, c_void_p, POINTER(ctypes.c_ulong),
                )
                pid = ctypes.c_ulong()
                if get_pid(ctl2, byref(pid)) != 0:
                    continue

                is_system_sounds_fn = _get_vtable_func(ctl2, 15, HRESULT, c_void_p)
                is_system_sounds = is_system_sounds_fn(ctl2) == 0  # S_OK == is system sounds

                simple_volume = _query_interface(ctl2, _IID_ISimpleAudioVolume)
                if not simple_volume:
                    continue
                yield instance_id, pid.value, is_system_sounds, simple_volume
            finally:
                _release(ctl2)
        finally:
            _release(ctl)


def _get_session_volume(simple_volume):
    # ISimpleAudioVolume::GetMasterVolume -- vtable index 4
    func = _get_vtable_func(simple_volume, 4, HRESULT, c_void_p, POINTER(c_float))
    level = c_float()
    if func(simple_volume, byref(level)) != 0:
        return None
    return level.value


def _set_session_volume(simple_volume, level):
    # ISimpleAudioVolume::SetMasterVolume -- vtable index 3
    func = _get_vtable_func(simple_volume, 3, HRESULT, c_void_p, c_float, c_void_p)
    return func(simple_volume, c_float(max(0.0, min(1.0, level))), None) == 0


# ── Public duck/restore API ──────────────────────────────────────────────────

_lock = threading.Lock()
_ducked = False
_saved_volumes = {}  # instance_id -> pre-duck master volume


def is_ducked() -> bool:
    with _lock:
        return _ducked


def duck(level: float = 0.2) -> None:
    """Lower every other app's audio session to `level` -- an absolute
    ISimpleAudioVolume scalar (0.0-1.0), not a multiplier of the session's
    current volume. No-op if already ducked, if COM is unavailable, or if
    nothing else is playing. Never raises."""
    global _ducked
    with _lock:
        if _ducked:
            return
        if not _ensure_com_init():
            _warn_once("CoInitialize failed")
            return
        try:
            session_enum = _get_session_enumerator()
            if session_enum is None:
                _warn_once("could not enumerate audio sessions")
                return
            try:
                own_pid = os.getpid()
                targets = []  # (simple_volume, from_level, to_level)
                saved = {}
                for instance_id, pid, is_system_sounds, simple_volume in _iter_sessions(session_enum):
                    try:
                        if pid == own_pid or is_system_sounds:
                            _release(simple_volume)
                            continue
                        current = _get_session_volume(simple_volume)
                        if current is None:
                            _release(simple_volume)
                            continue
                        saved[instance_id] = current
                        targets.append((simple_volume, current, level))
                    except Exception:
                        _release(simple_volume)
                        continue
                try:
                    _fade_all(targets)
                finally:
                    for simple_volume, _from, _to in targets:
                        _release(simple_volume)
                _saved_volumes.clear()
                _saved_volumes.update(saved)
                _ducked = True
            finally:
                _release(session_enum)
        except Exception as e:
            _warn_once(f"duck() failed ({e})")


def restore() -> None:
    """Ramp every session ducked by the last duck() call back to its saved
    volume, then clear the record. No-op if not currently ducked. Sessions
    that vanished mid-duck (process exited, stream torn down) are skipped
    silently -- their saved-volume entry is simply never matched against
    the fresh enumeration. Sessions that started mid-duck are untouched
    (out of scope for v1). Never raises -- this is called from `finally`
    blocks and an atexit hook that must never themselves fail."""
    global _ducked
    with _lock:
        if not _ducked:
            return
        try:
            if _ensure_com_init():
                session_enum = _get_session_enumerator()
                if session_enum is not None:
                    try:
                        targets = []
                        for instance_id, _pid, _is_system, simple_volume in _iter_sessions(session_enum):
                            saved_level = _saved_volumes.get(instance_id)
                            if saved_level is None:
                                _release(simple_volume)
                                continue
                            try:
                                current = _get_session_volume(simple_volume)
                                if current is None:
                                    _release(simple_volume)
                                    continue
                                targets.append((simple_volume, current, saved_level))
                            except Exception:
                                _release(simple_volume)
                        try:
                            _fade_all(targets)
                        finally:
                            for simple_volume, _from, _to in targets:
                                _release(simple_volume)
                    finally:
                        _release(session_enum)
        except Exception as e:
            logger.warning(f"[DUCKING] restore() failed ({e}) -- some sessions may stay quiet "
                            f"until their app is restarted or volume adjusted manually")
        finally:
            _saved_volumes.clear()
            _ducked = False


def _fade_all(targets) -> None:
    """Step every (simple_volume, from_level, to_level) target in lockstep
    over _FADE_STEPS increments spanning _FADE_DURATION_S total -- one
    shared step loop, not one fade per session, so total duck()/restore()
    latency is bounded at ~150ms regardless of session count."""
    if not targets:
        return
    for step in range(1, _FADE_STEPS + 1):
        frac = step / _FADE_STEPS
        for simple_volume, from_level, to_level in targets:
            level = from_level + (to_level - from_level) * frac
            try:
                _set_session_volume(simple_volume, level)
            except Exception:
                continue
        if step < _FADE_STEPS:
            time.sleep(_STEP_DELAY_S)


atexit.register(restore)
