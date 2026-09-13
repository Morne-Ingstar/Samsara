"""Pure helpers for audio-device discovery.

Kept separate from dictation.py's input-path logic so unit tests can exercise
device selection deterministically, and all Qt callers can share one stable,
single source-of-truth implementation.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import sounddevice as sd

_logger = logging.getLogger("Samsara.audio_devices")

_SKIP_KEYWORDS = {
    'stereo mix', 'wave out mix', 'what u hear', 'loopback',
    'cable', 'virtual audio', 'vb-audio', 'voicemeeter',
    'sound mapper', 'primary sound', 'wave speaker', 'wave microphone',
    'stream wave', 'chat capture', 'hands-free', 'hf audio', 'input ()',
    'line in (', 'vdvad', 'steelseries sonar', 'oculusvad',
    'vad wave', 'wc4400_8200'
}


def pick_index_by_name(devices, name):
    """Return the position of the device named `name` in `devices`, or None.

    `devices` is a list of dicts shaped like get_available_microphones()'s
    return value (each with a 'name' key). Matching is by name, not by
    PortAudio device id/index -- indices shift after re-enumeration, but a
    device's reported name is stable across reconnects.

    Returns None (never raises, never guesses) when `name` is falsy or not
    present in `devices` -- callers fall back to their own default (e.g. the
    first item, or "System default") in that case.
    """
    if not name:
        return None
    for idx, dev in enumerate(devices):
        if dev.get('name') == name:
            return idx
    return None


def _ensure_com_initialized() -> bool:
    """Ensure COM is initialized before PortAudio-side WASAPI enumeration.

    On Windows, WASAPI-backed host API visibility can be empty when COM is not
    initialized on the calling thread. CoInitializeEx with STA is the leading
    theory for first-run's frozen-thread empty-enumeration failure.

    Returns True when this call owns a COM init reference and must call
    CoUninitialize(). Returns False when no COM action was possible or needed
    (non-Windows or mode mismatch).
    """
    if sys.platform != "win32":
        return False

    import ctypes

    COINIT_APARTMENTTHREADED = 0x2
    RPC_E_CHANGED_MODE = 0x80010106

    hr = ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    hr_u = hr & 0xFFFFFFFF  # ctypes returns HRESULT as signed c_long; normalize
    if hr_u == RPC_E_CHANGED_MODE:
        # Existing COM apartment with incompatible model; we must keep going
        # with whatever host-side visibility is currently active.
        return False
    if hr_u not in (0, 1):
        raise RuntimeError(f"CoInitializeEx failed with HRESULT 0x{hr_u:08X}")
    return True


def list_microphones(show_all: bool = False) -> list[dict]:
    """Return stable, deduplicated input microphones.

    Keeps the legacy behavior from DictationApp.get_available_microphones(), with
    the critical differences that it no longer belongs to DictationApp and it
    raises instead of converting enumeration failures to [].
    """
    should_uninit = _ensure_com_initialized()
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        microphones: list[dict] = []
        seen_names: set[str] = set()
        preferred_api_idx = None

        for idx, api in enumerate(hostapis):
            if 'wasapi' in str(api.get('name', '')).casefold():
                preferred_api_idx = idx
                break

        for index, device in enumerate(devices):
            if int(device.get('max_input_channels', 0) or 0) <= 0:
                continue

            host_index = int(device.get('hostapi', -1))
            if preferred_api_idx is not None and not show_all:
                if host_index != preferred_api_idx:
                    continue

            name = str(device.get('name', f'Input {index}'))
            dedup_key = name.strip().casefold()
            if not dedup_key or dedup_key in seen_names:
                continue

            if not show_all:
                if any(keyword in dedup_key for keyword in _SKIP_KEYWORDS):
                    continue
                if name.strip() == "Microphone ()":
                    continue
                if '@System32\\drivers\\' in name:
                    continue

            seen_names.add(dedup_key)
            microphones.append({
                'id': index,
                'name': name,
                'channels': int(device.get('max_input_channels', 0) or 0),
            })

        return microphones
    finally:
        if should_uninit:
            try:
                import ctypes
                ctypes.windll.ole32.CoUninitialize()
            except Exception as exc:
                _logger.debug("CoUninitialize failed in list_microphones: %s", exc)


def force_rescan(sd_module: Any | None = None) -> None:
    """Force a PortAudio re-scan.

    Mirrors the documented sd._terminate()/sd._initialize() cycle used when the
    input device set changes while Samsara is running. Errors are logged and not
    treated as fatal; callers may still proceed with a normal re-query.
    """
    module = sd_module or sd
    try:
        module._terminate()
    except Exception as exc:
        _logger.warning("[MIC] PortAudio _terminate() failed during rescan: %s", exc)
    try:
        module._initialize()
    except Exception as exc:
        _logger.warning("[MIC] PortAudio _initialize() failed during rescan: %s", exc)


def get_device_info(device_index, kind: str | None = None) -> dict:
    """Query one specific PortAudio device record via sounddevice.

    Thin wrapper is present so UI call sites can call through this module instead
    of importing sounddevice directly.
    """
    if kind is None:
        return sd.query_devices(device_index)
    return sd.query_devices(device_index, kind)


def detect_capture_rate(device_index) -> int:
    """The input device's own default sample rate (fallback
    DEFAULT_CAPTURE_RATE). A guide that must open its own transient stream
    uses this instead of a fixed rate: a fixed 16 kHz fails with
    PortAudioError -9997 on interfaces that only offer 44.1/48 kHz."""
    from samsara.constants import DEFAULT_CAPTURE_RATE  # noqa: PLC0415

    try:
        info = get_device_info(device_index, kind='input')
        rate = int(info.get("default_samplerate", DEFAULT_CAPTURE_RATE))
        return rate if rate > 0 else DEFAULT_CAPTURE_RATE
    except Exception:
        return DEFAULT_CAPTURE_RATE
