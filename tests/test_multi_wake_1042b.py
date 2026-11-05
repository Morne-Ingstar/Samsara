"""1042b: isolated primary phrase list, shared heads, UI and capture admission.

Synthetic score routing verifies wiring, not acoustic voice accuracy.
"""
from copy import deepcopy
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import wake_profiles as profiles
from samsara.wake_detector import WakeWordDetector


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path / 'profile'))


def entries(*phrases):
    return [dict(phrase=p, model='', enabled=True, threshold=.2, role='samsara') for p in phrases]


@pytest.mark.parametrize('relative', [True, False])
def test_model_guard_checkpoint_stays_in_profile(tmp_path, monkeypatch, relative):
    import json
    from pathlib import Path
    from samsara import model_guard

    cwd = tmp_path / 'cwd'
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    profile = tmp_path / 'profile'
    profile.mkdir()
    config_path = Path('config.json') if relative else tmp_path / 'explicit' / 'config.json'
    directory = profile if relative else config_path.parent
    directory.mkdir(exist_ok=True)
    saved = {'model_size': 'small.en', 'performance_mode': 'accurate', 'compute_type': 'float16'}
    (directory / 'config.json').write_text(json.dumps(saved), encoding='utf-8')
    app = SimpleNamespace(config_path=config_path, config=dict(saved))
    attempted = {**saved, 'model_size': 'base.en'}
    assert model_guard.protect_save(app, attempted)['model_size'] == 'small.en'
    model_guard.loaded(app)
    checkpoint = directory / 'config_backups' / 'model_guard.json'
    assert json.loads(checkpoint.read_text(encoding='utf-8')) == saved
    assert app._model_guard.checkpoint == checkpoint
    assert not (cwd / 'config_backups').exists()
    # Profile identity and baseline reads must be independent of later cwd changes.
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert model_guard.ModelGuard(app).profile == app._model_guard.profile
    assert not (elsewhere / 'config_backups').exists()


@pytest.fixture
def fake_model(monkeypatch):
    class Model:
        calls = []

        def __init__(self, wakeword_models, **kwargs):
            self.models = list(wakeword_models)
            self.frames = 0
            self.calls.append(self)

        def predict(self, audio):
            self.frames += 1
            return {'hey_jarvis': .8 if audio[0] == 1 else .01,
                    'alexa': .8 if audio[0] == 2 else .01,
                    'hey_mycroft': .8 if audio[0] == 3 else .01}

        def reset(self):
            pass
    # 1042b FINISH IT 5: synthetic tests must not initialize native backends.
    # An interrupted real package import leaves model.py bound to an incomplete
    # package and poisons the later bundled-model integration in a batch run.
    package = ModuleType('openwakeword')
    model = ModuleType('openwakeword.model')
    package.model = model
    model.Model = Model
    monkeypatch.setitem(sys.modules, 'openwakeword', package)
    monkeypatch.setitem(sys.modules, 'openwakeword.model', model)
    return Model


def test_one_model_each_head_fires_independently(fake_model):
    detector = WakeWordDetector(entries('hey jarvis', 'alexa'))
    assert len(fake_model.calls) == 1
    assert fake_model.calls[0].models == ['hey_jarvis', 'alexa']
    for trigger, expected in [(1, 'hey jarvis'), (2, 'alexa'), (0, None), (3, None)]:
        frame = np.full(1280, trigger, dtype=np.int16)
        assert detector.detected(frame) == expected
    assert fake_model.calls[0].frames == 4


def test_per_phrase_threshold_disabled_and_deduplication(fake_model):
    rows = entries('hey jarvis', 'alexa', 'jarvis')
    rows[0]['threshold'] = .9
    rows[1]['enabled'] = False
    rows[2]['threshold'] = .7
    detector = WakeWordDetector(rows)
    assert fake_model.calls[0].models == ['hey_jarvis']
    assert detector.detected(np.ones(1280, dtype=np.int16)) == 'jarvis'
    assert detector.detected(np.full(1280, 2, dtype=np.int16)) is None


