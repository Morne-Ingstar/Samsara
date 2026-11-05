"""One resolver for speech pace endpointing values.

``standard`` is deliberately an identity: callers pass the fallback they used
before this setting existed.  The other profiles scale that already-effective
value, avoiding a new competing default during the current config-drift work.
"""
from __future__ import annotations

from statistics import median
from copy import deepcopy
from math import isfinite
from typing import Any


STANDARD = "standard"
RELAXED = "relaxed"
UNHURRIED = "unhurried"
CUSTOM = "custom"
PROFILES = (STANDARD, RELAXED, UNHURRIED, CUSTOM)

# Long pauses need room; a one-word utterance needs a lower admission floor.
_RULES = {
    RELAXED: {"chunk": 1.0, "silence": 2.5, "wake": 2.0, "minimum": 2 / 3},
    UNHURRIED: {"chunk": 1.0, "silence": 4.0, "wake": 3.0, "minimum": 1 / 3},
}
_KIND = {
    "command_mode.dictate_utterance_silence_s": "chunk",
    "silence_threshold": "silence",
    "wake_word_config.quick_silence_timeout": "silence",
    "wake_word_config.audio.wake_command_timeout": "wake",
    "min_speech_duration": "minimum",
}
_BOUNDS = {
    "command_mode.dictate_utterance_silence_s": (0.3, 12.0),
    "silence_threshold": (0.5, 40.0),
    "wake_word_config.quick_silence_timeout": (0.2, 20.0),
    "wake_word_config.audio.wake_command_timeout": (1.0, 90.0),
    "min_speech_duration": (0.1, 2.0),
}


def _get(config: dict, key: str, fallback: float) -> float:
    value: Any = config
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return float(fallback)
        value = value[part]
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def profile(config: dict) -> str:
    pause = _get(config, "accessibility.speech_pause_s", float("nan"))
    if isfinite(pause):
        if (config.get("accessibility") or {}).get("speech_pause_anchor"):
            return CUSTOM
        return next((name for name, stop in STOPS.items() if abs(pause - stop) < .005), CUSTOM)
    value = _get_text(config, "accessibility.speech_pace")
    return {"slow": RELAXED, "very slow": UNHURRIED}.get(value, value if value in PROFILES else STANDARD)


def _get_text(config: dict, key: str) -> str:
    value: Any = config
    for part in key.split("."):
        if not isinstance(value, dict):
            return ""
        value = value.get(part)
    return str(value or "").strip().lower()


def effective_value(config: dict, key: str, fallback: float) -> float:
    """Return this caller's prior value at standard, otherwise paced/clamped."""
    base = _get(config, key, fallback)
    if isfinite(_get(config, "accessibility.speech_pause_s", float("nan"))):
        # The raw keys are already derived. Never apply the old profile twice.
        return max(_BOUNDS[key][0], min(_BOUNDS[key][1], base))
    selected = profile(config)
    if selected == STANDARD:
        return base
    custom_values = (config.get("accessibility", {}) or {}).get("speech_pace_custom", {}) or {}
    try:
        custom = float(custom_values.get(key, base))
    except (TypeError, ValueError):
        custom = base
    if selected == CUSTOM:
        return max(_BOUNDS[key][0], min(_BOUNDS[key][1], custom))
    factor = _RULES[selected][_KIND[key]]
    return max(_BOUNDS[key][0], min(_BOUNDS[key][1], base * factor))


def clear_gap_s(config: dict) -> float:
    """Manual DICTATE chunks stage internally; pace must not delay controls."""
    chunk = effective_value(config, "command_mode.dictate_utterance_silence_s", 0.65)
    gap = max(1.0, 1.5 * chunk)
    if profile(config) == CUSTOM:
        custom = (config.get("accessibility", {}) or {}).get("speech_pace_custom", {}) or {}
        gap = _get(custom, "fragment_clear_gap_s", gap)
    return max(0.0, gap)


STOPS = {STANDARD: 2.0, RELAXED: 5.0, UNHURRIED: 8.0}
_BASE = dict(zip(_KIND, (.65, 2.0, 1.0, 5.0, .3)))


def _bounded_pause(value: float) -> float:
    value = float(value)
    return round(max(.5, min(8.0, value if isfinite(value) else 2.0)), 2)


def pause_seconds(config: dict) -> float:
    """Project legacy endpointing onto the slider without changing config."""
    pause = _get(config, "accessibility.speech_pause_s", float("nan"))
    if isfinite(pause):
        return _bounded_pause(pause)
    return _bounded_pause(effective_value(config, "silence_threshold", _BASE["silence_threshold"]))


