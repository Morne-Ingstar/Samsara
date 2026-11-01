"""978: setup follows System default until a person chooses an input."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt

from samsara.ui import first_run_pages_qt as pages, first_run_wizard_qt as wizard


@pytest.fixture
def setup(qapp, monkeypatch, tmp_path):
    import sounddevice as sd
    monkeypatch.setattr(wizard.thread_registry, 'spawn', lambda *a, **k: None)
    monkeypatch.setattr(wizard, 'register_notifications', lambda *a, **k: None)
    monkeypatch.setattr(wizard, 'capture_endpoints', lambda: ())
    monkeypatch.setattr(pages.autostart, 'sync_run_entry', Mock())
    default = {'id': 2, 'name': 'Built-in microphone'}
    monkeypatch.setattr(sd, 'query_devices', lambda *a, **k: dict(default))
    monkeypatch.setattr(sd, 'default', SimpleNamespace(device=(2, -1)), raising=False)
    stream = Mock()
    monkeypatch.setattr(sd, 'InputStream', stream)
    windows = []

    def make(saved=None):
        path = tmp_path / f'config{len(windows)}.json'
        if saved is not None:
            path.write_text(json.dumps(saved), encoding='utf-8')
        win = wizard._WizardWindow(path)
        windows.append(win)
        win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        win._mics = [{'id': 1, 'name': 'Desk microphone'}, dict(default)]
        win._mic_result.emit('_load_done_', '')
        win._step = 1
        win._show_step()
        return win

    yield make, stream, default
    for win in windows:
        win.close()
        win.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def test_default_uses_windows_input_and_can_finish_with_no_preference(setup):
    make, stream, _ = setup
    win = make()
    assert win._mic_combo.currentText() == 'System default'
    assert win._get_current_mic_id() == 2  # First enumerated input is id 1.
    assert stream.call_args.kwargs['device'] == 2
    assert win._next_btn.isEnabled()
    win._go_next()
    assert win._step_name == 'Recognition'
    win._step = len(wizard._STEPS) - 1
    win._show_step()
    win._finish()
    assert win.result['first_run_complete']
    assert win.result['microphone'] is None
    assert win.result['microphone_name'] == 'System default'


def test_explicit_choice_survives_reorder_and_saves_raw_name(setup):
    make, _, _ = setup
    win = make()
    win._mic_combo.setCurrentText('Desk microphone')
    win._mics.reverse()
    next(m for m in win._mics if m['name'] == 'Desk microphone')['id'] = 11
    win._populate_mic_combo(preserve_selection=True)
    win._collect_step()
    assert win._mic_combo.currentText() == 'Desk microphone'
    assert win._get_current_mic_id() == 11
    assert win._config['microphone'] == 11
    assert win._config['microphone_name'] == 'Desk microphone'


def test_disconnect_keeps_preference_reconnect_restores_and_default_clears(setup):
    make, _, default = setup
    win = make()
    win._mic_combo.setCurrentText('Desk microphone')
    win._mics = [dict(default)]
    win._populate_mic_combo(preserve_selection=True)
    win._collect_step()
    assert win._mic_combo.currentText() == 'System default'
    assert win._get_current_mic_id() == 2
    assert win._config['microphone_name'] == 'Desk microphone'
    win._mics.append({'id': 3, 'name': 'Desk microphone'})
    win._populate_mic_combo(preserve_selection=True)
    assert win._mic_combo.currentText() == 'Desk microphone'
    assert win._get_current_mic_id() == 3
    win._mic_combo.setCurrentIndex(0)
    win._populate_mic_combo(preserve_selection=True)
    win._collect_step()
    assert win._mic_combo.currentText() == 'System default'
    assert win._config['microphone'] is None
    assert win._config['microphone_name'] == 'System default'


def test_saved_name_wins_over_reused_id_and_missing_preference_is_retained(setup):
    make, _, default = setup
    win = make({'microphone': 1, 'microphone_name': 'Headset'})
    assert win._mic_combo.currentText() == 'System default'
    assert win._get_current_mic_id() == 2
    win._collect_step()
    assert win._config['microphone_name'] == 'Headset'
    win._mics = [dict(default), {'id': 4, 'name': 'Headset'}]
    win._populate_mic_combo(preserve_selection=True)
    assert win._mic_combo.currentText() == 'Headset'
    assert win._get_current_mic_id() == 4
