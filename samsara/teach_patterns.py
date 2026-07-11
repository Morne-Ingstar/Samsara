"""Voice-teaching patterns for the transcription VOCABULARY and CORRECTIONS
stores -- siblings to samsara/ava_corrections.py's TEACHING_PATTERNS /
parse_teaching / parse_forget, but targeting samsara/ui/voice_training_qt.py's
VoiceTrainingQt.custom_vocab / corrections_dict instead of ava_corrections.json.

Kept in ONE discoverable module (not folded into ask_ollama.py or
ava_corrections.py) so a future Quick Reference window can list every
supported voice-teaching phrasing from a single import.

Linguistic split from ava_corrections' existing patterns -- deliberately
DISTINCT trigger words, verified non-overlapping against every
ava_corrections/ava_profile pattern:
  - "I say X, I mean Y" / "X means Y"        -> Ava alias (ava_corrections.py, unchanged)
  - "correct X to Y" / "when you hear X write/type/use Y" -> correction (this module)
  - "add the word X to my vocabulary" / "learn the word X" -> vocabulary (this module)

ONE real collision was found during audit: ava_corrections.FORGET_PATTERNS'
generic `^forget (.+)$` would otherwise swallow "forget the word X" /
"forget the correction X" before this module's own, more specific forget
pattern ever got a chance to run. Resolved at the dispatch site
(plugins/commands/ask_ollama.py's _check_teaching_intent) by checking this
module's patterns BEFORE ava_corrections' checks -- see that function's
comment for the full reasoning.
"""
import re
import threading

from samsara.correction_capture import _MAX_PHRASE_WORDS, _is_case_only, _is_punctuation_only

VOCAB_ADD_PATTERNS = [
    re.compile(r'^add (?:the word )?(.+?) to (?:my )?(?:vocabulary|vocab|dictionary)$', re.IGNORECASE),
    re.compile(r'^learn the word (.+)$', re.IGNORECASE),
]

CORRECTION_ADD_PATTERNS = [
    re.compile(r'^correct (.+?) to (.+)$', re.IGNORECASE),
    re.compile(r'^when you hear (.+?) (?:write|type|use) (.+)$', re.IGNORECASE),
]

UNDO_PATTERN = re.compile(r'^undo that$', re.IGNORECASE)

FORGET_PATTERN = re.compile(r'^forget the (word|correction) (.+)$', re.IGNORECASE)


def _word_count_ok(phrase: str) -> bool:
    """Same 1-4 word bound as correction_capture.py's _MAX_PHRASE_WORDS --
    imported, not duplicated."""
    words = phrase.split()
    return 0 < len(words) <= _MAX_PHRASE_WORDS


def parse_vocab_add(text: str) -> "str | None":
    """Return the word/phrase to add, or None. Enforces the word-count
    bound up front so a mis-parsed sentence-length "word" never reaches
    the vocabulary store."""
    text = (text or "").strip()
    for pattern in VOCAB_ADD_PATTERNS:
        m = pattern.match(text)
        if m:
            word = m.group(1).strip()
            if word and _word_count_ok(word):
                return word
    return None


def parse_correction_add(text: str) -> "tuple[str, str] | None":
    """Return (wrong, right) if text is a correction-teaching command, else
    None. Word-count/atomicity validation happens in
    validate_correction_pair() below, not here -- matches
    ava_corrections.parse_teaching's own split of "parse" vs "validate"."""
    text = (text or "").strip()
    for pattern in CORRECTION_ADD_PATTERNS:
        m = pattern.match(text)
        if m:
            wrong = m.group(1).strip()
            right = m.group(2).strip()
            if wrong and right:
                return (wrong, right)
    return None


def parse_undo(text: str) -> bool:
    return bool(UNDO_PATTERN.match((text or "").strip()))


def parse_forget(text: str) -> "tuple[str, str] | None":
    """Return (kind, phrase) where kind is 'word' or 'correction', else
    None."""
    m = FORGET_PATTERN.match((text or "").strip())
    if m:
        return (m.group(1).lower(), m.group(2).strip())
    return None


def validate_correction_pair(wrong: str, right: str) -> "tuple[bool, str | None]":
    """Reuse correction_capture.py's ATOMIC-SUBSTITUTION RULE PREDICATES
    (case-only / punctuation-only / max-phrase-words) directly on a
    standalone (wrong, right) pair.

    Deliberately NOT calling correction_capture.extract_corrections()
    itself: that's a whole-SENTENCE diff extractor (original vs corrected
    full text), and empirically it rejects EVERY standalone short pair --
    e.g. extract_corrections("flat", "hat") returns rejected=[('flat',
    'hat', 'looks like a rewrite')] -- because its whole-text rewrite gate
    (matcher.ratio(), calibrated for a mostly-unchanged sentence with a
    small edited region) always fires when the ENTIRE short input differs
    with no common words. That gate doesn't apply to this module's input
    shape (a standalone pair, not a before/after sentence), so only the
    underlying per-span RULES are reused, not the diff machinery around
    them.

    Returns (True, None) or (False, reason)."""
    wrong, right = wrong.strip(), right.strip()
    if not wrong or not right:
        return False, "empty phrase"
    if wrong == right:
        return False, "identical phrase"
    if not _word_count_ok(wrong) or not _word_count_ok(right):
        return False, "too long -- looks like a rewrite"
    if _is_case_only(wrong, right):
        return False, "case-only difference"
    if _is_punctuation_only(wrong, right):
        return False, "punctuation-only difference"
    return True, None


# ── Last-action stack (in-memory only, THIS session -- "undo that") ──────────
#
# Deliberately not persisted: undo is a live safety net for a just-spoken
# teaching command, not a durable history. Only successful ADDs are
# recorded (per spec -- "undo that" reverses the most recent vocab OR
# correction add, not a forget).

_last_action_lock = threading.Lock()
_last_action = None


def record_last_action(kind: str, **data) -> None:
    global _last_action
    with _last_action_lock:
        _last_action = {'kind': kind, **data}


def pop_last_action() -> "dict | None":
    global _last_action
    with _last_action_lock:
        action = _last_action
        _last_action = None
        return action


def peek_last_action() -> "dict | None":
    """Read-only -- for tests; dispatch code should use pop_last_action()."""
    with _last_action_lock:
        return dict(_last_action) if _last_action is not None else None
