"""
Shared token-aware wake-word matching.

Used by:
  - dictation.py::process_wake_word_buffer   (the real pipeline)
  - samsara/ui/wake_word_debug.py            (the debug tool)

Contract:
    match_wake_phrase(text, phrase) -> (matched: bool, match_type: str, match_index: int)

Match types, ordered by preference:
    "exact"     : text.strip().lower() == phrase.lower()
    "prefix"    : phrase at start of text, followed by word boundary
    "suffix"    : phrase at end of text,   preceded by word boundary
    "token"     : phrase in middle, surrounded by word boundaries
    "substring" : phrase appears but is NOT token-bounded (REPORTED, but matched=False)
    "none"      : phrase not present at all

A "word boundary" here means start/end of string OR any non-alphanumeric character
(whitespace, punctuation). That makes "samsara," or "samsara." count as boundary-
ended, which matches how Whisper transcribes natural speech.

Critical behaviour: "samsara-like" / "samsara's" / "prosamsara" all return
matched=False with type="substring". This is intentional — pure substrings
should NOT trigger the wake word. The substring type is reported so debug
tooling can surface it.
"""

import re


_WORD_CHAR = re.compile(r"\w")

# Characters that, when adjacent to the phrase, should be treated as
# word-INTERNAL (not as boundaries). This makes "samsara-like", "samsara's",
# and similar compounds fall into the "substring" bucket (reported but not
# triggering), instead of being mistaken for prefix/suffix matches.
_INTERNAL_PUNCT = frozenset("-'")


def _is_boundary(ch):
    """True if *ch* is a word boundary character (non-word, or None for edge).

    Hyphens and apostrophes count as word-internal — "samsara-like" is one
    unit, not "samsara" next to "like".
    """
    if ch is None:
        return True
    if ch in _INTERNAL_PUNCT:
        return False
    return not _WORD_CHAR.match(ch)


def match_wake_phrase(text, phrase):
    """Check whether *phrase* appears in *text* at a word boundary.

    Returns (matched, match_type, match_index).
        matched      : True only for exact/prefix/suffix/token. False for substring/none.
        match_type   : one of "exact", "prefix", "suffix", "token", "substring", "none".
        match_index  : character position of the phrase in text_lower, or -1 if not present.

    Case-insensitive. Leading/trailing whitespace in text is tolerated.
    """
    if not text or not phrase:
        return (False, "none", -1)

    text_lower = text.lower()
    phrase_lower = phrase.lower().strip()
    if not phrase_lower:
        return (False, "none", -1)

    stripped = text_lower.strip()

    # 1. Exact match (ignoring surrounding whitespace)
    if stripped == phrase_lower:
        return (True, "exact", 0)

    # Find first occurrence of the phrase anywhere
    idx = text_lower.find(phrase_lower)
    if idx == -1:
        return (False, "none", -1)

    # Determine boundary characters on either side of the match
    before = text_lower[idx - 1] if idx > 0 else None
    end = idx + len(phrase_lower)
    after = text_lower[end] if end < len(text_lower) else None

    left_ok = _is_boundary(before)
    right_ok = _is_boundary(after)

    if not (left_ok and right_ok):
        # Present but not token-bounded — reported but not a trigger
        return (False, "substring", idx)

    # Token-bounded: classify by position
    at_start = (before is None)
    at_end = (after is None)

    if at_start and at_end:
        # Only reachable if phrase equals trimmed text but with interior whitespace
        # differences; treat as exact for consistency.
        return (True, "exact", idx)
    if at_start:
        return (True, "prefix", idx)
    if at_end:
        return (True, "suffix", idx)
    return (True, "token", idx)
