"""919: honest wake choices, first-run capture and connected calibration routes.

Synthetic microphones and model scores only; no dictation bootstrap or user files.
"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import numpy as np
import pytest
from PySide6.QtWidgets import QCheckBox, QComboBox, QVBoxLayout, QWidget

from samsara import config_defaults, config_seed
from samsara.config_schema import SETTINGS_SCHEMA
from samsara.constants import DEFAULT_WAKE_PHRASE, DEFAULT_WAKE_PHRASE_OPTIONS
from samsara.ui import first_run_wizard_qt as setup, home_qt, home_signals
from samsara.ui import mic_setup_wizard_qt as mic, wake_setup_qt as wake_ui
from samsara.wake_setup import (calibration_needed, modelled_phrases,
                                normalize_wake_phrase, MORE_WAKE_WORDS, FALLBACK_NOTE)


@pytest.fixture
def isolated(qapp, monkeypatch):
    monkeypatch.setattr(setup.thread_registry, 'spawn', lambda *a, **k: None)
    monkeypatch.setattr(setup._WizardWindow, '_start_meter', lambda self: None)
    monkeypatch.setattr(mic._WizardWindow, '_ensure_audio_running', lambda self: None)
    monkeypatch.setattr(mic._WizardWindow, '_setup_oww_test', lambda self: None)
    yield
    for widget in qapp.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    qapp.processEvents()


def config(enabled=True):
    return {'wake_word_enabled': enabled, 'microphone': 7,
            'wake_word_config': {'phrase': 'jarvis', 'audio': {'wake_command_timeout': 9}}}


def test_defaults_are_empty_and_saved_profiles_survive(tmp_path):
    seed = config_seed.build_default_config(home=tmp_path, macro_defaults={}, notification_defaults={})
    assert seed['wake_profiles'] == SETTINGS_SCHEMA['wake_profiles']['default'] == []
    assert config_defaults.DEFAULTS['wake_profiles'] == []
    assert seed['wake_word_config']['phrase_options'] == modelled_phrases()
    personal = [{'id': 'custom', 'phrase': 'my private phrase', 'enabled': True}]
    saved = tmp_path / 'config.json'
    import json
    saved.write_text(json.dumps({'wake_profiles': personal}), encoding='utf-8')
    from samsara.ui.first_run_pages_qt import initial_config
    merged, _ = initial_config(setup._DEFAULTS, saved)
    assert merged['wake_profiles'] == personal


def test_choices_match_shipped_detector_models():
    from samsara.runtime_manifest import OWW_MODEL_FILENAMES
    from samsara.wake_detector import PHRASE_TO_MODEL
    assert DEFAULT_WAKE_PHRASE == 'jarvis'  # 919 FINISH IT 4: keep the owner's original phrase.
    assert modelled_phrases() == ['jarvis']
    assert PHRASE_TO_MODEL['jarvis'] == 'hey_jarvis'
    assert DEFAULT_WAKE_PHRASE_OPTIONS == modelled_phrases()
    assert setup.Decisions.wake_phrases() == modelled_phrases()
    assert all(any(name.startswith(PHRASE_TO_MODEL[p] + '_v') for name in OWW_MODEL_FILENAMES)
               for p in modelled_phrases())


@pytest.mark.parametrize('phrase', ['jarvis', 'hey jarvis', 'computer', 'alexa', 'hey mycroft', 'hey rhasspy'])
def test_custom_choice_requires_opt_in_and_preserves_saved_phrase(isolated, phrase):
    host = QWidget()
    layout = QVBoxLayout(host)
    combo = QComboBox()
    layout.addWidget(combo)
    note = wake_ui.configure_phrase_choice(combo, phrase, layout)
    assert [combo.itemText(i) for i in range(combo.count())] == ['Jarvis', MORE_WAKE_WORDS]
    assert combo.currentText() == 'Jarvis' and combo.currentData() == 'jarvis'
    assert not combo.isEditable() and not host.findChildren(QCheckBox)
    assert combo.model().item(0).isEnabled()
    assert not combo.model().item(1).isEnabled()
    assert not combo.model().item(1).isSelectable()
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    QTest.keyClick(combo, Qt.Key.Key_Down)
    assert combo.currentText() == 'Jarvis' and combo.currentData() == 'jarvis'
    assert note.isHidden() == (phrase == 'jarvis')
    assert note.text() == FALLBACK_NOTE


@pytest.mark.parametrize('value', [None, 0, -1, 'bad', float('nan'), float('inf'), True])
def test_invalid_calibration_keeps_notice(value):
    cfg = config()
    cfg['wake_word_config']['audio']['measured_noise_floor'] = value
    notice = home_signals.problem_notice(SimpleNamespace(config=cfg))
    assert notice.headline == wake_ui.CALIBRATION_NOTE
    assert notice.action == 'calibrate_wake_word'


def test_off_success_and_changed_microphone():
    cfg = config(False)
    assert not calibration_needed(cfg)
    cfg['wake_word_enabled'] = True
    cfg['wake_word_config']['audio'].update(measured_noise_floor=0.005, calibrated_microphone=7)
    assert not calibration_needed(cfg)
    assert home_signals.problem_notice(SimpleNamespace(config=cfg)) is None
    cfg['microphone'] = 8
    assert calibration_needed(cfg)


def test_real_fault_takes_precedence_over_calibration():
    app = SimpleNamespace(config=config(), available_mics=[])
    notice = home_signals.problem_notice(app)
    assert notice.capability == home_signals.DICTATION


def make_guide():
    app = wake_ui.SetupWakeApp(config(), [{'id': 7, 'name': 'Synthetic mic'}])
    return app, mic.open_wake_calibration(app)


def test_direct_guide_starts_on_wake_and_skip_does_not_complete(isolated):
    app, guide = make_guide()
    assert guide._current_step == guide._STEP_WAKE
    assert guide._back_btn.isHidden()
    assert mic.open_wake_calibration(app) is guide
    done = Mock()
    guide.finished.connect(done)
    guide._skip_step()
    assert done.call_count == 1
    assert calibration_needed(app.config)


def test_only_current_success_clears_notice_and_merges_config(isolated):
    app, guide = make_guide()
    generation = guide._wake_cal_generation
    guide._on_wake_calibration_result(generation - 1, 0.006, '', {})
    assert calibration_needed(app.config)
    guide._on_wake_calibration_result(generation, None, 'unavailable', {})
    assert calibration_needed(app.config)
    guide._on_wake_calibration_result(generation, 0.006, '',
                                     {'frames': 30, 'seconds': 3.0, 'status': 'ok'})
    assert not calibration_needed(app.config)
    assert home_signals.problem_notice(app) is None
    assert app.config['wake_word_config']['audio']['wake_command_timeout'] == 9
    guide.close()


def test_closed_calibration_ignores_late_result(isolated):
    app, guide = make_guide()
    generation = guide._wake_cal_generation
    guide.close()
    guide._on_wake_calibration_result(generation, 0.005, '', {})
    assert calibration_needed(app.config)


def test_setup_next_offers_calibration_once_and_skip_continues(isolated, tmp_path):
    win = setup._WizardWindow(tmp_path / 'config.json')
    win._mics = [{'id': 7, 'name': 'Synthetic mic'}]
    win._config['microphone'] = 7
    win._decisions.wake.buttons()[0].setChecked(True)
    win._step = 4
    win._show_step()
    win._go_next()
    assert win._step == 4
    guide = win._wake_setup_app._wake_calibration_window
    assert guide._current_step == guide._STEP_WAKE
    guide._skip_step()
    assert win._step == 5
    assert calibration_needed(win._config)


def test_setup_turning_on_runs_calibration_immediately(isolated, tmp_path):
    win = setup._WizardWindow(tmp_path / 'config.json')
    win._decisions.wake.buttons()[1].setChecked(True)
    win._decisions.wake.buttons()[0].click()
    assert win._wake_setup_app.config['wake_word_enabled']
    assert win._wake_setup_app._wake_calibration_window._current_step == mic._WizardWindow._STEP_WAKE


def test_setup_saves_canonical_phrase_and_preserves_existing_calibration(isolated, tmp_path):
    win = setup._WizardWindow(tmp_path / 'config.json')
    win._decisions.collect('Talking')
    assert win._decisions.phrase.currentText() == 'Jarvis'
    assert win._config['wake_word_config']['phrase'] == 'jarvis'
    cfg = config()
    cfg['wake_word_config']['audio'].update(measured_noise_floor=0.004, calibrated_microphone=7)
    normalize_wake_phrase(cfg)
    assert not calibration_needed(cfg)
    assert not cfg['wake_word_config'].get('phrase_fallback_pending', False)
    assert not normalize_wake_phrase(cfg)


def test_closing_setup_cancels_its_calibration_without_advancing(isolated, tmp_path):
    win = setup._WizardWindow(tmp_path / 'config.json')
    win._step = 4
    win._decisions.wake.buttons()[0].setChecked(True)
    win._go_next()
    guide = win._wake_setup_app._wake_calibration_window
    generation = guide._wake_cal_generation
    win.close()
    assert not guide.isVisible() and win._step == 4 and win.result is None
    guide._on_wake_calibration_result(generation, 0.004, '', {})
    assert calibration_needed(win._config)


def test_home_button_opens_direct_wake_step_and_success_hides_notice(isolated, monkeypatch):
    from tests.test_home_qt import _app, _FakeSettings
    monkeypatch.setattr(home_qt, 'QSettings', _FakeSettings)
    monkeypatch.setattr(home_signals, 'corrections_waiting', lambda app: 0)
    monkeypatch.setattr(home_signals, '_diag_records', lambda app, limit: [])
    monkeypatch.setattr(home_signals, 'generic_hints', lambda app: [])
    app = _app()
    app.get_available_microphones = lambda: app.available_mics
    app.update_config = lambda updates, save=True: app.config.update(updates)
    home = home_qt.HomePage(app)
    assert home._notice.action == 'calibrate_wake_word'
    home._notice_btn.click()
    guide = app._wake_calibration_window
    assert guide._current_step == guide._STEP_WAKE
    guide._on_wake_calibration_result(guide._wake_cal_generation, 0.004, '', {})
    guide.close()
    assert home._notice is None
    assert home._notice_card.isHidden()


def test_first_run_capture_uses_selected_mic_and_does_not_save_in_worker(monkeypatch):
    import sounddevice as sd
    from samsara import audio_devices
    streams = []
    class Stream:
        def __init__(self, **kwargs):
            streams.append(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            return np.full((size, 2), 0.006, dtype=np.float32), False
    monkeypatch.setattr(sd, 'InputStream', Stream)
    monkeypatch.setattr(sd, 'query_devices', lambda *a, **k: {'max_input_channels': 2})
    monkeypatch.setattr(audio_devices, 'detect_capture_rate', lambda device: 48000)
    app = wake_ui.SetupWakeApp(config(), [])
    original = deepcopy(app.config)
    floor = app.calibrate_wake_mic(seconds=0.3)
    assert floor == pytest.approx(0.006, abs=0.0001)
    assert streams[0]['device'] == 7 and streams[0]['channels'] == 2
    assert app.last_wake_calibration['frames'] == 3
    assert app.config == original
    cancelled = threading.Event()
    cancelled.set()
    assert app.calibrate_wake_mic(cancel_event=cancelled) is None
    assert app.config == original


def test_tutorial_checks_calibration_before_wake_instructions(isolated):
    from samsara.ui.tutorial_qt import TutorialWindow
    from PySide6.QtWidgets import QLabel, QPushButton
    app = wake_ui.SetupWakeApp(config(), [])
    win = TutorialWindow(app)
    win._step = next(i for i, (key, _) in enumerate(win._steps) if key == 'ways')
    win._show_step()
    detail = win.findChild(QLabel, 'tutorialWakeInstruction')
    button = win.findChild(QPushButton, 'tutorialWakeCalibrate')
    assert detail.text() == wake_ui.CALIBRATION_NOTE and not button.isHidden()
    app.config['wake_word_config']['audio']['measured_noise_floor'] = 0.004
    wake_ui.refresh_tutorial_wake(win)
    assert button.isHidden() and 'Say "jarvis"' in detail.text()


def test_settings_ignores_legacy_options_and_preserves_saved_custom(isolated, monkeypatch):
    from samsara import autostart
    from samsara.ui import settings_qt
    from samsara.ui.first_run_qt import ComponentsPage
    from tests.test_settings_pages import _StubApp, _schema_defaults
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda self: None)
    cfg = deepcopy(_schema_defaults())
    cfg['wake_word_config'] = {'phrase': 'computer', 'phrase_options': ['computer', 'samsa']}
    window = settings_qt._SettingsWindow(_StubApp(cfg))
    window.show_tab('Dictation')
    combo = window._widgets['wake_word_phrase']
    # 919 FINISH IT 2: legacy/custom choices must fall back with a visible notice.
    assert [combo.itemText(i) for i in range(combo.count())] == ['Jarvis', MORE_WAKE_WORDS]
    assert not combo.isEditable() and combo.currentText() == 'Jarvis'
    assert combo.currentData() == 'jarvis'
    assert not combo.model().item(1).isEnabled()
    assert window._collect_destination_updates({})['wake_word_config']['phrase'] == 'jarvis'
    from PySide6.QtWidgets import QLabel
    assert any(label.text() == FALLBACK_NOTE and not label.isHidden()
               for label in window.findChildren(QLabel))


@pytest.mark.parametrize('phrase', ['hey jarvis', 'computer', 'alexa', 'hey mycroft', 'hey rhasspy', 'my own phrase'])
def test_unsupported_load_falls_back_once_and_preserves_other_settings(phrase, tmp_path):
    import json
    from samsara.ui.first_run_pages_qt import initial_config
    cfg = config()
    cfg['wake_word_config'].update(phrase=phrase, phrase_options=[phrase])
    cfg['wake_word_config']['audio']['measured_noise_floor'] = 0.005
    assert normalize_wake_phrase(cfg)
    assert cfg['wake_word_config']['phrase'] == 'jarvis'
    assert cfg['wake_word_config']['phrase_fallback_pending']
    assert cfg['wake_word_config']['audio']['wake_command_timeout'] == 9
    assert calibration_needed(cfg)
    assert not normalize_wake_phrase(cfg)
    assert home_signals.problem_notice(SimpleNamespace(config=cfg)).headline == FALLBACK_NOTE
    saved = tmp_path / 'config.json'
    original = config()
    original['wake_word_config']['phrase'] = phrase
    saved.write_text(json.dumps(original), encoding='utf-8')
    merged, _ = initial_config(setup._DEFAULTS, saved)
    assert merged['wake_word_config']['phrase'] == 'jarvis'
    assert merged['wake_word_config']['phrase_fallback_pending']


def test_runtime_load_migrates_even_when_old_wake_migration_is_noop():
    from samsara.config_migrations import migrate_loaded_config
    app = SimpleNamespace(config=config(), save_config=Mock())
    app.config['wake_word_config']['phrase'] = 'alexa'
    for name in ('_migrate_wake_word_config', '_migrate_command_matching_enabled_flag',
                 '_migrate_ai_command_mode_config', '_migrate_retired_ava_command_session_config',
                 '_migrate_recording_media_mode'):
        setattr(app, name, Mock())
    migrate_loaded_config(app, {})
    assert app.config['wake_word_config']['phrase'] == 'jarvis'
    assert app.config['wake_word_config']['phrase_fallback_pending']
    assert app.save_config.called


def test_home_fallback_acknowledgement_keeps_calibration_reminder(isolated, monkeypatch):
    from tests.test_home_qt import _app, _FakeSettings
    monkeypatch.setattr(home_qt, 'QSettings', _FakeSettings)
    monkeypatch.setattr(home_signals, 'corrections_waiting', lambda app: 0)
    monkeypatch.setattr(home_signals, '_diag_records', lambda app, limit: [])
    monkeypatch.setattr(home_signals, 'generic_hints', lambda app: [])
    app = _app()
    app.config['wake_word_config']['phrase'] = 'computer'
    normalize_wake_phrase(app.config)
    app.update_config = Mock(side_effect=lambda updates: app.config.update(updates))
    home = home_qt.HomePage(app)
    assert home._notice.headline == FALLBACK_NOTE
    home._notice_btn.click()
    assert home._notice.action == 'calibrate_wake_word'
    app.update_config.assert_called_once()
    assert not normalize_wake_phrase(app.config)
    assert not app.config['wake_word_config']['phrase_fallback_pending']


@pytest.mark.parametrize('was_enabled,enabled,offers', [(False, True, 1), (False, False, 0), (True, True, 0)])
def test_settings_apply_opens_calibration_after_saving(isolated, monkeypatch, was_enabled, enabled, offers):
    from samsara import ava_readiness, listening_display, model_guard
    from samsara.ui import command_pack_actions
    from samsara.ui.settings.services import SettingsServices
    app = SimpleNamespace(config=config(was_enabled), _config_lock=threading.RLock())
    events = []
    window = SimpleNamespace(app=app,
        _save_fns=[lambda acc: {'wake_word_enabled': enabled}],
        close=lambda: events.append('closed'))
    monkeypatch.setattr(ava_readiness, 'settings_signature', lambda app: None)
    monkeypatch.setattr(ava_readiness, 'recheck_after_settings', lambda *a: None)
    monkeypatch.setattr(listening_display, 'run_display', lambda app: None)
    monkeypatch.setattr(listening_display, 'request_restart_once', lambda *a: None)
    monkeypatch.setattr(model_guard, 'settings_choices', lambda *a: {})
    monkeypatch.setattr(model_guard, 'save_settings', lambda *a: events.append('saved'))
    monkeypatch.setattr(command_pack_actions, 'refresh_runtime', lambda *a, **k: None)
    opened = []
    def calibrate(app):
        assert app.config['wake_word_enabled'] is True
        assert events == ['saved', 'closed']
        opened.append(app)
    monkeypatch.setattr(mic, 'open_wake_calibration', calibrate)
    SettingsServices._apply_and_close(window)
    assert len(opened) == offers
