"""Unified toggle-command-mode session: a latched mode state machine.

Generalizes the existing per-utterance toggle command mode (dictation.py
_handle_command_mode_utterance, serviced by WakeConsumer) into a session with
LATCHED MODES instead of always executing every utterance as a command.

    SessionMode.COMMAND  -- the hub, and the default. Grammar match/execute,
                            same as today.
    SessionMode.DICTATE  -- utterances are typed into the locked focus
                            target instead of matched against commands.

A third mode (AVA) is Phase 2 and deliberately NOT added here -- see the
SessionMode docstring for how the dispatch below stays additive for it.

This module is pure orchestration: it never touches audio, Whisper, pyautogui,
or Qt directly. All side effects (injecting text, removing characters,
resolving the foreground process, executing a COMMAND-mode phrase, playing
earcons) are passed in as callables, so SessionModeManager is unit-testable
without mocking hardware. dictation.py supplies the concrete callables and
owns the one instance per toggle-command-mode session.

Dispatch order for every silence-bounded utterance (see dispatch_utterance):
  1. Global abort phrase       -- always wins, every mode.
  2. "scratch that"             -- global control word, any mode.
  3. Switch word (prefix-or-whole) -- "command mode" / "dictate mode" /
                                    "dictate <payload>".
  4. Otherwise: the whole utterance belongs to the CURRENT mode.

Steps 2 and 3 both require passing passes_switch_anti_hallucination_gate()
first; on failure they are not treated as a switch/scratch at all and fall
through to step 4 instead (fail CLOSED for switches, not fail-open).
"""
from __future__ import annotations

import logging
import string
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

log = logging.getLogger("Samsara.session_modes")


# ---------------------------------------------------------------------------
# Mode enum
# ---------------------------------------------------------------------------

class SessionMode(Enum):
    """COMMAND is the hub and the default entry mode. DICTATE is a lane you
    switch into and back out of. A future AVA member slots in the same way:
    add the enum value, a whole-utterance switch phrase, a prefix switch
    ("ava "), and a _dispatch_in_mode branch -- the abort/scratch-that/
    switch-word plumbing above does not change."""
    COMMAND = "command"
    DICTATE = "dictate"


TERMINAL_PUNCTUATION = ".!?:;"


# ---------------------------------------------------------------------------
# Switch-word matching: PREFIX-OR-WHOLE, normalized
# ---------------------------------------------------------------------------

_LEADING_FILLERS = ("um", "uh", "uhh", "umm", "like", "so", "well", "okay", "ok")

_WHOLE_UTTERANCE_SWITCHES: dict[str, SessionMode] = {
    "command mode": SessionMode.COMMAND,
    "dictate mode": SessionMode.DICTATE,
    "dictation mode": SessionMode.DICTATE,
    "dictate": SessionMode.DICTATE,
}

# Prefix forms: "dictate <payload>" switches mode AND delivers payload as the
# first chunk of the new mode. Reserve the same shape for "ava " in Phase 2 --
# add a key here, nothing else in this matcher needs to change.
_PREFIX_SWITCHES: dict[str, SessionMode] = {
    "dictate": SessionMode.DICTATE,
}

SCRATCH_THAT_PHRASE = "scratch that"


def normalize_utterance(text: str) -> str:
    """lowercase, strip punctuation, strip leading filler words.

    MATCHING ONLY -- never applied to text that gets injected/dictated.
    """
    t = (text or "").strip().lower()
    t = t.translate(str.maketrans("", "", string.punctuation))
    words = t.split()
    while words and words[0] in _LEADING_FILLERS:
        words.pop(0)
    return " ".join(words)


@dataclass(frozen=True)
class SwitchMatch:
    target_mode: SessionMode
    payload: str = ""      # remainder text, original casing, for prefix matches
    is_prefix: bool = False


def is_scratch_that(raw_text: str) -> bool:
    """Whole-utterance only -- 'let's scratch that idea' must NOT match."""
    return normalize_utterance(raw_text) == SCRATCH_THAT_PHRASE


