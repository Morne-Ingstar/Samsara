"""Stable enumeration and reconciliation for Samsara audio outputs."""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable, Optional


logger = logging.getLogger(__name__)


_HOST_API_PRIORITY = {
    "windows wasapi": 0,
    "windows wdm-ks": 1,
    "windows directsound": 2,
    "mme": 3,
}

# Windows MME truncates PortAudio endpoint names to 31 characters.  A saved
# MME selection can therefore look like ``Headphones (Arctis Nova Pro Wir``
# while the preferred WASAPI endpoint reports the full device name.  Prefix
# recovery is deliberately limited to long, unique names so ordinary short
# names ("Speakers", "Headset", etc.) are never guessed.
_MIN_TRUNCATED_NAME_CHARS = 24
_missing_playback_logged: set[tuple[str, str]] = set()


def _key(name: str) -> str:
    return " ".join(str(name).split()).casefold()


def enumerate_output_devices(sd_module: Any, show_all: bool = False) -> list[dict]:
    """Return one stable entry per physical output name.

    PortAudio exposes the same endpoint through several Windows host APIs,
    often with raw driver names that differ enough to defeat name-based
    deduplication.  Match the microphone selector: show only WASAPI devices
    by default, and expose every host API only when ``show_all`` is enabled.
    """
    devices = sd_module.query_devices()
    try:
        hostapis = sd_module.query_hostapis()
    except Exception:
        hostapis = []

    preferred_api_idx = next(
        (
            index
            for index, api in enumerate(hostapis)
            if "wasapi" in str(api.get("name", "")).casefold()
        ),
        None,
    )

    chosen: dict[str, tuple[tuple[int, int], dict]] = {}
    for index, raw in enumerate(devices):
        if int(raw.get("max_output_channels", 0) or 0) <= 0:
            continue
        host_index = int(raw.get("hostapi", -1))
        if not show_all and preferred_api_idx is not None and host_index != preferred_api_idx:
            continue
        name = " ".join(str(raw.get("name", f"Output {index}")).split())
        host_name = ""
        if 0 <= host_index < len(hostapis):
            host_name = str(hostapis[host_index].get("name", ""))
        rank = (_HOST_API_PRIORITY.get(host_name.casefold(), 50), index)
        entry = {"id": index, "name": name, "hostapi": host_name}
        prior = chosen.get(_key(name))
        if prior is None or rank < prior[0]:
            chosen[_key(name)] = (rank, entry)

    return [item[1] for item in sorted(chosen.values(), key=lambda x: _key(x[1]["name"]))]


def output_identity(sd_module: Any, device_id: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    """Return the stable (name, host API) identity for one live output index."""
    if device_id is None:
        return None, None
    try:
        item = sd_module.query_devices(device_id, "output")
        name = " ".join(str(item.get("name", "")).split()) or None
        host_index = int(item.get("hostapi", -1))
        hostapis = sd_module.query_hostapis()
        host = str(hostapis[host_index].get("name", "")) if 0 <= host_index < len(hostapis) else None
        return name, host
    except Exception:
        return None, None


def windows_default_output(sd_module: Any) -> Optional[int]:
    """The output endpoint Windows/PortAudio calls default *right now*.

    `sounddevice.default.device[1]` is intentionally queried per playback;
    it is not a selection to cache.  Returning None remains a valid PortAudio
    default when a backend does not expose an index.
    """
    try:
        default = getattr(getattr(sd_module, "default", None), "device", None)
        if isinstance(default, (tuple, list)) and len(default) > 1:
            candidate = default[1]
            if isinstance(candidate, int) and candidate >= 0:
                return candidate
    except Exception:
        pass
    return None


def resolve_playback_device(
    sd_module: Any,
    stored_name: Optional[str] = None,
    stored_hostapi: Optional[str] = None,
) -> Optional[int]:
    """Resolve the current output at playback time, never using a saved index.

    A chosen endpoint is identified by name plus host API.  If it disappeared,
    return the current Windows default and log that fallback once.  A missing
    selection means "Follow Windows default" and is re-read for every call.
    """
    name = " ".join(str(stored_name or "").split())
    if not name:
        return windows_default_output(sd_module)
    try:
        devices = sd_module.query_devices()
        hostapis = sd_module.query_hostapis()
    except Exception as exc:
        logger.warning("[AUDIO] Could not enumerate selected output; using Windows default: %s", exc)
        return windows_default_output(sd_module)
    wanted_name, wanted_host = _key(name), _key(stored_hostapi or "")
    candidates: list[tuple[int, str]] = []
    for index, item in enumerate(devices):
        if int(item.get("max_output_channels", 0) or 0) <= 0 or _key(item.get("name", "")) != wanted_name:
            continue
        host_index = int(item.get("hostapi", -1))
        host = str(hostapis[host_index].get("name", "")) if 0 <= host_index < len(hostapis) else ""
        candidates.append((index, host))
    if candidates:
        for index, host in candidates:
            if wanted_host and _key(host) == wanted_host:
                return index
        return min(candidates, key=lambda pair: (_HOST_API_PRIORITY.get(_key(pair[1]), 50), pair[0]))[0]
    key = (wanted_name, wanted_host)
    if key not in _missing_playback_logged:
        _missing_playback_logged.add(key)
        logger.warning("[AUDIO] Selected output '%s' is unavailable; following Windows default", name)
    return windows_default_output(sd_module)


def output_sample_rate(
    sd_module: Any,
    device_id: Optional[int],
    fallback: int = 44100,
) -> int:
    """Return the output endpoint's native/default sample rate.

    ``None`` intentionally queries PortAudio's current default output. Invalid
    or incomplete device metadata safely falls back rather than preventing
    Samsara from starting audio feedback.
    """
    try:
        info = sd_module.query_devices(device_id, "output")
        rate = float(info.get("default_samplerate", 0) or 0)
        if math.isfinite(rate) and rate > 0:
            return int(round(rate))
    except Exception:
        pass
    return int(fallback)


def reconcile_output_device(
    devices: Iterable[dict],
    stored_id: Optional[int],
    stored_name: Optional[str],
) -> tuple[Optional[int], Optional[str], bool]:
    """Resolve a saved output by stable name first, then by legacy index.

    Returns ``(id, name, missing)``. A missing explicit selection resolves to
    the system default (``None``) without rewriting the saved preference.
    """
    items = list(devices)
    if stored_id is None and not stored_name:
        return None, None, False
    if stored_name:
        wanted = _key(stored_name)
        for item in items:
            if _key(item.get("name", "")) == wanted:
                return int(item["id"]), str(item["name"]), False

        # PortAudio's legacy MME API truncates endpoint names, whereas WASAPI
        # usually exposes the complete name.  Prefer WASAPI during enumeration
        # and recover a legacy saved identity only when exactly one candidate
        # shares the sufficiently long prefix.  Ambiguity remains a safe
        # system-default fallback instead of routing sound to the wrong device.
        if len(wanted) >= _MIN_TRUNCATED_NAME_CHARS:
            prefix_matches = [
                item
                for item in items
                if len(_key(item.get("name", ""))) >= _MIN_TRUNCATED_NAME_CHARS
                and (
                    _key(item.get("name", "")).startswith(wanted)
                    or wanted.startswith(_key(item.get("name", "")))
                )
            ]
            if len(prefix_matches) == 1:
                item = prefix_matches[0]
                return int(item["id"]), str(item["name"]), False
    if stored_name is None and stored_id is not None:
        for item in items:
            if int(item["id"]) == int(stored_id):
                return int(item["id"]), str(item["name"]), False
    return None, None, True