def test_migrate_legacy_custom_phrase_and_threshold_idempotently(tmp_path, monkeypatch):
    from samsara.wake_setup import normalize_wake_phrase
    monkeypatch.setattr(profiles, 'WAKE_MODELS_DIR', tmp_path / 'no_bundle')
    cfg = {'wake_word_config': {'phrase': 'My Custom Phrase', 'oww_threshold': .43}}
    assert normalize_wake_phrase(cfg)
    row, ava = cfg['wake_word_config']['phrases']
    assert row == dict(phrase='my custom phrase', model='', enabled=True, threshold=.43, role='samsara')
    assert ava == dict(phrase='hey ava', model='', enabled=False, threshold=.2, role='ava')
    assert cfg['wake_word_config']['phrase'] == 'jarvis'
    assert cfg['wake_word_config']['phrase_fallback_pending']
    assert not normalize_wake_phrase(cfg)


def test_seed_does_not_mask_saved_legacy_values(tmp_path, monkeypatch):
    from samsara.config_seed import build_default_config
    from samsara.config_schema import SETTINGS_SCHEMA
    monkeypatch.setattr(profiles, 'WAKE_MODELS_DIR', tmp_path / 'no_bundle')
    seed = build_default_config(home=tmp_path, macro_defaults={}, notification_defaults={})
    seed['wake_word_config'].update(phrase='alexa', oww_threshold=.37)
    profiles.normalize_wake_phrases(seed['wake_word_config'])
    assert seed['wake_word_config']['phrases'] == [
        dict(phrase='alexa', model='alexa', enabled=True, threshold=.37, role='samsara'),
        dict(phrase='hey ava', model='', enabled=False, threshold=.2, role='ava')]
    assert SETTINGS_SCHEMA['wake_word_config.phrases']['item_type'] == 'dict'


def test_cap_preserves_disabled_row_and_fast_models():
    wake = {'phrases': entries('computer', 'my custom phrase', 'alexa')}
    profiles.normalize_wake_phrases(wake)
    assert [r['enabled'] for r in wake['phrases']] == [True, False, True]
    assert [r['phrase'] for r in wake['phrases']] == ['computer', 'my custom phrase', 'alexa']


def test_custom_models_in_both_directories(tmp_path, monkeypatch):
    bundle = tmp_path / 'bundle'
    user = tmp_path / 'profile' / 'wake_models'
    bundle.mkdir()
    user.mkdir(parents=True)
    monkeypatch.setattr(profiles, 'WAKE_MODELS_DIR', bundle)
    (bundle / 'hey_ava.onnx').write_bytes(b'synthetic')
    (user / 'hey_samsara.onnx').write_bytes(b'synthetic')
    assert profiles.resolve_wake_model('hey Ava') == str(bundle / 'hey_ava.onnx')
    assert profiles.resolve_wake_model('hey Samsara') == str(user / 'hey_samsara.onnx')
    assert profiles.resolve_wake_model('custom', '../escape.onnx') is None
    assert profiles.resolve_wake_model('custom', str(user / 'hey_samsara.onnx')) is not None


def test_model_failure_keeps_one_runtime_fallback(monkeypatch, fake_model):
    import openwakeword.model
    monkeypatch.setattr(openwakeword.model, 'Model', Mock(side_effect=RuntimeError('unavailable')))
    detector = WakeWordDetector(entries('hey jarvis', 'alexa', 'computer'))
    assert not detector.is_available
    assert detector.fallback_phrases == ('hey jarvis',)
    wake = {'phrases': entries('hey jarvis', 'alexa', 'computer')}
    assert profiles.match_primary_wake('alexa hello', wake, detector)[1] is False
    assert profiles.match_primary_wake('hey jarvis hello', wake, detector)[:2] == ('hey jarvis', True)


def test_empty_list_stays_off(fake_model):
    wake = {'phrase': 'jarvis', 'phrases': []}
    profiles.normalize_wake_phrases(wake)
    detector = profiles.detector_for_config(wake)
    assert not detector.is_available and not detector.requires_fallback
    assert not fake_model.calls
    assert profiles.match_primary_wake('jarvis', wake)[1] is False


