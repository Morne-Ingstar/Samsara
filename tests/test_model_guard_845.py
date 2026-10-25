"""Model guard with synthetic profiles; never boots dictation or reads owner data."""
import copy
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara import model_guard as guard


ORIGINAL = {'model_size': 'small.en', 'performance_mode': 'accurate', 'compute_type': 'float16'}
DOWNGRADE = {'model_size': 'base.en', 'performance_mode': 'balanced', 'compute_type': 'int8'}


def read(app):
    return json.loads(app.config_path.read_text(encoding='utf-8'))


@pytest.fixture
def app(tmp_path, monkeypatch):
    from tests.test_config_backup_safeguard import _make_app
    host = _make_app(tmp_path, monkeypatch)
    host.command_executor = SimpleNamespace(commands={}, find_command=lambda p: None)
    host.hints = host.alarm_manager = None
    host.load_commands = lambda: {}
    host.play_sound = lambda *a, **kw: None
    host.config_path.write_text(json.dumps(ORIGINAL), encoding='utf-8')
    with host._config_lock:
        host.load_config()
    return host


@pytest.mark.parametrize('attempt', [
    {'model_size': 'base.en'}, {'performance_mode': 'balanced'}, {'compute_type': 'int8'},
    DOWNGRADE,
])
def test_programmatic_fallback_or_migration_refused_and_logged(app, caplog, attempt):
    caplog.set_level(logging.WARNING)
    app.update_config_and_save = lambda updates: None
    app.config.update(attempt)
    app.config['unrelated'] = True
    with app._config_lock:
        app.save_config()
        app.config.update(attempt)
        app.save_config()
    assert guard.choices(read(app)) == ORIGINAL
    assert guard.choices(app.config) == ORIGINAL
    assert read(app)['unrelated'] is True
    records = [r.message for r in caplog.records if '[MODEL-GUARD]' in r.message]
    assert len(records) == 1
    assert '->' in records[0] and 'caller=' in records[0] and 'save_config' in records[0]
    assert len(app._model_guard.pending_notices) == 1


def test_disk_merge_cannot_smuggle_another_profiles_choice(app, caplog):
    imported = read(app)
    imported.update(DOWNGRADE, external_setting=42)
    imported[guard.MARKER] = {'source': 'settings', 'profile': app._model_guard.profile,
                              'values': DOWNGRADE}
    app.config_path.write_text(json.dumps(imported), encoding='utf-8')
    with app._config_lock:
        app.save_config()
    assert guard.choices(read(app)) == ORIGINAL
    assert read(app)['external_setting'] == 42
    assert '[MODEL-GUARD]' in caplog.text


def test_migration_save_is_guarded_before_migrations_run(tmp_path, monkeypatch, caplog):
    from tests.test_config_backup_safeguard import _make_app
    from samsara import config_migrations
    app = _make_app(tmp_path, monkeypatch)
    app.config_path.write_text(json.dumps(ORIGINAL), encoding='utf-8')

    def migration(host, defaults):
        host.config.update(DOWNGRADE)
        host._config_migration_save = True
        host.save_config()

    monkeypatch.setattr(config_migrations, 'migrate_loaded_config', migration)
    with app._config_lock:
        app.load_config()
    assert guard.choices(read(app)) == ORIGINAL
    assert '[MODEL-GUARD]' in caplog.text


def test_explicit_voice_change_persists_and_is_marked(app):
    with app._config_lock, guard.user_change(app, DOWNGRADE, source='voice'):
        app.config.update(DOWNGRADE)
        app.save_config()
    assert guard.choices(read(app)) == DOWNGRADE
    assert read(app)[guard.MARKER]['source'] == 'voice'
    # Authorization expires: a later programmatic change cannot reuse it.
    with app._config_lock:
        app.config.update(ORIGINAL)
        app.save_config()
    assert guard.choices(read(app)) == DOWNGRADE


def test_authorization_is_exact_value_and_exact_app(app, tmp_path):
    other = SimpleNamespace()
    with app._config_lock, guard.user_change(other, DOWNGRADE, source='settings'):
        app.config.update(DOWNGRADE)
        app.save_config()
    assert guard.choices(read(app)) == ORIGINAL
    with app._config_lock, guard.user_change(app, {'model_size': 'medium.en'}, source='settings'):
        app.config.update(DOWNGRADE)
        app.save_config()
    assert guard.choices(read(app)) == ORIGINAL


def test_failed_user_save_does_not_advance_or_leak_authorization(app, monkeypatch):
    import dictation
    original = dictation.json.dump
    with monkeypatch.context() as patch:
        patch.setattr(dictation.json, 'dump', lambda *a, **kw: (_ for _ in ()).throw(OSError('synthetic')))
        with pytest.raises(OSError), app._config_lock:
            app.config.update(DOWNGRADE)
            guard.save_settings(app, DOWNGRADE)
    assert dictation.json.dump is original
    assert app._model_guard.accepted == ORIGINAL
    with app._config_lock:
        app.save_config()
    assert guard.choices(read(app)) == ORIGINAL


