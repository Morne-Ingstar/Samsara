"""Reboot index drift and late USB enumeration, with no real audio or bootstrap."""
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import microphone_recovery as recovery
from samsara.audio_engine import device_resolver
from samsara.audio_engine.engine import AudioCaptureEngine
from samsara.portaudio_guard import StreamGuard

MIC = 'Analogue 1 + 2 (Focusrite USB Audio)'


def device(name, channels=1, api=0):
    return dict(name=name, max_input_channels=channels, hostapi=api, default_samplerate=16000)


class Audio:
    def __init__(self, devices=(), default=-1):
        self.devices = list(devices)
        self.default = SimpleNamespace(device=(default, -1))
        self._samsara_stream_guard = StreamGuard()
        self.pending = None
        self.opened = []
        self.scans = 0

    def query_devices(self, index=None, kind=None):
        if index is None and kind is None:
            return self.devices
        index = self.default.device[0] if index is None else index
        if not isinstance(index, int) or not 0 <= index < len(self.devices):
            raise RuntimeError('No input')
        return self.devices[index]

    def query_hostapis(self):
        return [{'name': 'Windows WASAPI'}, {'name': 'MME'}]

    def _terminate(self):
        pass

    def _initialize(self):
        self.scans += 1
        if self.pending is not None:
            self.devices, self.pending = self.pending, None

    def InputStream(self, **kwargs):
        self.opened.append(kwargs['device'])
        return SimpleNamespace(start=lambda: None, stop=lambda: None, close=lambda: None)


def application(config=None):
    app = SimpleNamespace(config=dict(config or {'microphone': 27, 'microphone_name': MIC}),
                          _config_lock=threading.Lock(), available_mics=[],
                          _schedule_ui=lambda fn, *args: fn(*args),
                          listening_indicator=SimpleNamespace(set_device_lost=Mock()),
                          tray_icon=SimpleNamespace(notify_warning=Mock()),
                          _update_tray_tooltip=Mock(), _show_outcome_chip=Mock())
    app.saved = []

    def persist():
        # Production persist_config acquires this non-reentrant lock itself.
        with app._config_lock:
            app.saved.append(dict(app.config))

    app.persist_config = persist
    app.get_current_microphone_name = lambda: next(
        (d['name'] for d in app.available_mics if d['id'] == app.config['microphone']), 'Unknown')
    return app


def drift_audio():
    devices = [device('Output', channels=0) for _ in range(28)]
    devices[1] = device(MIC, api=1)
    devices[24] = device(MIC)
    return Audio(devices, default=1)


def test_named_index_drift_updates_memory_label_and_existing_save_path():
    app, audio = application(), drift_audio()
    app.available_mics = [{'id': 24, 'name': MIC}]
    recovery.reconcile(app, audio)
    assert app._capture_device == 24
    assert app.config == {'microphone': 24, 'microphone_name': MIC}
    assert app.get_current_microphone_name() == MIC
    assert recovery.selection_name(app) == MIC
    assert app.saved == [app.config]
    recovery.reconcile(app, audio)
    assert len(app.saved) == 1


def test_fallback_never_overwrites_chosen_identity_or_saves_its_index():
    app = application()
    recovery.reconcile(app, Audio([device('Built-in')], default=0))
    assert (app._capture_device, app._using_default_mic) == (0, True)
    assert app.config == {'microphone': 27, 'microphone_name': MIC}
    assert app.saved == []
    assert recovery.selection_name(app) == MIC


def test_selection_changed_during_enumeration_wins(monkeypatch):
    app = application()

    def resolve(*args):
        with app._config_lock:
            app.config.update(microphone=2, microphone_name='New choice')
        return 24, False

    monkeypatch.setattr(device_resolver, 'select_input', resolve)
    recovery.reconcile(app, drift_audio())
    assert app.config == {'microphone': 2, 'microphone_name': 'New choice'}
    assert app.saved == []