def test_reload_threshold_and_enabled_changes(fake_model):
    wake = {'phrases': entries('hey jarvis', 'alexa')}
    app = SimpleNamespace(config={'wake_word_config': wake}, _wake_detector=None)
    profiles.refresh_primary_detector(app)
    first = app._wake_detector
    profiles.refresh_primary_detector(app)
    assert app._wake_detector is first
    wake['phrases'][1]['threshold'] = .95
    profiles.refresh_primary_detector(app)
    assert app._wake_detector is not first
    assert app._wake_detector.detected(np.full(1280, 2, dtype=np.int16)) is None
    wake['phrases'][0]['enabled'] = False
    profiles.refresh_primary_detector(app)
    assert app._wake_detector.detected(np.ones(1280, dtype=np.int16)) is None


def test_config_factory_seam_and_legacy_phrase_edit():
    built = []

    def factory(phrase, **kwargs):
        built.append((phrase, kwargs))
        return SimpleNamespace(_wake_phrase=phrase)

    app = SimpleNamespace(config={'wake_word_config': {'phrase': 'alexa', 'oww_threshold': .37}},
                          _wake_detector=None)
    profiles.refresh_primary_detector(app, factory=factory)
    assert built[0] == ('alexa', dict(threshold=.37, model_path='alexa'))
    assert app.config['wake_word_config']['phrases'][1]['role'] == 'ava'
    assert not app.config['wake_word_config']['phrases'][1]['enabled']
    app.config['wake_word_config']['phrase'] = 'jarvis'
    profiles.refresh_primary_detector(app, {'phrase': 'jarvis'}, factory=factory)
    assert built[-1] == ('jarvis', dict(threshold=.37, model_path='hey_jarvis'))


@pytest.mark.parametrize('value', [None, 'bad', float('nan'), float('inf'), -.1, 2, True])
def test_bad_threshold_is_bounded(value):
    assert profiles.wake_threshold(value) == .2


def test_match_each_phrase_and_longer_alias():
    wake = {'phrases': entries('jarvis', 'hey jarvis', 'alexa', 'hey mycroft')}
    for phrase in ('hey jarvis', 'alexa', 'hey mycroft'):
        matched_phrase, ok, _, index = profiles.match_primary_wake(phrase + ' hello', wake)
        assert (matched_phrase, ok, index) == (phrase, True, 0)
    wake['phrases'][2]['enabled'] = False
    assert profiles.match_primary_wake('alexa hello', wake)[1] is False


def test_mixed_fast_slow_admits_one_decode(fake_model):
    from samsara.audio_engine.wake_consumer import WakeConsumer
    detector = WakeWordDetector(entries('hey jarvis', 'computer'))
    app = SimpleNamespace(app_state='asleep', wake_word_triggered=False, _wake_detector=detector,
                          _wake_profile_detectors={}, _oww_primary_hit=False, _oww_profile_hits=set(),
                          _oww_wake_detected=False, _wake_rearm_needs_onset=False, config={},
                          process_wake_word_buffer=Mock(), _close_hands_free_capture_duck=Mock())
    consumer = WakeConsumer.__new__(WakeConsumer)
    consumer._app = app
    consumer._hands_free_capture_duck_token = None
    consumer._is_toggle_cmd = lambda a: False
    consumer._flush([np.zeros(160, dtype=np.float32)], owner_token=5)
    app.process_wake_word_buffer.assert_called_once()


def test_no_enabled_primary_phrase_drops_before_decode(fake_model):
    from samsara.audio_engine.wake_consumer import WakeConsumer
    app = SimpleNamespace(app_state='asleep', wake_word_triggered=False,
                          _wake_detector=WakeWordDetector([]), _wake_profile_detectors={},
                          _oww_primary_hit=False, _oww_profile_hits=set(), _oww_wake_detected=False,
                          _wake_rearm_needs_onset=False, config={}, process_wake_word_buffer=Mock(),
                          _close_hands_free_capture_duck=Mock())
    consumer = WakeConsumer.__new__(WakeConsumer)
    consumer._app = app
    consumer._hands_free_capture_duck_token = None
    consumer._is_toggle_cmd = lambda a: False
    consumer._flush([np.zeros(160, dtype=np.float32)], owner_token=5)
    app.process_wake_word_buffer.assert_not_called()


