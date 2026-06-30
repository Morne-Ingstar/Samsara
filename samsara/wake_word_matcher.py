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

# Sentence punctuation that Whisper inserts mid-phrase (e.g. "hey, claude").
# Replaced with a space before matching so the literal find still works.
# Hyphen and apostrophe are intentionally excluded: they are word-internal
# per _INTERNAL_PUNCT and must not be treated as boundaries.
_PUNCT_TO_SPACE = re.compile(r"[,.;:!?]+")
_MULTI_SPACE    = re.compile(r" {2,}")


def _normalize_for_match(s):
    """Lowercase, convert sentence punctuation to spaces, collapse runs of spaces.

    'hey, claude.'  -> 'hey claude'
    'samsara-like'  -> 'samsara-like'   (hyphen preserved)
    "samsara's"     -> "samsara's"      (apostrophe preserved)
    """
    s = s.lower()
    s = _PUNCT_TO_SPACE.sub(" ", s)
    s = _MULTI_SPACE.sub(" ", s)
    return s.strip()


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
    Sentence punctuation (, . ; : ! ?) inside either argument is normalised to
    a single space before matching, so 'hey, claude' matches 'hey claude'.
    Hyphens and apostrophes are NOT normalised (they are word-internal per
    _INTERNAL_PUNCT), preserving the existing substring semantics for
    'samsara-like' and "samsara's".

    match_index refers to a position in the normalised text (not the original).
    Callers that slice for a command tail (e.g. corrected[match_index + len(phrase):])
    will get a slightly wrong result when the original contained mid-phrase
    punctuation — but this only matters for multi-word wake phrases with a trailing
    command, which no current caller relies on correctly in that edge case.
    """
    if not text or not phrase:
        return (False, "none", -1)

    text_norm   = _normalize_for_match(text)
    phrase_norm = _normalize_for_match(phrase)
    if not phrase_norm:
        return (False, "none", -1)

    # 1. Exact match (ignoring surrounding whitespace)
    if text_norm == phrase_norm:
        return (True, "exact", 0)

    # Find first occurrence of the phrase anywhere in the normalised text
    idx = text_norm.find(phrase_norm)
    if idx == -1:
        return (False, "none", -1)

    # Determine boundary characters on either side of the match
    before = text_norm[idx - 1] if idx > 0 else None
    end = idx + len(phrase_norm)
    after = text_norm[end] if end < len(text_norm) else None

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
