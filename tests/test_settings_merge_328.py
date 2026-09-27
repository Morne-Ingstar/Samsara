"""Cross-page Settings collectors must see all earlier edits on Apply."""
import copy

from samsara.ui.settings_qt import _SettingsWindow
from tests.test_settings_pages import _StubApp, _schema_defaults


def test_each_collector_receives_accumulated_updates(qapp):
    app = _StubApp(copy.deepcopy(_schema_defaults()))
    window = _SettingsWindow(app)
    try:
        seen = []
        window._save_fns = [
            lambda accumulated: {'one': 1},
            lambda accumulated: seen.append(dict(accumulated)) or {'two': 2},
            lambda accumulated: seen.append(dict(accumulated)) or {},
        ]
        window._apply_and_close()
        assert seen == [{'one': 1}, {'one': 1, 'two': 2}]
    finally:
        window.deleteLater()


def test_captions_mute_and_speech_pace_survive_same_apply(qapp):
    app = _StubApp(copy.deepcopy(_schema_defaults()))
    window = _SettingsWindow(app)
    try:
        window._widgets['ava_captions'].setChecked(True)
        window._widgets['ava_mute_audio'].setChecked(True)
        window._apply_and_close()
        saved = app.config['accessibility']
        assert saved['ava_captions'] is True
        assert saved['ava_mute_audio'] is True
        assert 'speech_pace' in saved
    finally:
        window.deleteLater()


def test_output_hostapi_is_saved_and_cleared_with_device(qapp):
    cfg = copy.deepcopy(_schema_defaults())
    app = _StubApp(cfg)
    app.available_outputs = [{'id': 7, 'name': 'Headphones', 'hostapi': 'WASAPI'}]
    app.switch_output_device = lambda *_args: None
    window = _SettingsWindow(app)
    try:
        window._widgets['output_combo'].setCurrentText('Headphones')
        window._apply_and_close()
        assert (cfg['output_device'], cfg['output_device_name'],
                cfg['output_device_hostapi']) == (7, 'Headphones', 'WASAPI')
    finally:
        window.deleteLater()

    window = _SettingsWindow(app)
    try:
        window._widgets['output_combo'].setCurrentText('System default')
        window._apply_and_close()
        assert (cfg['output_device'], cfg['output_device_name'],
                cfg['output_device_hostapi']) == (None, None, None)
    finally:
        window.deleteLater()
