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
from ctypes import POINTER, HRESULT, byref, cast, c_float, c_void_p
from ctypes import Structure, Union, c_ulong

from samsara.plugin_commands import command

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
_ole32 = ctypes.windll.ole32
_ole32.CoInitialize.argtypes = [c_void_p]
_ole32.CoInitialize.restype = HRESULT
_ole32.CoCreateInstance.argtypes = [
    c_void_p, c_void_p, ctypes.c_ulong, c_void_p, POINTER(c_void_p)
]
_ole32.CoCreateInstance.restype = HRESULT


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
        hr = _ole32.CoInitialize(None)
        if hr < 0 and hr != 1:  # S_OK or S_FALSE (already initialized)
            print(f"[VOLUME] CoInitialize failed: {hr:#010x}")
            return False
        self._initialized = True
        return True

    def _get_enumerator(self):
        """Get IMMDeviceEnumerator."""
        enumerator = c_void_p()
        hr = _ole32.CoCreateInstance(
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
            enumerator, 4, HRESULT,
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
            device, 3, HRESULT,
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
                vol, 9, HRESULT,
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

    def set_volume(self, level):
        """Set master volume scalar (0.0 - 1.0).
        IAudioEndpointVolume vtable index 7: SetMasterVolumeLevelScalar
        """
        vol = self._get_volume_interface()
        if not vol:
            return False
        try:
            func = _get_vtable_func(
                vol, 7, HRESULT,
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
        IAudioEndpointVolume vtable index 13: GetMute
        """
        vol = self._get_volume_interface()
        if not vol:
            return None
        try:
            func = _get_vtable_func(
                vol, 13, HRESULT,
                c_void_p,              # this
                POINTER(ctypes.c_int)  # pbMute
            )
            muted = ctypes.c_int()
            hr = func(vol, byref(muted))
            if hr != 0:
                return None
            return bool(muted.value)
        finally:
            _get_vtable_func(vol, 2, ctypes.c_ulong, c_void_p)(vol)

    def set_mute(self, mute):
        """Set mute state.
        IAudioEndpointVolume vtable index 12: SetMute
        """
        vol = self._get_volume_interface()
        if not vol:
            return False
        try:
            func = _get_vtable_func(
                vol, 12, HRESULT,
                c_void_p,      # this
                ctypes.c_int,  # bMute
                c_void_p       # pguidEventContext
            )
            hr = func(vol, int(mute), None)
            return hr == 0
        finally:
            _get_vtable_func(vol, 2, ctypes.c_ulong, c_void_p)(vol)


_audio = _CoreAudio()


# ── Commands ───────────────────────────────────────────────────────────────

@command("volume up", aliases=[
    "turn it up", "louder", "increase volume",
    "turn up the volume", "raise the volume"
], pack="media")
def handle_volume_up(app, remainder):
    """Increase system volume by 20%."""
    current = _audio.get_volume()
    if current is None:
        print("[VOLUME] Failed to read volume")
        return False
    new = min(1.0, current + VOLUME_STEP)
    ok = _audio.set_volume(new)
    print(f"[VOLUME] {int(current*100)}% -> {int(new*100)}%")
    return ok


@command("volume down", aliases=[
    "turn it down", "quieter", "decrease volume",
    "lower the volume", "turn down the volume", "softer"
], pack="media")
def handle_volume_down(app, remainder):
    """Decrease system volume by 20%."""
    current = _audio.get_volume()
    if current is None:
        print("[VOLUME] Failed to read volume")
        return False
    new = max(0.0, current - VOLUME_STEP)
    ok = _audio.set_volume(new)
    print(f"[VOLUME] {int(current*100)}% -> {int(new*100)}%")
    return ok


@command("toggle mute", aliases=[
    "mute", "unmute", "silence", "unsilence"
], pack="media")
def handle_mute(app, remainder):
    """Toggle system mute."""
    muted = _audio.get_mute()
    if muted is None:
        print("[VOLUME] Failed to read mute state")
        return False
    ok = _audio.set_mute(not muted)
    print(f"[VOLUME] Mute: {not muted}")
    return ok
