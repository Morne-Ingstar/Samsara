"""1274: computer/hey-computer overlap routing, using synthetic head scores.

These checks establish dispatch behavior, not model availability or accuracy.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import wake_detector, wake_profiles, wake_roles


@pytest.fixture
def detector(monkeypatch, tmp_path):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path / 'profile'))

    class Model:
        def __init__(self, **kwargs):
            self.scores = {}

        def predict(self, audio):
            return self.scores

        def reset(self):
            self.scores = {}

    monkeypatch.setattr(wake_detector, '_load_model_class', lambda: Model)
    monkeypatch.setattr(wake_detector, 'resolve_wake_model',
                        lambda phrase, model=None: phrase.replace(' ', '_') + '.onnx')
    return wake_detector.WakeWordDetector([
        dict(phrase=phrase, enabled=True, role='samsara', threshold=.4)
        for phrase in ('computer', 'hey computer')
    ])


@pytest.mark.parametrize('scores, expected', [
    ({'computer': .8, 'hey_computer': .9}, 'hey computer'),
    ({'computer': .9, 'hey_computer': .8}, 'computer'),
    ({'computer': .8, 'hey_computer': .8}, 'hey computer'),
    ({'computer': .3, 'hey_computer': .4}, 'hey computer'),
    ({'computer': .3, 'hey_computer': .3}, None),
])
def test_overlapping_heads_return_one_phrase(detector, scores, expected):
    detector._model.scores = scores
    assert detector.detected(np.zeros(1600, dtype=np.int16)) == expected


@pytest.mark.parametrize('first, second', [
    ('computer', 'hey computer'), ('hey computer', 'computer'),
])
def test_staggered_overlap_queues_one_utterance(detector, first, second):
    from samsara.audio_engine.wake_consumer import WakeConsumer

    app = SimpleNamespace(
        app_state='asleep', wake_word_triggered=False, _wake_detector=detector,
        _wake_profile_detectors={}, _oww_primary_hit=False, _oww_profile_hits=set(),
        _oww_wake_detected=False, _wake_rearm_needs_onset=False,
        config={'wake_word_config': {'phrases': detector._entries}},
        process_wake_word_buffer=Mock(), _close_hands_free_capture_duck=Mock(),
    )
    consumer = WakeConsumer.__new__(WakeConsumer)
    consumer._app = app
    consumer._hands_free_capture_duck_token = None
    consumer._is_toggle_cmd = lambda app: False
    audio = np.zeros(1600, dtype=np.float32)
    for phrase in (first, second):
        detector._model.scores = {phrase.replace(' ', '_'): .9}
        hit = detector.detected(audio)
        wake_roles.begin_primary_capture(consumer, hit, audio)
        app._oww_primary_hit = True
        app._oww_wake_detected = True
        detector.reset()
    assert app._oww_primary_phrase == first
    consumer._flush([audio], owner_token=5)
    app.process_wake_word_buffer.assert_called_once()
    buffer = app.process_wake_word_buffer.call_args.args[0]
    assert buffer.primary_phrase == first
    assert buffer.primary_role == 'samsara'
    assert buffer.primary_hit is True
    assert app._oww_primary_phrase is None
    # Re-flushing without another admitted hit cannot enqueue a second decode.
    consumer._flush([audio], owner_token=5)
    app.process_wake_word_buffer.assert_called_once()
    wake = {'phrase': 'jarvis', 'phrases': detector._entries}
    assert wake_profiles.match_primary_wake('hey computer open settings', wake)[0] == 'hey computer'


def test_computer_defaults_preserve_jarvis_and_explicit_choices(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path / 'profile'))
    wake = {'phrase': 'jarvis', 'oww_threshold': .2}
    assert wake_profiles.normalize_wake_phrases(wake)
    rows = {r['phrase']: r for r in wake['phrases']}
    assert (rows['jarvis']['enabled'], rows['jarvis']['role'], rows['jarvis']['threshold']) == (True, 'samsara', .2)
    for phrase in ('computer', 'hey computer'):
        assert rows[phrase]['enabled'] is False
        assert rows[phrase]['role'] == 'samsara'
        assert rows[phrase]['threshold'] == wake_profiles.COMPUTER_WAKE_THRESHOLDS[phrase]
        assert rows[phrase]['model'].endswith(phrase.replace(' ', '_') + '.onnx')
    rows['computer'].update(enabled=True, threshold=.57, role='ava')
    wake['phrases'].remove(rows['hey computer'])
    assert not wake_profiles.normalize_wake_phrases(wake)
    assert rows['computer']['enabled'] and rows['computer']['threshold'] == .57
    assert rows['computer']['role'] == 'ava'
    assert not any(r['phrase'] == 'hey computer' for r in wake['phrases'])
    removed = {'phrases': []}
    assert not wake_profiles.normalize_wake_phrases(removed)
    assert removed['phrases'] == []


@pytest.mark.parametrize('phrase', ['computer', 'hey computer'])
def test_adding_computer_in_picker_stays_disabled(qapp, monkeypatch, phrase):
    from samsara.ui import dictionary_panel_qt as ui
    store = SimpleNamespace(custom_vocab=[], corrections_dict={}, get_builtin_words=lambda: [])
    monkeypatch.setattr(ui, 'get_dictionary_store', lambda app: store)
    monkeypatch.setattr(ui.DictionaryPanelQt, '_kv_load', lambda *a: None)
    monkeypatch.setattr(ui, 'QSettings', lambda *a: SimpleNamespace(value=lambda k, d=None, **kw: d))
    app = SimpleNamespace(config={'wake_word_config': {'phrases': []}}, update_config=Mock())
    panel = ui.DictionaryPanelQt(app)
    try:
        assert panel._wake_phrase_input.findText(phrase) >= 0
        panel._wake_phrase_input.setCurrentText(phrase)
        panel._wake_add()
        row = app.update_config.call_args.args[0]['wake_word_config']['phrases'][0]
        assert (row['phrase'], row['enabled'], row['role']) == (phrase, False, 'samsara')
        assert row['threshold'] == wake_profiles.COMPUTER_WAKE_THRESHOLDS[phrase]
        assert row['model'].endswith(phrase.replace(' ', '_') + '.onnx')
        table = panel._wake_table
        assert table.fontMetrics().horizontalAdvance(phrase) + 16 < table.columnWidth(1)
    finally:
        panel.close()
        panel.deleteLater()
