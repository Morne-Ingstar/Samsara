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
                            unsent. Entry is EXACT-PHRASE-ONLY from a
                            configurable invocation list (see
                            match_ava_invocation / DEFAULT_AVA_INVOCATIONS)
                            -- there is no prefix form, unlike COMMAND/DICTATE.

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
from samsara.log import describe_text
import logging
import re
import string
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Union

from samsara.draft_document import DraftDocument
from samsara.utterance_undo import UtteranceUndoMixin
from samsara.draft_clear_undo import ClearDraftUndo
from samsara.live_surface.suggestion_lifecycle import bind_pending_reply
from samsara import clipboard_recovery, delivery_verify
from samsara.hands_free_entry import HANDS_FREE_ENTRY_PHRASES, dispatch_dictate_hands_free_entry
from samsara.stage_timing import current_stage_timing as _current_stage_timing
from samsara.session_text import (
    EllipsisSeam as EllipsisSeam,
    REDECODE_MIN_STAGED_MARKS as REDECODE_MIN_STAGED_MARKS,
    REDECODE_RUN_ON_WORDS_PER_MARK as REDECODE_RUN_ON_WORDS_PER_MARK,
    REDECODE_STAGED_ADVANTAGE as REDECODE_STAGED_ADVANTAGE,
    TERMINAL_PUNCTUATION as TERMINAL_PUNCTUATION,
    _CANNOT_FOLLOW_DETERMINER as _CANNOT_FOLLOW_DETERMINER,
    _DENSITY_WORD_RE as _DENSITY_WORD_RE,
    _HESITATION_SOUNDS as _HESITATION_SOUNDS,
    _LEADING_ELLIPSIS as _LEADING_ELLIPSIS,
    _LEADING_FILLERS as _LEADING_FILLERS,
    _OPEN_CLAUSE_CONJUNCTIONS as _OPEN_CLAUSE_CONJUNCTIONS,
    _PRONOUN_I as _PRONOUN_I,
    _SENTENCE_CANNOT_END_WORDS as _SENTENCE_CANNOT_END_WORDS,
    _SENTENCE_MARK_RE as _SENTENCE_MARK_RE,
    _TRAILING_ELLIPSIS as _TRAILING_ELLIPSIS,
    _is_continuation_chunk as _is_continuation_chunk,
    chunk_ends_terminal as chunk_ends_terminal,
    decide_ellipsis_seam as decide_ellipsis_seam,
    redecode_is_poorer as redecode_is_poorer,
    seam_join as seam_join,
    sentence_mark_density as sentence_mark_density,
)

log = logging.getLogger("Samsara.session_modes")


def is_self_hear_capture(coordinator, start_ns: int | None, end_ns: int | None) -> bool:
    """Refuse a capture wholly inside Ava speech unless speech interrupted TTS.

    The coordinator owns the clock and keeps recent windows so a queued
    command still sees the window that covered its original capture.
    """
    if coordinator is None or start_ns is None or end_ns is None or end_ns < start_ns:
        return False
    windows = getattr(coordinator, 'speech_windows', None)
    if not callable(windows):
        return False
    return any(
        not window.interrupted and window.start_ns <= start_ns <= end_ns <= window.end_ns
        for window in windows()
    )


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

# ---------------------------------------------------------------------------
# Switch-word matching: PREFIX-OR-WHOLE, normalized
# ---------------------------------------------------------------------------
_WHOLE_UTTERANCE_SWITCHES: dict[str, SessionMode] = {
    "command mode": SessionMode.COMMAND,
    "dictate mode": SessionMode.DICTATE,
    "dictation mode": SessionMode.DICTATE,
    "dictate": SessionMode.DICTATE,
    # 2026-09-11: "ava mode" is a first-class switch word, symmetric with
    # "command mode"/"dictate mode". It is WHOLE-UTTERANCE ONLY (this table
    # is only consulted for whole-utterance equality below), so the prefix
    # trap the 2026-07-18 incident exploited stays shut -- see the comment
    # on _PREFIX_SWITCHES. Bare "ava" is deliberately NOT here: it is an
    # ordinary content word, and that is the half of the incident worth
    # keeping out.
    "ava mode": SessionMode.AVA,
}

# Prefix form: "dictate <payload>" switches mode AND delivers the payload as
# the first chunk/utterance of the new mode. Ava has NO prefix form -- see
# match_ava_invocation below and the 2026-07-18 incident it fixes: "Ava
# Omniscience Mode" spoken as ordinary DICTATE content, isolated into its own
# utterance by a natural pause, used to hijack mid-dictation into a
# mode-switch+dispatch. That prefix grammar stays removed: "we should use ava
# mode later" is dictation, never a switch.
#
# Whole-utterance "ava mode" WAS also excluded for the same incident, but the
# exclusion cost more than it bought (2026-09-11): with no "ava mode" switch
# word, the phrase fell through to the command registry, where ask_ollama's
# "hey ava" command claims the alias "ava" -- so "ava mode" prefix-matched it
# with remainder "mode" and was sent to the LLM as a question, and in DICTATE
# it was simply typed. Bare "ava" remains excluded from this table; Ava is
# now reachable by BOTH this switch word and the configurable, exact-phrase
# match_ava_invocation() mechanism, and both land on SessionMode.AVA.
_PREFIX_SWITCHES: dict[str, SessionMode] = {
    "dictate": SessionMode.DICTATE,
}

SCRATCH_THAT_PHRASE = "scratch that"
DICTATE_COMMIT_PHRASE = "end"
# Words that commit when they are the whole utterance. "and" USED to be here
# as a homophone of "end" (a lone "and" isolated by a pause commits too). The
# owner's logs (2026-08-02..2026-09-14, queue 54) reversed that trade: ~39
# isolated commit words decoded as "End"/"end"/"END", none of the failed
# commits decoded as a lone "and", and 6 lone "and"/"And."/"And..." utterances
# all committed -- at least 4 of them mid-thought pauses ("And..." then the
# sentence carried on), each pasting an unfinished thought. A pause after a
# sentence-initial "and" is ordinary speech; a pause after "end" is not.
# Cost of dropping it: a spoken "end" that Whisper hears as a lone "and" now
# stages "and" and the user repeats "end". The name is kept: command_catalog
# and quick_reference_qt read this set to reserve/display the commit words.
_DICTATE_COMMIT_HOMOPHONES = frozenset({DICTATE_COMMIT_PHRASE})
# A consumed trailing commit word is re-decoded from the same audio at commit
# (see _commit_dictate_buffer); Whisper renders it either way there, so the
# re-decode tail is stripped of both spellings. Never used for matching.
_COMMIT_WORD_REDECODE_SPELLINGS = frozenset({"end", "and"})

#: Queue 142. The commit word is configurable, because "end" is a homophone of
#: the owner's most common filler and queue 54 (above) could only trade one
#: failure direction for the other: with "and" in the set his fillers committed
#: unfinished thoughts; without it, his spoken "end" decodes as "And." and gets
#: typed. Owner log 2026-09-16 11:21 has five consecutive "And." staged as
#: prose. No matcher can separate two words that sound identical -- the way out
#: is a word that does not collide, so the default is now "finish".
#:
#: Rebinding the module constants rather than threading the word through
#: SessionModeManager is deliberate: command_catalog, quick_reference_qt,
#: tutorial_qt, first_run_wizard_qt and settings/modes_qt all READ
#: DICTATE_COMMIT_PHRASE at call time, so they follow a change for free, and
#: the 25+ tests that construct a manager keep their signature.
DEFAULT_DICTATE_COMMIT_PHRASE = "finish"
#: Words rejected as a commit word: each is a common filler or sentence opener
#: the decoder emits alone, which is the exact failure this setting exists to
#: end. Not a hard block -- the caller decides -- but never a default.
COMMIT_WORD_HOMOPHONE_RISKS = frozenset({"and", "end", "in", "then", "um", "uh", "so", "but"})


def set_commit_phrase(word: str) -> str:
    """Rebind the dictation commit word. Returns the word actually installed.

    Called once at boot from the live config. An empty or multi-word value is
    refused (the owner rejected multi-word commit tokens twice) and the current
    word is kept, because a session with no commit word cannot paste a thought.
    """
    global DICTATE_COMMIT_PHRASE, _DICTATE_COMMIT_HOMOPHONES
    candidate = normalize_utterance(word or "")
    if not candidate or len(candidate.split()) != 1:
        return DICTATE_COMMIT_PHRASE
    DICTATE_COMMIT_PHRASE = candidate
    _DICTATE_COMMIT_HOMOPHONES = frozenset({candidate})
    return candidate

_SENTENCE_TERMINALS = ".!?"
#: "Sleep" exits of the latched session (SAMSARA_VISION.md section 1: armed
#: once by a wake phrase, open until sleep). WHOLE-UTTERANCE only -- unlike
#: the older exit phrases below, which match anywhere in an utterance, "go to
#: sleep" is ordinary prose ("the kids need to go to sleep") and must never
#: end a session from inside a dictated sentence. Sleep stops capture; it
#: never discards: a staged-but-uncommitted DICTATE draft is retained and
#: restored the next time the hands-free session opens (Astra review #10).
SESSION_SLEEP_PHRASES = (
    "go to sleep",
    "samsara sleep",
    "sleep now",
)
#: The stop phrases (02_conversational_architecture.md section 5): WHOLE
#: utterance only. Advances the execution generation, cancels model requests,
#: queued effects and the answer being spoken, keeps the draft and keeps the
#: microphone armed -- it does NOT end the session. "go to sleep" is
#: stop + disarm.
#:
#: Queue 116 replaced ("stop",) with these two, when the stop was wired up for
#: real. The match has always been whole-utterance only, so "stop the music"
#: and "I told him to stop" were never at risk -- but a bare "Stop." spoken as
#: a complete utterance is common enough in real dictation to lose, and once
#: the branch is live losing it means the word is eaten instead of typed.
#: "Halt" and "cease" are not words a person dictates alone by accident.
#: TWO of them, deliberately: a panic control should have more than one way
#: in, and one may transcribe better than the other in practice.
#:
#: config command_mode.stop_phrases overrides this list (not adds to it), so
#: the owner can change or add a word without a release. An empty or unusable
#: config value falls back to these -- the emergency stop is not something a
#: typo in config.json gets to switch off.
SESSION_STOP_PHRASES = ("halt", "cease")

GLOBAL_SESSION_EXIT_PHRASES = (
    "stop listening",
    "exit hands free",
    "exit command mode",
    *SESSION_SLEEP_PHRASES,
)

#: Queue 84. EVERY session-ending phrase -- the configured aborts ("cancel",
#: "cancel dictation", "abort") and the exits above -- matches the WHOLE
#: utterance only, after normalize_utterance(). Until 2026-09-15 the aborts
#: were word-boundary regexes searched ANYWHERE in the text, so at 15:12:29
#: "Have it stop listening to you, or something like that." ended the session
#: and destroyed a 477-character draft. Sleep, stop, scratch-that, clear-draft
#: and the commit word were already whole-utterance; the aborts were the last
#: anywhere-matchers, and they were the destructive ones.
#:
#: The rule costs nothing an ordinary user notices: a bare "cancel" still
#: aborts. It only stops a sentence that CONTAINS the phrase from ending the
#: session -- which is the only way this has ever fired by accident.
ABORT_PHRASES_ARE_WHOLE_UTTERANCE = True

#: Queue 84. An abort or a confirmed "scratch everything" no longer destroys
#: text: the draft goes to a recovery slot that survives the session ending,
#: and one of these phrases (whole utterance, in the dictation lane) puts it
#: back. Deliberately NOT auto-restored on the next session, unlike a draft
#: set aside by "go to sleep": an abort is usually meant, so silently
#: prepending the old draft to the next thought would paste text the user
#: thought was gone. Recovery is asked for, never assumed.
#:
#: Phrase choice: multi-word and verb-first, so ordinary prose does not
#: collide with them even as a whole utterance; they reuse "draft", the word
#: the clear-draft question itself uses.
RECOVER_DRAFT_PHRASES = (
    "bring back my draft",
    "bring back the draft",
    "bring my draft back",
    "restore my draft",
    "restore the draft",
)
#: How long a recovered draft is kept. Long enough to end a session, restart
#: it and ask (the owner re-dictated the lost 477 characters 22 s later);
#: short enough that dictated text is not held in memory indefinitely.
RECOVERABLE_DRAFT_TTL_S = 300.0


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


#: Queue 99: counted repetition of the SAME scratch. "scratch that twice",
#: "scratch that three times", "scratch that 4 times" -- N pops of exactly the
#: path one "scratch that" takes, never a second deletion mechanism.
#:
#: Number parsing reuses samsara.intent.normalize.parse_number, which is
#: already the shared front door the brief asks for: its own docstring records
#: that it reuses show_numbers._WORD_TO_NUM / _parse_spoken_number for digits,
#: words and compounds, windows._ORDINALS for ordinals and
#: window_switcher.PHONETIC for letters. The consolidation queue 45 identified
#: was done by the intent-grammar work; this adds no fourth parser.
#:
#: "twice"/"thrice"/"once" are NOT numbers -- parse_number returns None for
#: all three -- they are multiplicative adverbs, so they belong to this phrase
#: grammar rather than to a number table. Three entries, written once.
_SCRATCH_ADVERBS = {"once": 1, "twice": 2, "thrice": 3}
_SCRATCH_COUNT_PREFIX = SCRATCH_THAT_PHRASE + " "
#: Upper bound on a spoken count. Not a policy limit -- the stack itself holds
#: UnitOfWorkStack.MAX_SIZE -- but a guard against a decode turning a sentence
#: into "scratch that 4000 times".
SCRATCH_COUNT_MAX = 20

#: Queue 99: "again" repeats the scratch just performed, once more. The owner's
#: phrasing and the cheaper one to say.
#:
#: NOTE there is already a builtin.again -> repeat_last_command ("Repeat the
#: last executed command", commands.json). This does NOT replace it and does
#: not add a second catalog entry: in the COMMAND lane that command already
#: repeats a scratch, because "scratch that" is not in dictation's
#: _REPEAT_BLACKLIST_NAMES. What it could not reach is the hands-free DICTATE
#: lane, where this module intercepts the utterance first and a lone "again"
#: was simply dictated. Same word, same meaning, now also in the lane where
#: the staged draft lives.
SCRATCH_AGAIN_PHRASE = "again"
#: How long after a scratch a bare "again" still means "do that again".
#:
#: 8 s. From the owner's own cadence (the 848 hands-free inter-utterance pauses
#: behind streaming.IDLE_DELAY_S_DEFAULT, 2026-09): median 1.8 s, 75% under
#: 5.0 s, 90th percentile 20 s. Above the 75th percentile, so a deliberate
#: follow-up lands inside it comfortably; well under the 90th, so a scratch the
#: user walked away from does not leave a live trigger behind. The window is
#: the SECOND guard, not the first: "again" only means this when the previous
#: resolved action in this session was a scratch, so any other utterance in
#: between disarms it regardless of the clock. Outside both, it is dictation.
SCRATCH_AGAIN_TTL_S = 8.0