def match_switch_word(raw_text: str) -> Optional[SwitchMatch]:
    """PREFIX-OR-WHOLE switch matching.

    - Whole-utterance match (normalized) wins outright, e.g. "dictate mode",
      bare "dictate".
    - Prefix form: normalized text starts with "dictate " (a registered
      prefix word) followed by more content -> switches mode and carries the
      remainder as payload, recovered from the ORIGINAL text so casing and
      punctuation of the dictated content survive.
    - A switch word appearing mid-utterance (not utterance-initial, not the
      whole utterance) NEVER matches: "we should dictate mode later" is
      plain text/miss for the current mode, not a switch.
    """
    normalized = normalize_utterance(raw_text)
    if not normalized:
        return None

    if normalized in _WHOLE_UTTERANCE_SWITCHES:
        return SwitchMatch(target_mode=_WHOLE_UTTERANCE_SWITCHES[normalized])

    for prefix_word, mode in _PREFIX_SWITCHES.items():
        if normalized.startswith(prefix_word + " "):
            payload = _strip_leading_token_preserving_case(raw_text, prefix_word)
            if payload.strip():
                return SwitchMatch(target_mode=mode, payload=payload, is_prefix=True)

    return None


def _strip_leading_token_preserving_case(raw_text: str, prefix_word: str) -> str:
    """Remove leading filler tokens and then one prefix_word token from
    raw_text, returning the remainder with the ORIGINAL casing/punctuation
    intact (normalize_utterance() is matching-only, never applied to
    dictated content)."""
    tokens = raw_text.strip().split()
    idx = 0
    while idx < len(tokens) and tokens[idx].strip(string.punctuation).lower() in _LEADING_FILLERS:
        idx += 1
    if idx < len(tokens) and tokens[idx].strip(string.punctuation).lower() == prefix_word:
        idx += 1
    return " ".join(tokens[idx:])


# ---------------------------------------------------------------------------
# Anti-hallucination gate for switch words / scratch-that
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UtteranceSignals:
    """Hallucination-detection signals for ONE utterance, computed by the
    caller via the EXISTING gate functions in dictation.py
    (_buffer_has_contiguous_speech, per-segment compression_ratio from
    Whisper's own segment objects). This module never touches audio/Whisper
    directly -- only these already-computed signals -- so it stays testable
    without mocking VAD/Whisper.

    has_contiguous_speech: True/False from _buffer_has_contiguous_speech, or
        None if that check could not be run for this utterance (VAD
        unavailable, exception, etc).
    compression_ratios: per-segment compression_ratio values (None entries
        mean the field was unavailable on that segment). Empty tuple means
        no segments were available to check.
    """
    has_contiguous_speech: Optional[bool]
    compression_ratios: tuple = ()


# Stricter than dictation.py's general hallucination backstop (3.0, a notch
# above Whisper's own 2.4 reject cutoff, chosen there to avoid touching
# borderline-but-real speech). A wrong switch/scratch is more disruptive
# than a wrong dictated word, so this reuses Whisper's OWN internal
# threshold as the stricter cut rather than inventing a new number.
SWITCH_WORD_MAX_COMPRESSION_RATIO = 2.4


def passes_switch_anti_hallucination_gate(signals: UtteranceSignals) -> bool:
    """Fail CLOSED: any unavailable/ambiguous signal blocks the switch.

    This is the opposite default of _buffer_has_contiguous_speech's own
    fail-OPEN behavior (eating a rare beep during normal dictation is fine;
    firing a phantom mode switch, or eating a real one, is worse here).
    """
    if signals.has_contiguous_speech is not True:
        return False
    if not signals.compression_ratios:
        return False
    for cr in signals.compression_ratios:
        if cr is None or cr > SWITCH_WORD_MAX_COMPRESSION_RATIO:
            return False
    return True


# ---------------------------------------------------------------------------
# DICTATE chunk seam-join heuristic
# ---------------------------------------------------------------------------

