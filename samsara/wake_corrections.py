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

User-added corrections live in ~/.samsara/user_wake_corrections.json as a
flat {"heard": "should be"} dict. They merge ON TOP of the hardcoded
defaults below (user wins on conflict). FULL_CORRECTIONS is the active
dict; it gets rebuilt at import and whenever reload_corrections() is called.
"""

import json
from pathlib import Path

USER_CORRECTIONS_PATH = Path.home() / ".samsara" / "user_wake_corrections.json"


# Known misrecognitions — add entries as you catch them in the debug console.
# Start empty; trim or expand as patterns emerge for your voice + mic.
CORRECTIONS = {
    # Wake word variants — Whisper misrecognitions of "jarvis"
    "charvis": "jarvis",
    "charvus": "jarvis",
    "jarviss": "jarvis",
    "jarves": "jarvis",
    "jarbus": "jarvis",
    "jervice": "jarvis",
    "jervis": "jarvis",
    "service": "jarvis",
}


def _load_user_corrections():
    """Read the user JSON file. Returns a flat {heard: should_be} dict."""
    if not USER_CORRECTIONS_PATH.exists():
        return {}
    try:
        with open(USER_CORRECTIONS_PATH, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {str(k).lower(): str(v).lower() for k, v in data.items() if k and v}
    except Exception as e:
        print(f"[WAKE] Could not load user wake corrections: {e}")
        return {}


def _save_user_corrections(corrections):
    """Persist the user wake corrections dict atomically."""
    USER_CORRECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = USER_CORRECTIONS_PATH.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(corrections, f, indent=2, ensure_ascii=False, sort_keys=True)
    tmp.replace(USER_CORRECTIONS_PATH)


# Active dict: defaults merged with user overrides. apply_corrections reads here.
FULL_CORRECTIONS = dict(CORRECTIONS)


def reload_corrections():
    """Rebuild FULL_CORRECTIONS from CORRECTIONS + user overrides.

    Called by the UI after the user edits the wake-word corrections list,
    so changes apply on the next wake-word check without a restart.
    """
    global FULL_CORRECTIONS
    FULL_CORRECTIONS = {**CORRECTIONS, **_load_user_corrections()}


def get_default_corrections():
    """Read-only view of the hardcoded defaults (for UI display)."""
    return dict(CORRECTIONS)


def get_user_corrections():
    """Return the current user-overrides dict (loaded fresh from disk)."""
    return _load_user_corrections()


def set_user_corrections(corrections):
    """Persist new user wake corrections and hot-reload FULL_CORRECTIONS."""
    _save_user_corrections(corrections)
    reload_corrections()


# Populate FULL_CORRECTIONS at import
reload_corrections()


def apply_corrections(text):
    """Return text with any known wake-word misrecognitions substituted.

    Token-level substitution preserves surrounding words and punctuation:
        "hey charvis dictate" -> "hey jarvis dictate"
        "charvis, open chrome" -> "jarvis, open chrome"

    Safe to call on empty strings. Non-string input returns unchanged.
    """
    if not text or not isinstance(text, str):
        return text
    if not FULL_CORRECTIONS:
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

        replacement = FULL_CORRECTIONS.get(core.lower(), core)
        out_tokens.append(f"{leading}{replacement}{trailing}")

    return " ".join(out_tokens)


def was_corrected(original, corrected):
    """True if apply_corrections changed the text. Cheap case-insensitive compare."""
    if original is None or corrected is None:
        return False
    return original.strip().lower() != corrected.strip().lower()