def test_ui_cap_add_remove_save_and_threshold(qapp, monkeypatch):
    from PySide6.QtGui import QAccessible
    from samsara.ui import dictionary_panel_qt as ui
    store = SimpleNamespace(custom_vocab=[], corrections_dict={}, get_builtin_words=lambda: [])
    monkeypatch.setattr(ui, 'get_dictionary_store', lambda app: store)
    monkeypatch.setattr(ui.DictionaryPanelQt, '_kv_load', lambda *a: None)
    monkeypatch.setattr(ui, 'QSettings', lambda *a: SimpleNamespace(value=lambda k,d=None,**kw: d))
    app = SimpleNamespace(config={'wake_word_config': {'phrases': entries('computer'),
                          'audio': {'speech_threshold': .015}, 'end_words': ['over']}}, update_config=Mock())
    panel = ui.DictionaryPanelQt(app)
    try:
        panel.resize(560, 740)
        panel._tabs.setCurrentIndex(2)
        panel.show()
        qapp.processEvents()
        assert (panel._wake_table.columnViewportPosition(4) + panel._wake_table.columnWidth(4)
                <= panel._wake_table.viewport().width())
        item = panel._wake_table.item(0, 2)
        assert item.text() == 'slow - no model'
        assert 'recognizer' in item.toolTip()
        assert item.data(ui.Qt.ItemDataRole.AccessibleDescriptionRole) == item.toolTip()
        assert panel._wake_table.fontMetrics().horizontalAdvance(item.text()) + 16 < panel._wake_table.columnWidth(2)
        for control, name, native_role in (
            (panel._wake_table, 'Wake phrases', QAccessible.Role.Table),
            (panel._wake_table.cellWidget(0, 0), 'Enable computer', QAccessible.Role.CheckBox),
            (panel._wake_table.cellWidget(0, 3), 'computer threshold', QAccessible.Role.SpinBox),
            (panel._wake_table.cellWidget(0, 4), 'computer role', QAccessible.Role.ComboBox),
        ):
            interface = QAccessible.queryAccessibleInterface(control)
            assert interface.text(QAccessible.Text.Name) == name
            assert interface.role() == native_role
            assert not interface.state().invisible
        panel._wake_phrase_input.setCurrentText('another phrase')
        panel._wake_add()
        assert panel._wake_entries[-1]['enabled'] is False
        panel._wake_table.cellWidget(1, 0).setChecked(True)
        assert not panel._wake_table.cellWidget(1, 0).isChecked()
        assert 'Only one slow phrase' in panel._wake_status.text()
        panel._wake_table.cellWidget(0, 0).setChecked(False)
        panel._wake_table.cellWidget(1, 0).setChecked(True)
        assert panel._wake_entries[1]['enabled']
        panel._wake_table.cellWidget(1, 3).setValue(.45)
        role = panel._wake_table.cellWidget(1, 4)
        assert role.currentData() == 'samsara'
        role.setCurrentIndex(role.findData('ava'))
        saved = app.update_config.call_args.args[0]['wake_word_config']['phrases']
        assert saved[1]['threshold'] == .45
        assert saved[1]['role'] == 'ava'
        assert app.update_config.call_args.args[0]['wake_word_config']['audio'] == {'speech_threshold': .015}
        assert app.update_config.call_args.args[0]['wake_word_config']['end_words'] == ['over']
        panel._wake_table.selectRow(0)
        panel._wake_remove()
        assert len(panel._wake_entries) == 1
        assert app.config['wake_word_config']['phrases'] == entries('computer')  # local edits use copies
        # A known model that failed to load must not keep claiming fast detection.
        import json
        app._wake_detector = SimpleNamespace(config_signature=json.dumps(panel._wake_entries, sort_keys=True),
                                             _heads=[], fallback_phrases=('another phrase',))
        panel._wake_update_statuses()
        assert panel._wake_table.item(0, 2).text() == 'slow - no model'  # 1042b FINISH IT 4: detail moved to tooltip.
        panel._wake_entries[0]['model'] = 'alexa'
        app._wake_detector.config_signature = json.dumps(panel._wake_entries, sort_keys=True)
        panel._wake_update_statuses()
        item = panel._wake_table.item(0, 2)
        assert item.text() == 'slow - failed'
        assert 'recognizer' in item.toolTip()
        assert item.data(ui.Qt.ItemDataRole.AccessibleDescriptionRole) == item.toolTip()
        app._wake_detector.fallback_phrases = ()
        panel._wake_update_statuses()
        assert item.text() == 'not listening'
    finally:
        panel.close()
        panel.deleteLater()


