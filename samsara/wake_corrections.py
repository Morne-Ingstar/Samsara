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
import logging
import re
import shutil

from samsara.paths import quarantine_corrupt_file, samsara_home_dir

logger = logging.getLogger(__name__)


def _user_corrections_path():
    """Resolved lazily (not a module-level constant) so SAMSARA_HOME_DIR
    set after import (e.g. by a test fixture) is still honored -- see
    2026-07-16 test-isolation audit."""
    return samsara_home_dir() / "user_wake_corrections.json"


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
    "pay claude": "hey claude",
    "clawed": "claude",
    "claud": "claude",
    # Phase 1 multi-wakeword — "activate hermes" misrecognitions.
    # Self-mapping provides separator-tolerant pattern so "activate, hermes"
    # (correctly spelled, comma inserted by Whisper) is also normalised.
    "activate hermes": "activate hermes",
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
    """Read the user JSON file. Returns a flat {heard: should_be} dict.

    A file that exists but fails to parse is quarantined (renamed aside,
    preserving the bytes) rather than silently treated as empty -- see
    2026-07-16 correction-store hardening.
    """
    path = _user_corrections_path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {str(k).lower(): str(v).lower() for k, v in data.items() if k and v}
    except Exception as e:
        quarantine_corrupt_file(path, logger, e)
        return {}


def _save_user_corrections(corrections, allow_empty: bool = False) -> bool:
    """Persist the user wake corrections dict atomically. Returns True on
    success, False if refused or on write failure.

    Reads the previous on-disk state once (via _load_user_corrections,
    which also quarantines it if corrupt) and reuses that single read for
    both the empty-overwrite guard below and the success-log delta.

    allow_empty=False refuses to overwrite a non-empty on-disk store with
    an empty one (2026-07-09 loss pattern). A genuinely intentional
    clear-to-empty must pass allow_empty=True.
    """
    path = _user_corrections_path()
    previous = _load_user_corrections()

    if not corrections and previous and not allow_empty:
        logger.error(
            f"[STORE] refused to overwrite {len(previous)} entries with "
            f"empty dict -- pass allow_empty=True if intentional"
        )
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            shutil.copy2(path, path.with_name(path.name + '.bak'))
        except OSError as e:
            logger.debug(f"[STORE] backup copy failed (non-fatal): {e}")

    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(corrections, f, indent=2, ensure_ascii=False, sort_keys=True)
    tmp.replace(path)

    added = len(corrections.keys() - previous.keys())
    removed = len(previous.keys() - corrections.keys())
    logger.info(
        f"[STORE] user_wake_corrections.json saved: {len(corrections)} "
        f"entries (+{added} added, -{removed} removed)"
    )
    return True


# Active dict: defaults merged with user overrides. apply_corrections reads here.
FULL_CORRECTIONS = dict(CORRECTIONS)

# Separator chars Whisper inserts between wake-phrase words ("hey, claude").
# Hyphen and apostrophe are intentionally excluded — they are word-internal.
_SEP = r'[\s,.;:!?]+'

# Derived state rebuilt by _rebuild_derived() / reload_corrections().
_PHRASE_CORRECTIONS: dict = {}
_TOKEN_CORRECTIONS:  dict = {}
_CANONICAL_VALUES: frozenset = frozenset()   # set of all correction values
_PHRASE_NORM_MAP: dict = {}                  # normalised-key -> canonical value
_PHRASE_PATTERN = None                       # compiled re.Pattern or None


def _norm_key(s):
    """Lowercase, collapse separator chars to one space, strip.

    Used to normalise phrase keys for lookup and to normalise matched spans
    so the replacement value can be found: 'hey, clod' -> 'hey clod'.
    """
    s = s.lower()
    s = re.sub(_SEP, ' ', s)
    return s.strip()


