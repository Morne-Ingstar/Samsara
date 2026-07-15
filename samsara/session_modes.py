"""Unified toggle-command-mode session: a latched mode state machine.

Generalizes the existing per-utterance toggle command mode (dictation.py
_handle_command_mode_utterance, serviced by WakeConsumer) into a session with
LATCHED MODES instead of always executing every utterance as a command.

    SessionMode.COMMAND  -- legacy command-only lane, retained for compatibility.
    SessionMode.DICTATE  -- the normal latched HANDS FREE lane: ordinary speech
                            stages across natural pauses; curated exact commands
                            execute without mode switching; sole-word "end"
                            pastes the complete thought and stays hands-free.
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
import re
import string
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Union

log = logging.getLogger("Samsara.session_modes")


# ---------------------------------------------------------------------------
# Mode enum
# ---------------------------------------------------------------------------

class SessionMode(Enum):
    """DICTATE is the combined hands-free entry lane for latched toggle
    sessions; COMMAND remains the legacy command-only lane. Any-to-any transitions work
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
DICTATE_COMMIT_PHRASE = "end"
GLOBAL_SESSION_EXIT_PHRASES = (
    "stop listening",
    "exit hands free",
    "exit command mode",
)


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


def is_dictate_commit(raw_text: str) -> bool:
    """Whole-utterance-only manual commit for buffered DICTATE mode."""
    return normalize_utterance(raw_text) == DICTATE_COMMIT_PHRASE