@pytest.mark.parametrize('legacy', ['checkpoint', 'last_known_good.json', 'config-20261024-120000.json'])
def test_startup_unmarked_change_restored_and_warned_once(tmp_path, monkeypatch, caplog, legacy):
    from tests.test_config_backup_safeguard import _make_app
    app = _make_app(tmp_path, monkeypatch)
    app.config_path.write_text(json.dumps(DOWNGRADE), encoding='utf-8')
    backups = tmp_path / 'config_backups'
    backups.mkdir()
    name = 'model_guard.json' if legacy == 'checkpoint' else legacy
    (backups / name).write_text(json.dumps(ORIGINAL), encoding='utf-8')
    with app._config_lock:
        app.load_config()
    assert guard.choices(app.config) == guard.choices(read(app)) == ORIGINAL
    assert len(app._model_guard.pending_notices) == 1
    assert 'startup/config_backups' in caplog.text
    caplog.clear()
    with app._config_lock:
        app.load_config()
    assert not app._model_guard.pending_notices
    assert '[MODEL-GUARD]' not in caplog.text


def test_user_marker_allows_next_start_and_ordinary_saves(app):
    with app._config_lock:
        app.config.update(DOWNGRADE)
        guard.save_settings(app, DOWNGRADE)
        app.config['ordinary'] = True
        app.save_config()
        app.load_config()
    assert guard.choices(read(app)) == DOWNGRADE
    assert not app._model_guard.pending_notices
    assert json.loads(app._model_guard.checkpoint.read_text()) == DOWNGRADE


def test_stale_marker_does_not_authorize_a_different_value(app, caplog):
    with app._config_lock:
        app.config.update(DOWNGRADE)
        guard.save_settings(app, DOWNGRADE)
        app.load_config()
    payload = read(app)
    payload.update(ORIGINAL)
    app.config_path.write_text(json.dumps(payload), encoding='utf-8')
    with app._config_lock:
        app.load_config()
    assert guard.choices(read(app)) == DOWNGRADE
    assert '[MODEL-GUARD]' in caplog.text


def test_private_values_never_appear_in_notice_or_log(app, caplog):
    app.config['model_size'] = 'synthetic private local model path'
    app.config['initial_prompt'] = 'synthetic private prompt'
    with app._config_lock:
        app.save_config()
    evidence = caplog.text + str(app._model_guard.pending_notices)
    assert 'synthetic private' not in evidence
    assert 'unknown' in evidence
    checkpoint = json.loads(app._model_guard.checkpoint.read_text())
    assert set(checkpoint) == set(guard.KEYS)


@pytest.fixture
def settings(app, qapp, monkeypatch):
    import sounddevice
    from samsara import autostart, ava_readiness, listening_display
    from samsara.ui import command_pack_actions
    from samsara.ui.settings_qt import _SettingsWindow
    monkeypatch.setattr(sounddevice, 'query_devices', lambda *a, **kw: [])
    monkeypatch.setattr(sounddevice, 'query_hostapis', lambda *a, **kw: [])
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **kw: None)
    monkeypatch.setattr(listening_display, 'run_display', lambda *a, **kw: None)
    monkeypatch.setattr(listening_display, 'request_restart_once', lambda *a, **kw: None)
    monkeypatch.setattr(ava_readiness, 'recheck_after_settings', lambda *a, **kw: None)
    monkeypatch.setattr(command_pack_actions, 'refresh_runtime', lambda *a, **kw: None)
    app.switch_microphone = lambda *a: None
    app.switch_output_device = lambda *a: None
    window = _SettingsWindow(app)
    window.show_tab('Dictation')
    yield window
    window.close()
    window.deleteLater()


def test_actual_settings_apply_persists_model_profile_and_precision(settings):
    window = settings
    widgets = window._widgets
    model = widgets['model_combo']
    label = next(label for label, size in widgets['model_label_to_size'].items() if size == 'base.en')
    model.setCurrentText(label)
    widgets['adv_perf_mode'].setCurrentText('balanced')
    widgets['adv_compute_type'].setCurrentText('int8')
    window._apply_and_close()
    assert guard.choices(read(window.app)) == DOWNGRADE
    assert read(window.app)[guard.MARKER]['source'] == 'settings'
    assert not window.app._model_guard.pending_notices


def test_unrelated_settings_apply_does_not_authorize_outside_model_change(settings):
    app = settings.app
    app.config.update(DOWNGRADE)
    settings._apply_and_close()
    assert guard.choices(read(app)) == ORIGINAL
    assert len(app._model_guard.pending_notices) == 1


