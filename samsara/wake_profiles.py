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

import re

from samsara.log import get_logger

logger = get_logger(__name__)

MIN_SYLLABLES = 3

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

        if not phrase:
            logger.warning(f"[WAKE-PROFILE] '{pid}': empty phrase -- disabling")
            profile['enabled'] = False
            continue

        syllables = count_syllables(phrase)
        if syllables < MIN_SYLLABLES:
            logger.warning(
                f"[WAKE-PROFILE] '{pid}': phrase '{phrase}' is ~{syllables} "
                f"syllable(s), below the {MIN_SYLLABLES}-syllable floor -- "
                f"short wake phrases false-trigger far more often (design "
                f"review arc_20260629_170545.md) -- disabling profile"
            )
            profile['enabled'] = False
            continue

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