def match_literal_payload(raw_text: str) -> Optional[str]:
    """Return payload for ``literal <reserved command>`` or None.

    This is the explicit escape hatch for dictating a whole utterance that is
    otherwise reserved by the hands-free command layer. Original payload case
    and internal spacing are preserved.
    """
    normalized = normalize_utterance(raw_text)
    if not normalized.startswith("literal "):
        return None
    payload = _strip_leading_token_preserving_case(raw_text, "literal")
    return payload if payload.strip() else None


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
    AND original internal whitespace/tabs intact (normalize_utterance() is
    matching-only, never applied to dictated content -- and neither is
    plain str.split()/" ".join(), which would collapse whitespace runs
    inside the payload and violate the preserve-formatting contract).
    Achieved by SLICING the original string at the payload's start offset
    rather than rejoining tokens."""
    text = raw_text.strip()
    tokens = list(re.finditer(r"\S+", text))
    idx = 0
    while idx < len(tokens) and tokens[idx].group().strip(string.punctuation).lower() in _LEADING_FILLERS:
        idx += 1
    if idx < len(tokens) and tokens[idx].group().strip(string.punctuation).lower() == prefix_word:
        idx += 1
    if idx >= len(tokens):
        return ""
    return text[tokens[idx].start():]


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
    "you", "the", "a", "yeah", "ok",
})

# Short but complete one-word turns that must never be rejected just for
# being short -- overrides every other rule below. Includes natural
# conversation-turn words for AVA's turn-taking (assent/ack/greeting/hedge),
# not just command-shaped words.
_SUBSTANTIVE_ONE_WORD_ALLOWLIST = frozenset({
    "yes", "no", "stop", "continue", "why", "how",
    "sure", "wait", "thanks", "maybe", "hello", "hi",
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
        like "um uh" is rejected even though it's two words)

    "okay" is deliberately NOT in _SUBSTANCE_FILLER_TOKENS -- an assent like
    "okay" or "yeah okay" is a legitimate AVA turn (acknowledging the
    agent's prior reply), not a stray syllable, so it must survive the
    all-filler rule above even though "yeah" alone is still filler.

    The one-word allowlist ("yes", "no", "stop", "continue", "why", "how",
    "sure", "wait", "thanks", "maybe", "hello", "hi") is checked FIRST and
    overrides the length/word-count rules -- "no" (2 characters) and "why"
    (3 characters) are complete, meaningful turns despite being short; the
    length rule exists to catch tiny NON-turns (stray syllables,
    hallucination fragments), not these.
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
    # True only when Whisper itself accepted every returned segment against
    # its normal log-probability and no-speech thresholds. This is separate
    # from VAD because a short, genuine control word may not form the general
    # gate's required contiguous run.
    transcript_confident: Optional[bool] = None


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


def passes_dictate_commit_gate(signals: UtteranceSignals, *, has_pending_text: bool) -> bool:
    """Accept a genuine sole ``end`` without weakening other control words.

    The normal switch gate remains preferred. For an already-buffered thought
    only, its short-word false-negative may use Whisper's accepted segment as
    corroboration, provided VAD actually ran and compression remains sane.
    """
    if passes_switch_anti_hallucination_gate(signals):
        return True
    if not has_pending_text or signals.has_contiguous_speech is not False:
        return False
    if signals.transcript_confident is not True or not signals.compression_ratios:
        return False
    return all(
        ratio is not None and ratio <= SWITCH_WORD_MAX_COMPRESSION_RATIO
        for ratio in signals.compression_ratios
    )


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


class PendingTextPolicy(Enum):
    """How a hands-free command interacts with staged dictation."""

    PRESERVE = "preserve"  # scrolling/overlays: leave pending text untouched
    COMMIT = "commit"      # focus/navigation: paste pending text before acting


@dataclass(frozen=True)
class HandsFreeCommandMatch:
    """A side-effect-free command probe result for the combined lane."""

    dispatch_text: str
    phrase: str
    pending_policy: PendingTextPolicy = PendingTextPolicy.PRESERVE


@dataclass(frozen=True)
class DispatchOutcome:
    kind: str
    # one of: "empty" | "abort" | "scratch_success" | "scratch_refuse" |
    # "mode_switch" | "prefix_switch_failed" | "command_executed" |
    # "command_miss" | "dictate_injected" | "dictate_suppressed_focus_lock" |
    # "dictate_staged" | "dictate_committed" |
    # "dictate_commit_refused" | "dictate_commit_blocked_focus_lock" |
    # "dictate_commit_failed" | "hands_free_command_executed" |
    # "hands_free_command_refused" | "hands_free_command_blocked" |
    # "hands_free_command_failed" |
    # "ava_dispatched" | "ava_rejected_not_substantive"
    detail: dict = field(default_factory=dict)


ForegroundResolver = Callable[[], Optional[str]]
# Raw foreground-window handle (HWND), finer-grained than ForegroundResolver's
# exe name -- two windows of the SAME exe are different windows. Used by the
# scratch-that focus guard; None when unavailable (fails closed, see
# _do_scratch_that).
ForegroundHwndResolver = Callable[[], Optional[int]]
# False means delivery was definitely refused/failed. None remains accepted
# for backwards-compatible callables that predate delivery status reporting.
# A returned str means success AND is the actual delivered/formatted text --
# buffered-commit callers (_commit_dictate_buffer) use it, when given, as the
# undo-stack/stage_buffer record of what was really typed, since the input
# they pass in is the pre-formatting accumulated buffer, not what a
# formatting-capable injector may have pasted.
# Buffered commits pass a second callable that must be checked immediately
# before the injector emits its paste keystroke. Legacy/unbuffered paths still
# call the injector with text only.
InjectFn = Callable[..., Union[bool, str, None]]
RemoveCharsFn = Callable[[int], None]
CommandDispatchFn = Callable[[str], CommandDispatchResult]
HandsFreeCommandProbeFn = Callable[[str], Optional[HandsFreeCommandMatch]]
# (utterance_text, stage_buffer_context_or_None) -> None. Fire-and-forget from
# this module's perspective -- the manager never blocks on or observes the
# agent's response. Threading/queueing/depth-limiting is the wired callable's
# job (dictation.py), same division of labor as inject_fn/command_dispatch_fn.
AgentDispatchFn = Callable[[str, Optional[str]], None]
# Pure text transform applied to DICTATE-lane chunks immediately before
# injection (see _dispatch_dictate) -- dictation.py wires this to its
# formatting-tokens gate (config-aware; this module stays config-free).
# Defaults to identity when not supplied.
FormatDictateFn = Callable[[str], str]


# ---------------------------------------------------------------------------
# SessionModeManager
# ---------------------------------------------------------------------------

class SessionModeManager:
    """Owns current mode, the unit-of-work stack, and per-mode config for
    one toggle voice-control session. Construct once per DictationApp; call
    reset() on every session entry/end. The caller chooses the entry lane;
    latched toggle chooses DICTATE/HANDS FREE."""

    def __init__(
        self,
        *,
        abort_phrases: list[str],
        foreground_exe_resolver: ForegroundResolver,
        inject_fn: InjectFn,
        remove_chars_fn: RemoveCharsFn,
        command_dispatch_fn: CommandDispatchFn,
        agent_dispatch_fn: AgentDispatchFn,
        foreground_hwnd_resolver: Optional[ForegroundHwndResolver] = None,
        format_dictate_fn: Optional[FormatDictateFn] = None,
        on_mode_change: Optional[Callable[[SessionMode], None]] = None,
        on_focus_lock_revert: Optional[Callable[[], None]] = None,
        on_scratch_result: Optional[Callable[[bool], None]] = None,
        on_abort: Optional[Callable[[], None]] = None,
        on_switch_dispatch_error: Optional[Callable[[Exception], None]] = None,
        buffer_dictate_until_commit: bool = False,
        hands_free_command_probe_fn: Optional[HandsFreeCommandProbeFn] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._abort_phrases = list(abort_phrases)
        # Word-boundary, case-insensitive match per phrase -- a substring
        # check here (phrase.lower() in text_lower) lets "cancel" false-fire
        # on "cancelation" (typo-real-word) or any other word that merely
        # CONTAINS an abort phrase. Precompiled once since this runs on
        # every single utterance, not just switch/scratch candidates.
        self._abort_patterns = [
            re.compile(r"\b" + re.escape(p.strip()) + r"\b", re.IGNORECASE)
            for p in self._abort_phrases if p.strip()
        ]
        self._foreground_exe_resolver = foreground_exe_resolver
        self._foreground_hwnd_resolver = foreground_hwnd_resolver or (lambda: None)
        self._inject_fn = inject_fn
        self._format_dictate_fn = format_dictate_fn or (lambda t: t)
        self._remove_chars_fn = remove_chars_fn
        self._command_dispatch_fn = command_dispatch_fn
        self._agent_dispatch_fn = agent_dispatch_fn
        self._on_mode_change = on_mode_change
        self._on_focus_lock_revert = on_focus_lock_revert
        self._on_scratch_result = on_scratch_result
        self._on_abort = on_abort
        self._on_switch_dispatch_error = on_switch_dispatch_error
        self._buffer_dictate_until_commit = buffer_dictate_until_commit
        self._hands_free_command_probe_fn = hands_free_command_probe_fn
        self._clock = clock

        self.mode: SessionMode = SessionMode.COMMAND
        self._stack = UnitOfWorkStack()
        self._dictate_target_process: Optional[str] = None
        self._dictate_target_hwnd: Optional[int] = None
        self._last_dictate_ended_terminal: Optional[bool] = None
        self._stage_buffer: str = ""
        self._dictate_pending_buffer: str = ""

    # -- session lifecycle -----------------------------------------------

    def reset(self, initial_mode: SessionMode = SessionMode.COMMAND) -> None:
        """Discard all mode state and choose the new session's initial lane.

        The default remains COMMAND for legacy/direct callers. The latched
        toggle workflow explicitly starts in DICTATE, which is now its combined
        hands-free command+dictation lane.
        """
        self.mode = initial_mode
        self._stack = UnitOfWorkStack()
        self._dictate_target_process = None
        self._dictate_target_hwnd = None
        self._last_dictate_ended_terminal = None
        self._stage_buffer = ""
        self._dictate_pending_buffer = ""

    @property
    def stack_depth(self) -> int:
        return len(self._stack)

    @property
    def stage_buffer(self) -> str:
        """The accumulated text from the most recent DICTATE excursion,
        available for explicit reference from AVA (or any mode) until it is
        sent or a fresh DICTATE entry / session reset clears it."""
        return self._stage_buffer

    @property
    def dictate_pending_buffer(self) -> str:
        """Text transcribed in manual-commit DICTATE mode but not pasted yet."""
        return self._dictate_pending_buffer

    @property
    def buffer_dictate_until_commit(self) -> bool:
        return self._buffer_dictate_until_commit

    # -- dispatch -----------------------------------------------------------

    def dispatch_utterance(self, raw_text: str, signals: UtteranceSignals) -> DispatchOutcome:
        text = (raw_text or "").strip()
        if not text:
            return DispatchOutcome(kind="empty")

        # 1. Global abort phrase -- always wins, every mode, and deliberately
        # checked BEFORE passes_switch_anti_hallucination_gate() below and
        # never subject to it: gating the abort path would mean degraded
        # audio (the exact condition the gate exists to distrust) could
        # leave a user stuck unable to escape a latched session -- deaf but
        # latched, the design's cardinal sin. Ordinary switches/scratch-that
        # still go through the gate, just not this.
        if self._matches_abort_phrase(text):
            if self._on_abort:
                self._on_abort()
            return DispatchOutcome(kind="abort")

        scratch = is_scratch_that(text)
        commit = (
            self._buffer_dictate_until_commit
            and self.mode is SessionMode.DICTATE
            and is_dictate_commit(text)
        )
        switch = None if (scratch or commit) else match_switch_word(text)

        if scratch or commit or switch is not None:
            control_gate_passed = (
                passes_dictate_commit_gate(
                    signals,
                    has_pending_text=bool(self._dictate_pending_buffer),
                )
                if commit
                else passes_switch_anti_hallucination_gate(signals)
            )
            if control_gate_passed:
                if scratch:
                    ok = self._do_scratch_that()
                    if self._on_scratch_result:
                        self._on_scratch_result(ok)
                    return DispatchOutcome(kind="scratch_success" if ok else "scratch_refuse")
                if commit:
                    return self._commit_dictate_buffer(target_mode=None)
                return self._do_switch(switch)
            # A sole-word DICTATE commit that fails the gate must never become
            # dictated content. Retain the pending thought and let the user
            # retry "end" after the refusal earcon.
            if commit:
                return DispatchOutcome(kind="dictate_commit_refused", detail={
                    "pending_chars": len(self._dictate_pending_buffer),
                })
            # Gate failed: fail CLOSED for the control-word interpretation
            # only -- fall through and let the current mode handle the text.
            log.info("[SESSION] control-word anti-hallucination gate failed for %r; "
                      "treating as ordinary %s text", text, self.mode.value)

        # Combined hands-free lane: exact reserved commands coexist with
        # buffered dictation. The probe is side-effect-free, allowing us to
        # commit text transactionally BEFORE commands that move focus or submit.
        if (self._buffer_dictate_until_commit
                and self.mode is SessionMode.DICTATE):
            literal_payload = match_literal_payload(text)
            if literal_payload is not None:
                if not passes_switch_anti_hallucination_gate(signals):
                    return DispatchOutcome(kind="hands_free_command_refused", detail={
                        "phrase": "literal", "pending_chars": len(self._dictate_pending_buffer),
                    })
                return self._dispatch_dictate(literal_payload)

            if self._hands_free_command_probe_fn is not None:
                hands_free_match = self._hands_free_command_probe_fn(text)
                if hands_free_match is not None:
                    if not passes_switch_anti_hallucination_gate(signals):
                        return DispatchOutcome(kind="hands_free_command_refused", detail={
                            "phrase": hands_free_match.phrase,
                            "pending_chars": len(self._dictate_pending_buffer),
                        })
                    return self._dispatch_hands_free_command(hands_free_match)

        return self._dispatch_in_mode(text)

    def _matches_abort_phrase(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in self._abort_patterns)

    def commit_pending_dictation(self) -> DispatchOutcome:
        """Commit buffered DICTATE immediately for a trusted local trigger.

        Keyboard/UI callers do not need speech hallucination gating, but they
        must reuse the same transactional focus/paste path as spoken ``end``.
        """
        if not self._buffer_dictate_until_commit or self.mode is not SessionMode.DICTATE:
            return DispatchOutcome(kind="dictate_commit_unavailable", detail={
                "mode": self.mode,
                "buffered": self._buffer_dictate_until_commit,
            })
        return self._commit_dictate_buffer(target_mode=None)

    def _do_switch(self, switch: SwitchMatch) -> DispatchOutcome:
        """Transactional: a prefix switch ("dictate <payload>") only counts
        as having happened if the payload actually got dispatched. If
        dispatch raises (injection failure, a command handler blowing up,
        an agent-dispatch error), the mode is reverted to whatever it was
        before this switch and the failure is surfaced audibly -- otherwise
        the user would be silently left in a new mode with nothing
        delivered and no indication anything went wrong."""
        prior_mode = self.mode
        if (self._buffer_dictate_until_commit
                and prior_mode is SessionMode.DICTATE
                and switch.target_mode is not SessionMode.DICTATE
                and self._dictate_pending_buffer):
            committed = self._commit_dictate_buffer(target_mode=None)
            if committed.kind != "dictate_committed":
                return committed
        self._switch_mode(switch.target_mode)
        if switch.is_prefix and switch.payload.strip():
            try:
                return self._dispatch_in_mode(switch.payload)
            except Exception as exc:
                log.exception(
                    "[SESSION] prefix-switch payload dispatch failed; reverting %s -> %s",
                    switch.target_mode.value, prior_mode.value,
                )
                self._switch_mode(prior_mode)
                if self._on_switch_dispatch_error:
                    self._on_switch_dispatch_error(exc)
                return DispatchOutcome(
                    kind="prefix_switch_failed",
                    detail={"mode": switch.target_mode, "reverted_to": prior_mode, "error": str(exc)},
                )
        return DispatchOutcome(kind="mode_switch", detail={"mode": switch.target_mode})

    def _switch_mode(self, new_mode: SessionMode) -> None:
        prior_mode = self.mode
        log.info("[SESSION] mode change %s -> %s", prior_mode.value, new_mode.value)

        if new_mode is SessionMode.DICTATE and self.mode is not SessionMode.DICTATE:
            # Lock onto whatever's focused right now -- injections later in
            # this DICTATE lane must stay within this process. A fresh
            # DICTATE entry also starts a fresh stage buffer -- "what I
            # dictated" from AVA should mean THIS excursion, not some stale
            # one from earlier in the session.
            if self._buffer_dictate_until_commit:
                # Persistent buffered DICTATE selects the destination only at
                # explicit commit, so staging may span deliberate app/window
                # changes. A successful "end" releases that commit target.
                self._dictate_target_process = None
                self._dictate_target_hwnd = None
            else:
                self._dictate_target_process = self._foreground_exe_resolver()
                self._dictate_target_hwnd = self._foreground_hwnd_resolver()
            self._last_dictate_ended_terminal = None
            self._stage_buffer = ""
            self._dictate_pending_buffer = ""
        self.mode = new_mode
        if self._on_mode_change:
            self._on_mode_change(new_mode)

    def force_mode(self, new_mode: SessionMode) -> None:
        """Apply a non-utterance-driven mode change."""
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

    def _dispatch_hands_free_command(
        self, match: HandsFreeCommandMatch,
    ) -> DispatchOutcome:
        """Execute one reserved command without leaving the DICTATE lane."""
        commit_detail = None
        if match.pending_policy is PendingTextPolicy.COMMIT:
            committed = self._commit_dictate_buffer(target_mode=None)
            if committed.kind != "dictate_committed":
                return DispatchOutcome(kind="hands_free_command_blocked", detail={
                    "phrase": match.phrase,
                    "commit_outcome": committed.kind,
                    **committed.detail,
                })
            commit_detail = committed.detail

        result = self._command_dispatch_fn(match.dispatch_text)
        if not result.matched:
            return DispatchOutcome(kind="hands_free_command_failed", detail={
                "phrase": match.phrase,
                "dispatch_text": match.dispatch_text,
                "committed": commit_detail,
            })

        self._stack.push(StackItem(
            kind="command", payload=result.phrase or match.phrase,
            mode=SessionMode.DICTATE, timestamp=self._clock(),
        ))
        return DispatchOutcome(kind="hands_free_command_executed", detail={
            "phrase": result.phrase or match.phrase,
            "dispatch_text": match.dispatch_text,
            "committed": commit_detail,
            "mode_retained": self.mode,
        })

    def _dispatch_dictate(self, chunk_raw: str) -> DispatchOutcome:
        if self._buffer_dictate_until_commit:
            return self._stage_dictate_chunk(chunk_raw)

        foreground = self._foreground_exe_resolver()
        target_process = self._dictate_target_process
        if not check_focus_lock(self._dictate_target_process, foreground):
            # Formatted even though suppressed -- retype_last_suppressed()
            # later re-injects this exact payload verbatim via inject_fn,
            # so it must already be the post-formatting text, not the raw
            # spoken words.
            self._stack.push(StackItem(
                kind="dictation_chunk", payload=self._format_dictate_fn(chunk_raw.strip()),
                mode=SessionMode.DICTATE, timestamp=self._clock(),
                extra={"suppressed": True, "target_process": self._dictate_target_process,
                       "foreground": foreground, "hwnd": self._foreground_hwnd_resolver()},
            ))
            # Keep DICTATE active while preserving the fail-closed focus lock.
            # A transient focus drift must not silently end long-form dictation;
            # later chunks resume once the original target is focused again.
            if self._on_focus_lock_revert:
                self._on_focus_lock_revert()
            return DispatchOutcome(kind="dictate_suppressed_focus_lock", detail={
                "target_process": target_process, "foreground": foreground,
                "mode_retained": self.mode,
            })

        if self._last_dictate_ended_terminal is None:
            adjusted = chunk_raw.strip()
            to_inject = adjusted
        else:
            adjusted = seam_join(self._last_dictate_ended_terminal, chunk_raw)
            to_inject = " " + adjusted

        # Inline formatting tokens ("new line" -> \n, etc.) -- applied AFTER
        # seam-join (so its filler/capitalization heuristics see the
        # original spoken words, not control characters) and as the LAST
        # transform before injection, so everything downstream (stage
        # buffer, scratch-that undo length, seam state for the NEXT chunk)
        # reflects what was actually typed, matching the hotkey/wake lanes'
        # "history stores post-substitution text" contract.
        to_inject = self._format_dictate_fn(to_inject)

        self._inject_fn(to_inject)
        self._last_dictate_ended_terminal = chunk_ends_terminal(to_inject)
        # Mirrors exactly what got injected (to_inject already carries the
        # seam-join leading space for non-first chunks) -- this IS "what I
        # dictated" for AVA's stage-reference contract. Suppressed chunks
        # (focus-lock reverted, above) never reach here, so nothing that
        # wasn't actually typed ends up in the buffer.
        self._stage_buffer += to_inject
        self._stack.push(StackItem(
            kind="dictation_chunk", payload=to_inject, mode=SessionMode.DICTATE,
            timestamp=self._clock(),
            # HWND recorded at push-time -- this is the window the text
            # actually landed in. _do_scratch_that() re-checks it against
            # the CURRENT foreground HWND before sending any destructive
            # backspace/delete keystrokes, since focus can drift between
            # dictation and the "scratch that" undo (see _do_scratch_that).
            extra={"target_process": self._dictate_target_process,
                   "hwnd": self._foreground_hwnd_resolver()},
        ))
        return DispatchOutcome(kind="dictate_injected", detail={"text": to_inject})

    def _stage_dictate_chunk(self, chunk_raw: str) -> DispatchOutcome:
        """Append one silence-bounded transcript without touching the editor.

        Deliberately does NOT call self._format_dictate_fn here. Formatting
        tokens (like every other pipeline step -- process_transcription,
        clean_text, smart_correct) must run exactly once, over the complete
        joined thought, at commit -- never per staged fragment. Running
        formatting_tokens.apply_formatting_tokens() here would both violate
        that module's own documented contract ("must run AFTER any LLM
        correction pass and immediately before delivery") and risk embedding
        control characters (e.g. a "new line" token's literal \\n) into text
        that clean_text/smart_correct haven't seen yet."""
        if self._last_dictate_ended_terminal is None:
            to_stage = chunk_raw.strip()
        else:
            to_stage = " " + seam_join(self._last_dictate_ended_terminal, chunk_raw)
        if not to_stage:
            return DispatchOutcome(kind="empty")

        self._dictate_pending_buffer += to_stage
        self._last_dictate_ended_terminal = chunk_ends_terminal(to_stage)
        self._stack.push(StackItem(
            kind="dictation_staged_chunk", payload=to_stage,
            mode=SessionMode.DICTATE, timestamp=self._clock(),
        ))
        return DispatchOutcome(kind="dictate_staged", detail={
            "text": to_stage,
            "pending_chars": len(self._dictate_pending_buffer),
        })

    def _commit_dictate_buffer(
        self, *, target_mode: Optional[SessionMode],
    ) -> DispatchOutcome:
        """Paste the complete staged thought once, retaining it on failure."""
        text = self._dictate_pending_buffer
        if not text:
            if target_mode is not None:
                self._switch_mode(target_mode)
            else:
                self._dictate_target_process = None
                self._dictate_target_hwnd = None
            return DispatchOutcome(kind="dictate_committed", detail={
                "text": "", "empty": True, "mode_retained": self.mode,
            })

        # A persistent hands-free session is intentionally allowed to span
        # applications and text boxes. The foreground at the explicit commit
        # word ("end") is therefore the destination for this thought; staging
        # speech must not pin the session to whichever window happened to be
        # focused minutes earlier.
        foreground = self._foreground_exe_resolver()
        current_hwnd = self._foreground_hwnd_resolver()
        self._dictate_target_process = foreground
        self._dictate_target_hwnd = current_hwnd

        def commit_target_still_focused() -> bool:
            return (
                check_focus_lock(foreground, self._foreground_exe_resolver())
                and current_hwnd is not None
                and self._foreground_hwnd_resolver() == current_hwnd
            )

        if not foreground or current_hwnd is None:
            log.warning(
                "[SESSION] DICTATE commit retained: current paste target unavailable "
                "(current_process=%r current_hwnd=%r)",
                foreground, current_hwnd,
            )
            if self._on_focus_lock_revert:
                self._on_focus_lock_revert()
            return DispatchOutcome(kind="dictate_commit_blocked_focus_lock", detail={
                "pending_chars": len(text),
                "target_process": self._dictate_target_process,
                "foreground": foreground,
                "target_hwnd": self._dictate_target_hwnd,
                "foreground_hwnd": current_hwnd,
            })

        delivered = self._inject_fn(text, commit_target_still_focused)
        if delivered is False:
            after_process = self._foreground_exe_resolver()
            after_hwnd = self._foreground_hwnd_resolver()
            focus_changed = (
                not check_focus_lock(foreground, after_process)
                or after_hwnd != current_hwnd
            )
            if focus_changed:
                log.warning(
                    "[SESSION] DICTATE commit retained: focus changed during injection "
                    "(target_process=%r current_process=%r target_hwnd=%r current_hwnd=%r)",
                    foreground, after_process, current_hwnd, after_hwnd,
                )
                if self._on_focus_lock_revert:
                    self._on_focus_lock_revert()
                return DispatchOutcome(kind="dictate_commit_blocked_focus_lock", detail={
                    "pending_chars": len(text),
                    "target_process": foreground,
                    "foreground": after_process,
                    "target_hwnd": current_hwnd,
                    "foreground_hwnd": after_hwnd,
                })
            error = RuntimeError("dictation paste was not delivered")
            log.error("[SESSION] DICTATE commit retained: paste callback reported failure")
            if self._on_switch_dispatch_error:
                self._on_switch_dispatch_error(error)
            return DispatchOutcome(kind="dictate_commit_failed", detail={
                "pending_chars": len(text), "error": str(error),
            })

        # inject_fn runs the full formatting pipeline (process_transcription,
        # clean_text, smart_correct, formatting tokens) over `text` and may
        # return the ACTUAL delivered/formatted string -- use that (not the
        # pre-formatting `text` passed in) for the undo record and
        # stage_buffer, so scratch-that and any "the staged text" AVA
        # reference reflect what was really typed. Legacy bool/None-returning
        # injectors keep today's behavior (record the pre-formatting text).
        final_text = delivered if isinstance(delivered, str) else text

        self._dictate_pending_buffer = ""
        self._stage_buffer = final_text
        self._last_dictate_ended_terminal = None
        self._stack.push(StackItem(
            kind="dictation_chunk", payload=final_text, mode=SessionMode.DICTATE,
            timestamp=self._clock(),
            extra={"target_process": self._dictate_target_process,
                   "hwnd": current_hwnd},
        ))
        # A completed thought must not end the persistent DICTATE lane. Drop
        # only its focus lock; the next staged chunk captures whichever text
        # box is focused then.
        self._dictate_target_process = None
        self._dictate_target_hwnd = None
        if target_mode is not None:
            self._switch_mode(target_mode)
        return DispatchOutcome(kind="dictate_committed", detail={
            "text": final_text, "chars": len(final_text),
            "mode_retained": self.mode,
        })

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
        """Pops the most recent unit of work and, for a dictation chunk,
        sends destructive backspace/select+delete keystrokes to undo it.
        Guarded by TWO independent focus checks before any keystroke is
        sent, both fail-closed:
          1. exe-name check_focus_lock (existing) -- catches switching to a
             different application entirely.
          2. HWND equality (this method) -- catches switching to a
             DIFFERENT WINDOW of the SAME exe (e.g. two Notepad windows),
             which (1) alone cannot see. A mismatch here means the window
             that received the original text is not the one in front of
             the user right now, so undoing here would delete content in
             the wrong window -- irreversible for a keyboard-unable user.
             We refuse rather than guess or auto-refocus."""
        item = self._stack.pop()
        if item is None:
            return False
        if item.kind == "dictation_staged_chunk":
            if not self._dictate_pending_buffer.endswith(item.payload):
                return False
            self._dictate_pending_buffer = self._dictate_pending_buffer[:-len(item.payload)]
            self._last_dictate_ended_terminal = (
                chunk_ends_terminal(self._dictate_pending_buffer)
                if self._dictate_pending_buffer else None
            )
            return True
        if item.kind != "dictation_chunk":
            return False  # command undo out of scope; consumed, refuse-style earcon
        foreground = self._foreground_exe_resolver()
        target = item.extra.get("target_process")
        if not check_focus_lock(target, foreground):
            return False
        current_hwnd = self._foreground_hwnd_resolver()
        recorded_hwnd = item.extra.get("hwnd")
        if recorded_hwnd is None or current_hwnd != recorded_hwnd:
            log.warning(
                "[SESSION] scratch-that refused: foreground window changed since this "
                "chunk was dictated (recorded_hwnd=%r current_hwnd=%r)",
                recorded_hwnd, current_hwnd,
            )
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