def test_roles_migrate_once_preserve_explicit_choices_and_removal():
    wake = {'phrase': 'jarvis', 'oww_threshold': .38}
    assert profiles.normalize_wake_phrases(wake)
    assert [(r['phrase'], r['role'], r['enabled']) for r in wake['phrases']] == [
        ('jarvis', 'samsara', True), ('hey ava', 'ava', False)]
    wake['phrases'][0]['role'] = 'ava'
    wake['phrases'][1]['role'] = 'samsara'
    assert not profiles.normalize_wake_phrases(wake)
    wake['phrases'].pop()
    assert not profiles.normalize_wake_phrases(wake)  # Removed Ava never reappears.
    assert wake['phrases'][0]['role'] == 'ava'
    wake['phrases'][0]['role'] = 'invalid'
    assert profiles.normalize_wake_phrases(wake)
    assert wake['phrases'][0]['role'] == 'samsara'


def test_ava_model_drop_in_preserves_role_and_fallback_cap(tmp_path, monkeypatch):
    bundle = tmp_path / 'models'
    bundle.mkdir()
    monkeypatch.setattr(profiles, 'WAKE_MODELS_DIR', bundle)
    wake = {'phrase': 'jarvis'}
    profiles.normalize_wake_phrases(wake)
    assert wake['phrases'][1]['role'] == 'ava'
    assert not wake['phrases'][1]['model']
    (bundle / 'hey_ava.onnx').write_bytes(b'synthetic')
    profiles.normalize_wake_phrases(wake)
    assert wake['phrases'][1]['model'] == str(bundle / 'hey_ava.onnx')
    assert wake['phrases'][1]['role'] == 'ava'
    assert not wake['phrases'][1]['enabled']
    fresh = {'phrase': 'jarvis'}
    profiles.normalize_wake_phrases(fresh)
    assert not fresh['phrases'][1]['enabled']  # A bundled model cannot auto-enable Ava.
    wake['phrases'][1]['enabled'] = True
    profiles.normalize_wake_phrases(wake)
    assert wake['phrases'][1]['enabled']  # Keep an explicit owner opt-in.


def test_legacy_edit_during_load_replaces_the_old_listener(fake_model):
    wake = {'phrase': 'jarvis', 'oww_threshold': .37}
    app = SimpleNamespace(config={'wake_word_config': wake}, _wake_detector=None)
    profiles.refresh_primary_detector(app)
    wake['phrase'] = 'alexa'  # Edited after the loader constructed the first head.
    profiles.refresh_primary_detector(app)
    assert wake['phrases'][0]['phrase'] == 'alexa'
    assert wake['phrases'][0]['threshold'] == .37
    assert app._wake_detector.detected(np.full(1280, 2, dtype=np.int16)) == 'alexa'
    assert app._wake_detector.detected(np.ones(1280, dtype=np.int16)) is None


@pytest.fixture
def role_app(monkeypatch):
    from samsara import wake_roles
    import sys
    rows = entries('jarvis', 'hey ava', 'alexa')
    rows[1]['role'] = 'ava'
    app = SimpleNamespace(config={'wake_word_config': {'phrases': rows}},
                          _wake_detector=None, wake_word_triggered=False,
                          _start_wake_timeout=Mock(), wake_word_timer=Mock())
    # Run dispatch inline only in these unit tests; no provider code is loaded.
    monkeypatch.setattr(wake_roles.thread_registry, 'spawn', lambda name, fn, **kw: fn())
    handler = Mock()
    monkeypatch.setitem(sys.modules, 'plugins.commands.ask_ollama', SimpleNamespace(handle_ask_ava=handler))
    return app, handler


