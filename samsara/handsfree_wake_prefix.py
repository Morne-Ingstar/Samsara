"""An explicit wake prefix selects one command inside buffered hands-free."""
from __future__ import annotations

import re
import string


def split_wake_prefix(text: str, phrase: str) -> str | None:
    """Match normalized whole words, returning the original command payload."""
    from samsara.session_modes import normalize_utterance

    wake = normalize_utterance(phrase)
    normalized = normalize_utterance(text)
    if not wake or not (normalized == wake or normalized.startswith(wake + " ")):
        return None
    # Find the raw boundary rather than dispatching normalized arguments:
    # normalization removes punctuation needed by questions, paths and URLs.
    for word in re.finditer(r"\S+", text):
        if normalize_utterance(text[:word.end()]) == wake:
            return text[word.end():].lstrip(string.whitespace + string.punctuation)
    return None


class HandsFreeWakePrefix:
    def __init__(self, app):
        self.app = app
        self.pending = None

    def dispatch(self, manager, text, signals):
        from samsara.constants import WAKE_COMMAND_TIMEOUT
        from samsara.session_modes import (
            DispatchOutcome, SessionMode, normalize_utterance,
            passes_switch_anti_hallucination_gate,
        )
        from samsara.speech_pace import effective_value

        pending, self.pending = self.pending, None
        if manager.mode is not SessionMode.DICTATE or not manager._buffer_dictate_until_commit:
            return None
        normalized = normalize_utterance(text)
        # Naked emergency/session controls retain their existing precedence.
        if (normalized in manager._stop_phrases or normalized in manager._sleep_phrases
                or manager._matches_abort_phrase(text)):
            return None
        phrase = (self.app.config.get("wake_word_config") or {}).get("phrase", "jarvis")
        payload = split_wake_prefix(text, phrase)
        armed = (pending is not None and pending[0] == manager._session_epoch
                 and pending[1] == phrase and manager._clock() < pending[2])
        if payload is None and not armed:
            return None
        manager._scratch_redo = None
        manager._last_scratch_at = None
        if not passes_switch_anti_hallucination_gate(signals):
            # Explicit commands never fall back to dictation, even on bad audio.
            return DispatchOutcome(kind="hands_free_command_refused", detail={"wake_prefix": True})
        if payload == "":
            timeout = effective_value(self.app.config,
                                     "wake_word_config.audio.wake_command_timeout",
                                     WAKE_COMMAND_TIMEOUT)
            self.pending = (manager._session_epoch, phrase, manager._clock() + timeout)
            self.app.play_sound("start")
            return DispatchOutcome(kind="empty", detail={"wake_prefix": True,
                                                         "wake_command_pending": True})
        outcome = manager.dispatch_command_utterance(text if payload is None else payload)
        # Keep downstream miss interpretation on the stripped command too.
        outcome.detail["command_text"] = text if payload is None else payload
        outcome.detail["wake_prefix"] = True
        return outcome


def install(app, manager):
    from samsara.hands_free_delivery import HandsFreeDelivery
    manager._hands_free_delivery = HandsFreeDelivery(app)
    manager._handsfree_wake_prefix = HandsFreeWakePrefix(app)


def dispatch_wake_prefix(manager, text, signals):
    router = getattr(manager, "_handsfree_wake_prefix", None)
    return None if router is None else router.dispatch(manager, text, signals)


def command_text(outcome, text):
    """Use a stripped payload only for an explicit in-session command."""
    detail = outcome.detail
    return detail.get("command_text", text) if isinstance(detail, dict) else text


def preview_text(outcome, text):
    """Capture preview ownership before a miss resolver replaces the outcome."""
    detail = outcome.detail
    return "" if isinstance(detail, dict) and detail.get("wake_prefix") else text
