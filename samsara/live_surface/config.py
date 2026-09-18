"""Validated, pure config contract and one-time migration for live surface."""
from __future__ import annotations

from copy import deepcopy
from math import isfinite
from typing import Any, Mapping

from .placement import Edge, Placement, normalize_placement, placement_record

SCHEMA_VERSION = 1
DEFAULT_IDLE_DELAY_S = 5.0
DEFAULT_FOCUS_HOTKEY = "ctrl+alt+f10"
DEFAULT_RESET_HOTKEY = "ctrl+alt+shift+f10"
DEFAULT_PAUSE_HOTKEY = "pause"
PARTIALS = ("auto", "on", "off")
LIVE_SURFACE_DEFAULTS = {
    "schema_version": SCHEMA_VERSION,
    "idle_delay_s": DEFAULT_IDLE_DELAY_S,
    "show_idle_mark": True,
    "partials": "auto",
    "focus_hotkey": DEFAULT_FOCUS_HOTKEY,
    "reset_hotkey": DEFAULT_RESET_HOTKEY,
    "pause_hotkey": DEFAULT_PAUSE_HOTKEY,
    "placement": {"preferred_monitor": None, "monitors": {}},
    "legacy": {},
}

# CapsLock streaming entries are deliberately absent. The 2026-09-17 owner
# decision retires them in a later package; this migration neither reads nor
# archives them so it cannot resurrect that product mode.
_LEGACY_FIELDS = (
    "command_mode.preview_idle_delay_s",
    "command_mode.preview_idle_opacity",
    "command_mode.session_streaming_preview",
    "listening_indicator_enabled",
    "command_mode.preview_position",
    "listening_indicator_position",
    "listening_indicator_custom_position",
    "accessibility.ava_captions_position",
)