def _build_phrase_pattern(key):
    """Return a bounded, separator-tolerant regex pattern for a phrase key.

    Inter-word spaces in the normalised key become _SEP groups so Whisper-
    inserted commas/periods between words still trigger the match.
    Lookarounds (?<!\\w) / (?!\\w) ensure the pattern never fires inside a
    longer word or partially into the already-correct phrase.
    Returns None for single-word keys (handled by the token pass instead).
    """
    norm = _norm_key(key)
    words = norm.split()
    if len(words) < 2:
        return None
    inner = _SEP.join(re.escape(w) for w in words)
    return r'(?<!\w)' + inner + r'(?!\w)'


def _rebuild_derived():
    global _PHRASE_CORRECTIONS, _TOKEN_CORRECTIONS
    global _CANONICAL_VALUES, _PHRASE_NORM_MAP, _PHRASE_PATTERN
    _PHRASE_CORRECTIONS = {k: v for k, v in FULL_CORRECTIONS.items() if ' ' in k}
    _TOKEN_CORRECTIONS  = {k: v for k, v in FULL_CORRECTIONS.items() if ' ' not in k}
    _CANONICAL_VALUES   = frozenset(FULL_CORRECTIONS.values())

    # Build normalised-key -> canonical-value lookup for phrase substitution.
    _PHRASE_NORM_MAP = {}
    for k, v in _PHRASE_CORRECTIONS.items():
        nk = _norm_key(k)
        if nk not in _PHRASE_NORM_MAP:
            _PHRASE_NORM_MAP[nk] = v

    # Build and compile the combined phrase regex (longest keys first so more
    # specific alternatives shadow overlapping shorter ones).
    if _PHRASE_CORRECTIONS:
        seen, parts = set(), []
        for k in sorted(_PHRASE_CORRECTIONS, key=len, reverse=True):
            pat = _build_phrase_pattern(k)
            if pat and pat not in seen:
                seen.add(pat)
                parts.append(pat)
        _PHRASE_PATTERN = re.compile('|'.join(parts), re.IGNORECASE) if parts else None
    else:
        _PHRASE_PATTERN = None


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


def set_user_corrections(corrections, allow_empty: bool = False) -> bool:
    """Persist new user wake corrections and hot-reload FULL_CORRECTIONS.

    Returns False without touching FULL_CORRECTIONS if the write was
    refused (see _save_user_corrections' allow_empty guard) or failed.
    """
    if not _save_user_corrections(corrections, allow_empty=allow_empty):
        return False
    reload_corrections()
    return True


# Populate FULL_CORRECTIONS and derived sub-dicts at import
reload_corrections()


def apply_corrections(text):
    """Return text with any known wake-word misrecognitions substituted.

    Two-pass approach:
      1. Phrase-level pass (multi-word keys) with three properties:
         a) Separator tolerance — inter-word spaces in keys match any run of
            Whisper-inserted punctuation/whitespace so "hey, clod" triggers
            the "hey clod" -> "hey claude" correction.
         b) Whole-phrase / word-boundary matching — (?<!w) / (?!w) lookarounds
            prevent a shorter key from matching inside a longer word or the
            already-correct phrase, e.g. "hey claud" key cannot match inside
            "hey claude" because the trailing 'e' is a word character.
         c) Canonical short-circuit — if the input is already a correct wake
            phrase (literal case-insensitive match against the set of all
            correction values), it is returned unchanged before any pattern
            can corrupt it.
      2. Token-level pass (single-word keys) — unchanged token-by-token
         substitution preserving surrounding punctuation.

    Safe to call on empty strings. Non-string input returns unchanged.
    """
    if not text or not isinstance(text, str):
        return text
    if not FULL_CORRECTIONS:
        return text

    # C) Canonical short-circuit: if the input is already a correct canonical
    # phrase return it unchanged — no pattern can alter it.
    if text.strip().lower() in _CANONICAL_VALUES:
        return text

    # 1. Phrase-level pass (multi-word keys).
    if _PHRASE_PATTERN is not None:
        def _phrase_sub(m):
            return _PHRASE_NORM_MAP.get(_norm_key(m.group(0)), m.group(0))
        text = _PHRASE_PATTERN.sub(_phrase_sub, text)

    # 2. Token-level pass (single-word keys; unchanged)
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
