"""Unified toggle-command-mode session: a latched mode state machine.

Generalizes the existing per-utterance toggle command mode (dictation.py
_handle_command_mode_utterance, serviced by WakeConsumer) into a session with
LATCHED MODES instead of always executing every utterance as a command.

    SessionMode.COMMAND  -- the hub, and the default. Grammar match/execute,
                            same as today.
    SessionMode.DICTATE  -- utterances are typed into the locked focus
                            target instead of matched against commands.
    SessionMode.AVA      -- Phase 2. Every utterance goes to the local agent
                            as natural language (see _dispatch_ava). The
                            agent NEVER auto-sends the DICTATE stage buffer;
                            an explicit reference ("submit that") is the
                            only way it gets attached to a request, and
                            agent exchanges are never pushed onto the
                            scratch-that stack -- an agent turn can't be
                            unsent.

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
    """COMMAND is the hub and the default entry mode. DICTATE and AVA are
    lanes you switch into and back out of; any-to-any transitions work
    identically regardless of current mode, since match_switch_word() is a
    pure function of the utterance text and _switch_mode() unconditionally
    sets the new mode -- the abort/scratch-that/switch-word plumbing does
    not change per mode."""
    COMMAND = "command"
    DICTATE = "dictate"
    AVA = "ava"


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
    "ava": SessionMode.AVA,
    "ava mode": SessionMode.AVA,
}

# Prefix forms: "dictate <payload>" / "ava <payload>" switch mode AND deliver
# the payload as the first chunk/utterance of the new mode.
_PREFIX_SWITCHES: dict[str, SessionMode] = {
    "dictate": SessionMode.DICTATE,
    "ava": SessionMode.AVA,
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
# Stage-buffer reference detection (AVA mode, Phase 2)
# ---------------------------------------------------------------------------

# Unambiguous noun phrases -- position in the utterance doesn't matter.
_STAGE_REFERENCE_PHRASES = ("the text", "what i dictated", "the dictation")


def detect_stage_reference(text: str) -> bool:
    """True only for an EXPLICIT reference to the DICTATE stage buffer.

    Deterministic token/phrase check, no NLU, by design (see Phase 2 spec).
    Two rules, either one is sufficient:

      1. Any of _STAGE_REFERENCE_PHRASES appears anywhere in the normalized
         text ("send the text", "check the dictation") -- unambiguous noun
         phrases, position doesn't matter.
      2. "that" or "this" appears as a token but is NOT the first word of
         the (filler-stripped) utterance -- i.e. used as the object of a
         verb ("submit that", "read this") rather than a sentence-initial
         demonstrative/subject ("that was fun", "this is great").

    Accepted ambiguity: rule 2 is a POSITION heuristic, not semantic
    understanding, so it also matches non-reference object-shaped uses like
    "was that clear" or "did you like that". This is a deliberate,
    documented trade-off: a false positive here only ever means extra
    (possibly irrelevant) context gets attached to an agent request -- it
    never causes an unwanted ACTION, because the caller never sends the
    buffer without this function returning True, and the agent never
    auto-acts on staged text regardless. Narrowing the heuristic further to
    kill those false positives would risk the opposite failure -- silently
    missing real references like "check that" -- which defeats the point of
    the feature. Given the choice, over-attaching harmless context beats
    dropping an intended one.
    """
    normalized = normalize_utterance(text)
    if not normalized:
        return False
    for phrase in _STAGE_REFERENCE_PHRASES:
        if phrase in normalized:
            return True
    words = normalized.split()
    for i, word in enumerate(words):
        if word in ("that", "this") and i > 0:
            return True
    return False


# ---------------------------------------------------------------------------
# Substance gate (AVA mode, Phase 2.5): reject micro-utterances before they
# become an agent API call + spoken reply
# ---------------------------------------------------------------------------

# Deliberately a SEPARATE set from normalize_utterance's _LEADING_FILLERS --
# that list only strips LEADING filler words for switch-word matching; this
# gate needs to catch filler tokens ANYWHERE in the utterance ("um okay" is
# two filler tokens, not one leading filler followed by real content).
_SUBSTANCE_FILLER_TOKENS = frozenset({
    "uh", "um", "hmm", "mhm", "ah", "oh", "eh", "huh", "hm",
    "you", "the", "a", "yeah", "okay", "ok",
})

# Short but complete one-word turns that must never be rejected just for
# being short -- overrides every other rule below.
_SUBSTANTIVE_ONE_WORD_ALLOWLIST = frozenset({
    "yes", "no", "stop", "continue", "why", "how",
})

_SUBSTANCE_MIN_LENGTH = 4  # characters, raw (pre-normalization) length


def _substance_tokens(text: str) -> list:
    """lowercase + strip punctuation only -- no filler-stripping. That's
    normalize_utterance's job for switch-word matching, a different concern
    from this gate's own _SUBSTANCE_FILLER_TOKENS set below."""
    t = (text or "").strip().lower()
    t = t.translate(str.maketrans("", "", string.punctuation))
    return t.split()