def match_scratch_count(raw_text: str) -> "Optional[int]":
    """N for a whole-utterance counted scratch, else None.

    Whole-utterance only, like every other scratch phrase: the normalised text
    must be exactly "scratch that" + a count expression. "let's scratch that
    idea twice" does not start with the phrase, and "scratch that idea twice"
    has a remainder that is not a count -- both are dictation.
    """
    text = normalize_utterance(raw_text)
    if not text.startswith(_SCRATCH_COUNT_PREFIX):
        return None
    rest = text[len(_SCRATCH_COUNT_PREFIX):].strip()
    if not rest:
        return None
    if rest in _SCRATCH_ADVERBS:
        return _SCRATCH_ADVERBS[rest]
    words = rest.split()
    if words[-1] not in ("times", "time"):
        return None
    from samsara.intent.normalize import parse_number  # noqa: PLC0415
    count = parse_number(words[:-1])
    if count is None or count < 1 or count > SCRATCH_COUNT_MAX:
        return None
    return count


def is_scratch_again(raw_text: str) -> bool:
    """Whole-utterance only -- "say that again" and "run it again" are
    dictation. Whether it MEANS anything is the session's call: see
    SCRATCH_AGAIN_TTL_S and _scratch_again_armed."""
    return normalize_utterance(raw_text) == SCRATCH_AGAIN_PHRASE


# Queue 80: discard the WHOLE staged draft ("scratch that" pops one chunk, and
# the undo stack holds only five). Whole utterance only, like "scratch that".
# The "scratch" family is the owner's own word for taking dictation back, so
# these extend a habit instead of adding a new verb. Checked against
# commands_catalog.json (2026-09-15): no command phrase starts with "scratch"
# except builtin.scratch_that; "clear ..."/"delete ..."/"cancel ..." phrases
# already belong to reminders, tasks, health log, Ava memory and the printer,
# and "cancel"/"abort" are session aborts matched anywhere, so none of those
# words are used. A dictated sentence that merely contains "scratch
# everything" never matches: the normalised utterance must equal a phrase.
# Canonical catalog id: builtin.scratch_everything (commands.json).
CLEAR_DRAFT_PHRASES = ("scratch everything", "scratch all of that", "scratch all that")
CLEAR_DRAFT_CONFIRM_TTL_S = 30.0  # Compatibility name; now the voice take-back window.
from .result_messages import CLEAR_RESTORE_PHRASES
CLEAR_DRAFT_RESTORE_PHRASE = CLEAR_RESTORE_PHRASES[0]


#: Queue 85: a word clicked in the live preview waits for ONE utterance, which
#: is taken as its replacement rather than dictated. The click is what makes
#: this unambiguous -- the user performed a deliberate physical act and then
#: spoke once -- so no reply vocabulary is needed and any wording works ("it's
#: Ingstar with an I"). Every control phrase still wins (they are checked
#: first), and saying "scratch that" cancels the correction instead. The TTL
#: exists so a click the user forgets about cannot swallow a sentence minutes
#: later; on expiry the utterance is ordinary dictation.
WORD_CORRECTION_TTL_S = 45.0
WORD_CORRECTION_CANCEL_PHRASES = ("scratch that", "never mind", "nevermind", "leave it")

#: Queue 85: scroll the PREVIEW, not the app being dictated into. The catalog's
#: existing "scroll up"/"scroll down"/"page up" commands move the target
#: document, which is a different and still-wanted action, so these name the
#: draft instead of overloading them. Whole-utterance only (queue 84) and
#: non-destructive: the worst a false match can do is move a view.
DRAFT_SCROLL_PHRASES = {
    "scroll the draft up": "up",
    "scroll the draft down": "down",
    "scroll the draft back": "up",
    "show me the top of the draft": "top",
    "top of the draft": "top",
    "start of the draft": "top",
    "bottom of the draft": "bottom",
    "end of the draft": "bottom",
    "show me the rest of the draft": "bottom",
    "latest in the draft": "bottom",
    "show draft": "bottom",
    # Queue 136: whole-utterance placement controls use the existing
    # draft-scoped callback, so ordinary sentences remain dictation.
    "move the draft box to the top left": "move:top-left",
    "move the draft box to the top center": "move:top-center",
    "move the draft box to the top right": "move:top-right",
    "move the draft box to the center left": "move:center-left",
    "move the draft box to the center": "move:center",
    "move the draft box to the center right": "move:center-right",
    "move the draft box to the bottom left": "move:bottom-left",
    "move the draft box to the bottom center": "move:bottom-center",
    "move the draft box to the bottom right": "move:bottom-right",
}


#: Queue 85: take back the last correction, without a mouse. Whole-utterance.
FORGET_CORRECTION_PHRASES = (
    "forget that correction",
    "undo that correction",
    "forget that word",
)


def is_forget_correction(raw_text: str) -> bool:
    return normalize_utterance(raw_text) in FORGET_CORRECTION_PHRASES


def match_draft_scroll(raw_text: str) -> Optional[str]:
    """Return a draft view or ``move:<preset>`` control for a whole utterance.

    A sentence containing a control phrase is still dictation.
    """
    return DRAFT_SCROLL_PHRASES.get(normalize_utterance(raw_text))


def is_clear_draft(raw_text: str) -> bool:
    """Whole-utterance only -- 'we should scratch everything we planned' must NOT match."""
    return normalize_utterance(raw_text) in CLEAR_DRAFT_PHRASES


def is_recover_draft(raw_text: str) -> bool:
    """Whole-utterance only (queue 84) -- 'I had to restore my draft from a
    backup' is dictation, not a recovery request."""
    return normalize_utterance(raw_text) in RECOVER_DRAFT_PHRASES


def is_dictate_commit(raw_text: str) -> bool:
    """Whole-utterance manual commit for buffered DICTATE mode.

    Case and punctuation never matter ("End.", "END", "end,"): the match runs
    on normalize_utterance(). Only DICTATE_COMMIT_PHRASE matches -- see
    _DICTATE_COMMIT_HOMOPHONES for why a lone "and" no longer does."""
    return normalize_utterance(raw_text) in _DICTATE_COMMIT_HOMOPHONES


def _registry_tokens(text: str) -> list:
    """The command registry's token view, via the intent grammar's normaliser
    (lowercase, punctuation removed per token). Imported lazily: this module
    stays free of catalog imports at load time."""
    from samsara.intent.normalize import tokens  # noqa: PLC0415
    return tokens(text)


def split_trailing_dictate_commit(raw_text: str) -> Optional[str]:
    """Dictation text before a trailing commit word, or None.

    2026-09-14 22:37:12: the owner said "end" without a long enough pause and
    Whisper returned one chunk, "...that it got fixed. End." -- the whole-
    utterance check never saw a lone "end", so "End." was staged as prose.

    Matches only when the commit word is its OWN final sentence: the text
    before it ends in . ! or ?, and the last token is exactly "end" (any case,
    trailing punctuation allowed). "we reached the end." and "and then the
    end" do not match (no sentence break before it); "...fixed. And." does not
    match (a sentence starting with "And" and a pause is ordinary speech). The
    returned text keeps its original casing and punctuation."""
    text = (raw_text or "").strip()
    if not text:
        return None
    parts = text.rsplit(None, 1)
    if len(parts) != 2:
        return None
    body, last = parts[0].rstrip(), parts[1]
    if not body or body[-1] not in _SENTENCE_TERMINALS:
        return None
    if _registry_tokens(last) != [DICTATE_COMMIT_PHRASE]:
        return None
    if not _registry_tokens(body):
        return None
    return body


def strip_redecoded_commit_word(text: str) -> str:
    """Remove the commit word a re-decode heard at the very end of a thought
    whose trailing "end" was already consumed ("...got fixed and." ->
    "...got fixed."). Only called when that consumption happened."""
    stripped = (text or "").rstrip()
    parts = stripped.rsplit(None, 1)
    if len(parts) != 2:
        return text
    last_tokens = _registry_tokens(parts[1])
    if len(last_tokens) != 1 or last_tokens[0] not in _COMMIT_WORD_REDECODE_SPELLINGS:
        return text
    body = parts[0].rstrip()
    trailing = parts[1][len(parts[1].rstrip(string.punctuation)):]
    if body and body[-1] in ",;:":
        body = body[:-1]
    if body and body[-1] not in _SENTENCE_TERMINALS and trailing[:1] in tuple(_SENTENCE_TERMINALS):
        body += trailing[:1]
    return body


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


def match_whole_utterance(raw_text: str, phrases) -> Optional[str]:
    normalized = (raw_text or "").strip().rstrip(string.punctuation + " ").casefold()
    return next((phrase for phrase in phrases
                 if phrase.strip().casefold() == normalized and normalized), None)


