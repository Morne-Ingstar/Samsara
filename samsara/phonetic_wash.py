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

User corrections live in ~/.samsara/user_corrections.json as a flat
{"heard": "should be"} dict. They merge ON TOP of the hardcoded defaults
(user wins on conflict). Multi-word keys go to phrase corrections;
single-word keys go to word corrections. The active dicts are recomputed
at import and whenever reload_corrections() is called from the UI.
"""

import json
import re
from pathlib import Path

USER_CORRECTIONS_PATH = Path.home() / ".samsara" / "user_corrections.json"

# Multi-word phrase corrections. Key must appear as a contiguous substring
# of the cleaned text. Applied before per-word corrections, so "fine tab"
# (phrase) wins over "fine" -> "find" (word) followed by " tab".
_DEFAULT_PHRASE_CORRECTIONS = {
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
_DEFAULT_WORD_CORRECTIONS = {
    "fine": "find",
    "mike": "mic",
    "tub": "tab",
    "tabs": "tab",
    "chrome's": "chrome",
    "screenshots": "screenshot",
    "clicks": "click",
}

# Active dicts: hardcoded defaults merged with user overrides. apply_phonetic_wash
# reads from these. Re-populated on reload_corrections().
_PHRASE_CORRECTIONS = dict(_DEFAULT_PHRASE_CORRECTIONS)
_WORD_CORRECTIONS = dict(_DEFAULT_WORD_CORRECTIONS)


def _load_user_corrections():
    """Read the user JSON file. Returns a flat {heard: should_be} dict.

    Missing file or unreadable JSON returns an empty dict -- the user can
    edit the file by hand without breaking startup.
    """
    if not USER_CORRECTIONS_PATH.exists():
        return {}
    try:
        with open(USER_CORRECTIONS_PATH, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {str(k).lower(): str(v) for k, v in data.items() if k and v}
    except Exception as e:
        print(f"[WASH] Could not load user corrections: {e}")
        return {}


def _save_user_corrections(corrections):
    """Persist the user corrections dict atomically."""
    USER_CORRECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = USER_CORRECTIONS_PATH.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(corrections, f, indent=2, ensure_ascii=False, sort_keys=True)
    tmp.replace(USER_CORRECTIONS_PATH)


def reload_corrections():
    """Rebuild the active correction dicts from defaults + user JSON.

    Called by the Voice Training UI after the user adds/removes an entry,
    so changes take effect on the next transcription without a restart.
    Multi-word keys land in phrase corrections; single-word in word
    corrections. User overrides win on conflict.
    """
    global _PHRASE_CORRECTIONS, _WORD_CORRECTIONS
    user = _load_user_corrections()
    phrase = dict(_DEFAULT_PHRASE_CORRECTIONS)
    word = dict(_DEFAULT_WORD_CORRECTIONS)
    for k, v in user.items():
        if ' ' in k.strip():
            phrase[k.strip()] = v
        else:
            word[k.strip()] = v
    _PHRASE_CORRECTIONS = phrase
    _WORD_CORRECTIONS = word


def get_default_phrase_corrections():
    """Read-only view of the hardcoded phrase defaults (for UI display)."""
    return dict(_DEFAULT_PHRASE_CORRECTIONS)


def get_default_word_corrections():
    """Read-only view of the hardcoded word defaults (for UI display)."""
    return dict(_DEFAULT_WORD_CORRECTIONS)


def get_user_corrections():
    """Return the current user-overrides dict (loaded fresh from disk)."""
    return _load_user_corrections()


def set_user_corrections(corrections):
    """Persist new user corrections and hot-reload the active dicts."""
    _save_user_corrections(corrections)
    reload_corrections()


# Populate active dicts at import time
reload_corrections()

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