@pytest.mark.parametrize('old_id', [27, None])
def test_default_selection_persists_name_and_follows_current_windows_input(old_id):
    app = application({'microphone': old_id, 'microphone_name': MIC})
    app.refresh_audio_devices = Mock()

    def switch(index):
        assert app.config['microphone_name'] == 'System default'
        app.config['microphone'] = index
        app.persist_config()

    app.switch_microphone = Mock(side_effect=switch)
    recovery.select_system_default(app)
    assert app.config == {'microphone': None, 'microphone_name': 'System default'}
    assert app.saved == [app.config]
    app.refresh_audio_devices.assert_called_once_with()
    audio = Audio([device('One'), device('Two')], default=0)
    assert device_resolver.select_input(audio, app.config) == (0, False)
    audio.default.device = (1, -1)
    assert device_resolver.select_input(audio, app.config) == (1, False)
    recovery.select_system_default(app)
    assert len(app.saved) == 1
    app.refresh_audio_devices.assert_called_once_with()


def test_default_switch_clears_old_fallback_and_unavailable_state(monkeypatch):
    app = application()
    app._ace_engine = SimpleNamespace(device_lost=False)
    app._no_microphone = app._using_default_mic = True
    app._mic_boot_ready = True
    app.switch_microphone = lambda index: app.config.update(microphone=index)
    monkeypatch.setitem(sys.modules, 'sounddevice', Audio([device('Windows input')], default=0))
    recovery.select_system_default(app)
    assert app._capture_device == 0
    assert app._using_default_mic is False
    assert app._no_microphone is False
    app.listening_indicator.set_device_lost.assert_called_with(False)
    app.tray_icon.notify_warning.assert_not_called()


def test_default_action_uses_tray_handler_and_restores_name_on_failure():
    app = application()

    def fail(index):
        assert index is None
        assert app.config['microphone_name'] == 'System default'
        raise RuntimeError('Switch failed')

    with pytest.raises(RuntimeError, match='Switch failed'):
        recovery.select_system_default(app, fail)
    assert app.config == {'microphone': 27, 'microphone_name': MIC}
    assert app.saved == []


def boot_app(monkeypatch, now, fallback):
    audio = Audio([device('Built-in')] if fallback else [], default=0 if fallback else -1)
    monkeypatch.setitem(sys.modules, 'sounddevice', audio)
    monkeypatch.setattr(recovery.time, 'monotonic', lambda: now[0])
    app = application()
    app._ace_engine = None
    app._audio_refresh_lock = threading.RLock()
    app._buffer_lock = threading.RLock()
    app._playback_buffer = []
    app.capture_rate = 16000
    app._run_calibration_if_auto = Mock()
    app.get_available_microphones = lambda: [
        {'id': i, 'name': d['name']} for i, d in enumerate(audio.devices)]
    app._prepare_audio_rescan = lambda: recovery.prepare_rescan(app, audio)

    def start():
        ring = SimpleNamespace(write=lambda *a, **k: None, bump_device_epoch=lambda: 1)
        app._ace_engine = AudioCaptureEngine(ring, config=dict(app.config),
                                             before_reopen=app._prepare_audio_rescan)
        app._ace_engine.start()
        recovery.set_state(app, False)

    app._start_ace_engine = start
    recovery.reconcile(app, audio)
    # Starting fallback capture is valid while the preferred input enumerates.
    if fallback:
        start()
    else:
        recovery.set_state(app, True)
    changes = recovery.DeviceChanges(app, clock=lambda: now[0], endpoints=lambda: frozenset())
    return app, audio, changes


@pytest.mark.parametrize('fallback', [False, True])
def test_late_usb_is_rescanned_opened_and_never_publishes_boot_warning(monkeypatch, fallback):
    now = [0.0]
    app, audio, changes = boot_app(monkeypatch, now, fallback)
    try:
        for moment in (0.5, 1.0, 1.5, 2.0):
            now[0] = moment
            if moment == 2.0:
                audio.pending = [device(MIC)]
                audio.default.device = (0, -1)
            changes.tick()
        assert audio.scans > 0
        assert audio.opened[-1] == 0
        assert app._no_microphone is False
        assert app._using_default_mic is False
        assert app.config == {'microphone': 0, 'microphone_name': MIC}
        assert app.saved == [app.config]
        app.tray_icon.notify_warning.assert_not_called()
        assert all(call.args == (False,) for call in app.listening_indicator.set_device_lost.call_args_list)
        scans = audio.scans
        now[0] = 3.0
        changes.tick()
        assert audio.scans == scans  # Stop boot retries as soon as capture is healthy.
    finally:
        if app._ace_engine:
            app._ace_engine.stop()


