"""Resolve an input device to a PortAudio INDEX, never a bare name.

Why this exists (2026-09-12 live incident): a USB interface whose name
appears under several host APIs cannot be opened by name at all. The engine's
recovery loop passed the bare string it had stashed at start()::

    [ACE] Opening stream: device='Analogue 1 + 2 (Focusrite USB Audio)'
    [ACE] Recovery attempt failed, retrying in 2s: Multiple input devices
          found for 'Analogue 1 + 2 (Focusrite USB Audio)':
          [.. MME] / [.. Windows DirectSound]

sounddevice raises that ValueError itself -- an ambiguous name is not
resolvable, so every one of the 30 retries failed identically and recovery
gave up after 60 s while the device was sitting there the whole time. Boot
never hit this because it opens the reconciled INDEX from
samsara.audio_devices.list_microphones() (device=30 in the log).

The fix is to resolve the same way boot does: a (name, hostapi) pair
captured when the stream was successfully opened, matched back to a live
index, and only then handed to sounddevice.

Every function here takes the sounddevice module as an argument so the
resolution rules are testable against a fake enumeration with no audio
hardware.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Host API preference, best first. WASAPI leads because that is what
#: samsara.audio_devices.list_microphones() prefers when it builds the device
#: list the user picks from (audio_devices.py's `preferred_api_idx` loop), so
#: recovery lands on the same backend boot chose. The rest are ordered by how
#: well they behave for capture on Windows.
HOSTAPI_PREFERENCE: tuple[str, ...] = (
    "wasapi",
    "windows wasapi",
    "mme",
    "directsound",
    "windows directsound",
    "wdm-ks",
    "asio",
)


class DeviceNotFound(RuntimeError):
    """No live input device matches the recorded identity or name."""


def _hostapi_names(sd) -> list[str]:
    try:
        return [str(api.get("name", "")) for api in sd.query_hostapis()]
    except Exception as exc:
        logger.debug("[ACE] query_hostapis failed: %s", exc)
        return []


def _preference_rank(hostapi_name: str) -> int:
    """Lower is better. Unknown host APIs sort last but stay usable."""
    lowered = (hostapi_name or "").casefold()
    for rank, candidate in enumerate(HOSTAPI_PREFERENCE):
        if candidate in lowered:
            return rank
    return len(HOSTAPI_PREFERENCE)


def capture_identity(sd, device) -> "tuple[str, str] | None":
    """Record (device_name, hostapi_name) for a device that just opened.

    `device` is whatever was handed to sounddevice -- an index, a name, or
    None for the system default. Returns None when the device cannot be
    described, in which case recovery falls back to name-only resolution.
    """
    try:
        info = sd.query_devices(device, kind="input")
    except Exception as exc:
        logger.debug("[ACE] identity capture failed for %r: %s", device, exc)
        return None
    name = str(info.get("name", "") or "")
    if not name:
        return None
    hostapis = _hostapi_names(sd)
    hostapi_index = info.get("hostapi", None)
    hostapi_name = ""
    if isinstance(hostapi_index, int) and 0 <= hostapi_index < len(hostapis):
        hostapi_name = hostapis[hostapi_index]
    return (name, hostapi_name)


def _input_candidates(sd, name: str) -> list[tuple[int, str]]:
    """[(index, hostapi_name)] for every live INPUT device with this name."""
    try:
        devices = sd.query_devices()
    except Exception as exc:
        raise DeviceNotFound(f"device enumeration failed: {exc}") from exc
    hostapis = _hostapi_names(sd)
    wanted = (name or "").strip().casefold()
    out: list[tuple[int, str]] = []
    for index, device in enumerate(devices):
        if int(device.get("max_input_channels", 0) or 0) <= 0:
            continue
        if str(device.get("name", "")).strip().casefold() != wanted:
            continue
        hostapi_index = device.get("hostapi", None)
        hostapi_name = ""
        if isinstance(hostapi_index, int) and 0 <= hostapi_index < len(hostapis):
            hostapi_name = hostapis[hostapi_index]
        out.append((index, hostapi_name))
    return out


def resolve(sd, name: str, hostapi_name: str = "") -> tuple[int, str, bool]:
    """Resolve (name, hostapi) to a live input index.

    Returns (index, hostapi_name, exact) where `exact` is True when the
    recorded host API was still available. When it is False the caller should
    log which host API was substituted -- the device came back on a different
    backend, which is normal after a USB re-enumeration.

    Raises DeviceNotFound when the name is not present under any host API.
    """
    if not name:
        raise DeviceNotFound("no device name recorded")
    candidates = _input_candidates(sd, name)
    if not candidates:
        raise DeviceNotFound(f"no live input device named {name!r}")

    wanted = (hostapi_name or "").strip().casefold()
    if wanted:
        for index, api in candidates:
            if api.strip().casefold() == wanted:
                return (index, api, True)

    # Recorded host API is gone (or was never recorded): take the best
    # available one in the same preference order boot uses.
    index, api = min(candidates, key=lambda pair: (_preference_rank(pair[1]), pair[0]))
    return (index, api, False)


def device_present(sd, name: str) -> bool:
    """Cheap "is it back yet?" probe for the slow background retry loop.

    Deliberately only enumerates -- it never opens a stream, so it is safe to
    call every few seconds forever.
    """
    if not name:
        return False
    try:
        return bool(_input_candidates(sd, name))
    except DeviceNotFound:
        return False
