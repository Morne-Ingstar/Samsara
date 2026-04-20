"""
Wake-word correction map — maps known Whisper misrecognitions of the wake
phrase back to the real wake phrase before the matcher runs.

This is separate from voice_training.py's general dictation corrections.
Those apply to final transcription output; these run upstream, specifically
to rescue wake-word detection from Whisper's occasional rendering quirks.

Start empty (or close to it) and grow as patterns show up in the Wake Word
Debug console. The debug trace emits both 'raw' and 'corrected' so you can
spot misrecognitions and add them here.

Keys should be lowercase. Values are the canonical wake phrase (also lowercase).
The correction is applied token-by-token on the lowercased transcription.
"""


# Known misrecognitions — add entries as you catch them in the debug console.
# Start empty; trim or expand as patterns emerge for your voice + mic.
CORRECTIONS = {
    # Wake word variants
    "charvis": "jarvis",
    "jarviss": "jarvis",
    "jarves": "jarvis",
    "jarbus": "jarvis",
}


def apply_corrections(text):
    """Return text with any known wake-word misrecognitions substituted.

    Token-level substitution preserves surrounding words and punctuation:
        "hey charvis dictate" -> "hey jarvis dictate"
        "charvis, open chrome" -> "jarvis, open chrome"

    Safe to call on empty strings. Non-string input returns unchanged.
    """
    if not text or not isinstance(text, str):
        return text
    if not CORRECTIONS:
        return text

    out_tokens = []
    for token in text.split():
        # Strip edge punctuation for the lookup but re-attach it afterwards
        # so "charvis," still becomes "jarvis,"
        leading = ""
        trailing = ""
        core = token
        while core and not core[0].isalnum():
            leading += core[0]
            core = core[1:]
        while core and not core[-1].isalnum():
            trailing = core[-1] + trailing
            core = core[:-1]

        replacement = CORRECTIONS.get(core.lower(), core)
        out_tokens.append(f"{leading}{replacement}{trailing}")

    return " ".join(out_tokens)


def was_corrected(original, corrected):
    """True if apply_corrections changed the text. Cheap case-insensitive compare."""
    if original is None or corrected is None:
        return False
    return original.strip().lower() != corrected.strip().lower()