@pytest.mark.parametrize('fallback', [False, True])
def test_persistent_absence_reports_once_after_five_seconds(monkeypatch, fallback):
    now = [0.0]
    app, audio, changes = boot_app(monkeypatch, now, fallback)
    try:
        now[0] = 4.99
        recovery.set_state(app, not fallback)
        app.tray_icon.notify_warning.assert_not_called()
        now[0] = 5.0
        recovery.set_state(app, not fallback)
        recovery.set_state(app, not fallback)
        app.tray_icon.notify_warning.assert_called_once_with(
            'Microphone' if fallback else 'No microphone',
            recovery.FALLBACK_MESSAGE if fallback else recovery.MISSING_MESSAGE)
        assert app.listening_indicator.set_device_lost.call_args.args == (not fallback,)
        assert app.saved == []
    finally:
        if app._ace_engine:
            app._ace_engine.stop()


def test_later_disconnect_has_no_grace(monkeypatch):
    monkeypatch.setattr(recovery.time, 'monotonic', lambda: 0.0)
    app = application()
    recovery.set_state(app, False)
    recovery.set_state(app, True)
    app.listening_indicator.set_device_lost.assert_called_with(True)
    app.tray_icon.notify_warning.assert_called_once_with('No microphone', recovery.MISSING_MESSAGE)


def test_deliberate_dictation_attempt_reports_missing_input_during_grace():
    app = application()
    recovery.set_state(app, True)
    recovery.set_state(app, True, notify=True)
    app._show_outcome_chip.assert_called_once_with(recovery.MISSING_MESSAGE, 'error')


def test_settings_already_matches_name_and_lists_default_first(qapp):
    from PySide6.QtWidgets import QComboBox
    from samsara.ui.settings.general_qt import _populate_microphone_combo, _microphone_updates

    combo = QComboBox()
    _populate_microphone_combo(combo, [{'id': 24, 'name': MIC}],
                               {'microphone': 27, 'microphone_name': MIC})
    assert combo.itemText(0) == 'System default'
    assert combo.currentText() == MIC
    assert _microphone_updates(combo) == {'microphone': 24, 'microphone_name': MIC}
    combo.setCurrentIndex(0)
    assert _microphone_updates(combo) == {'microphone': None, 'microphone_name': 'System default'}


@pytest.mark.parametrize('default', [False, True])
def test_real_tray_menu_refreshes_before_label_and_default_action_works(qapp, monkeypatch, default):
    from samsara.ui.tray_qt import SamsaraTrayQt
    from tests.test_tray_qt import _make_app

    app = _make_app()
    app._config_lock = threading.Lock()
    app.config.update(microphone=None if default else 27,
                      microphone_name='System default' if default else MIC)
    app.available_mics = [{'id': 27, 'name': 'Wrong input'}]
    app._mic_refresh_blocked.return_value = False
    app.get_available_microphones.return_value = [{'id': 24, 'name': MIC}]
    app._reconcile_microphone_selection.side_effect = lambda: recovery.reconcile(app, drift_audio())
    app.get_current_microphone_name.side_effect = lambda: next(
        (d['name'] for d in app.available_mics if d['id'] == app.config['microphone']), 'Unknown')
    chosen = Mock()
    monkeypatch.setattr('samsara.ui.tray_microphones.select_system_default', chosen)
    tray = SamsaraTrayQt(app)
    tray._rebuild_menu()
    mic = next(a.menu() for a in tray._menu.actions() if a.text().startswith('[MIC]'))
    assert mic.title() == '[MIC]  ' + ('System default' if default else MIC)
    actions = mic.actions()
    assert [a.text() for a in actions] == ['System default', MIC]
    assert [a.isChecked() for a in actions] == [default, not default]
    actions[0].trigger()
    chosen.assert_called_once_with(app, app.switch_microphone_and_refresh)
    tray._tray.hide()