def test_ava_same_utterance_bypasses_samsara_and_uses_own_role(role_app):
    from samsara.wake_roles import route_transcript
    from samsara.audio_engine.wake_consumer import AdmittedWakeBuffer
    app, handler = role_app
    audio = AdmittedWakeBuffer([], primary_hit=True, primary_phrase='hey ava', primary_role='ava')
    # Simulate the *next* detector result and a role edit before FIFO completion.
    app._last_wake_phrase = 'jarvis'
    app.config['wake_word_config']['phrases'][1]['role'] = 'samsara'
    assert route_transcript(app, 'Hey Ava, explain gravity', audio)
    handler.assert_called_once_with(app, remainder='explain gravity')
    assert not app.wake_word_triggered
    handler.reset_mock()
    assert not route_transcript(app, 'Jarvis, type hello', [])
    handler.assert_not_called()


def test_slow_ava_and_bare_wake_listen_before_dispatch(role_app):
    from samsara.wake_roles import route_transcript
    app, handler = role_app
    assert route_transcript(app, 'hey Ava', [])
    handler.assert_not_called()  # No greeting, provider probe or confirmation.
    assert app.wake_word_triggered
    app._start_wake_timeout.assert_called_once_with(timeout_s=5.0)
    assert route_transcript(app, 'Explain gravity', [])
    handler.assert_called_once_with(app, remainder='Explain gravity')


def test_ava_timeout_disabled_phrase_false_hit_and_samsara_override(role_app, monkeypatch):
    from samsara import wake_roles
    from samsara.audio_engine.wake_consumer import AdmittedWakeBuffer
    app, handler = role_app
    assert wake_roles.route_transcript(app, 'hey ava', [])
    monkeypatch.setattr(wake_roles.time, 'monotonic', lambda: app._ava_wake_pending_until + 1)
    assert not wake_roles.route_transcript(app, 'unrelated room speech', [])
    app.wake_word_triggered = False
    app.config['wake_word_config']['phrases'][1]['enabled'] = False
    assert not wake_roles.route_transcript(app, 'hey ava hello', [])
    false_hit = AdmittedWakeBuffer([], primary_hit=True, primary_phrase='hey ava', primary_role='ava')
    assert wake_roles.route_transcript(app, 'unrelated room speech', false_hit)
    handler.assert_not_called()
    app.config['wake_word_config']['phrases'][0]['role'] = 'ava'
    assert wake_roles.route_transcript(app, 'Jarvis explain gravity', [])
    handler.assert_called_once_with(app, remainder='explain gravity')


def test_primary_role_metadata_survives_flush_and_capture_clear(monkeypatch):
    from samsara.audio_engine.wake_consumer import WakeConsumer
    from samsara import wake_roles
    rows = entries('alexa')
    rows[0]['role'] = 'ava'
    app = SimpleNamespace(config={'wake_word_config': {'phrases': rows}},
                          app_state='asleep', wake_word_triggered=False,
                          _wake_detector=SimpleNamespace(is_available=True, reset=Mock()),
                          _oww_primary_hit=True, _oww_wake_detected=True,
                          _oww_profile_hits=set(), _wake_rearm_needs_onset=False,
                          process_wake_word_buffer=Mock())
    consumer = WakeConsumer.__new__(WakeConsumer)
    consumer._app = app
    consumer._utterance_frames = [np.ones(160, dtype=np.float32)]
    consumer._hands_free_capture_duck_token = None
    monkeypatch.setattr(wake_roles.AvaWakeStream, 'start', Mock())
    wake_roles.begin_primary_capture(consumer, 'alexa', consumer._utterance_frames[0])
    stream = app._ava_wake_stream
    wake_roles.begin_primary_capture(consumer, 'jarvis', consumer._utterance_frames[0])
    consumer._flush(consumer._utterance_frames)
    audio = app.process_wake_word_buffer.call_args.args[0]
    assert (audio.primary_phrase, audio.primary_role) == ('alexa', 'ava')
    assert stream.stopped.is_set()
    assert app._oww_primary_phrase is None
    assert app._ava_wake_stream is None