def derived_values(pause: float) -> dict[str, float]:
    """Interpolate the existing profile ratios; preserve 743's chunk plateau.

    Minimum speech decreases as pauses grow; all other values never decrease.
    At the labelled stops every legacy schema-default output is exact.
    """
    pause = _bounded_pause(pause)
    nodes = ((.5, {"chunk": .25, "silence": .25, "wake": .5, "minimum": 1.5}),
             (2., {"chunk": 1., "silence": 1., "wake": 1., "minimum": 1.}),
             (5., _RULES[RELAXED]), (8., _RULES[UNHURRIED]))
    for (lo, left), (hi, right) in zip(nodes, nodes[1:]):
        if pause <= hi:
            weight = (pause - lo) / (hi - lo)
            factors = {kind: left[kind] + weight * (right[kind] - left[kind]) for kind in left}
            return {key: max(_BOUNDS[key][0], min(_BOUNDS[key][1], base * factors[_KIND[key]]))
                    for key, base in _BASE.items()}
    raise AssertionError("bounded pause outside interpolation")


def _put(config: dict, key: str, value) -> None:
    parts = key.split(".")
    for part in parts[:-1]:
        config = config.setdefault(part, {})
    config[parts[-1]] = value


def with_pause(config: dict, pause: float, *, preserve_legacy: bool = True) -> dict:
    """Return migrated config, retaining a hand-tuned starting point losslessly.

    The anchor preserves all five legacy effective values, even if its silence
    exceeds the slider range. Moving selects the shared profile curve.
    Original raw values and the Custom dictionary remain available for recovery.
    """
    result = deepcopy(config)
    acc = result.setdefault("accessibility", {})
    start = pause_seconds(config)
    if not isfinite(_get(config, "accessibility.speech_pause_s", float("nan"))):
        values = {key: effective_value(config, key, base) for key, base in _BASE.items()}
        canonical = derived_values(start)
        if any(abs(values[key] - canonical[key]) > 1e-9 for key in values):
            acc["speech_pause_anchor"] = {"pause_s": start, "values": values,
                "legacy_raw": {key: _get(config, key, base) for key, base in _BASE.items()}}
    pause = _bounded_pause(pause)
    values = derived_values(pause)
    anchor = acc.get("speech_pause_anchor")
    if anchor and (pause != start or not preserve_legacy):
        acc["speech_pause_legacy"] = acc.pop("speech_pause_anchor")
        anchor = None
    if anchor:
        # No arithmetic on a preserved starting point: retain exact floats.
        values = dict(anchor["values"])
    acc["speech_pause_s"] = pause
    acc["speech_pace"] = next((name for name, stop in STOPS.items() if pause == stop), CUSTOM)
    # A migrated noncanonical stop is Custom until the user explicitly resets it.
    if anchor:
        acc["speech_pace"] = CUSTOM
    for key, value in values.items():
        _put(result, key, value)
    return result


def measured_interword_pauses(samples, sample_rate: int, *, speech_threshold: float = 0.02) -> list[float]:
    """Return only silence bounded by speech; exclude starting/ending waits."""
    pauses, start, in_silence, heard_speech = [], None, False, False
    for index, sample in enumerate(samples):
        silent = abs(float(sample)) < speech_threshold
        if silent and heard_speech and not in_silence:
            start, in_silence = index, True
        elif not silent:
            if in_silence:
                pauses.append((index - start) / float(sample_rate))
                in_silence = False
            heard_speech = True
    return pauses


def typical_pause_s(pauses: list[float]) -> float:
    """Median internal gap, so one hesitation cannot dominate a reading."""
    return float(median(pauses)) if pauses else 0.0


def recommend_profile(pauses: list[float]) -> str:
    """A recommendation only; the wizard never writes it without a choice."""
    typical = typical_pause_s(pauses)
    # At 100ms guide resolution, Relaxed owns twenty distinct gap bins.
    return UNHURRIED if typical >= 2.5 else RELAXED if typical >= 0.5 else STANDARD


def pace_recommendation(pauses: list[float]) -> str:
    """Shared one-line explanation for both calibration surfaces (stand-in)."""
    reason = f"typical pause {typical_pause_s(pauses):g} s" if pauses else "no pauses between speech"
    return f"Suggested pace: {recommend_profile(pauses).capitalize()} ({reason})."  # STAND-IN COPY (615)
