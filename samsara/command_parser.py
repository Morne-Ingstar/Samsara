"""
Wake word command parser.

Turns raw text (after wake word extraction) into structured intent dicts.
Pure functions, no side effects, no dependencies beyond stdlib + re.

Intent dict format:
    {
        "type": "dictation" | "command_text" | "unknown",
        "name": str or None,       # canonical name for dictation modes
        "content": str or None,    # payload text
        "raw": str                 # original input before normalization
    }
"""

import re

# Filler words stripped from edges only. Interior occurrences are preserved
# so payload text like "I like cats" isn't corrupted.
DEFAULT_FILLERS = frozenset({'please', 'uh', 'um', 'like'})

# Dictation mode keywords, ordered longest-prefix-first.
# "type" → quick_dictation (silence-based auto-finalize, 1s timeout)
# "dictate" → long_dictation (no silence timeout, requires end word)
DICTATION_COMMANDS = {
    "long dictate": "long_dictation",
    "long dictation": "long_dictation",
    "short dictate": "quick_dictation",
    "short dictation": "quick_dictation",
    "quick dictate": "quick_dictation",
    "dictate": "long_dictation",
    "dictation": "long_dictation",
    "type": "quick_dictation",
}

# Separator characters Whisper may insert between command keyword and payload
_SEP_PATTERN = r'[\s:,\-]+'


def normalize_command_text(text):
    """Lowercase, strip, collapse whitespace, strip leading non-word chars."""
    text = text.lower().strip()
    text = re.sub(r'^[^\w]+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def strip_fillers(text, fillers=None):
    """Remove leading/trailing filler words from *text*.

    Only whole words at the edges are removed; interior fillers are left alone.
    """
    if fillers is None:
        fillers = DEFAULT_FILLERS
    words = text.split()
    while words and words[0].lower() in fillers:
        words.pop(0)
    while words and words[-1].lower() in fillers:
        words.pop()
    return ' '.join(words)


def parse_wake_command(raw_text):
    """Parse a wake word command into a structured intent dict.

    *raw_text* is everything after the wake phrase has been removed
    (e.g. "dictate hello world" or ", dictate: hello world").
    """
    raw = raw_text
    normalized = normalize_command_text(raw_text)
    stripped = strip_fillers(normalized)

    # Check if there's anything meaningful
    word_content = re.sub(r'[^\w\s]', '', stripped).strip()
    if len(word_content) < 2:
        return {"type": "unknown", "name": None, "content": None, "raw": raw}

    # Try dictation commands (longest prefix first — dict is ordered)
    for phrase, mode_name in DICTATION_COMMANDS.items():
        # Exact match (bare command, no payload)
        if stripped == phrase:
            return {"type": "dictation", "name": mode_name, "content": None, "raw": raw}

        # Prefix with separator (space, colon, comma, dash)
        pattern = rf'^{re.escape(phrase)}{_SEP_PATTERN}(.+)$'
        m = re.match(pattern, stripped)
        if m:
            content = strip_fillers(m.group(1).strip())
            return {
                "type": "dictation",
                "name": mode_name,
                "content": content if content else None,
                "raw": raw,
            }

        # Joined tokens (Whisper sometimes omits the space)
        if stripped.startswith(phrase) and len(stripped) > len(phrase):
            remainder = stripped[len(phrase):]
            # Only accept if the remainder starts with a letter (not punctuation)
            if remainder and remainder[0].isalpha():
                content = strip_fillers(remainder.strip())
                return {
                    "type": "dictation",
                    "name": mode_name,
                    "content": content if content else None,
                    "raw": raw,
                }

    # No dictation keyword matched -- treat as freeform command/text
    return {"type": "command_text", "name": None, "content": normalized, "raw": raw}