def test_ava_local_partial_starts_before_endpoint_and_stops_without_provider(monkeypatch):
    import threading
    from samsara import wake_roles, vocabulary_decode
    # Exercise the worker without loading a recognizer or provider.
    app = SimpleNamespace(model_lock=threading.Lock(), model_rate=16000,
                          get_transcription_params=lambda **kw: {})
    frames = [np.ones(16000, dtype=np.float32) * .1]
    stream = wake_roles.AvaWakeStream(app, lambda: frames)
    calls = []
    def transcribe(host, audio, **params):
        calls.append((host, audio.copy(), params))
        # End capture while lazy inference is running: no stale partial publish.
        stream.close()
        return iter([SimpleNamespace(text='Hey Ava hello')]), None
    monkeypatch.setattr(vocabulary_decode, 'transcribe_with_vocabulary', transcribe)
    stream._run()
    assert len(calls) == 1
    assert calls[0][2]['path'] == 'ava'
    assert calls[0][2]['beam_size'] == 1
    np.testing.assert_array_equal(calls[0][1], frames[0])
    assert stream.partial_text == ''
    assert not app.model_lock.locked()


def test_real_decode_routes_ava_before_media_or_command_parsers(role_app, monkeypatch):
    # Reuse the repository's source-extracted real decoder harness. No import
    # of dictation (or bootstrapping an app) is needed for this integration.
    from tests.test_wake_gate_in_session import _Harness, _NS
    from samsara import mic_score, speech_presence
    from samsara.audio_engine import wake_consumer
    from samsara.audio_engine.wake_consumer import AdmittedWakeBuffer
    _, handler = role_app
    app = _Harness(app_state='asleep')
    app.config['wake_word_config']['phrases'] = [dict(
        phrase='hey ava', model='', role='ava', enabled=True, threshold=.2)]
    app._wake_detector = None
    app._probe_media_transport = Mock(side_effect=AssertionError('Ava reached media routing'))
    app._process_wake_command = Mock(side_effect=AssertionError('Ava reached command routing'))
    monkeypatch.setitem(_NS, 'get_dictionary_store', lambda a: SimpleNamespace(apply_corrections=lambda t: t))
    monkeypatch.setitem(_NS, 'transcribe_with_vocabulary', lambda *a, **kw: (
        [SimpleNamespace(text='hey Ava explain gravity')], SimpleNamespace(language='en')))
    monkeypatch.setattr(wake_consumer, 'pending_short_answer_gate', lambda *a: None)
    monkeypatch.setattr(speech_presence, 'reject_wake', lambda *a: False)
    monkeypatch.setattr(mic_score, 'open_history', lambda *a: None)
    monkeypatch.setattr(mic_score, 'remember_segments', lambda *a: None)
    monkeypatch.setattr(mic_score, 'close_history', lambda *a: None)
    audio = AdmittedWakeBuffer([np.ones(16000, dtype=np.float32) * .1],
                              primary_hit=True, primary_phrase='hey ava', primary_role='ava')
    app._decode_wake_word_buffer(audio, src_rate=16000, oww_confirmed=True)
    handler.assert_called_once_with(app, remainder='explain gravity')
    app._probe_media_transport.assert_not_called()
    app._process_wake_command.assert_not_called()


def test_other_profile_hit_cannot_authorize_fast_ava(role_app):
    from samsara.wake_roles import route_transcript
    from samsara.audio_engine.wake_consumer import AdmittedWakeBuffer
    app, handler = role_app
    app._wake_detector = SimpleNamespace(_heads=[('hey ava', 'hey_ava', .2)], fallback_phrases=())
    audio = AdmittedWakeBuffer([], profile_hits=('other-profile',))
    assert not route_transcript(app, 'hey ava hello', audio)
    handler.assert_not_called()


def test_bundled_custom_heads_load_together_and_ava_stays_opt_in():
    """Actual ONNX inference and shipped bytes, without voice/owner readiness claims."""
    import hashlib
    from pathlib import Path
    pins = {
        'hey samsara': '5f56ab3582138fe0f4124931c79dce32dd44a20a4a2137fbd5d42631223c0491',
        'hey ava': 'a47bcad5eb67f275b8410d7bad4c387e499990dd2d448fc54dc07a20b48f5fd5',
    }
    for phrase, expected in pins.items():
        model = Path(profiles.resolve_wake_model(phrase))
        assert model.parent == profiles.WAKE_MODELS_DIR
        assert hashlib.sha256(model.read_bytes()).hexdigest() == expected
    wake = {'phrase': 'jarvis'}
    profiles.normalize_wake_phrases(wake)
    ava = next(r for r in wake['phrases'] if r['phrase'] == 'hey ava')
    assert not ava['enabled'] and ava['role'] == 'ava'
    assert profiles.resolve_wake_model('hey ava', ava['model'])
    detector = WakeWordDetector(entries(*pins))
    assert detector.is_available and not detector.requires_fallback
    assert {key for _, key, _ in detector._heads} == {'hey_samsara', 'hey_ava'}
    scores = detector._model.predict(np.zeros(1280, dtype=np.int16))
    assert set(scores) == {'hey_samsara', 'hey_ava'}
    assert all(np.isfinite(score) for score in scores.values())
    detector.reset()


