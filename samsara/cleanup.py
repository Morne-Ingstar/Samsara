"""Deterministic post-transcription cleanup.

Runs AFTER process_transcription (auto-cap + number formatting), BEFORE the
text is pasted. Filler-word removal and spacing/comma fixes only -- no LLM,
no model calls, zero added latency. Pure regex.

Conservative by design: every rule here is a potential corruption of free-form
dictation. We err toward keeping a word in than stripping it; "verbatim" mode
exists for code dictation and other content where every token must survive.

The raw Whisper output is preserved separately in history (raw_text column),
so any over-cleaning is recoverable.
"""

import re

# Filler-word patterns. Each runs with re.IGNORECASE.
#
# Notes on the lookaheads:
# - `\blike\b(?=,)`: only filler "like," with a trailing comma. The wider
#   `(?=,|\s)` would also strip the word "like" from "it looks like a bug",
#   which is a meaningful comparison.
# - `\bso\b(?=,\s)` etc.: comma-anchored so we don't disturb meaningful uses
#   ("I'm so tired" stays; "so, anyway" becomes "anyway").
FILLERS = [
    r'\bum\b',
    r'\buh\b',
    r'\blike\b(?=,)',
    r'\byou know\b',
    r'\bI mean\b(?=,)',
    r'\bbasically\b(?=,)',
    r'\bactually\b(?=,)',
    r'\bso\b(?=,\s)',
]


def clean_text(text, mode="clean"):
    """Clean Whisper output.

    Modes:
      "verbatim" -- return text unchanged (for code dictation, etc.)
      "clean"    -- remove fillers, fix spacing, normalize commas, ensure
                    final punctuation, capitalize sentence starts and "I".

    Returns the input unchanged for empty/None or in verbatim mode.
    """
    if mode == "verbatim" or not text:
        return text

    result = text

    # Strip filler words
    for pattern in FILLERS:
        result = re.sub(pattern, '', result, flags=re.IGNORECASE)

    # Collapse runs of whitespace left by filler removal
    result = re.sub(r'\s{2,}', ' ', result)

    # Orphaned commas: ", ," -> ","  (run twice in case of three-in-a-row)
    result = re.sub(r',\s*,', ',', result)
    result = re.sub(r',\s*,', ',', result)

    # Strip leading whitespace BEFORE the sentence-cap regex, otherwise the
    # `^` anchor never sees the first letter.
    result = result.lstrip()

    # Drop a leading comma if filler removal left one
    result = re.sub(r'^\s*,\s*', '', result)

    # Trailing comma -> period
    result = re.sub(r',\s*$', '.', result)

    # Capitalize first letter of each sentence
    result = re.sub(
        r'(^|[.!?]\s+)([a-z])',
        lambda m: m.group(1) + m.group(2).upper(),
        result,
    )

    # Capitalize standalone "I"
    result = re.sub(r'\bi\b', 'I', result)

    # Tighten space before punctuation, ensure space after
    result = re.sub(r'\s+([.,!?;:])', r'\1', result)
    result = re.sub(r'([.,!?;:])([A-Za-z])', r'\1 \2', result)

    # Final trim + ensure terminal punctuation
    result = result.strip()
    if result and result[-1] not in '.!?':
        result += '.'

    return result
