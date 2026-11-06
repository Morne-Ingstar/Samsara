"""Whole-utterance voice names for visible Samsara controls (1150).

The Qt adapter clicks the existing control; this module never starts a UI,
loads configuration, or guesses which window the user can see.
"""
from __future__ import annotations

import re
import sys


def normalize_label(text: str) -> str:
    """Keep words/numbers, ignoring display punctuation and mnemonic marks."""
    text = str(text).casefold().replace("&", "").replace("'", "").replace("’", "")
    return " ".join(re.findall(r"[^\W_]+", text))


# Bare fragments are ordinary dictation even while a window is visible.
# Every such control still has the explicit whole phrase "click <label>".
_ACTION_STARTS = frozenset({
    "add", "always", "check", "clean", "clear", "close", "copy", "correct",
    "dismiss", "edit", "enable", "hide", "insert", "install", "keep", "open", "paste",
    "preview", "recalibrate", "release", "remove", "report", "reset",
    "restart", "resume", "review", "send", "show", "stop", "test", "turn",
    "view",
})
_NAMED_ACTIONS = frozenset({
    "quick reference", "command reference", "next words", "try again",
    "re enable mouse hotkey", "i understand turn ava on",
})
_VARIANTS = {
    "clear draft": ("clear the draft", "clear my draft"),
    "insert text": ("insert the text", "insert my draft"),
    "edit as new draft": ("edit as a new draft",),
    "re enable mouse hotkey": ("reenable mouse hotkey",),
}

# Shared with Quick Reference; these controls have no painted button label.
CAPTION_DISMISS_LABEL = "Dismiss Ava captions"
_SOURCE_NUMBERS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def source_phrases(number: int) -> tuple[str, ...]:
    phrases = (f"open source {number}",)
    if 1 <= number <= len(_SOURCE_NUMBERS):
        phrases += (f"open source {_SOURCE_NUMBERS[number - 1]}",)
    return phrases


def collision_reason(label: str) -> str:
    words = normalize_label(label).split()
    if not words:
        return "No spoken label; use the control's accessible name."
    if len(words) == 1:
        return "A single word can be a complete dictated utterance."
    if words[0] not in _ACTION_STARTS and " ".join(words) not in _NAMED_ACTIONS:
        return "A value or ordinary phrase can be a complete dictated utterance."
    return ""


def phrases_for_label(label: str) -> tuple[str, ...]:
    name = normalize_label(label)
    if not name:
        return ()
    explicit = ("click " + name,)
    if collision_reason(label):
        return explicit
    return (name, *_VARIANTS.get(name, ()), *explicit)


def match_label(text: str, label: str) -> bool:
    return normalize_label(text) in phrases_for_label(label)


_bridge = None
_install_pending = False


def prepare_visible_actions() -> None:
    """Attach only to an already-owned Qt runtime; no imports/startup otherwise."""
    global _install_pending
    runtime = sys.modules.get("samsara.ui.qt_runtime")
    if _bridge is not None or _install_pending or runtime is None:
        return
    if not runtime.is_alive():
        return
    _install_pending = True

    def install():
        global _bridge, _install_pending
        try:
            from samsara.ui.button_voice_qt import VisibleActions
            _bridge = VisibleActions(runtime._app, runtime.post)
        finally:
            _install_pending = False

    runtime.post(install)


def dispatch_visible_action(text: str) -> bool:
    prepare_visible_actions()
    return bool(_bridge is not None and _bridge.dispatch(text))


def consume_before_pending(app, text, signals, *, self_hear, capture_path):
    """Let a visible control precede Ava's question in the toggle speech lane.

    The caller has already screened language, quality and context echoes.
    Only the control's existing handler may answer/dismiss the question; an
    unrelated click leaves its identity, deadline and capture ownership alone.
    """
    from samsara import ava_pending
    from samsara.session_modes import (
        match_literal_payload, passes_switch_anti_hallucination_gate,
    )

    with ava_pending.ownership_lock(app):
        question = getattr(app, "_ava_question", None)
        if (question is not None
                and capture_path == "toggle_command"
                and not self_hear
                and not getattr(getattr(app, "audio_coordinator", None), "is_speaking", False)
                and match_literal_payload(text) is None
                and passes_switch_anti_hallucination_gate(signals)
                and question.opened
                and ava_pending._live(app, question)
                and ava_pending.time.monotonic() < question.deadline
                and dispatch_visible_action("click dismiss" if normalize_label(text) == "dismiss" else text)):
            return True
        # Keep answer classification, source checks and retirement in their
        # original owner. No answer, deadline or confirmation is manufactured.
        return ava_pending.consume(app, text, capture_path=capture_path)