def test_orphaned_backend_import_is_reinitialized(fake_model, tmp_path, monkeypatch):
    """Retry after package initialization fails with a child still cached."""
    backend = tmp_path / 'openwakeword'
    backend.mkdir()
    (backend / '__init__.py').write_text('from .model import Model\nready = True\n')
    (backend / 'model.py').write_text(
        'import openwakeword\n'
        'class Model:\n'
        '    def __init__(self, wakeword_models, **kwargs):\n'
        '        assert openwakeword.ready\n'
        '        self.models = wakeword_models\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, 'openwakeword')

    def interrupted_parent(*args, **kwargs):
        raise AttributeError("module 'openwakeword' has no attribute 'get_pretrained_model_paths'")

    monkeypatch.setattr(fake_model, '__init__', interrupted_parent)
    # The abandoned child remains, just as it does after an import exception.
    assert 'openwakeword.model' in sys.modules
    detector = WakeWordDetector(entries('hey jarvis', 'alexa'))
    assert detector.is_available and not detector.requires_fallback
    assert type(detector._model) is not fake_model
    assert detector._model.models == ['hey_jarvis', 'alexa']


@pytest.mark.parametrize('epoch', [3, 4])
def test_device_epoch_cleanup_preserves_speech_stop_observer(monkeypatch, epoch):
    """Current-master speech stop still observes frames after phrase cleanup."""
    import threading
    from samsara import wake_roles
    from samsara.audio_engine import speech_stop, wake_consumer

    app = SimpleNamespace(config={}, wake_word_active=True, app_state='asleep',
                          _oww_primary_phrase='hey ava', _oww_primary_role='ava',
                          _oww_primary_hit=True, _oww_wake_detected=True,
                          _oww_profile_hits={'other'}, _oww_primary_hit_ns=100,
                          _oww_profile_hit_ns={'other': 100}, _vad_reset=Mock())
    stream = app._ava_wake_stream = wake_roles.AvaWakeStream(app, lambda: [])
    listener = SimpleNamespace(consume=Mock(), accepted=threading.Event(), quiet_until=float('inf'))
    monkeypatch.setattr(speech_stop, 'SpeechStopListener', lambda host: listener)
    monkeypatch.setattr(wake_consumer, 'resume_wake_ready_output', lambda host: None)
    monkeypatch.setattr(wake_consumer.mic_score, 'prepare_wake_frame', lambda consumer: True)
    consumer = wake_consumer.WakeConsumer.__new__(wake_consumer.WakeConsumer)
    consumer._app = app
    consumer._mouth_shadow = None
    consumer._last_epoch = 3
    consumer._log_frame = Mock()
    consumer._close_hands_free_duck_safe = Mock()
    consumer._hands_free_capture_duck_token = 7
    consumer._utterance_frames = [np.ones(160, dtype=np.float32)]
    consumer._buffer_rms_history = [.1]
    consumer._last_voice_ns = consumer._speech_start_ns = 100
    consumer._onset_context = object()
    frame = SimpleNamespace(device_epoch=epoch)

    consumer._process_frame(frame)

    listener.consume.assert_called_once_with(frame)
    assert consumer._last_epoch == epoch
    changed = epoch != 3
    assert stream.stopped.is_set() == changed
    assert app._oww_primary_hit == (not changed)
    assert app._oww_primary_phrase == (None if changed else 'hey ava')
    assert app._oww_primary_role == (None if changed else 'ava')
    assert app._oww_profile_hits == (set() if changed else {'other'})
    assert len(consumer._utterance_frames) == (0 if changed else 1)
    assert app._vad_reset.call_count == int(changed)
