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
import math
import re
from pathlib import Path

from samsara.log import get_logger

logger = get_logger(__name__)

MIN_SYLLABLES = 3
WAKE_MODELS_DIR = Path(__file__).parent / 'wake_models'

# Primary listener phrases; app-target profiles below remain independent.
PHRASE_TO_MODEL = {
    'jarvis': 'hey_jarvis', 'hey jarvis': 'hey_jarvis', 'alexa': 'alexa',
    'hey mycroft': 'hey_mycroft', 'hey rhasspy': 'hey_rhasspy',
}


def resolve_wake_model(phrase, model=None, models_dirs=None):
    """Resolve a model ID or user-dropped ONNX without creating directories."""
    if models_dirs is None:
        from samsara.paths import samsara_home_dir
        models_dirs = (WAKE_MODELS_DIR, samsara_home_dir() / 'wake_models')
    name = str(model or '').strip()
    if name and name in set(PHRASE_TO_MODEL.values()) | {'timer', 'weather'}:
        return name
    candidate = Path(name) if name else None
    if candidate and candidate.is_absolute():
        return str(candidate) if candidate.is_file() and candidate.suffix.lower() == '.onnx' else None
    filename = name or phrase.lower().strip().replace(' ', '_') + '.onnx'
    if Path(filename).name == filename:
        for directory in models_dirs:
            candidate = Path(directory) / filename
            if candidate.suffix.lower() == '.onnx' and candidate.is_file():
                return str(candidate)
    return PHRASE_TO_MODEL.get(phrase.lower().strip()) if not name else None


def wake_threshold(value, default=0.2):
    try:
        number = float(value)
        if not isinstance(value, bool) and math.isfinite(number) and 0 <= number <= 1:
            return number
    except (ValueError, TypeError, OverflowError):
        pass
    return default


def normalize_wake_phrases(wake):
    """Migrate once without replacing a custom phrase or valid threshold.

    A second model-less phrase is retained but disabled. An empty list means
    the user intentionally removed every primary wake phrase.
    """
    before = json.dumps(wake, sort_keys=True)
    entries = wake.get('phrases')
    migrate_roles = not isinstance(entries, list) or (bool(entries) and not any(
        isinstance(row, dict) and 'role' in row for row in entries))
    if not isinstance(entries, list):
        entries = [{'phrase': wake.get('phrase') or 'jarvis',
                    'model': wake.get('model') or wake.get('model_path'),
                    'enabled': True, 'threshold': wake.get('oww_threshold', .2), 'role': 'samsara'}]
    if migrate_roles and not any(isinstance(row, dict) and str(row.get('phrase') or '').strip().lower() == 'hey ava'
                                 for row in entries):
        # 1042a measured 1.67 false episodes/hour at .2: owner opt-in only.
        entries = [*entries, dict(phrase='hey ava', role='ava', enabled=False, threshold=.2)]
    normalized, seen, fallback = [], set(), False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        phrase = str(entry.get('phrase') or '').strip().lower()
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        row = dict(entry)
        row.update(phrase=phrase, model=entry.get('model') or '',
                   enabled=bool(entry.get('enabled', True)),
                   role=(entry.get('role') if entry.get('role') in ('samsara', 'ava')
                         else 'ava' if phrase == 'hey ava' else 'samsara'),
                   threshold=wake_threshold(entry.get('threshold', wake.get('oww_threshold', .2))))
        model = resolve_wake_model(phrase, row['model'])
        if model and not row['model']:
            row['model'] = model
        if row['enabled'] and not model:
            if fallback:
                row['enabled'] = False
                logger.warning('[WAKE] Disabled additional recognizer fallback: %s', phrase)
            fallback = True
        normalized.append(row)
    wake['phrases'] = normalized
    return json.dumps(wake, sort_keys=True) != before


def detector_for_config(wake, factory=None):
    from samsara.wake_detector import WakeWordDetector
    factory = factory or WakeWordDetector
    normalize_wake_phrases(wake)
    enabled = [row for row in wake['phrases'] if row['enabled']]
    if len(enabled) == 1:
        row = enabled[0]
        detector = factory(row['phrase'], threshold=row['threshold'], model_path=row.get('model'))
    else:
        detector = factory(wake['phrases'], threshold=wake.get('oww_threshold', .2))
    detector.config_signature = json.dumps(wake['phrases'], sort_keys=True)
    detector.legacy_phrase = wake.get('phrase')
    return detector


def refresh_primary_detector(app, changes=None, factory=None):
    wake = app.config.setdefault('wake_word_config', {})
    detector = getattr(app, '_wake_detector', None)
    if changes is None and getattr(detector, 'legacy_phrase', wake.get('phrase')) != wake.get('phrase'):
        changes = {'phrase': wake.get('phrase', 'jarvis')}
    # Older Settings controls still edit the primary legacy phrase alone.
    if changes and 'phrase' in changes and 'phrases' not in changes:
        entries = wake.get('phrases') or []
        phrase = str(changes['phrase']).strip().lower()
        if entries and entries[0]['phrase'] != phrase:
            entries[0] = dict(entries[0], phrase=phrase, model='')
    normalize_wake_phrases(wake)
    signature = json.dumps(wake['phrases'], sort_keys=True)
    if getattr(getattr(app, '_wake_detector', None), 'config_signature', None) != signature:
        detector = detector_for_config(wake, factory=factory)
        app._wake_detector = detector
        app._oww_wake_detected = False
        app._oww_primary_hit = False
        from samsara.wake_roles import clear_capture
        clear_capture(app)


def match_primary_wake(text, wake, detector=None):
    """Match enabled primary phrases, longest first for overlapping aliases."""
    from samsara.wake_word_matcher import match_wake_phrase
    entries = wake.get('phrases')
    if not isinstance(entries, list):
        entries = [{'phrase': wake.get('phrase', 'jarvis'), 'enabled': True}]
    for row in sorted(entries, key=lambda r: len(r.get('phrase', '')), reverse=True):
        if row.get('enabled', True):
            phrase = row['phrase']
            if isinstance(getattr(detector, '_heads', None), list):
                fast = {p for p, _, _ in getattr(detector, '_heads', ())}
                allowed = fast | set(getattr(detector, 'fallback_phrases', ()))
                if phrase not in allowed:
                    continue
            matched, kind, index = match_wake_phrase(text, phrase)
            if matched:
                return phrase, matched, kind, index
    return wake.get('phrase', 'jarvis'), False, None, -1


def is_fallback_phrase(detector, phrase):
    return phrase in getattr(detector, 'fallback_phrases', ())

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
