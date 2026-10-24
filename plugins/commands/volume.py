"""Volume control plugin.

Controls Windows default audio output via Core Audio COM API.
Pure ctypes, no external dependencies. Targets the correct
default multimedia endpoint directly.

"Jarvis, volume up"    - increase system volume by 20%
"Jarvis, volume down"  - decrease system volume by 20%
"Jarvis, mute"         - toggle mute on/off
"""

import ctypes
import ctypes.wintypes as wintypes
import sys
from math import isfinite
from numbers import Real
from ctypes import POINTER, byref, cast, c_float, c_void_p
from ctypes import Structure, Union, c_ulong

from samsara.plugin_commands import command
from samsara.command_registry import DispatchResult, DispatchState

VOLUME_STEP = 0.20

# ── Core Audio COM definitions ─────────────────────────────────────────────
# Manually defined vtable layout for IAudioEndpointVolume
# Reference: Windows SDK endpointvolume.h, mmdeviceapi.h

CLSCTX_ALL = 23
E_RENDER = 0        # eRender
E_MULTIMEDIA = 1    # eMultimedia

CLSID_MMDeviceEnumerator = ctypes.c_char * 16
IID_IMMDeviceEnumerator  = ctypes.c_char * 16
IID_IAudioEndpointVolume = ctypes.c_char * 16


def _guid_bytes(s):
    """Convert GUID string to bytes for COM."""
    import struct
    parts = s.strip('{}').split('-')
    return struct.pack('<IHH', int(parts[0], 16), int(parts[1], 16),
                       int(parts[2], 16)) + bytes.fromhex(parts[3] + parts[4])