def validate_live_surface_config(raw: object) -> dict[str, Any]:
    """Return a complete bounded config block without mutating ``raw``."""
    source = raw if isinstance(raw, Mapping) else {}
    result = deepcopy(LIVE_SURFACE_DEFAULTS)
    version = source.get("schema_version")
    result["schema_version"] = SCHEMA_VERSION if not isinstance(version, int) or version < 1 else SCHEMA_VERSION
    delay = source.get("idle_delay_s")
    result["idle_delay_s"] = _bounded_float(delay, 1.0, 60.0, DEFAULT_IDLE_DELAY_S)
    for key in ("show_idle_mark",):
        if isinstance(source.get(key), bool):
            result[key] = source[key]
    partials = source.get("partials")
    if partials in PARTIALS:
        result["partials"] = partials
    for key in ("focus_hotkey", "reset_hotkey", "pause_hotkey"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip().lower()
    result["placement"] = validate_placement(source.get("placement"))
    if isinstance(source.get("legacy"), Mapping):
        result["legacy"] = _bounded_legacy(source["legacy"])
    return result


def validate_placement(raw: object) -> dict[str, Any]:
    """Validate the persisted monitor map without discarding unknown monitors."""
    if not isinstance(raw, Mapping):
        return deepcopy(LIVE_SURFACE_DEFAULTS["placement"])
    preferred = raw.get("preferred_monitor")
    preferred = preferred if isinstance(preferred, str) and preferred else None
    monitors_raw = raw.get("monitors")
    monitors: dict[str, dict[str, float | str]] = {}
    if isinstance(monitors_raw, Mapping):
        for monitor_id, record in monitors_raw.items():
            if not isinstance(monitor_id, str) or not monitor_id or len(monitors) >= 32:
                continue
            placement = normalize_placement(record)
            monitors[monitor_id] = placement_record(placement)
    return {"preferred_monitor": preferred, "monitors": monitors}


def migrate_live_surface(persisted: dict) -> dict:
    """Pure, idempotent migration based on persisted presence, not defaults.

    Existing ``ui.live_surface`` keys always win. Only values actually used to
    supply an absent new key enter the small ``legacy`` backup.
    """
    result = deepcopy(persisted) if isinstance(persisted, dict) else {}
    ui = result.get("ui")
    if not isinstance(ui, dict):
        ui = {}
        result["ui"] = ui
    original_live = ui.get("live_surface")
    present_live = original_live if isinstance(original_live, Mapping) else {}
    live = validate_live_surface_config(present_live)
    legacy: dict[str, Any] = dict(live.get("legacy", {}))

    def old(path: str) -> tuple[bool, Any]:
        return _get_present(persisted, path)

    def migrate(key: str, value: Any, sources: tuple[str, ...]) -> None:
        if key in present_live:
            return
        live[key] = value
        legacy[key] = {
            "sources": {source: {"present": old(source)[0], "value": deepcopy(old(source)[1])}
                        for source in sources if old(source)[0]}
        }

    delay_present, delay = old("command_mode.preview_idle_delay_s")
    if delay_present:
        migrate("idle_delay_s", _bounded_float(delay, 1.0, 60.0, DEFAULT_IDLE_DELAY_S),
                ("command_mode.preview_idle_delay_s",))

    if "show_idle_mark" not in present_live:
        opacity_present, opacity = old("command_mode.preview_idle_opacity")
        preview_present, preview = old("command_mode.session_streaming_preview")
        indicator_present, indicator = old("listening_indicator_enabled")
        show = True
        sources: tuple[str, ...] = ()
        if opacity_present:
            show = not isinstance(opacity, (int, float)) or bool(opacity)
            sources = ("command_mode.preview_idle_opacity",)
        elif preview_present and indicator_present and preview is False and indicator is False:
            show = False
            sources = ("command_mode.session_streaming_preview", "listening_indicator_enabled")
        if sources:
            migrate("show_idle_mark", show, sources)

    if "placement" not in present_live:
        placement, sources = _legacy_placement(persisted)
        if sources:
            migrate("placement", placement, sources)

    live["legacy"] = _bounded_legacy(legacy)
    ui["live_surface"] = validate_live_surface_config(live)
    return result


def _legacy_placement(persisted: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    preview_present, preview = _get_present(persisted, "command_mode.preview_position")
    if preview_present and isinstance(preview, str) and preview not in ("", "bottom-center"):
        return _legacy_position_record(preview, _get_present(persisted, "listening_indicator_custom_position")[1]), (
            "command_mode.preview_position",)
    indicator_present, indicator = _get_present(persisted, "listening_indicator_position")
    custom_present, custom = _get_present(persisted, "listening_indicator_custom_position")
    if indicator_present and isinstance(indicator, str) and indicator not in ("", "bottom-center"):
        sources = ("listening_indicator_position",) + (("listening_indicator_custom_position",) if custom_present else ())
        return _legacy_position_record(indicator, custom), sources
    captions_present, captions = _get_present(persisted, "accessibility.ava_captions_position")
    if captions_present and isinstance(captions, str) and captions not in ("", "bottom-center"):
        return _legacy_position_record(captions, None), ("accessibility.ava_captions_position",)
    return deepcopy(LIVE_SURFACE_DEFAULTS["placement"]), ()


def _legacy_position_record(position: str, custom: Any) -> dict[str, Any]:
    if position == "custom" and isinstance(custom, Mapping):
        monitor = custom.get("screen") if isinstance(custom.get("screen"), str) else "legacy:default"
        p = Placement(Edge.FREE, free_cx=_unit(custom.get("cx")), free_cy=_unit(custom.get("cy")))
    elif position.startswith("custom|"):
        parts = position.split("|")
        monitor = parts[1] if len(parts) > 1 and parts[1] else "legacy:default"
        p = Placement(Edge.FREE, free_cx=_unit(parts[2] if len(parts) > 2 else 0.5),
                      free_cy=_unit(parts[3] if len(parts) > 3 else 0.5))
    else:
        vertical, _, horizontal = position.partition("-")
        if vertical in ("top", "bottom"):
            p = Placement(Edge(vertical), t={"left": 0.0, "center": 0.5, "right": 1.0}.get(horizontal, 0.5))
        else:
            p = Placement()
        monitor = "legacy:default"
    return {"preferred_monitor": monitor, "monitors": {monitor: placement_record(p)}}


def _get_present(mapping: Mapping[str, Any], dotted: str) -> tuple[bool, Any]:
    node: Any = mapping
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _bounded_float(value: Any, low: float, high: float, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) and low <= number <= high else default


def _unit(value: Any) -> float:
    try:
        number = float(value)
        return min(1.0, max(0.0, number)) if isfinite(number) else 0.5
    except (TypeError, ValueError):
        return 0.5


def _bounded_legacy(raw: Mapping[str, Any]) -> dict[str, Any]:
    # The fixed mapping has at most three values. Never retain arbitrary
    # config copies, transcripts, or CapsLock streaming settings here.
    allowed = {"idle_delay_s", "show_idle_mark", "placement"}
    return {key: deepcopy(value) for key, value in raw.items() if key in allowed}
