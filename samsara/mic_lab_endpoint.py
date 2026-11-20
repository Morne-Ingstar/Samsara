"""Read-only endpoint metadata and opt-in native preflight capture.

Imports do not enumerate or open hardware. COM inspection never changes gain,
default devices, or enhancements. PortAudio indices and IMMDevice IDs are
different namespaces; only an explicit ID or unique exact name is accepted.
All public operations return Observation('unknown', reason) on failure.
"""
from __future__ import annotations

import ctypes as ct
from dataclasses import dataclass
import math
import sys
import uuid


@dataclass(frozen=True)
class Observation:
    value: object = "unknown"
    reason: str = ""

    @property
    def known(self):
        return not self.reason


CAPTURE_CONTRACT = {
    "host_api": "selected device's host API; saved identity preferred, WASAPI preferred on name fallback",
    "wasapi_mode": "shared (no extra_settings override in production stream)",
    "sample_rate": "app detects default_samplerate (query fallback 48000) and passes _capture_rate; engine's own query fallback is 16000",
    "channels": 1,
    "dtype": "float32",
    "evidence": ("dictation.py:2387", "dictation.py:3160", "samsara/constants.py:10", "samsara/audio_engine/engine.py:418", "samsara/audio_engine/engine.py:428", "samsara/audio_engine/engine.py:435", "samsara/audio_engine/engine.py:471", "samsara/audio_engine/device_resolver.py:156"),
}
GAIN_DELTA_THRESHOLD = .02  # Carbon review 2026-11-18 section 5, >=2 percentage points.


def bluetooth_hands_free(name="", enumerator=""):
    try:
        text = (name + " " + enumerator).casefold().replace("-", " ")
        return Observation(any(s in text for s in ("hands free", "handsfree", "bthhfenum", "hfp", "headset ag audio")))
    except Exception as exc:
        return Observation(reason=str(exc))


def interface_kind(name="", enumerator=""):
    try:
        text = (name + " " + enumerator).casefold()
        if bluetooth_hands_free(name, enumerator).value is True:
            return Observation("Bluetooth hands-free")
        if "a2dp" in text or "btha2dp" in text:
            return Observation("Bluetooth A2DP")
        if any(s in text for s in ("focusrite", "scarlett", "audio interface", "motu", "steinberg", "asio")):
            return Observation("interface")
        if "usb" in text:
            return Observation("USB")
        if any(s in text for s in ("built-in", "built in", "microphone array", "realtek", "internal")):
            return Observation("built-in")
        return Observation(reason="Interface cannot be inferred from endpoint name/enumerator")
    except Exception as exc:
        return Observation(reason=str(exc))


def capture_configuration(device=None, *, backend=None, capture_rate=None):
    """Inspect an explicitly selected PortAudio index/default; never read live config."""
    try:
        if backend is None:
            import sounddevice as backend
        info = backend.query_devices(device, kind="input")
        api = backend.query_hostapis(info["hostapi"])["name"]
        return Observation(dict(name=info["name"], host_api=api,
                                mode="shared" if "wasapi" in api.casefold() else "not WASAPI",
                                sample_rate=int(capture_rate or info["default_samplerate"]),
                                channels=1, evidence=CAPTURE_CONTRACT["evidence"]))
    except Exception as exc:
        return Observation(reason=f"Capture configuration unavailable: {exc}")


class _GUID(ct.Structure):
    _fields_ = [("data1", ct.c_uint32), ("data2", ct.c_uint16), ("data3", ct.c_uint16), ("data4", ct.c_ubyte * 8)]

    def __init__(self, value):
        super().__init__()
        ct.memmove(ct.byref(self), uuid.UUID(value).bytes_le, 16)


class _PropertyKey(ct.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ct.c_uint32)]


class _VariantValue(ct.Union):
    # PROPVARIANT is 24 bytes on Win64 and 16 on Win32 (counted pointer union).
    _fields_ = [("text", ct.c_void_p), ("storage", ct.c_byte * (16 if ct.sizeof(ct.c_void_p) == 8 else 8))]


