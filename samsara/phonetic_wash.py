"""
Phonetic wash -- fixes known Whisper misrecognitions of command phrases.

Runs AFTER transcription, BEFORE the CommandMatcher. This layer exists
because Whisper is a probabilistic model that outputs what it thinks
you PROBABLY said, not what you actually said. Common failures:

- Homophones: "find" -> "fine", "to" -> "two", "mic" -> "Mike"
- Compound splits: "github" -> "get hub", "newline" -> "new line"
- Punctuation injection: "refresh page" -> "refresh page."
- Symbol rendering: user says "period" -> Whisper outputs "."

The wake_corrections.py module handles wake WORD misrecognitions only
(e.g. "charvis" -> "jarvis"). This module handles everything AFTER
the wake word has been extracted.

Only add entries that have been observed as failures. Over-correcting
is worse than under-correcting -- every rule here is a potential false
positive on free-form dictation that shares the command pipeline.
"""

import re

# Multi-word phrase corrections. Key must appear as a contiguous substring
# of the cleaned text. Applied before per-word corrections, so "fine tab"
# (phrase) wins over "fine" -> "find" (word) followed by " tab".
_PHRASE_CORRECTIONS = {
    "fine tab": "find tab",
    "find the tab": "find tab",
    "get hub": "github",
    "go two": "go to",
    "go too": "go to",
    "switch two": "switch to",
    "switch apps": "switch app",
    "use mike": "use mic",
    "switch mike": "switch mic",
    "press he": "press e",
    "snap laugh": "snap left",
    "snap write": "snap right",
    "snap wright": "snap right",
    "volume app": "volume up",
    "play paws": "play pause",
    "play pours": "play pause",
    "play pies": "play pause",
    "school up": "scroll up",
    "school down": "scroll down",
    "school to top": "scroll to top",
    "school to bottom": "scroll to bottom",
    "browse two": "browse to",
    "new lying": "new line",
    "you line": "new line",
    "open crow": "open chrome",
    "open crone": "open chrome",
    "open krone": "open chrome",
    "open chrome,": "open chrome",
    "open chronicles": "open chrome",
    "open screen": "open chrome",
    "open crom": "open chrome",
    "open crumb": "open chrome",
}

# Single-word corrections applied token-by-token AFTER phrase corrections.
# Keep this list short -- each entry is a landmine for free-form dictation
# (e.g. "fine" -> "find" corrupts legitimate use of the word "fine").
# The trade-off is acceptable here because the wash runs on post-wake
# command text and on matcher input (where false matches are already the
# common case); the executor's dictation fallthrough uses the ORIGINAL text.
_WORD_CORRECTIONS = {
    "fine": "find",
    "mike": "mic",
    "tub": "tab",
    "tabs": "tab",
    "chrome's": "chrome",
    "screenshots": "screenshot",
    "clicks": "click",
}

# Symbols that Whisper renders instead of the spoken word. Only applied
# when the ENTIRE stripped input is a single symbol -- we don't want to
# rewrite end-of-sentence punctuation here (that's the job of the
# punctuation scrub below).
_SYMBOL_TO_WORD = {
    ".": "period",
    "..": "period",
    "...": "ellipsis",
    ",": "comma",
    "?": "question mark",
    "!": "exclamation mark",
    ":": "colon",
    ";": "semicolon",
    "'": "apostrophe",
    '"': "quote",
    "-": "hyphen",
    "(": "open parenthesis",
    ")": "close parenthesis",
    "[": "open bracket",
    "]": "close bracket",
}

_PUNCT_SCRUB_RE = re.compile(r'[.,?!;:]+')
_MULTISPACE_RE = re.compile(r'\s+')


def apply_phonetic_wash(text):
    """Clean Whisper's known phonetic hallucinations from command text.

    Args:
        text: raw command text AFTER wake word extraction and
              normalize_command_text (lowercase, stripped).

    Returns:
        cleaned text ready for CommandMatcher. Returns the input
        unchanged (including None / empty) when there's nothing to wash.
    """
    if not text or not isinstance(text, str):
        return text

    original = text
    raw_stripped = text.strip()

    # Symbol-to-word check FIRST on the raw input. The punctuation scrub
    # below would destroy these one-symbol utterances otherwise.
    if raw_stripped in _SYMBOL_TO_WORD:
        cleaned = _SYMBOL_TO_WORD[raw_stripped]
    else:
        # Strip hallucinated punctuation anywhere in the text.
        cleaned = _PUNCT_SCRUB_RE.sub(' ', raw_stripped.lower())
        cleaned = _MULTISPACE_RE.sub(' ', cleaned).strip()

        # Phrase corrections (longest keys first so "find the tab" beats
        # any shorter key that might prefix-match).
        # Use word boundaries to prevent substring corruption
        # (e.g. "fine tab" must not match inside "define tablet")
        for bad in sorted(_PHRASE_CORRECTIONS, key=len, reverse=True):
            pattern = rf'\b{re.escape(bad)}\b'
            if re.search(pattern, cleaned):
                cleaned = re.sub(pattern, _PHRASE_CORRECTIONS[bad], cleaned)

        # Per-token word corrections.
        if cleaned:
            tokens = cleaned.split()
            cleaned = ' '.join(_WORD_CORRECTIONS.get(t, t) for t in tokens)
            cleaned = _MULTISPACE_RE.sub(' ', cleaned).strip()

    if cleaned != original:
        print(f"[WASH] '{original}' -> '{cleaned}'")

    return cleaned