def seam_join(previous_chunk_ended_terminal: bool, new_chunk_raw: str) -> str:
    """Case-adjust a new DICTATE chunk for joining onto the previous one.

    Returns the chunk text only (no leading space) -- the caller prepends a
    single space when injecting a non-first chunk, mirroring the existing
    wake-session dictation pattern (_output_dictation: ' ' + text).

    If the previous chunk ended in terminal punctuation (. ! ? : ;), this is
    a fresh sentence -- text is returned unchanged, no case adjustment.

    Otherwise the seam word is lowercased UNLESS it looks like a genuine
    proper noun rather than Whisper's automatic utterance-initial
    capitalization. Heuristic: Whisper always capitalizes the very first
    word of whatever it transcribes, positionally, regardless of content.
    If the chunk's raw text has filler words ("um", "so", ...) BEFORE the
    capitalized token, that capital landed past position 0 of Whisper's own
    output -- not explainable by the automatic sentence-initial rule, so
    it's kept. If the capitalized token IS literally the first word Whisper
    produced (no fillers ahead of it), the capitalization is presumed
    automatic and gets lowercased.
    """
    stripped = (new_chunk_raw or "").strip()
    if not stripped:
        return stripped
    if previous_chunk_ended_terminal:
        return stripped

    tokens = stripped.split()
    idx = 0
    while idx < len(tokens) and tokens[idx].strip(string.punctuation).lower() in _LEADING_FILLERS:
        idx += 1
    if idx >= len(tokens):
        return stripped  # entirely filler words -- nothing to adjust

    seam_word = tokens[idx]
    fillers_were_skipped = idx > 0
    core = seam_word.strip(string.punctuation)
    looks_capitalized = bool(core) and core[0].isupper()

    if looks_capitalized and not fillers_were_skipped:
        tokens[idx] = seam_word[0].lower() + seam_word[1:]

    return " ".join(tokens)


def chunk_ends_terminal(text: str) -> bool:
    """True if text's last non-whitespace character is terminal punctuation."""
    t = (text or "").rstrip()
    return bool(t) and t[-1] in TERMINAL_PUNCTUATION


# ---------------------------------------------------------------------------
# Focus-lock decision function
# ---------------------------------------------------------------------------

def check_focus_lock(target_process: Optional[str], foreground_process: Optional[str]) -> bool:
    """Pure decision function -- both args are already-resolved process
    names (lowercase exe names, same identity comparison the wake registry's
    process-name targeting uses -- see samsara.handlers._get_foreground_exe_lower).

    Fails CLOSED: either side unknown means the lock does NOT pass (safer to
    suppress + revert than to inject blind into an unknown window).
    """
    if not target_process or not foreground_process:
        return False
    return target_process.lower() == foreground_process.lower()


# ---------------------------------------------------------------------------
# Cross-mode unit-of-work stack ("scratch that")
# ---------------------------------------------------------------------------

@dataclass
class StackItem:
    kind: str            # "dictation_chunk" | "command"
    payload: str
    mode: SessionMode
    timestamp: float
    extra: dict = field(default_factory=dict)


class UnitOfWorkStack:
    """Bounded (last 5) LIFO of output events, for global 'scratch that'."""

    MAX_SIZE = 5

    def __init__(self) -> None:
        self._items: "deque[StackItem]" = deque(maxlen=self.MAX_SIZE)

    def push(self, item: StackItem) -> None:
        self._items.append(item)

    def pop(self) -> Optional[StackItem]:
        if not self._items:
            return None
        return self._items.pop()

    def peek(self) -> Optional[StackItem]:
        return self._items[-1] if self._items else None

    def items_newest_first(self):
        return reversed(self._items)

    def __len__(self) -> int:
        return len(self._items)


# ---------------------------------------------------------------------------
# Dispatch result + callable contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandDispatchResult:
    matched: bool
    phrase: Optional[str] = None


@dataclass(frozen=True)
class DispatchOutcome:
    kind: str
    # one of: "empty" | "abort" | "scratch_success" | "scratch_refuse" |
    # "mode_switch" | "command_executed" | "command_miss" |
    # "dictate_injected" | "dictate_suppressed_focus_lock"
    detail: dict = field(default_factory=dict)


