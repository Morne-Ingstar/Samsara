"""One resolver for speech pace endpointing values.

``standard`` is deliberately an identity: callers pass the fallback they used
before this setting existed.  The other profiles scale that already-effective
value, avoiding a new competing default during the current config-drift work.
"""
from __future__ import annotations

from typing import Any


STANDARD = "standard"
RELAXED = "relaxed"
UNHURRIED = "unhurried"
CUSTOM = "custom"
PROFILES = (STANDARD, RELAXED, UNHURRIED, CUSTOM)

# Long pauses need room; a one-word utterance needs a lower admission floor.
_RULES = {
    RELAXED: {"silence": 2.5, "wake": 2.0, "minimum": 2 / 3, "clear_gap": 3.75},
    UNHURRIED: {"silence": 4.0, "wake": 3.0, "minimum": 1 / 3, "clear_gap": 6.0},
}
_KIND = {
    "command_mode.dictate_utterance_silence_s": "silence",
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
    """A command is alone only after this silence under paced profiles."""
    selected = profile(config)
    return _RULES.get(selected, {}).get("clear_gap", 0.0)


def measured_interword_pauses(samples, sample_rate: int, *, speech_threshold: float = 0.02) -> list[float]:
    """Return silence runs between voiced samples; pure synthetic-VAD helper."""
    pauses, start, in_silence = [], None, False
    for index, sample in enumerate(samples):
        silent = abs(float(sample)) < speech_threshold
        if silent and not in_silence:
            start, in_silence = index, True
        elif not silent and in_silence:
            pauses.append((index - start) / float(sample_rate))
            in_silence = False
    return pauses


def recommend_profile(pauses: list[float]) -> str:
    """A recommendation only; the wizard never writes it without a choice."""
    typical = max(pauses, default=0.0)
    return UNHURRIED if typical >= 2.5 else RELAXED if typical >= 1.0 else STANDARD