def match_switch_word(
    raw_text: str, current_mode: Optional[SessionMode] = None,
) -> Optional[SwitchMatch]:
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

    current_mode: when given, a switch entry whose TARGET mode equals
    current_mode is not a match -- switching to where the session already
    is is never real intent. Applies to both the whole-utterance and prefix
    maps. Fixes the 2026-07-19 incident where bare "dictate" spoken mid-
    DICTATE (an ordinary word to say while dictating) was consumed as a
    no-op self-switch instead of dictated -- same acoustic-isolation class
    as the "Ava Omniscience" incident (see match_ava_invocation), just
    triggered by a real word instead of a hallucination. None (the
    default) preserves the original mode-agnostic matching for callers
    without session context (e.g. streaming.py's display-only control-
    phrase check, and this module's own pure-function test suite).
    """
    normalized = normalize_utterance(raw_text)
    if not normalized:
        return None

    # normalize_utterance() DELETES punctuation rather than replacing it with
    # a space, so a hyphenated two-word switch ("ava-mode", and equally
    # "command-mode") would collapse into a single unmatchable token. Retry
    # the whole-utterance lookup with separator punctuation treated as a
    # space. Whole-utterance only -- this never widens the prefix grammar.
    candidates = [normalized]
    separated = normalize_utterance(re.sub(r"[-_/]+", " ", raw_text or ""))
    if separated and separated != normalized:
        candidates.append(separated)

    for candidate in candidates:
        if candidate in _WHOLE_UTTERANCE_SWITCHES:
            target = _WHOLE_UTTERANCE_SWITCHES[candidate]
            if current_mode is not None and target is current_mode:
                return None
            return SwitchMatch(target_mode=target)

    for prefix_word, mode in _PREFIX_SWITCHES.items():
        if normalized.startswith(prefix_word + " "):
            if current_mode is not None and mode is current_mode:
                continue
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
# Ava invocation matching: EXACT WHOLE-UTTERANCE ONLY, configurable list
# ---------------------------------------------------------------------------
#
# 2026-07-18 incident: "Ava Omniscience Mode" spoken as DICTATE content, split
# into its own utterance by a natural pause, hijacked mid-dictation via the
# old prefix-or-bare-word grammar (see _PREFIX_SWITCHES comment above).
# Recovery made it worse -- Ava mode utterances aren't forced to English (see
# dictation.py's _handle_command_mode_utterance / CHANGE 2), so the first
# "dictate mode" recovery attempt decoded as Vietnamese and was dispatched to
# Ava as a query instead of switching mode. Fix has two independent halves:
# this section (exact-phrase-only entry, no prefix trap) and the language fix
# in dictation.py (recovery phrases stay recognizable regardless of dictation
# language).

DEFAULT_AVA_INVOCATIONS: tuple[str, ...] = ("hey ava", "so ava", "oracle")


def resolve_ava_invocations(config: dict | None) -> list[str]:
    """Resolve Ava invocation phrases from config in one canonical place.

    Both the hands-free command-mode path and the Quick Reference window use
    this helper so the configured fallback behavior cannot drift.
    """
    value = (
        config.get("ava_invocations", list(DEFAULT_AVA_INVOCATIONS))
        if isinstance(config, dict)
        else list(DEFAULT_AVA_INVOCATIONS)
    )
    if isinstance(value, str):
        return [value]
    return list(value)


def _normalize_exact_phrase(text: str) -> str:
    """Case/punctuation-insensitive normalization for Ava invocation matching
    ONLY -- trailing period tolerated (full punctuation stripping handles it,
    same technique normalize_utterance() uses).

    Deliberately NOT normalize_utterance(): that function also strips LEADING
    FILLER WORDS, and "so" is one of them (_LEADING_FILLERS). If invocation
    matching reused normalize_utterance() wholesale, the configured phrase
    "so ava" would normalize down to bare "ava" -- identical to normalizing
    bare "ava" itself -- and accidentally let bare "ava" match via a phrase
    that's supposed to be a DISTINCT, more-deliberate invocation. Bare "ava"
    is intentionally excluded from DEFAULT_AVA_INVOCATIONS (see match_switch_
    word's _PREFIX_SWITCHES comment); this narrower normalizer keeps that
    exclusion real instead of it being silently reopened by "so ava"'s filler
    word. Matching phrases still get case/punctuation-folded exactly like
    every other exact control ("dictate mode", "scratch that") via the same
    lowercase + strip-all-punctuation technique -- just without the filler
    step.
    """
    t = (text or "").strip().lower()
    t = t.translate(str.maketrans("", "", string.punctuation))
    return " ".join(t.split())


def match_ava_invocation(raw_text: str, invocations) -> bool:
    """Exact whole-utterance match ONLY against a configurable invocation set
    (pre-normalized via _normalize_exact_phrase -- see SessionModeManager's
    ava_invocations constructor parameter). No prefix form: an utterance that
    merely STARTS with an invocation but contains more content is ordinary
    text for the current mode, not a switch -- this is the direct fix for
    the 2026-07-18 "Ava Omniscience Mode" incident (see module comment
    above). Bare "ava" does not match unless explicitly added to the
    configured list; it is deliberately absent from DEFAULT_AVA_INVOCATIONS.
    """
    normalized = _normalize_exact_phrase(raw_text)
    if not normalized:
        return False
    return normalized in invocations


# ---------------------------------------------------------------------------
# Stage-buffer reference detection (AVA mode, Phase 2)
# ---------------------------------------------------------------------------

# Unambiguous noun phrases -- position in the utterance doesn't matter.
_STAGE_REFERENCE_PHRASES = ("the text", "what i dictated", "the dictation",
                            "last sentence", "everything", "the whole draft")


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
    # Queue 102: the word that applies a staged edit proposal. Exactly the
    # same case as "yes"/"no"/"stop" above -- a complete, meaningful turn
    # that happens to be one word. Without it the substance gate eats the
    # only utterance that can resolve a pending proposal.
    "apply",
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
    # Optional opaque payload for call-site extras (e.g., per-utterance
    # audio for commit-time re-decoding in dictation.py).
    audio_ref: Any = None


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
# Spoken-notice policy (queue 103)
# ---------------------------------------------------------------------------
#
# Nothing in this app talks except Ava, so a voice arriving after a MOUSE
# CLICK reads as a malfunction. The incident: clicking "Clear draft" on the
# streaming preview and being told out loud to say "bring back my draft" --
# the same sentence the chip was already showing.
#
# Every _speak call site is one of exactly two classes:

#: The app is WAITING on an answer and cannot proceed without one. It has
#: installed state -- a pending clear, an armed correction -- and the next
#: utterance is the answer. These always speak: a question nobody heard is a
#: hang.
QUESTION = "question"

#: The app is reporting what it has ALREADY done. The work is finished, the
#: chip carries it, and nothing is waiting. Silent by default.
ACKNOWLEDGEMENT = "acknowledgement"

#: Config key (samsara/config_schema.py, Sounds tab). "questions" (default)
#: or "everything". There is no "off" -- see the queue 103 report.
SPOKEN_NOTICES_KEY = "feedback.spoken_notices"
SPOKEN_NOTICES_QUESTIONS = "questions"
SPOKEN_NOTICES_EVERYTHING = "everything"


def should_speak(notice_class: str, setting: str) -> bool:
    """Whether a notice of `notice_class` is spoken at `setting`.

    Pure, so the policy can be tested without a manager, a config or a voice.

    "everything" speaks both classes -- it is the accessibility escape hatch
    for a user who cannot see the chip, and it is the ONLY thing standing
    between them and silence, so it is deliberately unconditional.

    Anything else, including an unrecognised value, is treated as
    "questions": the default must survive a typo in config.json, and the
    failure direction that matters is "too quiet", never "asks something and
    says nothing".
    """
    if setting == SPOKEN_NOTICES_EVERYTHING:
        return True
    return notice_class == QUESTION


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


#: How long a focus-lock-SUPPRESSED dictation chunk may be re-typed by "retype
#: that" (queue 50, ARC audit5b). Suppressed text used to live until the
#: session ended -- up to command_mode.inactivity_timeout_s (default 300 s) --
#: so a later "retype that" could inject something sensitive long after the
#: user walked away. 20 s, not the auditor's 10 s: recovery by voice is two
#: utterances (refocus the window, then "retype that"), each needing speech,
#: an end-of-utterance pause and a decode (~4-5 s apiece), so 10 s would refuse
#: the very recovery the command exists for. Older items are refused and their
#: text is dropped from memory (see SessionModeManager._purge_expired_suppressed).
SUPPRESSED_RETYPE_TTL_S = 20.0


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
class CommandSuggestionCandidate:
    """A safe, canonical phrase offered after a command-lane miss."""

    phrase: str
    risk: str = "ui"


@dataclass(frozen=True)
class CommandDispatchResult:
    """matched: a command CLAIMED the utterance (any state but miss).
    state: the command_registry.DispatchState value -- "completed", "queued",
    "matched", "failed", "rejected", "cancelled" or "miss". None means a
    legacy dispatch fn that only knows matched (treated as completed)."""

    matched: bool
    phrase: Optional[str] = None
    state: Optional[str] = None
    # Held by the execution policy for a spoken yes/no: nothing has run.
    awaiting_confirmation: bool = False
    # A miss because the utterance is exactly a phrase of this disabled pack.
    disabled_pack: Optional[str] = None
    detail: dict = field(default_factory=dict)


#: Claimed-but-not-carried-out command states: never dictation, never a
#: new model request, and nothing to push on the undo stack.
COMMAND_UNSUCCESSFUL_STATES = frozenset({"failed", "rejected", "cancelled"})


def _command_state(result: "CommandDispatchResult") -> str:
    state = getattr(result, "state", None)
    state = getattr(state, "value", state)
    if state:
        return str(state)
    return "completed" if result.matched else "miss"


class PendingTextPolicy(Enum):
    """How a hands-free command interacts with staged dictation."""

    PRESERVE = "preserve"  # scrolling/overlays: leave pending text untouched
    COMMIT = "commit"      # text-field effects: paste first, block on failure


@dataclass(frozen=True)
class HandsFreeCommandMatch:
    """A side-effect-free command probe result for the combined lane."""

    dispatch_text: str
    phrase: str
    pending_policy: PendingTextPolicy = PendingTextPolicy.PRESERVE
    # Queue 69: matched from the lane's curated everyday words (submit, enter,
    # next field, focus ...) rather than the generic whole-utterance fallback.
    reserved: bool = False


@dataclass(frozen=True)
class DispatchOutcome:
    kind: str
    # one of: "empty" | "abort" | "scratch_success" | "scratch_refuse" |
    # "mode_switch" | "mode_switch_failed" | "prefix_switch_failed" | "command_executed" (it ran,
    # or its handler accepted the work) | "command_awaiting_confirmation" (held
    # for a yes/no; nothing ran) |
    # "command_failed" | "stopped" | "pending_reply" |
    # "command_miss" | "dictate_injected" | "dictate_suppressed_focus_lock" |
    # "dictate_staged" | "dictate_committed" |
    # "dictate_commit_refused" | "dictate_commit_blocked_focus_lock" |
    # "dictate_commit_failed" | "hands_free_command_executed" |
    # "hands_free_command_refused" | "hands_free_command_blocked" |
    # "hands_free_command_failed" |
    # "dictate_draft_cleared" |
    # "dictate_clear_nothing" |
    # "dictate_clear_refused" (queue 80: clear the whole staged draft) |
    # "dictate_draft_recovered" | "dictate_recover_nothing" (queue 84: put
    # back the draft an abort or a confirmed clear set aside) |
    # "command_cancel_window" (queue 69: recognised in the dictation lane and
    # staged behind a cancel window; nothing has run yet) |
    # "edit_thinking" (queue 102: the model was asked, nothing staged yet) |
    # "edit_proposed" (queue 102: a rewrite is staged, nothing applied) |
    # "edit_applied" | "edit_refused" | "edit_discarded" |
    # "ava_dispatched" | "ava_rejected_not_substantive" |
    # "ava_entry_failed" | "dictate_commit_unavailable" |
    # "dictate_blocked_elevated" (queue 50: foreground window runs at a higher
    # integrity level; nothing was typed, the text is retained)
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class InjectionDelivery:
    """Text sent by an injector, plus whether the target accepted it."""
    text: str
    confirmed: bool
    undo_unit: Any = None


from samsara.session_feedback import (
    CHIP_KINDS as CHIP_KINDS,
    CHIP_TTL_MS as CHIP_TTL_MS,
    _CHIP_TTL_OVERRIDES as _CHIP_TTL_OVERRIDES,
    COMMAND_CHIP_COPY as COMMAND_CHIP_COPY,
    _COMMAND_HEARD_MAX as _COMMAND_HEARD_MAX,
    _NO_CHIP as _NO_CHIP,
    _REASON_MAX as _REASON_MAX,
    CHIP_CHECK as CHIP_CHECK,
    CHIP_CROSS as CHIP_CROSS,
    CHIP_ARROW as CHIP_ARROW,
    CHIP_ELLIPSIS as CHIP_ELLIPSIS,
    CHIP_DASH as CHIP_DASH,
    _short as _short,
    _STOPPED_LABELS as _STOPPED_LABELS,
    _stopped_chip as _stopped_chip,
    _reason as _reason,
    _first_two_words as _first_two_words,
    _heard as _heard,
    _command_copy as _command_copy,
    _command_chip_label as _command_chip_label,
    _mode_label as _mode_label,
    _abort_chip as _abort_chip,
    _INTERACT_CHIPS as _INTERACT_CHIPS,
    outcome_chip as outcome_chip,
    is_mapped_outcome as is_mapped_outcome,
    chip_ttl_ms as chip_ttl_ms,
)

_COMMAND_SUGGESTION_TIMEOUT_S = 8.0


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
# InjectionDelivery carries the same text plus explicit target acknowledgement.
InjectFn = Callable[..., Union[bool, str, InjectionDelivery, None]]
RemoveCharsFn = Callable[[int], None]
CommandDispatchFn = Callable[[str], CommandDispatchResult]
CommandSuggestionFn = Callable[[str], Optional[CommandSuggestionCandidate]]
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

class SessionModeManager(UtteranceUndoMixin):
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
        command_suggestion_fn: Optional[CommandSuggestionFn] = None,
        foreground_hwnd_resolver: Optional[ForegroundHwndResolver] = None,
        format_dictate_fn: Optional[FormatDictateFn] = None,
        prepare_dictate_fn: Optional[FormatDictateFn] = None,
        inject_prepared_fn: Optional[InjectFn] = None,
        on_mode_change: Optional[Callable[[SessionMode], None]] = None,
        on_focus_lock_revert: Optional[Callable[[], None]] = None,
        on_scratch_result: Optional[Callable[[bool], None]] = None,
        on_abort: Optional[Callable[[], None]] = None,
        on_switch_dispatch_error: Optional[Callable[[Exception], None]] = None,
        commit_redecode_fn: Optional[Callable[[str, list], Optional[str]]] = None,
        buffer_dictate_until_commit: bool = False,
        hands_free_command_probe_fn: Optional[HandsFreeCommandProbeFn] = None,
        ava_invocations: Optional[list[str]] = None,
        ava_ready_probe_fn: Optional[Callable[[], object]] = None,
        pending_action_scratch_fn: Optional[Callable[[], Optional[bool]]] = None,
        clock: Callable[[], float] = time.time,
        extra_sleep_phrases: Optional[list[str]] = None,
        stop_fn: Optional[Callable[[str], Optional[dict]]] = None,
        stop_phrases: Optional[list[str]] = None,
        pending_reply_fn: Optional[Callable[[str], Optional[str]]] = None,
        window_integrity_fn: Optional[Callable[[], object]] = None,
        cancel_window_fn: Optional[Callable[[HandsFreeCommandMatch, Callable[[], None]], float]] = None,
        on_deferred_outcome: Optional[Callable[[DispatchOutcome], None]] = None,
        speak_fn: Optional[Callable[[str, str], None]] = None,
        fragment_clear_gap_s: float = 0.0,
        live_surface_enabled: bool = False,
    ) -> None:
        # Queue 80: speak_fn(text, category) says the clear-draft question and
        # its result out loud. dictation.py routes it to
        # audio_coordinator.speak with category "confirmation", which queue 58
        # exempts from command_mode.tts_char_limit. None = silent (tests).
        self._speak_fn = speak_fn
        # 220-F is deliberately inert until the unified surface is enabled.
        # This is captured when the manager is built, like the other session
        # policy inputs; Settings changes take effect at the next session.
        self._live_surface_enabled = bool(live_surface_enabled)
        self._parked_hold = False
        # Queue 103. The live value of SPOKEN_NOTICES_KEY. Defaults to the
        # schema default, so an app that never sets it gets the intended
        # "questions" behaviour and the incident is fixed with no wiring at
        # all. set_spoken_notices() is how the app and Settings keep it
        # current; a bad value is treated as the default by should_speak().
        self._spoken_notices = SPOKEN_NOTICES_QUESTIONS
        # One model-free command correction at a time. It shares the ordinary
        # pending-answer stage below, so a stray follow-up is never swallowed.
        self._pending_command_suggestion: Optional[dict] = None
        self._clipboard_choice = None
        # Queue 85: {"word", "occurrence", "expected_count", "expires", "source"}
        # while a word clicked in the preview is waiting for its replacement.
        self._pending_correction: Optional[dict] = None
        # Called with (wrong, right, context) once a replacement is applied, so
        # the capture can be queued for review. Never writes the dictionary.
        self._correction_capture_fn: Optional[Callable[[str, str, str], None]] = None
        # draft_scroll_fn(where): move the preview's own view. None = the
        # phrases stay ordinary dictation (no preview, nothing to scroll).
        self._draft_scroll_fn: Optional[Callable[[str], None]] = None
        self._surface_review_action_fn: Optional[Callable[[str, object], bool]] = None
        # correction_undo_fn(): take back the last correction (dictionary entry
        # first, else the newest un-reviewed capture). Returns a dict.
        self._correction_undo_fn: Optional[Callable[[], dict]] = None
        # Queue 69 cancel window. cancel_window_fn(match, run) decides whether a
        # command recognised in the dictation lane waits behind a cancel window;
        # when it stages one it returns the window in seconds (>0) and calls
        # run() later, at most once, from whichever thread resolves the window.
        # 0 = dispatch now, exactly as before. None = no windows at all.
        # on_deferred_outcome(outcome) receives the outcome of a deferred run.
        self._cancel_window_fn = cancel_window_fn
        self._on_deferred_outcome = on_deferred_outcome
        # Serialises utterance dispatch with deferred runs (a window elapsing
        # on its timer thread). Re-entrant: a window can be flushed from inside
        # a dispatch on the same thread.
        self._dispatch_lock = threading.RLock()
        # A temporary, domain-neutral interception point for captures that
        # own the whole utterance "finish" or "cancel" while they are active.
        # Callbacks return a DispatchOutcome when they consume the phrase, or
        # None to leave the existing session routing untouched.
        self._active_capture_interceptors = {}
        self._active_capture_interceptor_id = 0
        self._session_epoch = 0
        # 345c is an opt-in controller seam. dictation.py does not install it
        # until onset transport and persistent feedback are wired in 345d/e.
        self._interact_controller = None
        # window_integrity_fn(): an injection_safety.IntegrityVerdict for the
        # foreground window (queue 50). A `blocked` verdict (the window runs at
        # a higher integrity level, e.g. elevated/admin) means Windows will
        # silently drop anything typed into it, so DICTATE delivery refuses
        # with its own outcome instead of reporting "typed". A verdict that is
        # not `confirmed_ok` (unknown) still delivers but is reported as
        # unconfirmed, never as a plain success. None = no check (tests).
        self._window_integrity_fn = window_integrity_fn
        # stop_fn(reason): the execution stop (execution_policy.stop_all --
        # bump the generation FIRST, cancel model requests, drain queued
        # effects). Called for a whole-utterance "stop" and BEFORE a sleep
        # phrase ends the session. None = "stop" stays ordinary text (this
        # module cannot stop anything on its own and never pretends to).
        self._stop_fn = stop_fn
        # stop_phrases: config command_mode.stop_phrases REPLACES the built-in
        # list rather than extending it -- the owner has to be able to change
        # a word, not only add one. Anything unusable (empty, all blank, not
        # strings) falls back to SESSION_STOP_PHRASES: a bad config value must
        # not be a way to silently lose the emergency stop.
        self._stop_phrases = frozenset(
            normalize_utterance(p) for p in (stop_phrases or ()) if normalize_utterance(p)
        ) or frozenset(normalize_utterance(p) for p in SESSION_STOP_PHRASES)
        # pending_reply_fn(text): answer the live pending question
        # (execution_policy.answer_pending). Returns None when the text is not
        # a complete-utterance reply or nothing is pending.
        self._pending_reply_fn = bind_pending_reply(self, pending_reply_fn)
        self._abort_phrases = list(abort_phrases)
        # Sleep phrases (SESSION_SLEEP_PHRASES) match the WHOLE utterance
        # only and are never compiled into the anywhere-in-the-text abort
        # patterns below, even when a caller passes them in abort_phrases
        # (dictation.py passes all of GLOBAL_SESSION_EXIT_PHRASES).
        # extra_sleep_phrases: config command_mode.abort_phrases -- user-added
        # exits with the same whole-utterance, draft-retaining behaviour.
        self._sleep_phrases = frozenset(
            normalize_utterance(p)
            for p in (*SESSION_SLEEP_PHRASES, *(extra_sleep_phrases or []))
            if normalize_utterance(p)
        )
        # Queue 84: WHOLE-UTTERANCE equality, normalized once here. This was a
        # word-boundary regex searched anywhere in the utterance until
        # 2026-09-15, which is how "Have it stop listening to you, or
        # something like that." ended a session and destroyed a 477-character
        # draft. A phrase inside a sentence is now dictation; only the phrase
        # spoken alone aborts. See ABORT_PHRASES_ARE_WHOLE_UTTERANCE.
        self._abort_utterances = frozenset(
            normalize_utterance(p)
            for p in self._abort_phrases
            if normalize_utterance(p) and normalize_utterance(p) not in self._sleep_phrases
        )
        self._foreground_exe_resolver = foreground_exe_resolver
        self._foreground_hwnd_resolver = foreground_hwnd_resolver or (lambda: None)
        self._inject_fn = inject_fn
        self._format_dictate_fn = format_dictate_fn or (lambda t: t)
        self._prepare_dictate_fn = prepare_dictate_fn
        self._inject_prepared_fn = inject_prepared_fn
        self._remove_chars_fn = remove_chars_fn
        # Queue 102. inject_fn runs the FULL formatting pipeline
        # (process_transcription -> clean_text -> smart_correct -> trailing
        # space -> formatting tokens) and is therefore unusable for applying
        # or undoing an edit: it would re-format the model's rewrite on the
        # way in and re-format the original on the way back out, so "scratch
        # that" could not restore the original EXACTLY. The edit path needs a
        # raw, no-pipeline delivery instead. Set by samsara.ava_edit via
        # set_raw_inject_fn(); None means the edit path is simply unavailable
        # (fail-closed -- apply_edit and the edit_apply undo both refuse).
        self._raw_inject_fn = None
        self._command_dispatch_fn = command_dispatch_fn
        self._command_suggestion_fn = command_suggestion_fn
        self._agent_dispatch_fn = agent_dispatch_fn
        self._on_mode_change = on_mode_change
        self._on_focus_lock_revert = on_focus_lock_revert
        self._on_scratch_result = on_scratch_result
        self._on_abort = on_abort
        self._on_switch_dispatch_error = on_switch_dispatch_error
        self._commit_redecode_fn = commit_redecode_fn
        self._buffer_dictate_until_commit = buffer_dictate_until_commit
        self._hands_free_command_probe_fn = hands_free_command_probe_fn
        # Optional cheap "can Ava answer right now?" check, consulted
        # before any switch into AVA -- see _ava_entry_blocked_reason.
        # None means "assume ready" (preserves pre-2026-09-11 behavior
        # for callers that do not wire one, e.g. this module's tests).
        self._ava_ready_probe_fn = ava_ready_probe_fn
        # Pre-normalized once at construction, not per-utterance -- see
        # match_ava_invocation / _normalize_exact_phrase above.
        self._ava_invocations = frozenset(
            _normalize_exact_phrase(p)
            for p in (ava_invocations if ava_invocations is not None else DEFAULT_AVA_INVOCATIONS)
        )
        self._pending_action_scratch_fn = pending_action_scratch_fn
        self._clock = clock
        self._fragment_clear_gap_s = max(0.0, float(fragment_clear_gap_s))
        self._last_dictate_dispatch_at: Optional[float] = None

        self.mode: SessionMode = SessionMode.COMMAND
        self._stack = UnitOfWorkStack()
        self._dictate_target_process: Optional[str] = None
        self._dictate_target_hwnd: Optional[int] = None
        self._last_dictate_ended_terminal: Optional[bool] = None
        self._stage_buffer: str = ""
        self._draft = DraftDocument(clock=self._clock)
        self._dictate_pending_audio: list = []
        # A staged DICTATE draft set aside by a sleep phrase. Survives reset()
        # and is put back by the next reset() into the buffered DICTATE lane.
        self._retained_draft: Optional[dict] = None
        # Queue 84: a draft an abort or a confirmed clear would otherwise have
        # destroyed. Survives reset() like the retained draft, but is only put
        # back when the user asks (RECOVER_DRAFT_PHRASES), never automatically.
        self._recoverable_draft: Optional[dict] = None
        self._clear_draft_undo = ClearDraftUndo(self._clock, CLEAR_DRAFT_CONFIRM_TTL_S)
        # Queue 99: when the last resolved action was a scratch. Arms a bare
        # "again" (SCRATCH_AGAIN_TTL_S) and keeps a scratch run open so the
        # whole run is one undo. Cleared by ANY other resolved utterance.
        self._last_scratch_at: Optional[float] = None

    # -- session lifecycle -----------------------------------------------

    def reset(self, initial_mode: SessionMode = SessionMode.COMMAND) -> None:
        """Start a new session under the dispatch lock."""
        with self._dispatch_lock:
            self._reset_locked(initial_mode)

    def _reset_locked(self, initial_mode: SessionMode) -> None:
        """Discard all mode state and choose the new session's initial lane.

        The default remains COMMAND for legacy/direct callers. The latched
        toggle workflow explicitly starts in DICTATE, which is now its combined
        hands-free command+dictation lane.

        A draft retained by a sleep phrase (see retain_draft) is NOT
        discarded here: exit resets leave it set aside, and the next reset
        into the buffered DICTATE lane restores it as the pending thought.
        """
        if self._interact_controller is not None:
            self._interact_controller.exit("session reset")
        self.mode = initial_mode
        # A cancel window staged in the previous session never runs in this one.
        self._session_epoch += 1
        if self._interact_controller is not None:
            self._interact_controller.session_epoch_changed()
        self._stack = UnitOfWorkStack()
        self._dictate_target_process = None
        self._dictate_target_hwnd = None
        self._last_dictate_ended_terminal = None
        self._stage_buffer = ""
        # A parked hold is process-lifetime state.  Session/lane transitions
        # must not silently destroy or deliver it; explicit Clear/Commit are
        # the only operations that resolve the latch.
        if not (self._parked_hold and self._draft.text):
            self._draft.clear()
        self._dictate_pending_audio = []
        self._clear_draft_undo.drop()
        self._pending_command_suggestion = None
        self._clipboard_choice = None
        # Queue 99: a new session never inherits an armed "again".
        self._last_scratch_at = None
        self._scratch_redo = None
        self._last_dictate_dispatch_at = None
        if (self._retained_draft is not None
                and initial_mode is SessionMode.DICTATE
                and self._buffer_dictate_until_commit):
            self._restore_retained_draft()

    def retain_draft(self) -> int:
        """Set the staged-but-uncommitted DICTATE thought aside so the session
        can end without losing it. Returns the retained character count (0
        when nothing was staged; an earlier retained draft is kept then)."""
        if not self._dictate_pending_buffer:
            return len(self._retained_draft["buffer"]) if self._retained_draft else 0
        staged_items = [item for item in self._stack._items
                        if item.kind == "dictation_staged_chunk"]
        self._retained_draft = {
            "buffer": self._dictate_pending_buffer,
            "audio": list(self._dictate_pending_audio),
            "last_ended_terminal": self._last_dictate_ended_terminal,
            "stack_items": staged_items,
        }
        return len(self._dictate_pending_buffer)

    @property
    def retained_draft(self) -> str:
        """The draft a sleep phrase set aside ('' when none)."""
        return self._retained_draft["buffer"] if self._retained_draft else ""

    def discard_retained_draft(self) -> None:
        """Explicitly drop a retained draft -- the only way one is destroyed."""
        self._retained_draft = None

    # -- queue 84: recoverable draft ----------------------------------------

    def _stash_recoverable_draft(self, source: str) -> int:
        """Copy the staged draft into the recovery slot before something
        destroys it. Returns the character count (0 when nothing was staged,
        which leaves any older recoverable draft alone)."""
        buffer = self._dictate_pending_buffer
        # Queue 88: .strip(), not truthiness -- a buffer of whitespace must
        # never become a recoverable draft, or "bring back my draft" reports
        # success and puts nothing back.
        if not buffer.strip():
            return 0
        self._draft.stash_recoverable(source)
        self._recoverable_draft = {
            "audio": list(self._dictate_pending_audio),
            "last_ended_terminal": self._last_dictate_ended_terminal,
            "stack_items": [item for item in self._stack._items
                            if item.kind == "dictation_staged_chunk"],
            "source": source,
            "stashed_at": self._clock(),
        }
        log.info("[SESSION] %d-char draft kept for recovery after %s; say %r",
                 len(buffer), source, RECOVER_DRAFT_PHRASES[0])
        return len(buffer)

    @property
    def recoverable_draft(self) -> str:
        """The draft an abort or clear set aside, '' when there is none or it
        has expired."""
        return self._draft.recoverable_text

    def recover_draft(self) -> Optional[dict]:
        """Put a recoverable draft back into the staged buffer. Returns
        {"chars", "source", "prepended"} or None when there is nothing to
        recover. An expired slot is dropped rather than restored."""
        draft = self._recoverable_draft
        if draft is None:
            return None
        if self._clock() - draft["stashed_at"] > RECOVERABLE_DRAFT_TTL_S:
            self._recoverable_draft = None
            log.info("[SESSION] recoverable draft expired (%.0f s limit)", RECOVERABLE_DRAFT_TTL_S)
            return None
        if not self._draft.recoverable_text.strip():
            # Queue 88: nothing to put back is "nothing to bring back", never
            # a success. Belt and braces with _stash_recoverable_draft's own
            # guard, so a slot written by any future caller cannot lie either.
            self._recoverable_draft = None
            log.info("[SESSION] recoverable draft was blank; nothing to bring back")
            return None
        self._recoverable_draft = None
        existing = self._dictate_pending_buffer
        restored = self._draft.recover(prepend=bool(existing))
        if restored is None:
            return None
        if restored["prepended"]:
            # Chronological: what was lost came first. Nothing is overwritten.
            self._dictate_pending_audio = list(draft["audio"]) + list(self._dictate_pending_audio)
        elif not existing:
            self._dictate_pending_audio = list(draft["audio"])
            self._last_dictate_ended_terminal = draft["last_ended_terminal"]
            for item in draft["stack_items"]:
                self._stack.push(item)
        log.info("[SESSION] recovered %d-char draft (%s)", restored["chars"], draft["source"])
        return {"chars": restored["chars"], "source": draft["source"],
                "prepended": restored["prepended"]}

    def _restore_retained_draft(self) -> None:
        draft, self._retained_draft = self._retained_draft, None
        self._dictate_pending_buffer = draft["buffer"]
        self._dictate_pending_audio = list(draft["audio"])
        self._last_dictate_ended_terminal = draft["last_ended_terminal"]
        for item in draft["stack_items"]:
            self._stack.push(item)
        log.info("[SESSION] restored %d-char draft retained at sleep", len(draft["buffer"]))

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
    def draft_document(self) -> DraftDocument:
        """The authoritative pending document for later live-surface packages."""
        return self._draft

    @property
    def has_parked_hold(self) -> bool:
        """Whether the authoritative document is waiting for explicit send."""
        return self._parked_hold and bool(self._draft.text)

    def pause_draft(self, *, source: str = "pause") -> DispatchOutcome:
        """Latch manual delivery without making a second parking buffer.

        A key/button may arrive before the final decoder has produced text,
        so the latch intentionally survives an empty document and is applied
        to the next appended hold segment.
        """
        if not self._live_surface_enabled:
            return DispatchOutcome(kind="draft_pause_unavailable")
        with self._dispatch_lock:
            self.invalidate_interact("hold entry")
            self._parked_hold = True
            self._draft.mark_manual_commit()
            self._draft.kept_session = source == "session exit"
        return DispatchOutcome(kind="draft_parked", detail={"source": source,
                                                               "pending_chars": self._draft.chars})

    def stage_parked_hold(self, text: str, *, source: str = "hold") -> DispatchOutcome:
        """Append one settled hold result to the shared parked document.

        This is called only after the authoritative final decode.  It never
        logs the text, invokes a paste callback, or persists it to disk.
        """
        if not self._live_surface_enabled:
            return DispatchOutcome(kind="draft_pause_unavailable")
        text = str(text or "")
        with self._dispatch_lock:
            if text.strip():
                self._clear_draft_undo.drop()
            self.invalidate_interact("hold entry")
            self._parked_hold = True
            self._draft.mark_manual_commit()
            # Holds are separate utterances; decoder finals do not necessarily
            # include the trailing space used by the hands-free staging path.
            if text and self._draft.text and not self._draft.text[-1].isspace() and not text[0].isspace():
                text = " " + text
            segment_id = self._draft.append(text)
            if segment_id is not None:
                self._stack.push(StackItem(
                    kind="dictation_staged_chunk", payload=text,
                    mode=SessionMode.DICTATE, timestamp=self._clock(),
                    extra={"parked_hold": True, "source": source},
                ))
        return DispatchOutcome(kind="dictate_staged", detail={
            "pending_chars": self._draft.chars, "parked_hold": True,
            "source": source,
        })

    @property
    def _dictate_pending_buffer(self) -> str:
        return self._draft.text

    @_dictate_pending_buffer.setter
    def _dictate_pending_buffer(self, text: str) -> None:
        if text:
            self._draft.replace_text(text)
        else:
            self._draft.clear()

    @property
    def buffer_dictate_until_commit(self) -> bool:
        """Whether DICTATE stages text until an explicit commit phrase."""
        return self._buffer_dictate_until_commit

    def dictate_context_tail(self, max_chars: int = 200) -> str:
        """Return the tail of the active dictate context for session continuity.

        Buffered mode uses the staging buffer (which belongs to the current
        unsent thought); immediate mode uses the immediate stage buffer (which
        reflects committed DICTATE text). Other modes intentionally return
        empty context.
        """
        if self._buffer_dictate_until_commit:
            source = self._dictate_pending_buffer
        elif self.mode is SessionMode.DICTATE:
            source = self._stage_buffer
        else:
            return ""

        if not source:
            return ""
        if max_chars <= 0:
            return ""
        return source[-max_chars:]

    # -- dispatch -----------------------------------------------------------

    def register_active_capture_interceptor(self, callback):
        """Register a handler for exact dictation-commit/cancel utterances.

        The returned callable unregisters this handler. The callback receives
        ``(normalized, raw_text, signals)`` and returns a DispatchOutcome only
        when an active capture consumes the utterance; returning None preserves
        the ordinary routing path.
        """
        if not callable(callback):
            raise TypeError("active capture interceptor must be callable")
        self._active_capture_interceptor_id += 1
        token = self._active_capture_interceptor_id
        self._active_capture_interceptors[token] = callback

        def unregister():
            return self._active_capture_interceptors.pop(token, None) is not None

        return unregister

    def _intercept_active_capture(self, text, normalized, signals):
        # Keep this seam deliberately narrow: arbitrary dictated words never
        # reach an interceptor, and inactive captures can decline unchanged.
        if normalized not in ("finish", "cancel") and not is_dictate_commit(text):
            return None
        for callback in tuple(self._active_capture_interceptors.values()):
            try:
                outcome = callback(normalized, text, signals)
            except Exception as exc:
                log.warning("[SESSION] active capture interceptor failed: %s", exc)
                continue
            if isinstance(outcome, DispatchOutcome):
                return outcome
            if outcome is not None:
                log.warning("[SESSION] active capture interceptor returned an invalid outcome")
        return None

    def install_interact_controller(self, controller) -> None:
        """Install only after all capture, policy and visible feedback ports exist."""
        with self._dispatch_lock:
            if self._interact_controller is not None:
                self._interact_controller.exit("controller replaced")
            self._interact_controller = controller

    def configure_interact(self, *, targets, effects, app, publish,
                           present_question, clock_ns=None, schedule=None,
                           known_names=None):
        """Build the optional controller after app ports are ready (345e wiring)."""
        from samsara import execution_policy
        from samsara.interact import InteractController
        controller = InteractController(
            targets=targets, effects=effects, app=app,
            session_epoch=lambda: self._session_epoch,
            get_mode=lambda: self.mode, set_mode=self._interact_set_mode,
            policy_generation=lambda: execution_policy.current_generation(app),
            publish=publish, present_question=present_question,
            pending_probe=execution_policy.pending_operation,
            answer=lambda text: execution_policy.answer_pending(app, text),
            known_names=(known_names if known_names is not None else
                         set(getattr(targets, "aliases", None) or {})),
            command_mode=SessionMode.COMMAND,
            switch_match=lambda text: match_switch_word(text, current_mode=self.mode) is not None
            or match_ava_invocation(text, self._ava_invocations),
            gate=passes_switch_anti_hallucination_gate,
            transition_lock=self._dispatch_lock,
            normalize=normalize_utterance,
            input_owned=self._interact_input_owned,
            **({"clock_ns": clock_ns} if clock_ns is not None else {}),
            **({"schedule": schedule} if schedule is not None else {}),
        )
        self.install_interact_controller(controller)
        return controller

    def _interact_input_owned(self) -> bool:
        if (self._pending_command_suggestion is not None
                or self._pending_correction is not None):
            return True
        try:
            from plugins.commands import ask_ollama
            with ask_ollama._pending_action_lock:
                return ask_ollama._pending_action is not None
        except Exception:
            return True

    def invalidate_interact(self, reason: str = "capture lost") -> None:
        """Lifecycle hook for microphone loss, hold entry and shutdown."""
        with self._dispatch_lock:
            if self._interact_controller is not None:
                self._interact_controller.exit(reason)

    def interact_capture_view(self):
        controller = self._interact_controller
        return controller.capture_view() if controller is not None else None

    def _interact_set_mode(self, mode: SessionMode) -> bool:
        """Change capture lane without committing or clearing a DICTATE draft."""
        old = self.mode
        if mode is old:
            return True
        self.mode = mode
        try:
            if self._on_mode_change:
                self._on_mode_change(mode)
        except Exception:
            self.mode = old
            return False
        return True

    def dispatch_utterance(self, raw_text: str, signals: UtteranceSignals,
                           onset_context=None) -> DispatchOutcome:
        timing = _current_stage_timing()
        timing.mark("dispatch_start")
        with self._dispatch_lock:
            outcome = self._dispatch_utterance_locked(raw_text, signals, onset_context)
        timing.mark("action_done")
        return outcome

    def _dispatch_utterance_locked(self, raw_text: str, signals: UtteranceSignals,
                                   onset_context=None) -> DispatchOutcome:
        # Queue 50: suppressed dictation past its retype TTL leaves memory at
        # the next utterance, whatever that utterance is.
        self._purge_expired_suppressed()
        text = (raw_text or "").strip()
        if not text:
            return DispatchOutcome(kind="empty")
        from samsara.handsfree_wake_prefix import dispatch_wake_prefix
        wake = dispatch_wake_prefix(self, text, signals)
        if wake is not None:
            return wake
        if normalize_utterance(text) not in ('redo that',):
            self._scratch_redo = None
        # Queue 99: "again" means "scratch once more" only when the PREVIOUS
        # resolved action was a scratch. Disarm here and let the scratch paths
        # re-arm, so any other utterance -- dictated, command, switch, refused
        # -- clears it. The clock (SCRATCH_AGAIN_TTL_S) is the second guard;
        # this is the first, and it does not depend on timing at all.
        was_scratching = self._last_scratch_at
        self._last_scratch_at = None

        # 1. Global abort phrase -- always wins, every mode, and deliberately
        # checked BEFORE passes_switch_anti_hallucination_gate() below and
        # never subject to it: gating the abort path would mean degraded
        # audio (the exact condition the gate exists to distrust) could
        # leave a user stuck unable to escape a latched session -- deaf but
        # latched, the design's cardinal sin. Ordinary switches/scratch-that
        # still go through the gate, just not this.
        # 1a. Sleep: a whole-utterance exit from any lane, same precedence and
        # same no-gate rule as the abort phrases. The staged draft is set
        # aside BEFORE on_abort ends the session (whose reset() would
        # otherwise discard it) -- sleep stops capture, it never discards.
        normalized = normalize_utterance(text)

        active_capture = (None if self._interact_controller is not None
                          and self._interact_controller.state.phase != "OFF"
                          else self._intercept_active_capture(text, normalized, signals))
        if active_capture is not None:
            return active_capture

        # 0. Stop: same no-gate rule as abort. Generation first (inside
        # stop_fn), draft untouched, mode and microphone unchanged.
        if self._stop_fn is not None and normalized in self._stop_phrases:
            if self._interact_controller is not None:
                self._interact_controller.exit("stop")
            cleared = self._stop_fn("stop") or {}
            return DispatchOutcome(kind="stopped", detail={
                "draft_kept_chars": len(self._dictate_pending_buffer),
                "mode_retained": self.mode,
                "cleared": dict(cleared) if isinstance(cleared, dict) else {},
            })

        if normalized in self._sleep_phrases:
            self._clear_draft_undo.drop()
            if self._interact_controller is not None:
                self._interact_controller.exit("sleep")
            # Sleep = stop + disarm: the same stop runs FIRST, so nothing
            # captured before "go to sleep" can act while the session ends.
            stopped = False
            if self._stop_fn is not None:
                self._stop_fn("sleep")
                stopped = True
            retained = self.retain_draft()
            if self._on_abort:
                self._on_abort()
            return DispatchOutcome(kind="mode_switch", detail={
                "mode": "asleep", "sleep": True, "draft_retained_chars": retained,
                "stopped": stopped,
            })

        if self._matches_abort_phrase(text):
            self._clear_draft_undo.drop()
            if self._interact_controller is not None:
                self._interact_controller.exit("abort")
            # Queue 84: an abort ends the session without committing, but it
            # must never DESTROY the draft -- a misheard "cancel" will happen
            # eventually. The draft is copied aside first (on_abort's
            # exit_command_mode -> reset() clears the live buffer), and the
            # user is told how to get it back.
            recoverable = self._stash_recoverable_draft("cancel")
            if recoverable:
                self._speak(f"Cancelled. Your draft is kept. Say {RECOVER_DRAFT_PHRASES[0]} to get it back.",
                            ACKNOWLEDGEMENT)
            if self._on_abort:
                self._on_abort()
            return DispatchOutcome(kind="abort", detail={
                "recoverable_chars": recoverable,
                "recover_phrase": RECOVER_DRAFT_PHRASES[0],
            })

        controller = self._interact_controller
        if controller is not None:
            reply = controller.handle(text, signals, onset_context)
            if reply is not None:
                return DispatchOutcome(kind=reply.kind, detail={"message": reply.message})

        # 1b-85. A word clicked in the live preview is waiting for its
        # replacement: this utterance is it, not dictation. Checked after
        # stop/sleep/abort (all of which still win) and
        # before the lane reads the words.
        correction = self._consume_word_correction(text, normalized, signals)
        if correction is not None:
            return correction

        # 220-G: these phrases are only meaningful while the opt-in review
        # surface has registered its scoped picker.  They never become global
        # commands and a literal session without that surface still dictates.
        if self._live_surface_enabled and self._surface_review_action_fn is not None:
            match = re.fullmatch(r"correct\s+(.+)", normalized)
            number = re.fullmatch(r"word\s+(\d+)", normalized)
            if match is None and number is None:
                handled = False
            else:
                action = "correct" if match is not None else "word"
                value = match.group(1) if match is not None else int(number.group(1))
                try:
                    handled = bool(self._surface_review_action_fn(action, value))
                except Exception as exc:
                    log.warning("[SESSION] surface review action failed: %s", exc)
                    handled = False
            if handled:
                return DispatchOutcome(kind="surface_review_action")

        # 1c-85. "forget that correction": undo the last one by voice. Checked
        # before the lane reads the words, whole-utterance, and gated like the
        # other control phrases.
        if self._correction_undo_fn is not None and is_forget_correction(text):
            if passes_switch_anti_hallucination_gate(signals):
                return self._undo_last_correction_outcome()
            log.info("[SESSION] forget-correction phrase refused by the gate for %r", describe_text(text))

        # 1d-85. Scroll the preview so the whole draft can be read back by
        # voice. Non-destructive, whole-utterance, and only while there is a
        # preview to scroll.
        if (self._draft_scroll_fn is not None
                and self._buffer_dictate_until_commit
                and self.mode is SessionMode.DICTATE):
            where = match_draft_scroll(text)
            if where is not None:
                try:
                    self._draft_scroll_fn(where)
                except Exception as exc:
                    log.warning("[SESSION] draft scroll failed: %s", exc)
                return DispatchOutcome(kind="draft_scrolled", detail={"where": where})

        # 776: only an explicit, gated reply to this draft's image refusal.
        clipboard_reply = clipboard_recovery.answer(
            self, text, allowed=passes_switch_anti_hallucination_gate(signals))
        if clipboard_reply is not None:
            return clipboard_reply

        # 1b. A complete-utterance answer to THE pending question has priority
        # over every lane's interpretation ("yes" / "no" / "wait").
        if self._pending_reply_fn is not None:
            answer = self._pending_reply_fn(text)
            if answer is not None:
                return DispatchOutcome(kind="pending_reply", detail={"answer": answer})

        suggestion_reply = self._answer_pending_command_suggestion(text)
        if suggestion_reply is not None:
            return suggestion_reply

        # 1c-84. "bring back my draft": put back what an abort or a confirmed
        # clear set aside. Checked before the lane reads the words, and gated
        # like the clear phrase -- on a gate failure it is ordinary dictation.
        if (self._buffer_dictate_until_commit
                and self.mode is SessionMode.DICTATE
                and is_recover_draft(text) and not (normalized in CLEAR_RESTORE_PHRASES and self._clear_draft_undo.pending)):
            if passes_switch_anti_hallucination_gate(signals):
                return self._recover_draft_outcome()
            log.info("[SESSION] recover-draft phrase refused by the anti-hallucination gate for %r; "
                     "treating as ordinary dictation", describe_text(text))

        if (self._buffer_dictate_until_commit
                and self.mode is SessionMode.DICTATE
                and normalized in CLEAR_RESTORE_PHRASES
                and passes_switch_anti_hallucination_gate(signals)):
            restored = self._clear_draft_undo.restore(self)
            return DispatchOutcome(kind="dictate_draft_recovered" if restored else "dictate_recover_nothing",
                                   detail={"recovered_chars": restored, "source": "voice clear"})

        if (self._buffer_dictate_until_commit
                and self.mode is SessionMode.DICTATE
                and is_clear_draft(text)):
            if passes_switch_anti_hallucination_gate(signals):
                return self._request_clear_draft()
            log.info("[SESSION] clear-draft phrase refused by the anti-hallucination gate for %r; "
                     "treating as ordinary dictation", describe_text(text))

        # 220-F: an exact, separately-spoken control phrase parks the shared
        # document.  It is deliberately unavailable in literal mode and does
        # not try to infer a phrase embedded in ordinary prose; ambiguous
        # terminal speech remains reviewable text rather than being stripped.
        if (self._live_surface_enabled
                and self.mode is SessionMode.DICTATE
                and normalized == "pause draft"
                and match_literal_payload(text) is None):
            if passes_switch_anti_hallucination_gate(signals):
                return self.pause_draft(source="voice")
            return DispatchOutcome(kind="draft_pause_refused", detail={
                "pending_chars": self._draft.chars,
            })

        # Queue 99: counted scratch and "again", checked with the rest of the
        # scratch family and BEFORE the single scratch, so "scratch that
        # twice" is never read as a bare "scratch that" with stray words. Both
        # go through the same anti-hallucination gate every control word uses;
        # on a gate failure they fall through and are dictated, exactly as
        # "scratch that" does.
        if normalize_utterance(text) == 'redo that':
            if passes_switch_anti_hallucination_gate(signals):
                return self._redo_scratch_outcome()
        counted = match_scratch_count(text)
        if counted is None and is_scratch_again(text):
            self._last_scratch_at = was_scratching      # read the PREVIOUS turn
            if self._scratch_again_armed():
                counted = 1
            self._last_scratch_at = None
        if counted is not None:
            if passes_switch_anti_hallucination_gate(signals):
                return self._scratch_counted_outcome(counted)
            log.info("[SESSION] counted-scratch phrase refused by the anti-hallucination gate "
                     "for %r; treating as ordinary dictation", describe_text(text))

        scratch = is_scratch_that(text) or normalize_utterance(text) == 'undo that'
        commit = (
            self._buffer_dictate_until_commit
            and self.mode is SessionMode.DICTATE
            and is_dictate_commit(text)
        )
        trailing_commit_body = None
        if (not (scratch or commit)
                and self._buffer_dictate_until_commit
                and self.mode is SessionMode.DICTATE):
            trailing_commit_body = split_trailing_dictate_commit(text)
            if trailing_commit_body is not None:
                if passes_dictate_commit_gate(signals, has_pending_text=True):
                    return self._stage_and_commit_trailing(trailing_commit_body, signals)
                # Fail closed for the control reading only: the chunk is
                # staged as prose, exactly as before this path existed.
                log.info("[SESSION] trailing commit word refused by the commit gate for %r; "
                         "staging it as dictation", describe_text(text))
        if (entry := dispatch_dictate_hands_free_entry(self, text, signals)): return entry
        switch = None if (scratch or commit) else match_switch_word(text, current_mode=self.mode)
        if (switch is None and not (scratch or commit)
                and match_ava_invocation(text, self._ava_invocations)):
            switch = SwitchMatch(target_mode=SessionMode.AVA)

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
                    # Unified "scratch that" (Ava Front Door spec v2,
                    # "Confirmation binding"): a pending staged action
                    # (ask_ollama._pending_action, shared with D1/D3) is
                    # cancelled FIRST if one exists; only when nothing is
                    # pending does this fall through to the ordinary
                    # dictation-commit stack pop below. Wired in via
                    # pending_action_scratch_fn so this module -- pure
                    # orchestration, no ask_ollama/audio/Qt imports --
                    # never needs to know what a "pending action" is.
                    pending_result = (
                        self._pending_action_scratch_fn()
                        if self._pending_action_scratch_fn is not None
                        else None
                    )
                    ok = (pending_result if pending_result is not None else
                          bool(self._do_scratch_that_n(1)['scratched']))
                    if self._on_scratch_result:
                        self._on_scratch_result(ok)
                    if ok:
                        # Queue 99: arms a following "again". A bare scratch
                        # does NOT stash -- its behaviour is unchanged -- so an
                        # "again" after it opens the run and stashes then.
                        self._last_scratch_at = self._clock()
                    return DispatchOutcome(kind="scratch_success" if ok else "scratch_refuse",
                        detail={} if ok else {"message": delivery_verify.scratch_message(self)})
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
                      "treating as ordinary %s text", describe_text(text), self.mode.value)

        # Combined hands-free lane: window commands retain buffered dictation;
        # explicit text-field metadata alone permits paste before the effect.
        if (self._buffer_dictate_until_commit
                and self.mode is SessionMode.DICTATE):
            fragment_guard = (
                self._fragment_clear_gap_s > 0
                and self._last_dictate_dispatch_at is not None
                and self._clock() - self._last_dictate_dispatch_at < self._fragment_clear_gap_s
                and bool(self._dictate_pending_buffer.strip())
                and not chunk_ends_terminal(self._dictate_pending_buffer)
            )
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
                    from samsara.command_catalog import hands_free_media_phrases
                    if fragment_guard and hands_free_match.dispatch_text not in hands_free_media_phrases():
                        log.info("[SESSION] paced fragment kept as dictation: %r", describe_text(text))
                        return self._dispatch_dictate(text)
                    if not passes_switch_anti_hallucination_gate(signals):
                        return DispatchOutcome(kind="hands_free_command_refused", detail={
                            "phrase": hands_free_match.phrase,
                            "pending_chars": len(self._dictate_pending_buffer),
                        })
                    # 2026-09-11: the registry resolved this utterance to the
                    # Ava front door (ask_ollama registers "hey ava" with the
                    # alias "ava", so bare "Ava." lands here). That is a MODE
                    # SWITCH, not a command to dispatch: dispatching it ran
                    # handle_ask_ava with an empty remainder, which spoke
                    # "Yes? How can I help?" and returned None -- reported
                    # back as hands_free_command_failed -- while the COMMIT
                    # pending-text policy had already pasted the staged
                    # thought as a side effect of the failure. Routing to the
                    # same _do_switch() every other switch word uses makes
                    # both Ava routes land identically.
                    if match_ava_invocation(hands_free_match.phrase, self._ava_invocations):
                        return self._do_switch(SwitchMatch(target_mode=SessionMode.AVA))
                    return self._dispatch_hands_free_command(hands_free_match)

        outcome = self._dispatch_in_mode(text, signals=signals)
        if self._buffer_dictate_until_commit and self.mode is SessionMode.DICTATE:
            self._last_dictate_dispatch_at = self._clock()
        return outcome

    # -- queue 80: clear the whole staged draft ----------------------------

    @property
    def spoken_notices(self) -> str:
        """Which classes of notice are spoken. See SPOKEN_NOTICES_KEY."""
        return self._spoken_notices

    def set_spoken_notices(self, value: str) -> str:
        """Apply the configured value. Accepts the raw config value; anything
        unrecognised falls back to the default rather than raising, because a
        hand-edited config.json must not be able to break dispatch. Returns
        what was actually set."""
        text = str(value or "").strip().lower()
        self._spoken_notices = (
            SPOKEN_NOTICES_EVERYTHING if text == SPOKEN_NOTICES_EVERYTHING
            else SPOKEN_NOTICES_QUESTIONS
        )
        return self._spoken_notices

    def _speak(self, text: str, notice_class: str = ACKNOWLEDGEMENT) -> None:
        """Say `text`, if this class of notice is spoken at the current
        setting. See should_speak(). Every call site names its class
        explicitly; the ACKNOWLEDGEMENT default is the safe one, because a
        new notice added without thinking about it is silent rather than
        unexpectedly loud."""
        if self._speak_fn is None:
            return
        if not should_speak(notice_class, self.spoken_notices):
            log.debug("[SESSION] notice not spoken (%s at %r): %s",
                      notice_class, self.spoken_notices, describe_text(text))
            return
        try:
            self._speak_fn(text, "confirmation")
        except Exception as exc:  # speech must never break dispatch
            log.warning("[SESSION] clear-draft speech failed: %s", exc)

    def _scratch_counted_outcome(self, count: int) -> DispatchOutcome:
        """ONE outcome, ONE chip, for however many chunks came off.

        The kinds are the existing scratch_success / scratch_refuse -- a
        counted scratch is the same event, not a new one, so every consumer
        (the outcome chip, the earcon via on_scratch_result, the history row)
        keeps working without knowing counts exist. The numbers ride in the
        detail dict, where outcome_chip() turns them into the honest label.
        """
        result = self._do_scratch_that_n(count)
        scratched = result["scratched"]
        if self._on_scratch_result:
            self._on_scratch_result(bool(scratched))
        detail = {
            "requested": result["requested"],
            "scratched": scratched,
            "reason": result["reason"],
        }
        if not scratched:
            detail["message"] = delivery_verify.scratch_message(self)
        return DispatchOutcome(
            kind="scratch_success" if scratched else "scratch_refuse", detail=detail)

    def _request_clear_draft(self) -> DispatchOutcome:
        """Clear by voice immediately, with a separate short-lived take-back."""
        with self._dispatch_lock:
            if not self._dictate_pending_buffer.strip():
                return DispatchOutcome(kind="dictate_clear_nothing")
            self._clear_draft_undo.arm(self)
            removed = self.clear_pending_draft()
        self._speak("Draft cleared", ACKNOWLEDGEMENT)  # Stand-in: copy.clear_draft.spoken_full.
        return DispatchOutcome(kind="dictate_draft_cleared", detail={
            "cleared_chars": removed, "recoverable_chars": removed, "voice_clear": True,
        })

    def _recover_draft_outcome(self) -> DispatchOutcome:
        """Queue 84: the spoken recovery path for an aborted or cleared draft."""
        recovered = self.recover_draft()
        if recovered is None:
            self._speak("There is no draft to bring back.", ACKNOWLEDGEMENT)
            return DispatchOutcome(kind="dictate_recover_nothing")
        words = len(self._dictate_pending_buffer.split())
        noun = "word" if words == 1 else "words"
        self._speak(f"Draft back, {words} {noun}. Say end to type it.", ACKNOWLEDGEMENT)
        return DispatchOutcome(kind="dictate_draft_recovered", detail={
            "recovered_chars": recovered["chars"], "source": recovered["source"],
            "prepended": recovered["prepended"],
            "pending_chars": len(self._dictate_pending_buffer),
        })

    def confirm_clear_draft(self, source: str = "scratch everything") -> DispatchOutcome:
        """Legacy click/quit clear; keep its existing recovery and feedback."""
        with self._dispatch_lock:
            self._clear_draft_undo.drop()
            self._stash_recoverable_draft(source)
            removed = self.clear_pending_draft()
        log.info("[SESSION] draft cleared via %s (%d chars, recoverable)", source, removed)
        self._speak(f"Draft cleared. Say {RECOVER_DRAFT_PHRASES[0]} if you want it back.",
                    ACKNOWLEDGEMENT)
        return DispatchOutcome(kind="dictate_draft_cleared", detail={
            "cleared_chars": removed, "recoverable_chars": removed, "source": source,
        })

    def clear_pending_draft(self) -> int:
        """Discard the staged-but-uncommitted DICTATE draft (text, audio and the
        staged-chunk undo entries). Returns the number of characters removed.
        Nothing is pasted or typed."""
        with self._dispatch_lock:
            removed = len(self._dictate_pending_buffer)
            self._clipboard_choice = None
            self._dictate_pending_buffer = ""
            self._dictate_pending_audio = []
            self._last_dictate_ended_terminal = None
            kept = [item for item in self._stack._items if item.kind != "dictation_staged_chunk"]
            self._stack._items.clear()
            self._stack._items.extend(kept)
            self._parked_hold = False
        log.info("[SESSION] staged draft cleared (%d chars)", removed)
        return removed

    # -- queue 85: correct one word of the staged draft ---------------------

    def set_draft_scroll_fn(self, fn) -> None:
        """fn(where): "up" | "down" | "top" | "bottom" on the live preview."""
        self._draft_scroll_fn = fn

    def set_surface_review_action_fn(self, fn) -> None:
        """Route scoped correction-picker phrases to the opt-in surface."""
        self._surface_review_action_fn = fn

    def set_correction_undo_fn(self, fn) -> None:
        """fn() -> dict: take back the last correction. See correction_queue."""
        self._correction_undo_fn = fn

    def _undo_last_correction_outcome(self) -> DispatchOutcome:
        from samsara.session_corrections import _undo_last_correction_outcome
        return _undo_last_correction_outcome(self)

    def set_correction_capture_fn(self, fn) -> None:
        """fn(wrong, right, context, audio): queue an applied correction for REVIEW.
        Nothing here ever writes the corrections dictionary."""
        self._correction_capture_fn = fn

    def request_word_correction(self, word: str, occurrence: int = 0, *,
                                expected_count: int = 0,
                                source: str = "preview_click") -> dict:
        from samsara.session_corrections import request_word_correction
        return request_word_correction(self, word, occurrence, expected_count=expected_count, source=source)

    def select_live_surface_word(self, selection: object) -> dict:
        from samsara.session_corrections import select_live_surface_word
        return select_live_surface_word(self, selection)

    def apply_live_surface_correction(self, selection: object, replacement: str) -> dict:
        from samsara.session_corrections import apply_live_surface_correction
        return apply_live_surface_correction(self, selection, replacement)

    def copy_sent_receipt_as_draft(self, text: str) -> dict:
        from samsara.session_corrections import copy_sent_receipt_as_draft
        return copy_sent_receipt_as_draft(self, text)

    def pending_word_correction(self) -> Optional[dict]:
        from samsara.session_corrections import pending_word_correction
        return pending_word_correction(self)

    def cancel_word_correction(self, reason: str = "cancelled") -> bool:
        from samsara.session_corrections import cancel_word_correction
        return cancel_word_correction(self, reason)

    def _consume_word_correction(self, text: str, normalized: str,
                                 signals: UtteranceSignals) -> Optional[DispatchOutcome]:
        from samsara.session_corrections import _consume_word_correction
        return _consume_word_correction(self, text, normalized, signals)

    def choose_word_correction(self, replacement: str) -> DispatchOutcome:
        from samsara.session_corrections import choose_word_correction
        return choose_word_correction(self, replacement)

    def _apply_pending_word_correction(self, pending: dict, replacement: str) -> DispatchOutcome:
        from samsara.session_corrections import _apply_pending_word_correction
        return _apply_pending_word_correction(self, pending, replacement)

    def replace_draft_word(self, word: str, occurrence: int = 0, replacement: str = "",
                           *, expected_count: int = 0) -> dict:
        from samsara.session_corrections import replace_draft_word
        return replace_draft_word(self, word, occurrence, replacement, expected_count=expected_count)

    def _matches_abort_phrase(self, text: str) -> bool:
        """Any session exit, as the WHOLE utterance (queue 84): an abort
        phrase or a sleep phrase. dispatch_utterance checks sleep first (it
        has its own outcome); the streaming preview's _is_control_phrase
        relies on this covering both."""
        normalized = normalize_utterance(text)
        return normalized in self._sleep_phrases or normalized in self._abort_utterances

    def _stage_and_commit_trailing(
        self, body: str, signals: UtteranceSignals,
    ) -> DispatchOutcome:
        """"<prose>. End." in one chunk: stage the prose, then commit it as if
        "end" had been its own utterance. The commit word never reaches the
        draft, and the commit re-decode (which hears the same audio) has it
        stripped from its tail."""
        staged = self._stage_dictate_chunk(body, audio_ref=signals.audio_ref)
        if staged.kind != "dictate_staged":
            return staged
        outcome = self._commit_dictate_buffer(target_mode=None, consumed_trailing_commit_word=True)
        if outcome.kind == "dictate_committed":
            return DispatchOutcome(kind="dictate_committed", detail={**outcome.detail, "commit_word": "trailing"})
        return outcome

    def commit_pending_dictation(self) -> DispatchOutcome:
        """Commit buffered DICTATE immediately for a trusted local trigger.

        Keyboard/UI callers do not need speech hallucination gating, but they
        must reuse the same transactional focus/paste path as spoken ``end``.
        """
        if (not self._parked_hold
                and (not self._buffer_dictate_until_commit or self.mode is not SessionMode.DICTATE)):
            return DispatchOutcome(kind="dictate_commit_unavailable", detail={
                "mode": self.mode,
                "buffered": self._buffer_dictate_until_commit,
            })
        self._draft.mark_manual_commit()
        return self._commit_dictate_buffer(target_mode=None)

    def _ava_entry_blocked_reason(self) -> Optional[str]:
        """Why AVA entry cannot complete right now, or None if it can.

        2026-09-11: entering AVA used to be unconditional, so a session whose
        agent could not answer (plugin disabled, Ollama down, dispatch fn
        never wired) switched anyway and then failed per-utterance with
        nothing but an earcon -- the user had no way to tell "Ava is off"
        from "Ava didn't hear me". Checked BEFORE the mode changes so a
        refusal leaves the user where they were."""
        if self._agent_dispatch_fn is None:
            return "no agent dispatch function is wired"
        if self._ava_ready_probe_fn is None:
            return None
        try:
            ready = self._ava_ready_probe_fn()
        except Exception as exc:
            return f"readiness probe raised {type(exc).__name__}: {exc}"
        if ready is None or ready is True:
            return None
        if isinstance(ready, str):
            return ready
        return "the agent backend reported it is not ready"

    def _do_switch(self, switch: SwitchMatch) -> DispatchOutcome:
        """Transactional: a prefix switch ("dictate <payload>") only counts
        as having happened if the payload actually got dispatched. If
        dispatch raises (injection failure, a command handler blowing up,
        an agent-dispatch error), the mode is reverted to whatever it was
        before this switch and the failure is surfaced audibly -- otherwise
        the user would be silently left in a new mode with nothing
        delivered and no indication anything went wrong."""
        prior_mode = self.mode
        if switch.target_mode is SessionMode.AVA:
            blocked = self._ava_entry_blocked_reason()
            if blocked is not None:
                log.warning(
                    "[SESSION] AVA entry refused: %s -- staying in %s mode",
                    blocked, prior_mode.value,
                )
                return DispatchOutcome(kind="ava_entry_failed", detail={
                    "reason": blocked, "mode_retained": prior_mode,
                })
        if (self._buffer_dictate_until_commit
                and prior_mode is SessionMode.DICTATE
                and switch.target_mode is not SessionMode.DICTATE
                and self._dictate_pending_buffer
                and not self._parked_hold):
            committed = self._commit_dictate_buffer(target_mode=None)
            if committed.kind != "dictate_committed":
                return committed
        mode_change_error = self._switch_mode(switch.target_mode)
        if mode_change_error is not None:
            return DispatchOutcome(
                kind="mode_switch_failed",
                detail={
                    "mode": switch.target_mode,
                    "reverted_to": prior_mode,
                    "error": mode_change_error,
                },
            )
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

    def _switch_mode(self, new_mode: SessionMode) -> Optional[str]:
        if self._interact_controller is not None:
            self._interact_controller.exit("lane switch")
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
            self._stage_buffer = ""
            # Kept drafts need explicit send/clear, just as on session reset.
            if not (self._parked_hold and self._draft.text):
                self._last_dictate_ended_terminal = None
                self._dictate_pending_buffer = ""
                self._dictate_pending_audio = []
        self.mode = new_mode
        self._last_mode_change_error = None
        if self._on_mode_change:
            # The callback drives the visible lane badge and preview. It is
            # part of the mode transaction: retaining the new routing state
            # after it fails can leave DICTATE on screen while COMMAND owns
            # the next utterance. Re-present the prior lane after rollback so
            # an error after a queued target update cannot leave the badge stale.
            try:
                self._on_mode_change(new_mode)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.mode = prior_mode
                self._last_mode_change_error = None
                log.exception("[SESSION] mode change %s -> %s failed; reverted to %s",
                              prior_mode.value, new_mode.value, prior_mode.value)
                try:
                    self._on_mode_change(prior_mode)
                except Exception:
                    log.exception("[SESSION] mode rollback presentation failed for %s",
                                  prior_mode.value)
                if self._on_switch_dispatch_error:
                    try:
                        self._on_switch_dispatch_error(exc)
                    except Exception:
                        log.exception("[SESSION] mode change error feedback failed")
                return error
        return None

    def force_mode(self, new_mode: SessionMode) -> None:
        """Apply a non-utterance-driven mode change."""
        if self._interact_controller is not None:
            self._interact_controller.exit("forced lane switch")
        self._switch_mode(new_mode)

    def _dispatch_in_mode(
        self, text: str, signals: Optional[UtteranceSignals] = None,
    ) -> DispatchOutcome:
        from samsara.numbered_picks import dispatch_session_pick
        from samsara.ava_edit import intent, session as edits
        app = getattr(self._agent_dispatch_fn, "__self__", None)
        if app is not None and passes_switch_anti_hallucination_gate(signals) and (intent.draft_cleanup_scope(text) is not None
                                or (edits.pending() is not None and
                                    (intent.is_apply_utterance(text) or intent.is_discard_utterance(text)))):
            if edits.handle_utterance(app, text):
                return DispatchOutcome(kind="ava_edit_handled")
        if self.mode is SessionMode.COMMAND:
            return self._dispatch_command(text)
        if self.mode is SessionMode.DICTATE:
            return dispatch_session_pick(self, text, signals) or self._dispatch_dictate(text, audio_ref=getattr(signals, 'audio_ref', None))
        if self.mode is SessionMode.AVA:
            return dispatch_session_pick(self, text, signals) or self._dispatch_ava(text)
        raise AssertionError(f"unhandled SessionMode {self.mode!r}")  # pragma: no cover

    def _answer_pending_command_suggestion(self, text: str) -> Optional[DispatchOutcome]:
        """Answer the command lane's existing pending-answer stage.

        This is deliberately before lane dispatch but after an execution-policy
        reply: a real staged action keeps ownership of its confirmation.  A
        non-answer simply drops the suggestion and proceeds normally, so a
        new command is never eaten by a stale "did you mean" prompt.
        """
        pending = self._pending_command_suggestion
        if pending is None:
            return None
        if self._clock() > pending["expires"]:
            self._pending_command_suggestion = None
            return None
        try:
            from samsara.execution_policy import REPLY_NO, REPLY_YES, classify_reply
            reply = classify_reply(text)
        except Exception:  # pragma: no cover - policy import is always available in app
            return None
        if reply == REPLY_NO:
            self._pending_command_suggestion = None
            return DispatchOutcome(kind="command_suggestion_declined", detail={
                "heard": pending["heard"], "phrase": pending["phrase"],
            })
        if reply != REPLY_YES:
            self._pending_command_suggestion = None
            return None
        self._pending_command_suggestion = None
        # The only fuzzy step was the offer. The accepted phrase is passed to
        # the ordinary command dispatcher as a literal whole utterance.
        outcome = self._dispatch_command(
            pending["phrase"], heard=pending["heard"], allow_suggestion=False,
        )
        outcome.detail["suggested"] = pending["phrase"]
        return outcome

    def _offer_command_suggestion(self, text: str) -> Optional[DispatchOutcome]:
        if self._command_suggestion_fn is None:
            return None
        try:
            candidate = self._command_suggestion_fn(text)
        except Exception as exc:
            log.warning("[SESSION] command suggestion failed: %s", exc)
            return None
        if candidate is None or str(getattr(candidate, "risk", "")) == "destructive":
            return None
        phrase = " ".join(str(getattr(candidate, "phrase", "")).split())
        if not phrase:
            return None
        self._pending_command_suggestion = {
            "heard": _short(text, _COMMAND_HEARD_MAX),
            "phrase": phrase,
            "expires": self._clock() + _COMMAND_SUGGESTION_TIMEOUT_S,
        }
        return DispatchOutcome(kind="command_suggestion_pending", detail={
            "heard": self._pending_command_suggestion["heard"], "phrase": phrase,
        })

    def dispatch_command_utterance(self, text: str) -> DispatchOutcome:
        """Run a complete command-lane utterance without session switches.

        The wake one-command window uses this same pending-answer and command
        dispatcher path as a latched COMMAND session. It deliberately does
        not examine switch or dictation phrases.
        """
        with self._dispatch_lock:
            if self._pending_reply_fn is not None:
                answer = self._pending_reply_fn(text)
                if answer is not None:
                    return DispatchOutcome(kind="pending_reply", detail={"answer": answer})
            answer = self._answer_pending_command_suggestion(text)
            return answer if answer is not None else self._dispatch_command(text)

    def _dispatch_command(
        self, text: str, *, heard: Optional[str] = None, allow_suggestion: bool = True,
    ) -> DispatchOutcome:
        from samsara.bug_report.routing import dispatch_command
        return dispatch_command(self, text, heard=heard, allow_suggestion=allow_suggestion)

    def _dispatch_hands_free_command(
        self, match: HandsFreeCommandMatch, *, deferred: bool = False,
    ) -> DispatchOutcome:
        """Execute one exact command using its draft policy; retain the lane."""
        if not deferred and self._cancel_window_fn is not None:
            # Queue 69: the window wraps the whole command dispatch. A cancel
            # leaves the draft staged and no command effect runs.
            epoch = self._session_epoch
            delay = self._cancel_window_fn(match, lambda: self._run_deferred_hands_free(match, epoch))
            if delay and delay > 0:
                return DispatchOutcome(kind="command_cancel_window", detail={
                    "phrase": match.phrase,
                    "dispatch_text": match.dispatch_text,
                    "heard": _short(match.dispatch_text, _COMMAND_HEARD_MAX),
                    "delay_s": float(delay),
                    "pending_chars": len(self._dictate_pending_buffer),
                    "mode_retained": self.mode,
                })
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
        state = _command_state(result)
        if not result.matched or state in COMMAND_UNSUCCESSFUL_STATES:
            return DispatchOutcome(kind="hands_free_command_failed", detail={
                **getattr(result, "detail", {}),
                "phrase": match.phrase,
                "dispatch_text": match.dispatch_text,
                "heard": _short(match.dispatch_text, _COMMAND_HEARD_MAX),
                "committed": commit_detail,
                "state": state,
            })

        if getattr(result, "awaiting_confirmation", False):
            return DispatchOutcome(kind="command_awaiting_confirmation", detail={
                "phrase": result.phrase or match.phrase,
                "dispatch_text": match.dispatch_text,
                "heard": _short(match.dispatch_text, _COMMAND_HEARD_MAX),
                "committed": commit_detail,
                "mode_retained": self.mode,
                "state": state,
            })

        self._stack.push(StackItem(
            kind="command", payload=result.phrase or match.phrase,
            mode=SessionMode.DICTATE, timestamp=self._clock(),
        ))
        return DispatchOutcome(kind="hands_free_command_executed", detail={
            **getattr(result, "detail", {}),
            "phrase": result.phrase or match.phrase,
            "dispatch_text": match.dispatch_text,
            "committed": commit_detail,
            "mode_retained": self.mode,
            "state": state,
        })

    def _run_deferred_hands_free(self, match: HandsFreeCommandMatch, epoch: int) -> DispatchOutcome:
        """A cancel window elapsed (or was answered "yes"): dispatch the command
        it held, unless the session it was staged in is gone."""
        with self._dispatch_lock:
            if epoch != self._session_epoch or self.mode is not SessionMode.DICTATE:
                log.info("[SESSION] deferred %r not run: the session or lane changed", match.phrase)
                outcome = DispatchOutcome(kind="hands_free_command_refused", detail={
                    "phrase": match.phrase, "reason": "session changed",
                    "pending_chars": len(self._dictate_pending_buffer),
                })
            else:
                outcome = self._dispatch_hands_free_command(match, deferred=True)
        log.info("[SESSION] deferred outcome=%s phrase=%r", outcome.kind, match.phrase)
        if self._on_deferred_outcome is not None:
            try:
                self._on_deferred_outcome(outcome)
            except Exception:
                log.exception("[SESSION] deferred outcome handler failed")
        return outcome

    def _dispatch_dictate(
        self, chunk_raw: str, audio_ref: Any = None,
    ) -> DispatchOutcome:
        delivery = getattr(self, '_hands_free_delivery', None)
        if delivery is not None and delivery.enabled(self):
            return delivery.deliver(self, chunk_raw, audio_ref)
        if self._buffer_dictate_until_commit:
            return self._stage_dictate_chunk(chunk_raw, audio_ref=audio_ref)

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

        verdict = self._foreground_verdict()
        if verdict is not None and getattr(verdict, "blocked", False):
            # Same as a focus-lock suppression: keep the formatted chunk for a
            # later "retype that", type nothing, and say why.
            self._stack.push(StackItem(
                kind="dictation_chunk", payload=self._format_dictate_fn(chunk_raw.strip()),
                mode=SessionMode.DICTATE, timestamp=self._clock(),
                extra={"suppressed": True, "target_process": self._dictate_target_process,
                       "foreground": foreground, "hwnd": self._foreground_hwnd_resolver(),
                       "blocked_elevated": True},
            ))
            log.warning("[SESSION] DICTATE chunk not typed: foreground window is elevated (%s)",
                        self._describe_verdict(verdict))
            return DispatchOutcome(kind="dictate_blocked_elevated", detail={
                "stage": "inject", "target_process": target_process,
                "integrity": self._describe_verdict(verdict),
            })

        if self._last_dictate_ended_terminal is None:
            adjusted = chunk_raw.strip()
            to_inject = adjusted
        else:
            if _is_continuation_chunk(chunk_raw):
                # Continuations from pause-bounded DICTATE are lower-case-start
                # and must not mutate already-typed punctuation or force any
                # retro-editing in immediate-inject mode.
                adjusted = chunk_raw.strip()
                to_inject = " " + adjusted
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

    def _stage_dictate_chunk(
        self, chunk_raw: str, audio_ref: Any = None,
    ) -> DispatchOutcome:
        """Prepare a chunk before it enters the displayed draft."""
        prepared = chunk_raw
        if self._prepare_dictate_fn is not None:
            try:
                prepared = self._prepare_dictate_fn(chunk_raw.strip())
            except Exception as exc:
                log.warning('[SESSION] draft preparation failed; previous draft retained (%s)', describe_text(exc))
                return DispatchOutcome(kind="dictate_commit_failed", detail={"pending_chars": self._draft.chars})
            if not prepared:
                return DispatchOutcome(kind="empty")
        stripped_suffix = ""
        ellipsis_seam = None
        if self._last_dictate_ended_terminal is not None:
            top = self._stack.peek()
            previous_fragment = (
                top.payload if top is not None and top.kind == "dictation_staged_chunk" else None
            )
            ellipsis_seam = decide_ellipsis_seam(
                self._dictate_pending_buffer, chunk_raw, previous_fragment)
        if self._last_dictate_ended_terminal is None:
            to_stage = prepared.strip()
            stripped_terminal_period = False
        elif ellipsis_seam is not None:
            # Queue 81: the previous fragment trailed off. Join only on the
            # clauses decide_ellipsis_seam lists; otherwise it is a new
            # sentence and the ellipsis is the speaker's, kept as spoken.
            stripped_terminal_period = False
            if ellipsis_seam.join:
                stripped_suffix = ellipsis_seam.stripped
                self._dictate_pending_buffer = self._dictate_pending_buffer[:-len(stripped_suffix)]
            # Remove decoder-leading dots; preserve a spoken trailing ellipsis.
            chunk_body = prepared
            if ellipsis_seam.lead_stripped:
                chunk_body = prepared[len(ellipsis_seam.lead_stripped):]
            to_stage = " " + chunk_body.strip()
            log.debug("[SESSION] DICTATE seam after ellipsis: %s join=%s lead_stripped=%r",
                      ellipsis_seam.clause, ellipsis_seam.join, ellipsis_seam.lead_stripped)
        else:
            if _is_continuation_chunk(chunk_raw):
                # Continuations strip one prior full stop, preserving !/?.
                stripped_terminal_period = False
                if self._dictate_pending_buffer.rstrip().endswith("."):
                    self._dictate_pending_buffer = self._dictate_pending_buffer.rstrip()[:-1]
                    stripped_terminal_period = True
                continuation = prepared.strip()
                if (chunk_raw[:1].islower() and continuation
                        and continuation[0].lower() == chunk_raw[0]):
                    continuation = continuation[0].lower() + continuation[1:]
                to_stage = " " + continuation
            else:
                stripped_terminal_period = False
                to_stage = " " + seam_join(self._last_dictate_ended_terminal, prepared)

        if not to_stage:
            return DispatchOutcome(kind="empty")

        self._clear_draft_undo.drop()
        self._dictate_pending_audio.append(audio_ref)
        models = {key: value for key, value in getattr(self, '_dictate_audio_models', {}).items()
                  if value[0]() is not None}
        owner = getattr(self._commit_redecode_fn, '__self__', None)
        try:
            models[id(audio_ref)] = (weakref.ref(audio_ref), weakref.ref(owner.model))
        except (AttributeError, TypeError):
            pass  # Missing provenance keeps the optional re-decode path.
        self._dictate_audio_models = models
        self._draft.append(to_stage)
        self._draft.delivery_prepared = self._prepare_dictate_fn is not None
        self._last_dictate_ended_terminal = chunk_ends_terminal(to_stage)
        self._stack.push(StackItem(
            kind="dictation_staged_chunk", payload=to_stage,
            mode=SessionMode.DICTATE, timestamp=self._clock(),
            extra={"stripped_terminal_period": stripped_terminal_period,
                   "stripped_suffix": stripped_suffix},
        ))
        return DispatchOutcome(kind="dictate_staged", detail={
            "text": to_stage,
            "pending_chars": len(self._dictate_pending_buffer),
        })

    def _commit_dictate_buffer(
        self, *, target_mode: Optional[SessionMode],
        consumed_trailing_commit_word: bool = False,
    ) -> DispatchOutcome:
        """Paste the complete staged thought once, retaining it on failure."""
        self._draft.mark_manual_commit()
        text = self._dictate_pending_buffer
        if not text:
            self._dictate_pending_audio = []
            if target_mode is not None:
                self._switch_mode(target_mode)
            else:
                self._dictate_target_process = None
                self._dictate_target_hwnd = None
            return DispatchOutcome(kind="dictate_committed", detail={
                "text": "", "empty": True, "mode_retained": self.mode,
                **({'immediate': True} if getattr(self, '_hands_free_delivery', None)
                   and self._hands_free_delivery.enabled(self) else {}),
            })

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

        verdict = self._foreground_verdict()
        if verdict is not None and getattr(verdict, "blocked", False):
            log.warning(
                "[SESSION] DICTATE commit retained: the foreground window runs at a higher "
                "integrity level (administrator) and would silently drop the text (%s)",
                self._describe_verdict(verdict),
            )
            return DispatchOutcome(kind="dictate_blocked_elevated", detail={
                "stage": "commit", "pending_chars": len(text), "target_process": foreground,
                "integrity": self._describe_verdict(verdict),
            })
        delivery_unverified = verdict is not None and not getattr(verdict, "confirmed_ok", False)

        final_text = text
        exact_draft = self._inject_prepared_fn is not None and (
            getattr(self._draft, 'delivery_prepared', False) or self._parked_hold)
        if not exact_draft and "  " in final_text:
            final_text = re.sub(r" {2,}", " ", final_text)
        owner = getattr(self._commit_redecode_fn, '__self__', None)
        refs = self._dictate_pending_audio
        models = getattr(self, '_dictate_audio_models', {})
        try:
            stable_chunks = bool(refs) and all(
                ref is not None and len(ref) / owner.model_rate >= 1.5
                and models[id(ref)][0]() is ref and models[id(ref)][1]() is owner.model
                for ref in refs)
        except (AttributeError, KeyError, TypeError, ZeroDivisionError):
            stable_chunks = False
        if stable_chunks:
            log.info('[SESSION] Skipping commit re-decode: same_model_no_short_fragments')
        if (not exact_draft and not stable_chunks and self._commit_redecode_fn is not None and not self._draft.edited
                and not self._parked_hold):
            try:
                redecode_text = self._commit_redecode_fn(
                    final_text, list(self._dictate_pending_audio),
                )
                if redecode_text and consumed_trailing_commit_word:
                    redecode_text = strip_redecoded_commit_word(redecode_text)
                if redecode_text:
                    if redecode_is_poorer(final_text, redecode_text):
                        log.warning(
                            "[SESSION] DICTATE commit kept the staged text: the re-decode is a "
                            "run-on (%.4f sentence marks/word over %d chars vs %.4f staged)",
                            sentence_mark_density(redecode_text), len(redecode_text),
                            sentence_mark_density(final_text),
                        )
                    else:
                        final_text = redecode_text
            except Exception as exc:
                log.warning(
                    "[SESSION] DICTATE commit re-decode failed; using staged text "
                    "(error=%r)",
                    exc,
                    exc_info=True,
                )

        # A displayed draft is immutable at delivery, including clipboard retries.
        delivered = clipboard_recovery.inject_commit(
            self, final_text, commit_target_still_focused, target_mode,
            inject_fn=self._inject_prepared_fn if exact_draft else None)
        if delivered is False:
            return clipboard_recovery.failed_commit(self, text, foreground, current_hwnd)

        # Record the delivered bytes for undo; legacy callbacks retain text.
        delivery_confirmed = True
        if isinstance(delivered, InjectionDelivery):
            final_text = delivered.text
            delivery_confirmed = delivered.confirmed
        elif isinstance(delivered, str):
            final_text = delivered
        if not delivery_confirmed:
            delivery_unverified = True
            log.warning("[SESSION] DICTATE Ctrl+V shortcut sent without target acknowledgement; "
                        "scratch-that is not armed")

        self._dictate_pending_buffer = ""
        self._dictate_pending_audio = []
        self._parked_hold = False
        self._stage_buffer = final_text
        self._last_dictate_ended_terminal = None
        if not delivery_unverified:
            self._stack.push(StackItem(
                kind="dictation_chunk", payload=final_text, mode=SessionMode.DICTATE,
                timestamp=self._clock(),
                extra={"target_process": self._dictate_target_process,
                       "hwnd": current_hwnd,
                       "undo_unit": delivered.undo_unit if isinstance(delivered, InjectionDelivery) else None},
            ))
        # Release the completed thought's focus lock, retaining DICTATE.
        self._dictate_target_process = None
        self._dictate_target_hwnd = None
        if target_mode is not None:
            self._switch_mode(target_mode)
        detail = {"text": final_text, "chars": len(final_text), "mode_retained": self.mode}
        if delivery_unverified:
            # Could not confirm the window accepts typing: the text was sent,
            # but this must never read as a plain "typed" success.
            detail["delivery_unverified"] = True
            detail["integrity"] = self._describe_verdict(verdict)
            if not delivery_confirmed:
                detail["delivery"] = "shortcut_sent_without_target_acknowledgement"
        clipboard_recovery.complete_commit(self, detail)
        return DispatchOutcome(kind="dictate_committed", detail=detail)

    def _dispatch_ava(self, text: str) -> DispatchOutcome:
        """Send substantive Ava turns after global controls and draft edits.

        Only explicit stage references attach and clear committed context.
        Dispatch is fire-and-forget: handoff defines 'sent', not model success.
        Agent exchanges never enter the scratch-that stack.
        """
        if not is_substantive_utterance(text):
            owner = getattr(self._agent_dispatch_fn, "__self__", None)
            follow_up = False
            if owner is not None:
                from plugins.commands.dispatch_voice import awaiting_follow_up
                follow_up = awaiting_follow_up(owner, text)
            if not follow_up:
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
        """Undo staged edits locally or remove a verified external insertion.

        External undo requires an owned-range receipt in production, plus
        independent exe-name and HWND checks. Mismatches refuse deletion.
        Legacy injected callbacks remain available to embedders.
        """
        top = self._stack.peek()
        if top is not None and top.kind == "draft_edit_apply":
            before = top.extra["document"]
            if self._draft.document_id != before.document_id or self._draft.text != top.payload:
                return False
            before.revision = self._draft.revision + 1
            self._draft = before
            self._stack.pop()
            return True
        # Immediate scratch uses only confirmed external undo; parked drafts stay local.
        delivery = getattr(self, '_hands_free_delivery', None)
        immediate = self.mode is SessionMode.DICTATE and delivery is not None and delivery.enabled(self)
        if immediate and not delivery.scratch_ready(self):
            return False
        if self._parked_hold and not immediate:
            operation = self._draft.newest_operation
            if operation and self._draft.undo():
                if operation == "append":
                    top = self._stack.peek()
                    if top is not None and top.kind == "dictation_staged_chunk":
                        self._stack.pop()
                    if self._dictate_pending_audio:
                        self._dictate_pending_audio.pop()
                return True
            return False
        item = self._stack.peek()
        if item is None:
            return False
        if item.kind == "dictation_staged_chunk":
            if not self._dictate_pending_buffer.endswith(item.payload):
                return False
            self._dictate_pending_buffer = self._dictate_pending_buffer[:-len(item.payload)]
            if self._dictate_pending_audio:
                self._dictate_pending_audio.pop()
            if item.extra.get("stripped_suffix"):
                self._dictate_pending_buffer += item.extra["stripped_suffix"]
            elif item.extra.get("stripped_terminal_period"):
                if not self._dictate_pending_buffer.endswith("."):
                    self._dictate_pending_buffer += "."
            self._last_dictate_ended_terminal = (
                chunk_ends_terminal(self._dictate_pending_buffer)
                if self._dictate_pending_buffer else None
            )
            self._stack.pop()
            return True
        if item.kind == "edit_apply" and getattr(self, '_require_unit_undo', False):
            return False  # No verified replacement receipt for legacy edits.
        if item.kind == "edit_apply":
            return self._undo_edit_apply(item)
        if item.kind != "dictation_chunk":
            self._stack.pop()
            return False  # command undo out of scope; deliberately consumed
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
        # remove_chars_fn may report the deletion did not complete (False):
        # focus changed mid-way, the window is elevated, or input was
        # refused. None (legacy callables) keeps meaning "done".
        unit = item.extra.get('undo_unit')
        if unit is not None:
            removed = unit.change()
        elif item.extra.get('readback_required') or getattr(self, '_require_unit_undo', False):
            return False
        else:
            removed = self._remove_chars_fn(len(item.payload))
        if removed is False:
            return False
        self._stack.pop()
        # A preceding owned insertion can now become newest again, only if
        # there was no user input between the two original deliveries.
        previous = self._stack.peek()
        previous = previous.extra.get('undo_unit') if previous is not None else None
        if unit is not None and previous is not None:
            previous.expected = unit.expected if previous.expected == unit.before else None
        return True

    # -- edit apply (queue 102) ----------------------------------------------

    def set_raw_inject_fn(self, fn) -> None:
        """Install the RAW (no formatting pipeline) delivery callable the
        edit path needs. See the _raw_inject_fn comment in __init__ for why
        _inject_fn cannot be used here. Passing None disables the edit path."""
        self._raw_inject_fn = fn

    def top_dictation_chunk(self) -> "Optional[StackItem]":
        """The most recent committed dictation chunk, or None.

        This -- not the history store, and not stage_buffer -- is what an
        edit edits. stage_buffer holds the same characters (both are set to
        `final_text` when a thought commits), but only the stack item also
        carries `target_process` and `hwnd`: the window identity that makes a
        destructive apply safe. An edit with no window to apply it to is not
        an edit anyone can accept.
        """
        item = self._stack.peek()
        if item is None or item.kind != "dictation_chunk" or not item.payload:
            return None
        return item

    def apply_edit(self, rewritten: str, *, before_delete_s: float = 0.0,
                   after_delete_s: float = 0.0, dwell_fn=None) -> dict:
        """Replace the most recent dictation chunk with `rewritten`, as ONE
        unit of work that ONE "scratch that" reverses exactly.

        Returns {"applied": bool, "reason": str, "chars": int}.

        Atomicity, and the honest limit of it. The stack is mutated only
        AFTER the window has accepted both halves of the change, so every
        refusal below leaves the text and the stack exactly as they were and
        "scratch that" still means the original chunk. The one case this
        cannot make atomic is an OS-level deletion that stops part-way (the
        user alt-tabs mid-backspace, the target refuses input): the
        characters already gone are gone. That is the same exposure
        _do_scratch_that has carried since queue 50 -- inherent to driving
        another process's text box by keystrokes, not something this path
        adds -- and it is reported as "the deletion was interrupted" rather
        than dressed up as a success. The deletion is ONE call, so the window
        for it is as small as the primitive allows.

        The two focus checks are the same fail-closed pair _do_scratch_that
        uses, in the same order, for the same reason: applying an edit when
        the user has moved on would rewrite content in a window that is not
        theirs.
        """
        item = self.top_dictation_chunk()
        if item is None:
            return {"applied": False, "reason": "nothing to edit", "chars": 0}
        if self._raw_inject_fn is None or self._remove_chars_fn is None:
            return {"applied": False, "reason": "the edit path is not wired", "chars": 0}
        original = item.payload
        if not isinstance(rewritten, str) or not rewritten:
            return {"applied": False, "reason": "nothing to apply", "chars": 0}
        if rewritten == original:
            return {"applied": False, "reason": "no change", "chars": 0}

        foreground = self._foreground_exe_resolver()
        if not check_focus_lock(item.extra.get("target_process"), foreground):
            log.warning("[AVA-EDIT] apply refused: focus is not the window this text went to")
            return {"applied": False, "reason": "focus moved", "chars": 0}
        recorded_hwnd = item.extra.get("hwnd")
        if recorded_hwnd is None or self._foreground_hwnd_resolver() != recorded_hwnd:
            log.warning("[AVA-EDIT] apply refused: foreground window changed since this text "
                        "was dictated (recorded_hwnd=%r)", recorded_hwnd)
            return {"applied": False, "reason": "the window changed", "chars": 0}

        dwell = dwell_fn if dwell_fn is not None else time.sleep
        if before_delete_s:
            dwell(before_delete_s)
        if self._remove_chars_fn(len(original)) is False:
            log.error("[AVA-EDIT] apply stopped: the deletion was interrupted")
            return {"applied": False, "reason": "the deletion was interrupted", "chars": 0}
        if after_delete_s:
            dwell(after_delete_s)
        if self._raw_inject_fn(rewritten) is False:
            log.error("[AVA-EDIT] apply stopped: the rewrite was not delivered")
            return {"applied": False, "reason": "the rewrite was not delivered", "chars": 0}

        # One unit in, one unit out: the chunk this edit consumed leaves the
        # stack and the edit takes its place, carrying the original so the
        # undo needs nothing else. Depth is unchanged, so an edit never costs
        # the user one of their five scratches.
        self._stack.pop()
        self._stack.push(StackItem(
            kind="edit_apply", payload=rewritten, mode=item.mode, timestamp=self._clock(),
            extra={"original": original,
                   "target_process": item.extra.get("target_process"),
                   "hwnd": recorded_hwnd},
        ))
        self._stage_buffer = rewritten
        log.info("[AVA-EDIT] applied: %d chars -> %d chars", len(original), len(rewritten))
        return {"applied": True, "reason": "applied", "chars": len(rewritten)}

    def _undo_edit_apply(self, item: StackItem) -> bool:
        """A "scratch that" over an applied edit: take the rewrite out and
        put the original back, byte for byte.

        The original goes back through the RAW injector, never _inject_fn --
        the formatting pipeline would capitalise, clean and smart-correct it
        on the way in, and the user would not get their text back, they would
        get a new formatting of it.

        The original chunk is pushed back afterwards, so a second "scratch
        that" deletes it exactly as if the edit had never happened. That also
        makes "scratch that twice" mean what a user would expect: undo the
        edit, then take the sentence back.
        """
        if self._raw_inject_fn is None or self._remove_chars_fn is None:
            log.warning("[AVA-EDIT] cannot undo an applied edit: the edit path is not wired")
            return False
        original = item.extra.get("original")
        if not isinstance(original, str):
            return False
        foreground = self._foreground_exe_resolver()
        if not check_focus_lock(item.extra.get("target_process"), foreground):
            return False
        recorded_hwnd = item.extra.get("hwnd")
        if recorded_hwnd is None or self._foreground_hwnd_resolver() != recorded_hwnd:
            log.warning("[AVA-EDIT] undo refused: foreground window changed since the edit "
                        "was applied (recorded_hwnd=%r)", recorded_hwnd)
            return False
        if self._remove_chars_fn(len(item.payload)) is False:
            return False
        if self._raw_inject_fn(original) is False:
            return False
        self._stack.pop()
        self._stack.push(StackItem(
            kind="dictation_chunk", payload=original, mode=item.mode, timestamp=self._clock(),
            extra={"target_process": item.extra.get("target_process"), "hwnd": recorded_hwnd},
        ))
        self._stage_buffer = original
        log.info("[AVA-EDIT] edit undone: %d chars restored", len(original))
        return True

    def _scratch_again_armed(self) -> bool:
        """True when a bare "again" means "scratch once more": a scratch was
        the last resolved action AND it was recent. Any other utterance clears
        _last_scratch_at, so the clock is the second guard, not the first."""
        if self._last_scratch_at is None:
            return False
        return (self._clock() - self._last_scratch_at) <= SCRATCH_AGAIN_TTL_S

    def retype_last_suppressed(self) -> bool:
        """COMMAND-mode 'retype that': re-attempt the most recent DICTATE
        chunk that focus-lock suppressed, with a fresh focus-lock check.

        Refuses (queue 50) a chunk older than SUPPRESSED_RETYPE_TTL_S -- and
        drops its text -- and refuses when the foreground window is elevated."""
        now = self._clock()
        for item in self._stack.items_newest_first():
            if item.kind == "dictation_chunk" and item.extra.get("suppressed"):
                age = now - item.timestamp
                if age > SUPPRESSED_RETYPE_TTL_S:
                    log.info("[SESSION] retype refused: the suppressed dictation is %.0f s old "
                             "(limit %.0f s); its text has been discarded", age, SUPPRESSED_RETYPE_TTL_S)
                    self._purge_expired_suppressed(now)
                    return False
                foreground = self._foreground_exe_resolver()
                target = item.extra.get("target_process")
                if not check_focus_lock(target, foreground):
                    return False
                verdict = self._foreground_verdict()
                if verdict is not None and getattr(verdict, "blocked", False):
                    log.warning("[SESSION] retype refused: foreground window is elevated (%s)",
                                self._describe_verdict(verdict))
                    return False
                self._inject_fn(item.payload)
                item.extra["suppressed"] = False
                return True
        return False

    def _purge_expired_suppressed(self, now: Optional[float] = None) -> int:
        """Drop the text of every suppressed chunk older than the retype TTL,
        so it is not kept in memory for the rest of the session. Returns how
        many were purged."""
        now = self._clock() if now is None else now
        purged = 0
        for item in self._stack.items_newest_first():
            if (item.kind == "dictation_chunk" and item.extra.get("suppressed")
                    and now - item.timestamp > SUPPRESSED_RETYPE_TTL_S):
                item.payload = ""
                item.extra["suppressed"] = False
                item.extra["expired"] = True
                purged += 1
        return purged

    def _foreground_verdict(self):
        """The foreground integrity verdict, or None when no check is wired
        or the check itself raised (never blocks delivery on a crash)."""
        if self._window_integrity_fn is None:
            return None
        try:
            return self._window_integrity_fn()
        except Exception as exc:
            log.warning("[SESSION] window integrity check failed: %r", exc)
            return None

    @staticmethod
    def _describe_verdict(verdict) -> str:
        describe = getattr(verdict, "describe", None)
        return describe() if callable(describe) else repr(verdict)
