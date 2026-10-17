"""615: measured internal gaps reach every pace without changing endpoint values."""
from copy import deepcopy
import struct

import pytest

from samsara.speech_pace import (
    effective_value, measured_interword_pauses, pace_recommendation,
    recommend_profile, typical_pause_s,
)
from tests.test_mic_setup_wizard_qt import _window
from tests.test_tutorial_try_389 import build


def utterance(word_s, gaps, hz=10, leading=0, trailing=0, amplitude=.1):
    """Twelve synthetic voiced words; no transcript or microphone needed."""
    samples = [0.] * round(leading * hz)
    for gap in [*gaps, None]:
        samples += [amplitude] * round(word_s * hz)
        if gap is not None:
            samples += [0.] * round(gap * hz)
    return samples + [0.] * round(trailing * hz)


@pytest.mark.parametrize('hz', [10, 100])
@pytest.mark.parametrize('word_s', [.2, .3, .4])
@pytest.mark.parametrize('gap,expected', [
    (.1, 'standard'), (.3, 'standard'), (.4, 'standard'),
    (.5, 'relaxed'), (.6, 'relaxed'), (1.2, 'relaxed'),
    (1.5, 'relaxed'), (2.6, 'unhurried'),
])
def test_speaking_rate_and_pause_sweep(hz, word_s, gap, expected):
    samples = utterance(word_s, [gap] * 11, hz, leading=3, trailing=6)
    pauses = measured_interword_pauses(samples, hz)
    assert pauses == pytest.approx([gap] * 11)
    assert recommend_profile(pauses) == expected


@pytest.mark.parametrize('gap,expected', [
    (.499999, 'standard'), (.5, 'relaxed'),
    (2.499999, 'relaxed'), (2.5, 'unhurried'),
])
def test_exact_decision_boundaries(gap, expected):
    assert recommend_profile([gap] * 5) == expected


@pytest.mark.parametrize('gap,expected', [(.3, 'standard'), (.6, 'relaxed')])
def test_one_long_hesitation_does_not_replace_typical_pace(gap, expected):
    samples = utterance(.3, [gap] * 5 + [3.2] + [gap] * 5)
    pauses = measured_interword_pauses(samples, 10)
    assert max(pauses) == 3.2
    assert typical_pause_s(pauses) == gap
    assert recommend_profile(pauses) == expected


@pytest.mark.parametrize('samples', [[0.] * 80, [.1] * 30, [0.] * 30 + [.1] * 5 + [0.] * 60])
def test_no_internal_pause_is_not_a_slow_reading(samples):
    pauses = measured_interword_pauses(samples, 10)
    assert pauses == []
    assert recommend_profile(pauses) == 'standard'
    assert 'no pauses between speech' in pace_recommendation(pauses)


def test_short_sentence_with_varied_gaps_still_reaches_relaxed():
    pauses = measured_interword_pauses(utterance(.3, [.3, .5, .6, .7, .8]), 10)
    assert recommend_profile(pauses) == 'relaxed'
    assert typical_pause_s(pauses) == .6


@pytest.mark.parametrize('key', [
    'command_mode.dictate_utterance_silence_s', 'silence_threshold',
    'wake_word_config.quick_silence_timeout',
    'wake_word_config.audio.wake_command_timeout', 'min_speech_duration',
])
def test_standard_preserves_float_bytes_for_configured_and_fallback_values(key):
    # Standard bypasses profile clamping even outside paced bounds, including -0.
    for value in (-0., .3, .65, 1., 2., 5., 90.5):
        cfg = {'accessibility': {'speech_pace': 'standard'}}
        assert struct.pack('!d', effective_value(cfg, key, value)) == struct.pack('!d', value)
        target = cfg
        parts = key.split('.')
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
        assert struct.pack('!d', effective_value(cfg, key, 7.)) == struct.pack('!d', value)


@pytest.mark.parametrize('gap,expected', [(.3, 'Standard'), (.6, 'Relaxed'), (2.6, 'Unhurried')])
def test_wizard_measures_full_reading_and_reports_why_without_saving(qapp, monkeypatch, gap, expected):
    _, app, window = _window(monkeypatch)
    before = deepcopy(app.config)
    try:
        window._go_to(window._STEP_LEVEL)
        for level in utterance(.3, [gap] * 5, leading=3, trailing=6, amplitude=.01):
            window._on_level(level)
        window._recommend_speech_pace()
        line = window._pace_recommendation.text()
        assert f'Suggested pace: {expected} (typical pause {gap:g} s).' in line
        assert len(window._level_history) == 20  # Existing calibration average stays 2s.
        assert len(window._pace_history) > 20
        assert app.config == before
        assert app.saved_updates == []
        window._go_to(window._STEP_LEVEL)
        assert not window._pace_history  # Another visit must not reuse this reading.
    finally:
        window.close()
        window.deleteLater()


@pytest.mark.parametrize('gap,expected', [(.3, 'standard'), (.6, 'relaxed'), (2.6, 'unhurried')])
def test_tutorial_reports_level_and_typical_pause_without_saving(build, gap, expected):
    _, page, app = build()
    before = deepcopy(app.config)
    page.show_stage(2)
    pauses = measured_interword_pauses(utterance(.3, [gap] * 5 + [3.2] + [gap] * 5), 10)
    page.suggest(pauses)
    assert page.pace.currentData() == expected
    assert page.states['pace'].text() == f'Suggested pace: {expected.capitalize()} (typical pause {gap:g} s).'
    assert app.config == before
    app.update_config_and_save.assert_not_called()
