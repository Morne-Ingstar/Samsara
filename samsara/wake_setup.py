"""Wake setup facts, shared by setup, Settings, Home and learning (919)."""
import math

from samsara.constants import DEFAULT_WAKE_PHRASE, DEFAULT_WAKE_PHRASE_OPTIONS

# PLACEHOLDER COPY (919)
MORE_WAKE_WORDS = 'More wake words coming soon'
FALLBACK_NOTE = 'Your previous wake word is not supported. Samsara now listens for "Jarvis".'


def modelled_phrases():
    """Release-approved choices; a filename/mapping alone is not voice proof."""
    return list(DEFAULT_WAKE_PHRASE_OPTIONS)


def normalize_wake_phrase(config):
    """Migrate unsupported saved choices, retaining a notice until acknowledged."""
    wake = config.setdefault('wake_word_config', {})
    phrase = str(wake.get('phrase') or '').lower().strip()
    changed = wake.get('phrase_options') != modelled_phrases()
    if phrase not in modelled_phrases():
        if phrase:
            wake['phrase_fallback_pending'] = True
            for key in ('measured_noise_floor', 'calibrated_microphone'):
                wake.get('audio', {}).pop(key, None)
        phrase = DEFAULT_WAKE_PHRASE
    changed = changed or wake.get('phrase') != phrase
    wake['phrase'] = phrase
    wake['phrase_options'] = modelled_phrases()
    return changed


def calibration_needed(config):
    if not config.get('wake_word_enabled', False):
        return False
    audio = (config.get('wake_word_config') or {}).get('audio') or {}
    floor = audio.get('measured_noise_floor')
    try:
        valid = not isinstance(floor, bool) and math.isfinite(float(floor)) and float(floor) > 0
    except (ValueError, TypeError, OverflowError):
        valid = False
    return (not valid or ('calibrated_microphone' in audio
                         and audio['calibrated_microphone'] != config.get('microphone')))
