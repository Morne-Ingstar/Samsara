"""Wake-profile config validation -- tribunal spec (docs design review
arc_20260629_170545.md).

Multi-wakeword profiles bind a spoken phrase to a target app (samsara/wake_
profiles list in dictation.py's default config). Two invariants from the
design review are enforced here, at config-load time, rather than left to
runtime false-triggers:

  1. Phrases under MIN_SYLLABLES are the auditor's flagged failure mode
     ("Claude" is effectively one syllable -- OWW false-trigger rates for
     short utterances are unacceptably high). Reject, don't tune around it.
  2. Two enabled profiles must never share a phrase -- ambiguous dispatch.

A bad profile is disabled (enabled=False) and logged -- never raises. This
module never touches audio/OWW; it is pure config validation so it is cheap
to unit test without mocking the wake pipeline.
"""

import hashlib
import json
import re
from pathlib import Path

from samsara.log import get_logger

logger = get_logger(__name__)

MIN_SYLLABLES = 3
WAKE_MODELS_DIR = Path(__file__).parent / 'wake_models'

_VOWEL_GROUPS = re.compile(r'[aeiouy]+')


def count_syllables(phrase: str) -> int:
    """Rough syllable estimate via vowel-group counting, one word at a time.

    Not phonetically exact (no dictionary, no stress rules) -- good enough
    to catch phrases that are clearly too short to be a safe always-on wake
    word. Each word contributes at least 1 syllable; a silent trailing 'e'
    (not part of '-le') is trimmed off a word with more than one group.
    """
    total = 0
    for word in phrase.lower().split():
        groups = _VOWEL_GROUPS.findall(word)
        n = len(groups)
        if word.endswith('e') and n > 1 and not word.endswith('le'):
            n -= 1
        total += max(1, n)
    return total


def profile_admitted(profile: dict, detectors: dict, *, primary_hit=False,
                     profile_hits=(), session_open=False) -> bool:
    """A transcript may name only the profile whose capture gate admitted it."""
    detector = detectors.get(profile.get('id', ''))
    if getattr(detector, 'is_available', False):
        return profile.get('id') in profile_hits
    # A failed/missing custom model must never turn a short profile into a
    # transcript-only wake word, even when the generic detector fired.
    if count_syllables(profile.get('phrase', '')) < MIN_SYLLABLES:
        return False
    return bool(primary_hit or session_open)


def passing_model_metrics(profile: dict, models_dir: Path | None = None) -> bool:
    """Require measured acceptance and the exact ONNX bytes named by the profile."""
    models_dir = WAKE_MODELS_DIR if models_dir is None else Path(models_dir)
    name = str(profile.get('oww_model') or '')
    if not name or Path(name).name != name or not name.endswith('.onnx'):
        return False
    model = models_dir / name
    try:
        metrics = json.loads((models_dir / 'metrics.json').read_text(encoding='utf-8'))
        if metrics.get('model') != name or metrics.get('phrase') != profile.get('phrase', '').strip().lower():
            return False
        if metrics.get('model_sha256') != hashlib.sha256(model.read_bytes()).hexdigest():
            return False
        return (float(metrics['false_accepts_per_hour']) < 1.0
                and float(metrics['true_accept_rate']) >= 0.95
                and float(metrics['owner_true_accept_rate']) >= 0.95
                and float(metrics['negative_hours']) > 0
                and int(metrics['held_out_positives']) > 0
                and int(metrics['owner_held_out_positives']) > 0)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def validate_wake_profiles(profiles: list) -> list:
    """Disable any enabled profile with an empty phrase, a sub-floor
    syllable count, or a phrase duplicating an earlier enabled profile's.
    Mutates and returns `profiles`. Never raises -- a malformed profile
    degrades to disabled, it does not crash config load.
    """
    seen: dict = {}
    for profile in profiles:
        if not isinstance(profile, dict) or not profile.get('enabled', True):
            continue

        pid = profile.get('id', '<unnamed>')
        phrase = (profile.get('phrase') or '').strip().lower()

        # Settings exposes the phrase but preserves the hidden model filename.
        # Keep these two documented Claude phrases paired with their models.
        if pid == 'claude' and phrase in ('okay claude', 'hey claude'):
            expected_model = phrase.replace(' ', '_') + '.onnx'
            if profile.get('oww_model') != expected_model:
                profile['oww_model'] = expected_model
                logger.info("[WAKE-PROFILE] '%s': model follows phrase '%s'", pid, phrase)

        if not phrase:
            logger.warning(f"[WAKE-PROFILE] '{pid}': empty phrase -- disabling")
            profile['enabled'] = False
            continue

        syllables = count_syllables(phrase)
        if syllables < MIN_SYLLABLES and not passing_model_metrics(profile):
            logger.warning(
                f"[WAKE-PROFILE] '{pid}': phrase '{phrase}' is ~{syllables} "
                f"syllable(s), below the {MIN_SYLLABLES}-syllable floor -- "
                f"short wake phrases false-trigger far more often (design "
                f"review arc_20260629_170545.md) -- disabling profile"
            )
            profile['enabled'] = False
            continue
        if syllables < MIN_SYLLABLES:
            logger.info("[WAKE-PROFILE] '%s': sub-floor phrase '%s' accepted with measured model",
                        pid, phrase)

        if phrase in seen:
            logger.warning(
                f"[WAKE-PROFILE] '{pid}': phrase '{phrase}' duplicates "
                f"enabled profile '{seen[phrase]}' -- disabling"
            )
            profile['enabled'] = False
            continue

        seen[phrase] = pid

    return profiles


# mode/send_policy value migration -- kept here (not just in dictation.py's
# migration code) so both the config-load migration and any future caller
# normalize old configs identically.
_LEGACY_MODE_MAP = {
    'enter': 'focus_dictate',
    'stage_only': 'stage_send',
}


def normalize_profile_mode_and_send_word(profile: dict, default_send_word: str = 'over') -> None:
    """In-place: migrate a legacy 'send_policy' value to 'mode', and make
    sure both 'mode' and 'send_word' are present. Each profile must carry
    its OWN send_word (agentic-safety requirement -- a stage_send target
    must never share a terminator with a focus_dictate one); this only
    fills a SANE DEFAULT when absent, it never overwrites an explicit
    per-profile value.
    """
    if 'send_policy' in profile:
        legacy = profile.pop('send_policy')
        profile.setdefault('mode', _LEGACY_MODE_MAP.get(legacy, 'focus_dictate'))
    profile.setdefault('mode', 'focus_dictate')
    profile.setdefault('send_word', default_send_word)