_CLSID_MMDeviceEnumerator = _guid_bytes('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
_IID_IMMDeviceEnumerator  = _guid_bytes('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
_IID_IAudioEndpointVolume = _guid_bytes('{5CDF2C82-841E-4546-9722-0CF74078229A}')


# COM function types
_ole32 = None  # Existing adapter seam; the real DLL is loaded only on Windows use.


def _hresult_type():
    """Return the signed 32-bit Windows LONG used for HRESULT results."""
    return ctypes.c_long


def _get_ole32():
    global _ole32
    if sys.platform != "win32":
        return None
    if _ole32 is None:
        _ole32 = ctypes.windll.ole32
        _ole32.CoInitialize.argtypes = [c_void_p]
        _ole32.CoInitialize.restype = _hresult_type()
        _ole32.CoCreateInstance.argtypes = [
            c_void_p, c_void_p, ctypes.c_ulong, c_void_p, POINTER(c_void_p)
        ]
        _ole32.CoCreateInstance.restype = _hresult_type()
    return _ole32


def _get_vtable_func(iface_ptr, index, restype, *argtypes):
    """Get a function from a COM vtable by index."""
    vtable = cast(iface_ptr, POINTER(POINTER(c_void_p)))
    func_ptr = vtable[0][index]
    functype = ctypes.WINFUNCTYPE(restype, *argtypes)
    return functype(func_ptr)


class _CoreAudio:
    """Minimal Core Audio wrapper using raw COM vtables."""

    def __init__(self):
        self._initialized = False
        self._enumerator = None

    def _ensure_init(self):
        if self._initialized:
            return True
        ole32 = _get_ole32()
        if ole32 is None:
            return False
        hr = ole32.CoInitialize(None)
        if hr < 0 and hr != 1:  # S_OK or S_FALSE (already initialized)
            print(f"[VOLUME] CoInitialize failed: {hr:#010x}")
            return False
        self._initialized = True
        return True

    def _get_enumerator(self):
        """Get IMMDeviceEnumerator."""
        enumerator = c_void_p()
        hr = _get_ole32().CoCreateInstance(
            _CLSID_MMDeviceEnumerator,
            None,
            CLSCTX_ALL,
            _IID_IMMDeviceEnumerator,
            byref(enumerator)
        )
        if hr != 0:
            print(f"[VOLUME] CoCreateInstance failed: {hr:#010x}")
            return None
        return enumerator

    def _get_default_device(self, enumerator):
        """IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eMultimedia)
        Vtable index: 4 (IUnknown=3 + EnumAudioEndpoints=3 + GetDefaultAudioEndpoint=4)
        """
        # IMMDeviceEnumerator vtable:
        # 0: QueryInterface
        # 1: AddRef
        # 2: Release
        # 3: EnumAudioEndpoints
        # 4: GetDefaultAudioEndpoint
        func = _get_vtable_func(
            enumerator, 4, _hresult_type(),
            c_void_p,          # this
            ctypes.c_uint,     # dataFlow (eRender=0)
            ctypes.c_uint,     # role (eMultimedia=1)
            POINTER(c_void_p)  # ppDevice
        )
        device = c_void_p()
        hr = func(enumerator, E_RENDER, E_MULTIMEDIA, byref(device))
        if hr != 0:
            print(f"[VOLUME] GetDefaultAudioEndpoint failed: {hr:#010x}")
            return None
        return device

    def _activate_endpoint_volume(self, device):
        """IMMDevice::Activate(IAudioEndpointVolume)
        Vtable index: 3
        """
        # IMMDevice vtable:
        # 0: QueryInterface
        # 1: AddRef
        # 2: Release
        # 3: Activate
        func = _get_vtable_func(
            device, 3, _hresult_type(),
            c_void_p,          # this
            c_void_p,          # iid
            ctypes.c_ulong,    # dwClsCtx
            c_void_p,          # pActivationParams
            POINTER(c_void_p)  # ppInterface
        )
        volume_iface = c_void_p()
        hr = func(device, _IID_IAudioEndpointVolume, CLSCTX_ALL, None,
                  byref(volume_iface))
        if hr != 0:
            print(f"[VOLUME] Activate failed: {hr:#010x}")
            return None
        return volume_iface

    def _get_volume_interface(self):
        """Full pipeline: enumerator -> device -> volume interface."""
        if not self._ensure_init():
            return None
        enum = self._get_enumerator()
        if not enum:
            return None
        device = self._get_default_device(enum)
        if not device:
            return None
        volume = self._activate_endpoint_volume(device)
        # Release intermediate objects
        _get_vtable_func(enum, 2, ctypes.c_ulong, c_void_p)(enum)     # Release
        _get_vtable_func(device, 2, ctypes.c_ulong, c_void_p)(device) # Release
        return volume

    def get_volume(self):
        """Get master volume scalar (0.0 - 1.0).
        IAudioEndpointVolume vtable:
        0-2: IUnknown
        3: RegisterControlChangeNotify
        4: UnregisterControlChangeNotify
        5: GetChannelCount
        6: SetMasterVolumeLevel
        7: SetMasterVolumeLevelScalar
        8: GetMasterVolumeLevel
        9: GetMasterVolumeLevelScalar
        """
        vol = self._get_volume_interface()
        if not vol:
            return None
        try:
            func = _get_vtable_func(
                vol, 9, _hresult_type(),
                c_void_p,          # this
                POINTER(c_float)   # pfLevel
            )
            level = c_float()
            hr = func(vol, byref(level))
            if hr != 0:
                print(f"[VOLUME] GetMasterVolumeLevelScalar failed: {hr:#010x}")
                return None
            return level.value
        finally:
            _get_vtable_func(vol, 2, ctypes.c_ulong, c_void_p)(vol)  # Release

    def get_volume_range(self):
        """Get the endpoint's min, max and increment in dB (vtable slot 20)."""
        vol = self._get_volume_interface()
        if not vol:
            return None
        try:
            func = _get_vtable_func(
                vol, 20, _hresult_type(),
                c_void_p,
                POINTER(c_float),
                POINTER(c_float),
                POINTER(c_float),
            )
            minimum, maximum, increment = c_float(), c_float(), c_float()
            hr = func(vol, byref(minimum), byref(maximum), byref(increment))
            if hr != 0:
                print(f"[VOLUME] GetVolumeRange failed: {hr:#010x}")
                return None
            return minimum.value, maximum.value, increment.value
        finally:
            _get_vtable_func(vol, 2, ctypes.c_ulong, c_void_p)(vol)  # Release

    def set_volume(self, level):
        """Set master volume scalar (0.0 - 1.0).
        IAudioEndpointVolume vtable index 7: SetMasterVolumeLevelScalar
        """
        vol = self._get_volume_interface()
        if not vol:
            return False
        try:
            func = _get_vtable_func(
                vol, 7, _hresult_type(),
                c_void_p,      # this
                c_float,       # fLevel
                c_void_p       # pguidEventContext (NULL)
            )
            clamped = max(0.0, min(1.0, level))
            hr = func(vol, c_float(clamped), None)
            if hr != 0:
                print(f"[VOLUME] SetMasterVolumeLevelScalar failed: {hr:#010x}")
                return False
            return True
        finally:
            _get_vtable_func(vol, 2, ctypes.c_ulong, c_void_p)(vol)  # Release

    def get_mute(self):
        """Get mute state.
        IAudioEndpointVolume vtable index 15: GetMute
        """
        vol = self._get_volume_interface()
        if not vol:
            return None
        try:
            func = _get_vtable_func(
                vol, 15, _hresult_type(),
                c_void_p,              # this
                POINTER(wintypes.BOOL) # pbMute (Windows BOOL)
            )
            muted = wintypes.BOOL()
            hr = func(vol, byref(muted))
            if hr != 0:
                return None
            return bool(muted.value)
        finally:
            _get_vtable_func(vol, 2, ctypes.c_ulong, c_void_p)(vol)

    def set_mute(self, mute):
        """Set mute state.
        IAudioEndpointVolume vtable index 14: SetMute
        """
        vol = self._get_volume_interface()
        if not vol:
            return False
        try:
            func = _get_vtable_func(
                vol, 14, _hresult_type(),
                c_void_p,      # this
                wintypes.BOOL, # bMute (Windows BOOL)
                c_void_p       # pguidEventContext
            )
            hr = func(vol, int(mute), None)
            return hr == 0
        finally:
            _get_vtable_func(vol, 2, ctypes.c_ulong, c_void_p)(vol)


_audio = _CoreAudio()


def _volume_failure(app, reason, *, fixed_endpoint=False):
    message = ("This device controls its own volume" if fixed_endpoint else reason)
    show = getattr(app, "_show_outcome_chip", None)
    if callable(show):
        show(message, "warning", 3500)
    return DispatchResult(DispatchState.FAILED, phrase="set volume",
                          detail={"reason": message})


def _set_and_verify_volume(app, level):
    # Older endpoint adapters may not expose a usable range. Preserve their
    # setter contract rather than doing arithmetic on unknown readback values.
    try:
        range_reader = _audio.get_volume_range
        # A native range cannot verify a substituted getter or setter: those
        # may target another device (or no device). Keep the adapter's contract
        # without querying unrelated native hardware.
        native_range = getattr(range_reader, "__func__", None) is _CoreAudio.get_volume_range
        native_io = (getattr(_audio.get_volume, "__func__", None) is _CoreAudio.get_volume
                     and getattr(_audio.set_volume, "__func__", None) is _CoreAudio.set_volume)
        volume_range = range_reader() if not native_range or native_io else None
        known_range = (isinstance(volume_range, (tuple, list)) and
                       len(volume_range) == 3 and
                       all(isinstance(value, Real) and not isinstance(value, bool)
                           and isfinite(value) for value in volume_range) and
                       volume_range[0] <= volume_range[1])
    except Exception:
        known_range = False

    def read_volume():
        try:
            value = _audio.get_volume()
            if (isinstance(value, Real) and not isinstance(value, bool)
                    and isfinite(value)):
                return value
        except Exception:
            pass
        return None

    previous = read_volume() if known_range else None
    if not _audio.set_volume(level):
        return False
    actual = level
    if known_range:
        readback = read_volume()
        fixed = (abs(volume_range[0]) <= 1e-6 and
                 abs(volume_range[1]) <= 1e-6)
        if previous is not None and readback is not None:
            unchanged = abs(readback - previous) <= 0.005
            if fixed and unchanged:
                return _volume_failure(app, "Could not set Windows volume", fixed_endpoint=True)
            if unchanged and abs(readback - level) > 0.005:
                return _volume_failure(app, "Could not set Windows volume")
            actual = readback
        # Unavailable readback is not evidence that a successful write failed.
    show = getattr(app, "_show_outcome_chip", None)
    if callable(show):
        show(f"Windows volume {round(actual * 100)}%", "success", 3500)
    return True


# ── Commands ───────────────────────────────────────────────────────────────

@command("set volume", aliases=[
    "volume", "volume to", "set volume to", "set the volume to",
    "lower volume to", "lower the volume to", "raise volume to",
], pack="media", risk_class="safe", ai_composable=True, side_effects=["audio"],
    param_schema={"level": {"type": "int", "required": False, "min": 0, "max": 100}})
def handle_set_volume(app, remainder):
    """Sets Windows volume to a spoken number from zero to one hundred."""
    from samsara.grammar import _number
    words = str(remainder or "").strip().lower().split()
    if words[:1] == ["to"]:
        words = words[1:]
    level = 100 if words in (["100"], ["one", "hundred"]) else _number(words)
    if level is None or not 0 <= level <= 100:
        return False
    return _set_and_verify_volume(app, level / 100)


@command("volume up", aliases=[
    "turn it up", "louder", "increase volume",
    "turn up the volume", "raise the volume"
], pack="media",
    risk_class='safe', ai_composable=True, side_effects=['audio'])
def handle_volume_up(app, remainder):
    """Turns the system volume up by twenty percent."""
    current = _audio.get_volume()
    if current is None:
        print("[VOLUME] Failed to read volume")
        return False
    new = min(1.0, current + VOLUME_STEP)
    ok = _set_and_verify_volume(app, new)
    print(f"[VOLUME] {int(current*100)}% -> {int(new*100)}%")
    return ok


@command("volume down", aliases=[
    "turn it down", "quieter", "decrease volume",
    "lower the volume", "turn down the volume", "softer"
], pack="media",
    risk_class='safe', ai_composable=True, side_effects=['audio'])
def handle_volume_down(app, remainder):
    """Turns the system volume down by twenty percent."""
    current = _audio.get_volume()
    if current is None:
        print("[VOLUME] Failed to read volume")
        return False
    new = max(0.0, current - VOLUME_STEP)
    ok = _set_and_verify_volume(app, new)
    print(f"[VOLUME] {int(current*100)}% -> {int(new*100)}%")
    return ok


@command("toggle mute", aliases=[
    "silence", "unsilence"
], pack="media",
    risk_class='safe', ai_composable=True, side_effects=['audio'])
def handle_mute(app, remainder):
    """Mutes or unmutes the system sound."""
    muted = _audio.get_mute()
    if muted is None:
        print("[VOLUME] Failed to read mute state")
        return False
    ok = _audio.set_mute(not muted)
    if not ok:
        return DispatchResult(DispatchState.FAILED, phrase="toggle mute",
                              detail={"reason": "Could not change mute"})
    actual = _audio.get_mute()
    if actual is None or actual != (not muted):
        return DispatchResult(DispatchState.FAILED, phrase="toggle mute",
                              detail={"reason": "Could not verify mute"})
    print(f"[VOLUME] Mute: {actual}")
    return True