@pytest.mark.parametrize('full', [False, True], ids=['Compact', 'Full'])
def test_queued_and_runtime_notice_delivered_once_for_each_display(app, monkeypatch, full):
    from samsara.ui import qt_runtime
    app.config.setdefault('ui', {})['live_surface'] = {'enabled': full}
    callbacks = []
    shown = []
    monkeypatch.setattr(qt_runtime, 'post', callbacks.append)
    monkeypatch.setattr(guard, 'show_notice', shown.append)
    with app._config_lock:
        app.config.update(DOWNGRADE)
        app.save_config()
    guard.attach_notices(app)
    guard.attach_notices(app)
    assert len(callbacks) == 1
    callbacks.pop()()
    assert len(shown) == 1 and 'kept' in shown[0]
    with app._config_lock:
        app.config['model_size'] = 'tiny.en'
        app.save_config()
    assert len(callbacks) == 1
    callbacks.pop()()
    assert len(shown) == 2


def test_real_notice_uses_plain_text_and_lives_until_dismissed(qapp):
    from PySide6.QtCore import Qt
    dialog = guard.show_notice('Speech model was about to change to base.en; kept small.en.')
    assert dialog.textFormat() == Qt.TextFormat.PlainText
    assert dialog in guard._dialogs and dialog.isVisible()
    dialog.accept()
    assert dialog not in guard._dialogs


def test_tray_without_a_loaded_guard_does_not_attach_to_dynamic_attributes():
    from unittest.mock import Mock
    host = Mock()
    guard.attach_notices(host)
    assert not host._model_guard.mock_calls


def test_first_save_accepts_initial_speech_choice_then_protects_it(tmp_path, monkeypatch):
    from tests.test_config_backup_safeguard import _make_app
    host = _make_app(tmp_path, monkeypatch)
    host.config = {**ORIGINAL, 'unrelated': {'enabled': True}}
    with host._config_lock:
        host.save_config()
        assert read(host) == host.config == {**ORIGINAL, 'unrelated': {'enabled': True}}
        host.config.update(DOWNGRADE)
        host.config['unrelated']['enabled'] = False
        host.save_config()
    assert read(host) == {**ORIGINAL, 'unrelated': {'enabled': False}}


@pytest.mark.parametrize('initial', [{}, {'unrelated': {'enabled': True}}])
def test_profile_without_speech_keys_saves_without_inserting_defaults(tmp_path, monkeypatch, initial):
    from tests.test_config_backup_safeguard import _make_app
    host = _make_app(tmp_path, monkeypatch)
    host.config_path.write_text(json.dumps(initial), encoding='utf-8')
    host.config = {**initial, 'another_setting': 42}
    with host._config_lock:
        host.save_config()
    assert read(host) == host.config == {**initial, 'another_setting': 42}


def test_unrelated_save_keeps_merged_payload_and_marker_untouched(app):
    with app._config_lock:
        app.config.update(DOWNGRADE)
        guard.save_settings(app, DOWNGRADE)
        before = read(app)
        external = {**before, 'external': {'enabled': True}}
        app.config_path.write_text(json.dumps(external), encoding='utf-8')
        app.config['local'] = [1, 2, 3]
        app.save_config()
    assert read(app) == app.config == {**external, 'local': [1, 2, 3]}
    assert not app._model_guard.pending_notices


def test_partial_user_choice_does_not_authorize_other_speech_keys(app):
    with app._config_lock:
        app.config.update(DOWNGRADE)
        app.config['unrelated'] = True
        guard.save_settings(app, {'model_size': DOWNGRADE['model_size']})
    saved = read(app)
    assert guard.choices(saved) == {**ORIGINAL, 'model_size': DOWNGRADE['model_size']}
    assert saved['unrelated'] is True
    assert saved[guard.MARKER]['values'] == guard.choices(saved)
    assert len(app._model_guard.pending_notices) == 1


@pytest.mark.parametrize('reason', ['', 'password_field', 'unverified_target'])
def test_blocked_model_save_preserves_history_and_target_privacy(app, monkeypatch, reason):
    from dictation import DictationApp
    from types import MethodType
    from samsara import recent_privacy
    monkeypatch.setattr(recent_privacy, 'focused_target',
                        lambda: recent_privacy.TargetPrivacy(reason, 'notepad.exe'))
    app.history = []
    app.max_history = 100
    app.add_to_history = MethodType(DictationApp.add_to_history, app)
    persisted = []
    app.save_history = lambda: persisted.append(copy.deepcopy(app.history))
    with app._config_lock:
        app.config.update(DOWNGRADE)
        app.config['unrelated'] = {'enabled': True}
        app.save_config()
    payload = read(app)
    app.add_to_history('invented violet meadow', is_command=False)
    app.add_to_history('copy', is_command=True)
    assert app.history[0][1:] == ('' if reason else 'invented violet meadow', False)
    assert app.history[1][1:] == ('copy', True)
    assert persisted == [app.history[:1], app.history]
    assert read(app) == payload
    assert payload['unrelated'] == {'enabled': True}
    assert guard.choices(payload) == ORIGINAL
    assert len(app._model_guard.pending_notices) == 1
