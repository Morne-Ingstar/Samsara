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
import re
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
    "charge us": "jarvis",
    "charge": "jarvis",
    "driver's": "jarvis",
    "drivers": "jarvis",
    "harvest": "jarvis",
    # Phase 1 multi-wakeword — "hey claude" misrecognitions.
    # Multi-word keys are handled by the phrase-level pass in apply_corrections.
    # Single-word keys ("clawed", "claud") are safe because they are not
    # common words — no collision risk with legitimate speech.
    "hey cloud": "hey claude",
    "hey clod": "hey claude",
    "hey clawed": "hey claude",
    "hey claud": "hey claude",
    "hey, claude": "hey claude",
    "a claude": "hey claude",
    "a cloud": "hey claude",
    "hey claude.": "hey claude",
    "hit clod": "hey claude",
    "hit claude": "hey claude",
    "hay claude": "hey claude",
    "clawed": "claude",
    "claud": "claude",
    # Phase 1 multi-wakeword — "activate hermes" misrecognitions.
    "activate hermès": "activate hermes",
    "activate hermies": "activate hermes",
    "activate herpes": "activate hermes",
    "activate hermez": "activate hermes",
    "activate her mes": "activate hermes",
    "activate harmony's": "activate hermes",
    "activate her mess": "activate hermes",
    "activate her message": "activate hermes",
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

# Derived sub-dicts rebuilt by reload_corrections().
# _PHRASE_CORRECTIONS: multi-word keys handled by the phrase-level pass.
# _TOKEN_CORRECTIONS:  single-word keys handled by the existing token pass.
_PHRASE_CORRECTIONS: dict = {}
_TOKEN_CORRECTIONS:  dict = {}


def _rebuild_derived():
    global _PHRASE_CORRECTIONS, _TOKEN_CORRECTIONS
    _PHRASE_CORRECTIONS = {k: v for k, v in FULL_CORRECTIONS.items() if ' ' in k}
    _TOKEN_CORRECTIONS  = {k: v for k, v in FULL_CORRECTIONS.items() if ' ' not in k}


def reload_corrections():
    """Rebuild FULL_CORRECTIONS from CORRECTIONS + user overrides.

    Called by the UI after the user edits the wake-word corrections list,
    so changes apply on the next wake-word check without a restart.
    """
    global FULL_CORRECTIONS
    FULL_CORRECTIONS = {**CORRECTIONS, **_load_user_corrections()}
    _rebuild_derived()


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


# Populate FULL_CORRECTIONS and derived sub-dicts at import
reload_corrections()


def apply_corrections(text):
    """Return text with any known wake-word misrecognitions substituted.

    Two-pass approach:
      1. Phrase-level pass — replaces multi-word keys (e.g. "hey cloud" ->
         "hey claude") using case-insensitive substring replacement on the
         full string. Longer keys win (sorted by length desc) so overlapping
         patterns resolve correctly.
      2. Token-level pass — replaces single-word keys token-by-token,
         preserving surrounding punctuation (e.g. "charvis," -> "jarvis,").

    The two passes are strictly additive: phrase pass handles multi-word
    keys, token pass handles single-word keys, no double-substitution.

    Safe to call on empty strings. Non-string input returns unchanged.
    """
    if not text or not isinstance(text, str):
        return text
    if not FULL_CORRECTIONS:
        return text

    # 1. Phrase-level pass (multi-word keys).
    # Single-pass regex alternation: longer keys sort first so they win over
    # overlapping shorter keys at the same position. The regex cursor advances
    # past each match, preventing a replaced value from being re-matched by a
    # shorter key (e.g. "hey cloud"→"hey claude" then "hey claud" re-matching
    # inside the replacement).
    if _PHRASE_CORRECTIONS:
        _phrase_pattern = '|'.join(
            re.escape(k)
            for k in sorted(_PHRASE_CORRECTIONS, key=len, reverse=True)
        )
        def _phrase_sub(m, _pc=_PHRASE_CORRECTIONS):
            return _pc[m.group(0).lower()]
        text = re.sub(_phrase_pattern, _phrase_sub, text, flags=re.IGNORECASE)

    # 2. Token-level pass (single-word keys; identical to original logic)
    if not _TOKEN_CORRECTIONS:
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

        replacement = _TOKEN_CORRECTIONS.get(core.lower(), core)
        out_tokens.append(f"{leading}{replacement}{trailing}")

    return " ".join(out_tokens)


def was_corrected(original, corrected):
    """True if apply_corrections changed the text. Cheap case-insensitive compare."""
    if original is None or corrected is None:
        return False
    return original.strip().lower() != corrected.strip().lower()
