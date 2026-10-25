"""886: cancelling setup must survive the real config load/save boundary."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt

from samsara.config_seed import build_default_config
from samsara.microphone_recovery import needs_setup
from samsara.ui import first_run_pages_qt as pages
from samsara.ui import first_run_wizard_qt as wizard


@pytest.fixture
def profile(tmp_path, monkeypatch):
    # Executed only through the bootstrap-free gate adapter.
    import dictation

    monkeypatch.setattr(dictation, 'samsara_home_dir', lambda: tmp_path)
    config_path = tmp_path / 'config.json'

    def make():
        app = object.__new__(dictation.DictationApp)
        app.config_path = config_path
        app.config = {}
        app._config_lock = threading.Lock()
        return app

    return config_path, make


def load(app):
    with app._config_lock:
        app.load_config()


def save(app):
    with app._config_lock:
        app.save_config()


def read(config_path):
    return json.loads(config_path.read_text(encoding='utf-8'))


@pytest.fixture
def setup(qapp, monkeypatch, profile):
    config_path, _ = profile
    monkeypatch.setattr(wizard.thread_registry, 'spawn', lambda *a, **k: None)
    monkeypatch.setattr(wizard, 'register_notifications', lambda *a, **k: None)
    monkeypatch.setattr(wizard, 'capture_endpoints', lambda: ())
    monkeypatch.setattr(pages, 'cuda_chosen', lambda _: False)
    monkeypatch.setattr(pages.autostart, 'sync_run_entry', Mock())
    monkeypatch.setattr(wizard._WizardWindow, '_open_meter_stream', lambda *a:
                        SimpleNamespace(stop=lambda: None, close=lambda: None))
    made = []

    def make():
        win = wizard._WizardWindow(config_path)
        made.append(win)
        win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        win._mics = [{'id': 7, 'name': 'Synthetic USB microphone'}]
        win._mic_result.emit('_load_done_', '')
        win._decisions.receive_facts({'cpu_cores': 8, 'ram_gb': 16, 'gpu_usable': False})
        win.show()
        qapp.processEvents()
        return win

    yield make
    for win in made:
        if win.isVisible():
            win.close()
        win.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


@pytest.mark.parametrize('step', [0, 1, len(wizard._STEPS) - 1],
                         ids=['welcome', 'microphone', 'before-start'])
def test_cancelled_fresh_setup_returns_after_load_save_and_reload(setup, profile, step):
    config_path, make_app = profile
    assert needs_setup(config_path) is True
    win = setup()
    win._step = step
    win._show_step()
    win.close()
    assert win.result is None
    assert not config_path.exists()

    app = make_app()
    load(app)
    assert app.config['first_run_complete'] is False
    assert read(config_path)['first_run_complete'] is False
    save(app)
    assert needs_setup(config_path) is True

    restarted = make_app()
    load(restarted)
    save(restarted)
    assert read(config_path)['first_run_complete'] is False
    assert needs_setup(config_path) is True


@pytest.mark.parametrize('retry', [False, True], ids=['fresh', 'after-cancel'])
def test_successful_finish_stays_complete(setup, profile, retry):
    config_path, make_app = profile
    if retry:
        cancelled = setup()
        cancelled.close()
        load(make_app())
        assert needs_setup(config_path) is True

    win = setup()
    for _ in wizard._STEPS:
        assert win._next_btn.isEnabled()
        win._next_btn.click()
    assert win.result['first_run_complete'] is True
    # This is the same persistence boundary used by the startup caller.
    config_path.write_text(json.dumps(win.result), encoding='utf-8')
    app = make_app()
    load(app)
    save(app)
    assert read(config_path)['first_run_complete'] is True
    assert needs_setup(config_path) is False


def test_completed_profile_with_disconnected_microphone_stays_complete(profile, monkeypatch):
    config_path, make_app = profile
    config = build_default_config(home=config_path.parent, macro_defaults={},
                                  notification_defaults={})
    config.update(microphone=7, microphone_name='Synthetic unplugged microphone',
                  hotkey='ctrl+f8')
    config_path.write_text(json.dumps(config), encoding='utf-8')
    query = Mock(return_value=[])
    monkeypatch.setattr('sounddevice.query_devices', query)

    assert needs_setup(config_path) is False
    app = make_app()
    load(app)
    save(app)
    assert app.config['microphone'] == 7
    assert app.config['hotkey'] == 'ctrl+f8'
    assert read(config_path)['first_run_complete'] is True
    assert needs_setup(config_path) is False
    query.assert_not_called()


def test_legacy_missing_completion_key_retains_migration_default(profile):
    config_path, make_app = profile
    config_path.write_text(json.dumps({'mode': 'wake_word', 'hotkey': 'ctrl+f8'}),
                           encoding='utf-8')
    app = make_app()
    load(app)
    assert app.config['mode'] == 'hold'
    assert app.config['wake_word_enabled'] is True
    assert app.config['hotkey'] == 'ctrl+f8'
    assert app.config['first_run_complete'] is True
    save(app)
    assert needs_setup(config_path) is False


def test_cancelling_setup_rerun_preserves_completed_profile(setup, profile):
    config_path, make_app = profile
    original = {'first_run_complete': True, 'microphone': 7, 'hotkey': 'ctrl+f8'}
    config_path.write_text(json.dumps(original), encoding='utf-8')
    win = setup()
    win.close()
    assert win.result is None
    assert read(config_path) == original
    app = make_app()
    load(app)
    save(app)
    assert read(config_path)['first_run_complete'] is True
    assert app.config['hotkey'] == 'ctrl+f8'
    assert needs_setup(config_path) is False


def test_finish_without_microphone_does_not_complete_setup(setup, profile):
    config_path, make_app = profile
    win = setup()
    assert win._config['microphone'] is None
    win._finish()
    assert win.result is None
    win.close()
    load(make_app())
    assert read(config_path)['first_run_complete'] is False
    assert needs_setup(config_path) is True
