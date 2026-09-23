"""Read-only, config-derived facts for guidance and onboarding copy.

This module deliberately accepts the already-live config dictionary.  It does
not load config, retain it, or try to apply pending settings changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from samsara.config_defaults import DEFAULTS
from samsara.constants import DEFAULT_CONTINUOUS_COMMIT_HOTKEY, DEFAULT_WAKE_PHRASE_OPTIONS
from samsara import session_modes


_DEFAULT_END_WORDS = ("over", "done", "end dictation")
_DEFAULT_SEND_WORDS = ("over", "send")


def _words(value: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Return usable, de-duplicated spoken phrases without mutating config."""
    if not isinstance(value, (list, tuple)):
        return fallback
    result: list[str] = []
    for item in value:
        phrase = item.strip() if isinstance(item, str) else ""
        if phrase and phrase not in result:
            result.append(phrase)
    return tuple(result) or fallback


def _word(value: Any, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


@dataclass(frozen=True)
class WakeProfileFacts:
    """The termination control for one enabled, configured wake profile."""

    phrase: str
    send_word: str
    mode: str


@dataclass(frozen=True)
class SessionEndFacts:
    """Distinct session actions; stopping keeps a draft, discard asks first."""

    commit_word: str
    stop_words: tuple[str, ...]
    discard_words: tuple[str, ...]


@dataclass(frozen=True)
class GuidanceFacts:
    wake_phrase: str
    wake_aliases: tuple[str, ...]
    wake_phrases: tuple[str, ...]
    wake_profile_send_words: tuple[WakeProfileFacts, ...]
    send_words: tuple[str, ...]
    end_words: tuple[str, ...]
    dictation_hotkey: str
    command_hotkey: str
    wake_word_hotkey: str
    dictate_commit_hotkey: str
    dictation_mode: str
    dictation_behavior: str
    idle_timeout_s: int
    wake_activation: str
    wake_activation_requested_session: bool
    session_end: SessionEndFacts

    @property
    def idle_timeout_meaning(self) -> str:
        return f"closes after {self.idle_timeout_s} seconds without activity"


def facts_from_config(config: dict[str, Any] | None) -> GuidanceFacts:
    """Derive display facts from one live config mapping, without side effects."""
    config = config if isinstance(config, dict) else {}
    wake = config.get("wake_word_config")
    wake = wake if isinstance(wake, dict) else {}
    command_mode = config.get("command_mode")
    command_mode = command_mode if isinstance(command_mode, dict) else {}

    wake_phrase = _word(wake.get("phrase"), str(DEFAULTS["wake_word_config.phrase"]))
    configured_aliases = _words(wake.get("phrase_options"), tuple(DEFAULT_WAKE_PHRASE_OPTIONS))
    wake_aliases = tuple(word for word in configured_aliases if word != wake_phrase)

    profiles: list[WakeProfileFacts] = []
    for profile in config.get("wake_profiles", ()):
        if not isinstance(profile, dict) or not profile.get("enabled", True):
            continue
        phrase = _word(profile.get("phrase"), "")
        send_word = _word(profile.get("send_word"), "")
        if phrase and send_word:
            profiles.append(WakeProfileFacts(phrase, send_word, _word(profile.get("mode"), "focus_dictate")))

    profile_phrases = tuple(profile.phrase for profile in profiles if profile.phrase != wake_phrase)
    wake_phrases = (wake_phrase, *wake_aliases, *profile_phrases)
    mode = _word(config.get("mode"), str(DEFAULTS["mode"]))
    requested_session = bool(wake.get("opens_session", False))
    activation = "hands_free_session" if requested_session and mode == "toggle" else "one_command_window"
    timeout = command_mode.get("inactivity_timeout_s", DEFAULTS["command_mode.inactivity_timeout_s"])
    try:
        idle_timeout_s = max(0, int(timeout))
    except (TypeError, ValueError):
        idle_timeout_s = int(DEFAULTS["command_mode.inactivity_timeout_s"])

    stop_words = _words(command_mode.get("stop_phrases"), tuple(session_modes.SESSION_STOP_PHRASES))
    return GuidanceFacts(
        wake_phrase=wake_phrase,
        wake_aliases=wake_aliases,
        wake_phrases=wake_phrases,
        wake_profile_send_words=tuple(profiles),
        send_words=_words(wake.get("send_words"), _DEFAULT_SEND_WORDS),
        end_words=_words(wake.get("end_words"), _DEFAULT_END_WORDS),
        dictation_hotkey=_word(config.get("hotkey"), str(DEFAULTS["hotkey"])),
        command_hotkey=_word(config.get("command_hotkey"), "ctrl+alt+c"),
        wake_word_hotkey=_word(config.get("wake_word_hotkey"), "ctrl+alt+w"),
        dictate_commit_hotkey=_word(config.get("dictate_commit_hotkey"), DEFAULT_CONTINUOUS_COMMIT_HOTKEY),
        dictation_mode=mode,
        dictation_behavior={"hold": "hold_to_dictate", "toggle": "press_to_start_press_to_stop"}.get(mode, "continuous"),
        idle_timeout_s=idle_timeout_s,
        wake_activation=activation,
        wake_activation_requested_session=requested_session,
        session_end=SessionEndFacts(
            commit_word=_word(command_mode.get("dictate_commit_word"), session_modes.DEFAULT_DICTATE_COMMIT_PHRASE),
            stop_words=stop_words,
            discard_words=tuple(session_modes.CLEAR_DRAFT_PHRASES),
        ),
    )