ForegroundResolver = Callable[[], Optional[str]]
InjectFn = Callable[[str], None]
RemoveCharsFn = Callable[[int], None]
CommandDispatchFn = Callable[[str], CommandDispatchResult]


# ---------------------------------------------------------------------------
# SessionModeManager
# ---------------------------------------------------------------------------

class SessionModeManager:
    """Owns current mode, the unit-of-work stack, and per-mode config for
    one toggle-command-mode session. Construct once per DictationApp; call
    reset() on every session entry (session end always discards mode state
    -- re-entry is always COMMAND)."""

    def __init__(
        self,
        *,
        abort_phrases: list[str],
        foreground_exe_resolver: ForegroundResolver,
        inject_fn: InjectFn,
        remove_chars_fn: RemoveCharsFn,
        command_dispatch_fn: CommandDispatchFn,
        on_mode_change: Optional[Callable[[SessionMode], None]] = None,
        on_focus_lock_revert: Optional[Callable[[], None]] = None,
        on_scratch_result: Optional[Callable[[bool], None]] = None,
        on_abort: Optional[Callable[[], None]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._abort_phrases = list(abort_phrases)
        self._foreground_exe_resolver = foreground_exe_resolver
        self._inject_fn = inject_fn
        self._remove_chars_fn = remove_chars_fn
        self._command_dispatch_fn = command_dispatch_fn
        self._on_mode_change = on_mode_change
        self._on_focus_lock_revert = on_focus_lock_revert
        self._on_scratch_result = on_scratch_result
        self._on_abort = on_abort
        self._clock = clock

        self.mode: SessionMode = SessionMode.COMMAND
        self._stack = UnitOfWorkStack()
        self._dictate_target_process: Optional[str] = None
        self._last_dictate_ended_terminal: Optional[bool] = None

    # -- session lifecycle -----------------------------------------------

    def reset(self) -> None:
        """Discard all mode state. Called on session entry AND session end
        (30s inactivity_timeout_s) -- re-entry is always COMMAND."""
        self.mode = SessionMode.COMMAND
        self._stack = UnitOfWorkStack()
        self._dictate_target_process = None
        self._last_dictate_ended_terminal = None

    @property
    def stack_depth(self) -> int:
        return len(self._stack)

    # -- dispatch -----------------------------------------------------------

    def dispatch_utterance(self, raw_text: str, signals: UtteranceSignals) -> DispatchOutcome:
        text = (raw_text or "").strip()
        if not text:
            return DispatchOutcome(kind="empty")

        # 1. Global abort phrase -- always wins, every mode.
        if self._matches_abort_phrase(text):
            if self._on_abort:
                self._on_abort()
            return DispatchOutcome(kind="abort")

        scratch = is_scratch_that(text)
        switch = None if scratch else match_switch_word(text)

        if scratch or switch is not None:
            if passes_switch_anti_hallucination_gate(signals):
                if scratch:
                    ok = self._do_scratch_that()
                    if self._on_scratch_result:
                        self._on_scratch_result(ok)
                    return DispatchOutcome(kind="scratch_success" if ok else "scratch_refuse")
                return self._do_switch(switch)
            # Gate failed: fail CLOSED for the switch/scratch interpretation
            # only -- fall through and let the current mode handle the text
            # normally (existing gating for DICTATE text, if any, applies
            # downstream of this module).
            log.info("[SESSION] switch/scratch anti-hallucination gate failed for %r; "
                      "treating as ordinary %s text", text, self.mode.value)

        return self._dispatch_in_mode(text)

    def _matches_abort_phrase(self, text: str) -> bool:
        text_lower = text.lower()
        return any(phrase.lower() in text_lower for phrase in self._abort_phrases)

    def _do_switch(self, switch: SwitchMatch) -> DispatchOutcome:
        self._switch_mode(switch.target_mode)
        if switch.is_prefix and switch.payload.strip():
            return self._dispatch_in_mode(switch.payload)
        return DispatchOutcome(kind="mode_switch", detail={"mode": switch.target_mode})

    def _switch_mode(self, new_mode: SessionMode) -> None:
        if new_mode is SessionMode.DICTATE and self.mode is not SessionMode.DICTATE:
            # Lock onto whatever's focused right now -- injections later in
            # this DICTATE lane must stay within this process.
            self._dictate_target_process = self._foreground_exe_resolver()
            self._last_dictate_ended_terminal = None
        self.mode = new_mode
        if self._on_mode_change:
            self._on_mode_change(new_mode)

    def force_mode(self, new_mode: SessionMode) -> None:
        """Non-utterance-driven mode change (e.g. focus-lock auto-revert)."""
        self._switch_mode(new_mode)

    def _dispatch_in_mode(self, text: str) -> DispatchOutcome:
        if self.mode is SessionMode.COMMAND:
            return self._dispatch_command(text)
        if self.mode is SessionMode.DICTATE:
            return self._dispatch_dictate(text)
        raise AssertionError(f"unhandled SessionMode {self.mode!r}")  # pragma: no cover

    def _dispatch_command(self, text: str) -> DispatchOutcome:
        result = self._command_dispatch_fn(text)
        if result.matched:
            self._stack.push(StackItem(
                kind="command", payload=result.phrase or text,
                mode=SessionMode.COMMAND, timestamp=self._clock(),
            ))
            return DispatchOutcome(kind="command_executed", detail={"phrase": result.phrase})
        return DispatchOutcome(kind="command_miss")

    def _dispatch_dictate(self, chunk_raw: str) -> DispatchOutcome:
        foreground = self._foreground_exe_resolver()
        if not check_focus_lock(self._dictate_target_process, foreground):
            self._stack.push(StackItem(
                kind="dictation_chunk", payload=chunk_raw.strip(),
                mode=SessionMode.DICTATE, timestamp=self._clock(),
                extra={"suppressed": True, "target_process": self._dictate_target_process,
                       "foreground": foreground},
            ))
            self.force_mode(SessionMode.COMMAND)
            if self._on_focus_lock_revert:
                self._on_focus_lock_revert()
            return DispatchOutcome(kind="dictate_suppressed_focus_lock")

        if self._last_dictate_ended_terminal is None:
            adjusted = chunk_raw.strip()
            to_inject = adjusted
        else:
            adjusted = seam_join(self._last_dictate_ended_terminal, chunk_raw)
            to_inject = " " + adjusted

        self._inject_fn(to_inject)
        self._last_dictate_ended_terminal = chunk_ends_terminal(adjusted)
        self._stack.push(StackItem(
            kind="dictation_chunk", payload=to_inject, mode=SessionMode.DICTATE,
            timestamp=self._clock(), extra={"target_process": self._dictate_target_process},
        ))
        return DispatchOutcome(kind="dictate_injected", detail={"text": to_inject})

    # -- scratch that / retype that ------------------------------------------

    def _do_scratch_that(self) -> bool:
        item = self._stack.pop()
        if item is None:
            return False
        if item.kind != "dictation_chunk":
            return False  # command undo out of scope; consumed, refuse-style earcon
        foreground = self._foreground_exe_resolver()
        target = item.extra.get("target_process")
        if not check_focus_lock(target, foreground):
            return False
        self._remove_chars_fn(len(item.payload))
        return True

    def retype_last_suppressed(self) -> bool:
        """COMMAND-mode 'retype that': re-attempt the most recent DICTATE
        chunk that focus-lock suppressed, with a fresh focus-lock check."""
        for item in self._stack.items_newest_first():
            if item.kind == "dictation_chunk" and item.extra.get("suppressed"):
                foreground = self._foreground_exe_resolver()
                target = item.extra.get("target_process")
                if not check_focus_lock(target, foreground):
                    return False
                self._inject_fn(item.payload)
                item.extra["suppressed"] = False
                return True
        return False