class _PropVariant(ct.Structure):
    _fields_ = [("vt", ct.c_uint16), ("reserved", ct.c_uint16 * 3), ("value", _VariantValue)]


class _WaveFormat(ct.Structure):
    _pack_ = 1
    _fields_ = [("tag", ct.c_uint16), ("channels", ct.c_uint16), ("rate", ct.c_uint32),
                ("bytes_sec", ct.c_uint32), ("align", ct.c_uint16), ("bits", ct.c_uint16), ("extra", ct.c_uint16)]


def _call(pointer, slot, args=(), types=()):
    table = ct.cast(pointer, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
    fn = ct.WINFUNCTYPE(ct.c_long, ct.c_void_p, *types)(table[slot])
    hr = fn(pointer, *args)
    if hr < 0:
        raise OSError(f"Core Audio HRESULT 0x{hr & 0xffffffff:08x} at slot {slot}")
    return hr


def _release(pointer):
    if pointer:
        table = ct.cast(pointer, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
        ct.WINFUNCTYPE(ct.c_ulong, ct.c_void_p)(table[2])(pointer)


class _CoreAudio:
    """Small ctypes COM adapter; all pointers are released in the calling thread."""
    def read(self, *, endpoint_id=None, name=None):
        if sys.platform != "win32":
            raise OSError("Windows Core Audio is unavailable on this platform")
        # 1297: inspect RPC_E_CHANGED_MODE ourselves; OleDLL raises before
        # the existing-apartment handler and also misreads void returns.
        ole = ct.WinDLL("ole32")
        ole.CoInitializeEx.argtypes = (ct.c_void_p, ct.c_uint32)
        ole.CoInitializeEx.restype = ct.c_long
        ole.CoCreateInstance.argtypes = (ct.POINTER(_GUID), ct.c_void_p, ct.c_uint32, ct.POINTER(_GUID), ct.POINTER(ct.c_void_p))
        ole.CoCreateInstance.restype = ct.c_long
        ole.CoTaskMemFree.argtypes = (ct.c_void_p,)
        ole.CoTaskMemFree.restype = None
        ole.CoUninitialize.argtypes = ()
        ole.CoUninitialize.restype = None
        ole.PropVariantClear.argtypes = (ct.POINTER(_PropVariant),)
        ole.PropVariantClear.restype = ct.c_long
        # RPC_E_CHANGED_MODE means this thread is already initialized as STA.
        hr = ole.CoInitializeEx(None, 0)
        if hr < 0 and (hr & 0xffffffff) != 0x80010106:
            raise OSError(f"CoInitializeEx failed: 0x{hr & 0xffffffff:08x}")
        uninitialize = hr >= 0
        enum = ct.c_void_p()
        device = ct.c_void_p()
        try:
            clsid = _GUID("bcde0395-e52f-467c-8e3d-c4579291692e")
            iid = _GUID("a95664d2-9614-4f35-a746-de8db63617e6")
            created = ole.CoCreateInstance(ct.byref(clsid), None, 1, ct.byref(iid), ct.byref(enum))
            if created < 0:
                raise OSError(f"MMDeviceEnumerator failed: 0x{created & 0xffffffff:08x}")
            if endpoint_id:
                _call(enum, 5, (endpoint_id, ct.byref(device)), (ct.c_wchar_p, ct.POINTER(ct.c_void_p)))
            else:
                if not name:
                    raise ValueError("Supply an endpoint ID or exact endpoint name; no default-device substitution")
                collection = ct.c_void_p()
                matches = []
                try:
                    _call(enum, 3, (1, 1, ct.byref(collection)), (ct.c_int, ct.c_uint32, ct.POINTER(ct.c_void_p)))
                    count = ct.c_uint32()
                    _call(collection, 3, (ct.byref(count),), (ct.POINTER(ct.c_uint32),))
                    for index in range(count.value):
                        candidate = ct.c_void_p()
                        try:
                            _call(collection, 4, (index, ct.byref(candidate)), (ct.c_uint32, ct.POINTER(ct.c_void_p)))
                            if self._property(candidate, ole, 14).casefold() == name.casefold():
                                matches.append(self._id(candidate, ole))
                        finally:
                            _release(candidate)
                finally:
                    _release(collection)
                if len(matches) != 1:
                    raise ValueError(f"Exact capture endpoint name matched {len(matches)} devices; supply IMMDevice ID")
                _call(enum, 5, (matches[0], ct.byref(device)), (ct.c_wchar_p, ct.POINTER(ct.c_void_p)))
            # Reject explicit render endpoints, even if they have a similar name.
            endpoint = ct.c_void_p()
            try:
                iid = _GUID("1be09788-6894-4089-8586-9a2a6c265ac5")
                _call(device, 0, (ct.byref(iid), ct.byref(endpoint)), (ct.POINTER(_GUID), ct.POINTER(ct.c_void_p)))
                flow = ct.c_int()
                _call(endpoint, 3, (ct.byref(flow),), (ct.POINTER(ct.c_int),))
                if flow.value != 1:
                    raise ValueError("Endpoint is not a capture endpoint")
            finally:
                _release(endpoint)
            result = {"endpoint_id": self._id(device, ole), "name": self._property(device, ole, 14)}
            try:
                result["enumerator"] = self._property(device, ole, 24)
            except Exception as exc:
                result["enumerator"] = Observation(reason=f"Enumerator unavailable: {exc}")
            for field, operation in (("mix_format", lambda: self._format(device, ole)), ("volume_scalar", lambda: self._volume(device))):
                try:
                    result[field] = operation()
                except Exception as exc:
                    result[field] = Observation(reason=f"{field} unavailable: {exc}")
            return result
        finally:
            _release(device)
            _release(enum)
            if uninitialize:
                ole.CoUninitialize()

    @staticmethod
    def _id(device, ole):
        value = ct.c_void_p()
        try:
            _call(device, 5, (ct.byref(value),), (ct.POINTER(ct.c_void_p),))
            return ct.wstring_at(value)
        finally:
            if value:
                ole.CoTaskMemFree(value)

    @staticmethod
    def _property(device, ole, pid):
        store = ct.c_void_p()
        variant = _PropVariant()
        try:
            _call(device, 4, (0, ct.byref(store)), (ct.c_uint32, ct.POINTER(ct.c_void_p)))
            key = _PropertyKey(_GUID("a45c254e-df1c-4efd-8020-67d146a850e0"), pid)
            _call(store, 5, (ct.byref(key), ct.byref(variant)), (ct.POINTER(_PropertyKey), ct.POINTER(_PropVariant)))
            if variant.vt != 31 or not variant.value.text:  # VT_LPWSTR
                raise ValueError("Endpoint string property unavailable")
            return ct.wstring_at(variant.value.text)
        finally:
            ole.PropVariantClear(ct.byref(variant))
            _release(store)

    @staticmethod
    def _activate(device, iid):
        pointer = ct.c_void_p()
        guid = _GUID(iid)
        _call(device, 3, (ct.byref(guid), 23, None, ct.byref(pointer)), (ct.POINTER(_GUID), ct.c_uint32, ct.c_void_p, ct.POINTER(ct.c_void_p)))
        return pointer

    def _format(self, device, ole):
        client = self._activate(device, "1cb9ad4c-dbfa-4c32-b178-c2f568a703b2")
        fmt = ct.c_void_p()
        try:
            _call(client, 8, (ct.byref(fmt),), (ct.POINTER(ct.c_void_p),))  # GetMixFormat, not Initialize
            wave = ct.cast(fmt, ct.POINTER(_WaveFormat)).contents
            return dict(sample_rate=int(wave.rate), channels=int(wave.channels))
        finally:
            if fmt:
                ole.CoTaskMemFree(fmt)
            _release(client)

    def _volume(self, device):
        volume = self._activate(device, "5cdf2c82-841e-4546-9722-0cf74078229a")
        try:
            scalar = ct.c_float()
            _call(volume, 9, (ct.byref(scalar),), (ct.POINTER(ct.c_float),))
            return float(scalar.value)
        finally:
            _release(volume)


def inspect_endpoint(*, endpoint_id=None, name=None, provider=None):
    """Return observed Core Audio identity, shared mix format and gain.

    Mix format is the Windows shared engine default, not an ADC specification.
    provider.read is injectable; fake providers are used by all automated tests.
    """
    try:
        data = dict((provider or _CoreAudio()).read(endpoint_id=endpoint_id, name=name))
        enumerator = data.get("enumerator", "")
        enum_text = enumerator if isinstance(enumerator, str) else ""
        data["interface_kind"] = interface_kind(data.get("name", ""), enum_text)
        hfp = bluetooth_hands_free(data.get("name", ""), enum_text)
        if not hfp.value and isinstance(enumerator, Observation) and not enumerator.known:
            hfp = Observation(reason="Enumerator unavailable and name does not establish hands-free status")
        data["bluetooth_hands_free"] = hfp
        data["format_source"] = "IAudioClient.GetMixFormat (shared engine default; hardware-native format unknown)"
        return Observation(data)
    except Exception as exc:
        return Observation(reason=f"Endpoint inspection unavailable: {exc}")


def endpoint_volume(*, endpoint_id=None, name=None, provider=None):
    try:
        observed = inspect_endpoint(endpoint_id=endpoint_id, name=name, provider=provider)
        if not observed.known:
            return observed
        value = observed.value.get("volume_scalar")
        if isinstance(value, Observation):
            return value
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("Volume scalar is outside 0..1")
        return Observation(value)
    except Exception as exc:
        return Observation(reason=f"Endpoint volume unavailable: {exc}")


def volume_delta(before, after):
    """Compare two endpoint_volume observations; caller schedules reads per take."""
    try:
        if not before.known or not after.known:
            return Observation(reason=before.reason or after.reason)
        values = [float(before.value), float(after.value)]
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in values):
            raise ValueError("Volume scalar is outside 0..1")
        delta = values[1] - values[0]
        # Core Audio returns float32; allow its rounding at the 2% boundary.
        return Observation(dict(delta=delta, changed=abs(delta) >= GAIN_DELTA_THRESHOLD - 1e-7))
    except Exception as exc:
        return Observation(reason=f"Volume comparison unavailable: {exc}")


def native_preflight(device, seconds=5., *, backend=None, stream_factory=None):
    """Opt-in blocking capture at PortAudio default rate, at most two channels.

    Returns PCM, sample count, monotonic elapsed read time and overflow flag.
    Never called by inspection/import. Tests MUST inject backend + fake stream.
    PortAudio default is not guaranteed ADC native; use the WASAPI device index
    for a shared-mix comparison. Caller obtains consent before Bluetooth capture.
    """
    stream = None
    try:
        import time
        import numpy as np
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("seconds must be positive and finite")
        if backend is None:
            import sounddevice as backend
        info = backend.query_devices(device, kind="input")
        rate = int(info["default_samplerate"])
        channels = min(2, int(info["max_input_channels"]))
        if rate <= 0 or channels <= 0:
            raise ValueError("Device has no usable input format")
        count = int(round(seconds * rate))
        if count < 1:
            raise ValueError("Probe is shorter than one sample")
        factory = stream_factory or backend.InputStream
        stream = factory(device=device, samplerate=rate, channels=channels, dtype="float32")
        with stream:
            start = time.perf_counter()
            audio, overflowed = stream.read(count)
            elapsed = time.perf_counter() - start
            audio = np.asarray(audio, dtype=np.float32)
            if audio.shape != (count, channels) or not np.isfinite(audio).all():
                raise ValueError("Stream returned malformed or incomplete PCM")
            return Observation(dict(audio=audio.copy(), sample_rate=rate, channels=channels,
                                    frames_captured=len(audio), read_wall_seconds=elapsed,
                                    input_overflow=bool(overflowed), format_source="PortAudio default_samplerate"))
    except Exception as exc:
        return Observation(reason=f"Native preflight unavailable: {exc}")
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