def is_substantive_utterance(text: str) -> bool:
    """AVA-lane-only gate: reject micro-utterances -- coughs, "uh", stray
    syllables -- that survive the near-silence/hallucination gates and
    transcribe as tiny valid strings, before each one costs an agent API
    request and a spoken reply. Deterministic, no NLU.

    Rejects when ANY of (unless the single-word allowlist exception below
    applies first):
      - fewer than 2 words after normalization (lowercase, strip punctuation)
      - total length < 4 characters
      - every token is in _SUBSTANCE_FILLER_TOKENS (whole-utterance filler
        like "um okay" is rejected even though it's two words)

    The one-word allowlist ("yes", "no", "stop", "continue", "why", "how")
    is checked FIRST and overrides the length/word-count rules -- "no" (2
    characters) and "why" (3 characters) are complete, meaningful turns
    despite being short; the length rule exists to catch tiny NON-turns
    (stray syllables, hallucination fragments), not these.
    """
    tokens = _substance_tokens(text)

    if len(tokens) == 1 and tokens[0] in _SUBSTANTIVE_ONE_WORD_ALLOWLIST:
        return True

    if len(tokens) < 2:
        return False

    stripped = (text or "").strip()
    if len(stripped) < _SUBSTANCE_MIN_LENGTH:
        return False

    if all(tok in _SUBSTANCE_FILLER_TOKENS for tok in tokens):
        return False

    return True


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
    # "dictate_injected" | "dictate_suppressed_focus_lock" | "ava_dispatched" |
    # "ava_rejected_not_substantive"
    detail: dict = field(default_factory=dict)


ForegroundResolver = Callable[[], Optional[str]]
InjectFn = Callable[[str], None]
RemoveCharsFn = Callable[[int], None]
CommandDispatchFn = Callable[[str], CommandDispatchResult]
# (utterance_text, stage_buffer_context_or_None) -> None. Fire-and-forget from
# this module's perspective -- the manager never blocks on or observes the
# agent's response. Threading/queueing/depth-limiting is the wired callable's
# job (dictation.py), same division of labor as inject_fn/command_dispatch_fn.
AgentDispatchFn = Callable[[str, Optional[str]], None]


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
        agent_dispatch_fn: AgentDispatchFn,
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
        self._agent_dispatch_fn = agent_dispatch_fn
        self._on_mode_change = on_mode_change
        self._on_focus_lock_revert = on_focus_lock_revert
        self._on_scratch_result = on_scratch_result
        self._on_abort = on_abort
        self._clock = clock

        self.mode: SessionMode = SessionMode.COMMAND
        self._stack = UnitOfWorkStack()
        self._dictate_target_process: Optional[str] = None
        self._last_dictate_ended_terminal: Optional[bool] = None
        self._stage_buffer: str = ""

    # -- session lifecycle -----------------------------------------------

    def reset(self) -> None:
        """Discard all mode state. Called on session entry AND session end
        (30s inactivity_timeout_s) -- re-entry is always COMMAND."""
        self.mode = SessionMode.COMMAND
        self._stack = UnitOfWorkStack()
        self._dictate_target_process = None
        self._last_dictate_ended_terminal = None
        self._stage_buffer = ""

    @property
    def stack_depth(self) -> int:
        return len(self._stack)

    @property
    def stage_buffer(self) -> str:
        """The accumulated text from the most recent DICTATE excursion,
        available for explicit reference from AVA (or any mode) until it is
        sent or a fresh DICTATE entry / session reset clears it."""
        return self._stage_buffer

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
            # this DICTATE lane must stay within this process. A fresh
            # DICTATE entry also starts a fresh stage buffer -- "what I
            # dictated" from AVA should mean THIS excursion, not some stale
            # one from earlier in the session.
            self._dictate_target_process = self._foreground_exe_resolver()
            self._last_dictate_ended_terminal = None
            self._stage_buffer = ""
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
        if self.mode is SessionMode.AVA:
            return self._dispatch_ava(text)
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
        # Mirrors exactly what got injected (to_inject already carries the
        # seam-join leading space for non-first chunks) -- this IS "what I
        # dictated" for AVA's stage-reference contract. Suppressed chunks
        # (focus-lock reverted, above) never reach here, so nothing that
        # wasn't actually typed ends up in the buffer.
        self._stage_buffer += to_inject
        self._stack.push(StackItem(
            kind="dictation_chunk", payload=to_inject, mode=SessionMode.DICTATE,
            timestamp=self._clock(), extra={"target_process": self._dictate_target_process},
        ))
        return DispatchOutcome(kind="dictate_injected", detail={"text": to_inject})

    def _dispatch_ava(self, text: str) -> DispatchOutcome:
        """Every AVA-mode utterance goes to the agent as natural language --
        except micro-utterances that fail is_substantive_utterance (coughs,
        "uh", stray syllables): those never reach _agent_dispatch_fn at all,
        so they never enter the caller's request queue and never cost an
        API call. This substance gate runs AFTER abort/scratch-that/switch-
        word handling (dispatch_utterance's job, above _dispatch_in_mode) --
        it only ever sees text already committed to being ordinary AVA
        content, so it can never eat a mode switch.

        The DICTATE stage buffer is attached ONLY when detect_stage_reference
        finds an explicit reference AND the buffer is non-empty -- never by
        default. Attaching (an "explicit send") clears the buffer here, at
        dispatch time: this module is synchronous and fire-and-forget
        towards the agent (see AgentDispatchFn) and never observes whether
        the agent's response succeeds, so "the moment Samsara hands off a
        buffer-attached request" is the only deterministic point at which
        "sent" can be defined. Agent turns are never pushed onto the
        scratch-that stack -- an agent exchange can't be unsent."""
        if not is_substantive_utterance(text):
            return DispatchOutcome(kind="ava_rejected_not_substantive", detail={"text": text})

        context = None
        if detect_stage_reference(text) and self._stage_buffer:
            context = self._stage_buffer
            self._stage_buffer = ""
        self._agent_dispatch_fn(text, context)
        return DispatchOutcome(
            kind="ava_dispatched",
            detail={"text": text, "has_context": context is not None},
        )

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
