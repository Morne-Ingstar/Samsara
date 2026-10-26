"""Laptop hot-plug regressions: mocked audio, no live app/config imports."""
import ast
import json
import logging
import sys
import types
import threading
from unittest.mock import Mock
from pathlib import Path

import pytest

from samsara.audio_engine.engine import AudioCaptureEngine
from samsara.audio_engine import device_resolver

ROOT = Path(__file__).resolve().parents[1]
MIC = 'Microphone (Jabra EVOLVE LINK)'


def app_method(name, **extra):
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(logger=logging.getLogger('test-hotplug'), **extra)
    exec(compile(ast.Module(body=[method], type_ignores=[]), 'dictation.py', 'exec'), namespace)
    return namespace[name]


class Audio:
    def __init__(self, devices=(), default=-1):
        self.devices = list(devices)
        self.default = types.SimpleNamespace(device=(default, -1))
        self.opened = []
        self.events = []
        self.pending = None
        from samsara.portaudio_guard import StreamGuard
        self._samsara_stream_guard = StreamGuard()

    def query_hostapis(self):
        return [{'name': 'Windows WASAPI'}]

    def query_devices(self, device=None, kind=None):
        if device is None and kind is None:
            return self.devices
        if device is None:
            device = self.default.device[0]
        if not isinstance(device, int) or not 0 <= device < len(self.devices):
            raise RuntimeError('Invalid device')
        return self.devices[device]

    def _terminate(self):
        self.events.append('terminate')

    def _initialize(self):
        self.events.append('initialize')
        if self.pending is not None:
            self.devices = self.pending
            self.pending = None

    def InputStream(self, **kw):
        index = kw['device']
        info = self.query_devices(index, kind='input')
        if not info['max_input_channels']:
            raise RuntimeError('Invalid number of channels')
        self.opened.append(index)
        return types.SimpleNamespace(start=lambda: None, stop=lambda: None, close=lambda: None)


def dev(name, channels=1):
    return dict(name=name, hostapi=0, max_input_channels=channels, default_samplerate=16000)


def engine(monkeypatch, audio, config=None, **kw):
    monkeypatch.setitem(sys.modules, 'sounddevice', audio)
    ring = types.SimpleNamespace(write=lambda *a, **k: None, bump_device_epoch=lambda: 1)
    return AudioCaptureEngine(ring, config=config or {'microphone': 9, 'microphone_name': MIC}, **kw)


def test_first_open_never_uses_an_index_for_a_different_name(monkeypatch):
    audio = Audio([dev('Wrong device'), dev(MIC)], default=0)
    capture = engine(monkeypatch, audio, {'microphone': 0, 'microphone_name': MIC})
    capture.start()
    assert audio.opened == [1]
    capture.stop()


def test_completed_install_with_null_microphone_does_not_need_wizard(tmp_path):
    from samsara.microphone_recovery import needs_setup
    config = tmp_path / 'config.json'
    assert needs_setup(config) is True
    config.write_text(json.dumps({'first_run_complete': True, 'microphone': None}))
    assert needs_setup(config) is False
    config.write_text(json.dumps({'first_run_complete': False, 'microphone': None}))
    assert needs_setup(config) is True
    config.write_text('{')
    assert needs_setup(config) is True
    # Check the bootstrap wiring without executing any fragment of __init__.
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
    assignment = next(n for n in init.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'need_wizard' for t in n.targets))
    assert ast.unparse(assignment.value) == 'needs_setup(self.config_path)'


def test_reopen_rescans_cached_devices_before_resolution(monkeypatch):
    from samsara.audio_devices import force_rescan
    audio = Audio([])
    audio.pending = [dev(MIC)]
    audio.default.device = (0, -1)
    capture = engine(monkeypatch, audio)
    capture._running = True
    capture._device_name = MIC
    capture._before_reopen = lambda: force_rescan(audio)
    assert capture.refresh(capture._config) is True
    assert audio.events == ['terminate', 'initialize']
    assert audio.opened == [0]
    capture.stop()


def test_refresh_retries_capture_after_failed_boot(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, 'sounddevice', Audio())
    app = types.SimpleNamespace(_ace_engine=None, available_mics=[],
        _mic_refresh_blocked=lambda: False, _capture_device=0, _audio_refresh_lock=threading.RLock(),
        _rescan_idle_since=float("-inf"),
        _prepare_audio_rescan=lambda: None,
        get_available_microphones=lambda: [],
        _reconcile_microphone_selection=lambda: None,
        _start_ace_engine=lambda: calls.append('start'))
    method = app_method('refresh_audio_devices', force_rescan=lambda: None)
    method(app)
    assert calls == ['start']


def test_missing_named_mic_uses_default_without_overwriting_choice(monkeypatch):
    audio = Audio([dev('Built-in')], default=0)
    config = {'microphone': 9, 'microphone_name': MIC}
    capture = engine(monkeypatch, audio, config)
    capture.start()
    assert audio.opened == [0]
    assert capture._using_default_fallback is True
    assert config == {'microphone': 9, 'microphone_name': MIC}
    capture.stop()


def test_no_input_devices_never_attempts_open(monkeypatch):
    audio = Audio([dev('Speakers', channels=0)], default=0)
    capture = engine(monkeypatch, audio)
    with pytest.raises(device_resolver.DeviceNotFound, match='No microphone'):
        capture.start()
    assert audio.opened == []


def test_duck_failure_without_output_endpoint_is_nonfatal(monkeypatch):
    import threading
    import time
    from samsara import hold_capture_onset
    monkeypatch.setattr(hold_capture_onset, 'open_owned_duck', lambda app, token, *a: (token, True))
    app = types.SimpleNamespace(config={}, _hold_capture_duck_seq=0, _running=True,
        _hands_free_duck_lock=threading.Lock(), _hands_free_capture_duck_starting=False,
        _hands_free_capture_duck_owners={-1},
        _hands_free_capture_ducker=types.SimpleNamespace(sessions_failed=1,
            last_error='GetDefaultAudioEndpoint failed'),
        _close_hold_capture_duck=lambda: None)
    recorder = types.SimpleNamespace(record=lambda *a, **k: None)
    method = app_method('_open_hold_capture_duck', time=time,
                        audio_ducking=types.SimpleNamespace(), flight_recorder=recorder)
    method(app)
    assert app._hold_capture_duck_confirmed_at is None


@pytest.mark.parametrize('streaming', [False, True])
def test_hotkey_without_capture_reports_missing_named_mic_before_ducking(streaming):
    seen = []
    app = types.SimpleNamespace(config={'microphone_name': MIC}, model_loaded=True,
        recording=False, _stop_in_flight=False,
        _live_surface_hold_parking_enabled=lambda: False,
        _dictation_consumer=None, _ace_engine=None,
        _set_microphone_state=lambda lost, notify=False: seen.append((lost, notify)),
        _open_hold_capture_duck=lambda: pytest.fail('ducking before missing mic guard'))
    assert app_method('_start_recording_impl')(app, streaming=streaming) is False
    assert seen == [(True, True)]


def test_reconciliation_does_not_replace_the_persisted_preference():
    config = {'microphone': 9, 'microphone_name': MIC}
    app = types.SimpleNamespace(config=config, available_mics=[{'id': 0, 'name': 'Other'}])
    app_method('_reconcile_microphone_selection', sd=Audio([dev('Other')], default=0))(app)
    assert config == {'microphone': 9, 'microphone_name': MIC}


def test_completed_install_can_start_hidden_without_a_mic():
    from samsara.autostart import should_show_main_window
    assert should_show_main_window({'first_run_complete': True, 'microphone': None,
                                    'start_hidden': True}) is False


def test_reconciliation_keeps_saved_index_even_when_name_returns_elsewhere():
    config = {'microphone': 9, 'microphone_name': MIC}
    app = types.SimpleNamespace(config=config, available_mics=[{'id': 1, 'name': MIC}],
                                persist_config=Mock())
    app_method('_reconcile_microphone_selection', sd=Audio([dev('Other'), dev(MIC)], default=0))(app)
    assert app._capture_device == 1
    # 901: retain the preferred name and persist its corrected runtime index.
    assert config == {'microphone': 1, 'microphone_name': MIC}
    app.persist_config.assert_called_once_with()


def test_refresh_closes_output_before_portaudio_and_restarts_capture(monkeypatch):
    events = []
    monkeypatch.setitem(sys.modules, 'sounddevice', Audio())
    ace = types.SimpleNamespace(running=True, device_lost=False, refresh=lambda *a, **k: events.extend(['input-stop', 'safe-rescan', 'input-start']))
    app = types.SimpleNamespace(_ace_engine=ace, available_mics=[],
        _mic_refresh_blocked=lambda: False, config={}, capture_rate=16000,
        get_available_microphones=lambda: [], _reconcile_microphone_selection=lambda: None,
        _prepare_audio_rescan=lambda: events.append('safe-rescan'),
        _set_microphone_state=lambda lost: events.append(('missing', lost)))
    app_method('refresh_audio_devices', force_rescan=lambda: events.append('unsafe-rescan'))(app)
    assert events[:3] == ['input-stop', 'safe-rescan', 'input-start']


def test_first_failed_start_waits_for_a_device_change_without_rescan(monkeypatch):
    import time
    events = []
    class Capture:
        def __init__(self, *a, **k):
            self.starts = 0
            self._config = k['config']
        def start(self):
            self.starts += 1
            events.append('start')
            if self.starts == 1:
                raise RuntimeError('No endpoints at initial enumeration')
        def stop(self):
            pass
        def update_config(self, config, **kw):
            self._config.update(config)
    app = types.SimpleNamespace(config={}, capture_rate=16000,
        _on_ace_stream_death=lambda: None, _on_ace_recovery_success=lambda: None,
        _on_ace_recovery_give_up=lambda: None, _on_wake_consumer_fatal=lambda: None,
        _prepare_audio_rescan=lambda: events.append('rescan'),
        _set_microphone_state=lambda lost: events.append(('missing', lost)))
    consumer = lambda **kw: object()
    app_method('_start_ace_engine', time=time, FrameBus=lambda: object(),
        AudioCaptureEngine=Capture, DictationSessionConsumer=consumer,
        ContinuousConsumer=consumer, WakeConsumer=consumer)(app)
    assert events == ['start', ('missing', True)]
    assert app._dictation_consumer is None


def recovery_app(audio):
    from samsara import microphone_recovery as recovery
    app = types.SimpleNamespace(config={'microphone': 9, 'microphone_name': MIC},
        _audio_refresh_lock=threading.RLock(), _ace_engine=None, capture_rate=16000,
        # 901: these long-running hotplug scenarios begin after startup grace.
        _mic_boot_ready=True, persist_config=Mock(),
        _rescan_idle_since=float("-inf"),
        _no_microphone=True, available_mics=[], _mic_refresh_blocked=lambda: False,
        _run_calibration_if_auto=Mock(), _start_ace_engine=Mock())
    app.get_available_microphones = lambda: [dict(id=i, **d) for i, d in enumerate(audio.devices)
                                            if d['max_input_channels']]
    app._prepare_audio_rescan = lambda: recovery.prepare_rescan(app, audio)
    app.refresh_audio_devices = lambda: recovery.refresh(app)
    return app


def test_cold_start_poll_rescans_only_after_windows_capture_list_changes(monkeypatch):
    from samsara import microphone_recovery as recovery
    audio = Audio([])
    monkeypatch.setitem(sys.modules, 'sounddevice', audio)
    app = recovery_app(audio)
    now, endpoints = [0.0], [frozenset()]
    changes = recovery.DeviceChanges(app, clock=lambda: now[0], endpoints=lambda: endpoints[0])
    changes.notifications = False
    changes.prime()
    for tick in range(61):
        now[0] = tick
        changes.tick()
    assert audio.events == []
    audio.pending = [dev(MIC)]
    audio.default.device = (0, -1)
    endpoints[0] = frozenset({('usb', MIC)})
    now[0] = 65
    changes.tick()
    assert audio.events == ['terminate', 'initialize']
    assert app._capture_device == 0
    app._run_calibration_if_auto.assert_called_once()
    app._start_ace_engine.assert_called_once()
    assert app.config['microphone'] == 0  # 901: persist the returned name's new index.


def test_removed_then_returned_uses_public_engine_recovery_and_fresh_index(monkeypatch):
    from samsara import microphone_recovery as recovery
    from samsara.audio_engine import engine as mod
    queued = []
    monkeypatch.setattr(mod.thread_registry, 'spawn', lambda name, fn, **kw: queued.append(fn))
    audio = Audio([dev(MIC)], default=0)
    app = recovery_app(audio)
    capture = engine(monkeypatch, audio, before_reopen=app._prepare_audio_rescan)
    capture.start()
    audio.pending = []
    audio.default.device = (-1, -1)
    assert not capture.refresh(app.config)
    assert capture.device_lost and len(queued) == 1
    audio.pending = [dev('Other'), dev(MIC)]
    audio.default.device = (1, -1)
    assert capture.refresh(app.config)
    assert audio.opened == [0, 1]
    assert not capture.device_lost
    assert app.config['microphone'] == 1  # 901: persist the returned name's new index.
    capture.stop()


def test_healthy_default_fallback_returns_to_chosen_name_on_notification(monkeypatch):
    audio = Audio([dev('Built-in')], default=0)
    app = recovery_app(audio)
    capture = engine(monkeypatch, audio, before_reopen=app._prepare_audio_rescan)
    capture.start()
    assert capture._using_default_fallback
    audio.pending = [dev('Built-in'), dev(MIC)]
    assert capture.refresh(app.config)
    assert audio.opened == [0, 1]
    assert not capture._using_default_fallback
    capture.stop()


def test_system_default_tracks_default_instead_of_old_runtime_identity():
    audio = Audio([dev('Old default'), dev('New default')], default=1)
    config = {'microphone': None, 'microphone_name': 'System default'}
    assert device_resolver.select_input(audio, config, ('Old default', 'Windows WASAPI')) == (1, False)


def test_refresh_closes_all_owned_streams_before_reinitializing_portaudio():
    from samsara import microphone_recovery as recovery
    audio = Audio([dev(MIC)], default=0)
    app = recovery_app(audio)
    app._sound_stream = object()
    app.stop_sound_stream = lambda: audio.events.append('sound-stop')
    app._start_sound_stream = lambda: audio.events.append('sound-start')
    app.tts_engine = types.SimpleNamespace(is_speaking=lambda: False, cancel_all=Mock(),
        _close_persistent_stream=lambda: audio.events.append('tts-close'),
        restart_stream=lambda: audio.events.append('tts-start'))
    recovery.prepare_rescan(app, audio)
    assert audio.events == ['sound-stop', 'tts-close',
                            'terminate', 'initialize', 'sound-start', 'tts-start']
    # A concurrent earcon must not create another stream during Pa_Terminate.
    entered, released = threading.Event(), threading.Event()
    real_start = app_method('_start_sound_stream', sd=audio)
    app._resolve_playback_output = lambda: entered.set() or None
    app._sound_stream_sr = 16000
    with app._audio_refresh_lock:
        worker = threading.Thread(target=lambda: (real_start(app), released.set()))
        worker.start()
        assert not entered.wait(0.05)
    assert entered.wait(1.0)
    worker.join(1.0)
    assert released.is_set()
    stream = Mock()
    stream.stop.side_effect = RuntimeError('removed')
    app._sound_stream = stream
    app_method('stop_sound_stream')(app)
    stream.close.assert_called_once()


def test_active_tts_output_defers_rescan_instead_of_terminating_live_stream():
    from samsara import microphone_recovery as recovery
    audio = Audio([dev(MIC)], default=0)
    app = recovery_app(audio)
    app.tts_engine = types.SimpleNamespace(is_speaking=lambda: True, cancel_all=Mock(), _worker_lock=Mock())
    app.tts_engine._worker_lock.acquire.return_value = False
    with pytest.raises(RuntimeError, match='playing'):
        recovery.prepare_rescan(app, audio)
    assert audio.events == []


def test_calibration_identity_is_actual_fallback_and_no_mic_writes_nothing():
    from samsara import microphone_recovery as recovery
    audio = Audio([dev('Built-in')], default=0)
    app = recovery_app(audio)
    assert recovery.calibration_identity(app, audio)['device_name'] == 'Built-in'
    app._save_calibration, app._apply_speech_threshold = Mock(), Mock()
    app._measure_speech_threshold = Mock()
    audio.devices = []
    assert app_method('_run_calibration_if_auto', sd=audio)(app) == 'unavailable'
    app._save_calibration.assert_not_called()
    app._apply_speech_threshold.assert_not_called()
    app._measure_speech_threshold.assert_not_called()


def test_no_healthy_device_polling_and_event_bursts_debounce(monkeypatch):
    from samsara import microphone_recovery as recovery
    app = recovery_app(Audio())
    app._no_microphone = False
    now = [0.0]
    snapshot = Mock(return_value=frozenset({('usb', MIC)}))
    changes = recovery.DeviceChanges(app, clock=lambda: now[0], endpoints=snapshot)
    changes.prime()
    snapshot.reset_mock()
    for t in (0, 5, 10):
        now[0] = t
        changes.tick()
    snapshot.assert_not_called()
    for t in (11.0, 11.2, 11.4):
        now[0] = t
        changes.notify()
        changes.tick()
    now[0] = 12.2
    changes.tick()
    snapshot.assert_called_once()


def test_device_event_waits_for_hold_to_end(monkeypatch):
    from samsara import microphone_recovery as recovery
    audio = Audio([dev(MIC)], default=0)
    monkeypatch.setitem(sys.modules, 'sounddevice', audio)
    app = recovery_app(audio)
    app.recording = True
    now = [0.0]
    changes = recovery.DeviceChanges(app, clock=lambda: now[0], endpoints=lambda: frozenset())
    changes.request_input()
    changes.tick()
    assert audio.events == []
    app.recording = False
    now[0] = 1
    changes.tick()
    assert audio.events == ['terminate', 'initialize']


def test_endpoint_registration_covers_input_arrival_and_state_changes(monkeypatch):
    from samsara import microphone_recovery as recovery
    notify, close_com = Mock(), Mock()
    enumerator = Mock()
    monkeypatch.setitem(sys.modules, 'comtypes', types.SimpleNamespace(
        CoInitializeEx=Mock(), CoUninitialize=close_com))
    monkeypatch.setitem(sys.modules, 'pycaw.callbacks', types.SimpleNamespace(MMNotificationClient=object))
    monkeypatch.setitem(sys.modules, 'pycaw.pycaw', types.SimpleNamespace(
        AudioUtilities=types.SimpleNamespace(GetDeviceEnumerator=lambda: enumerator)))
    close = recovery.register_notifications(notify)
    receiver = enumerator.RegisterEndpointNotificationCallback.call_args.args[0]
    receiver.on_device_added('input')
    receiver.on_device_removed('input')
    receiver.on_device_state_changed('input', 'Active', 1)
    receiver.on_default_device_changed('eCapture', 1, 'eConsole', 0, 'input')
    assert notify.call_count == 4
    close()
    enumerator.UnregisterEndpointNotificationCallback.assert_called_once_with(receiver)
    close_com.assert_not_called()  # The watcher owns COM lifetime.


def test_missing_feedback_persists_and_only_one_tray_warning_per_outage():
    from samsara import microphone_recovery as recovery
    app = types.SimpleNamespace(config={}, listening_indicator=Mock(), tray_icon=Mock(),
        _schedule_ui=lambda fn, *args: fn(*args), _show_outcome_chip=Mock(), _update_tray_tooltip=Mock())
    for _ in range(3):
        recovery.set_state(app, True, notify=True)
    assert app._show_outcome_chip.call_count == 3
    app.tray_icon.notify_warning.assert_called_once()
    assert app.tray_icon.title == 'Samsara - No microphone'
    recovery.set_state(app, False)
    recovery.set_state(app, True)
    assert app.tray_icon.notify_warning.call_count == 2


def test_fallback_is_explained_without_overwriting_the_saved_choice():
    from samsara import microphone_recovery as recovery
    app = types.SimpleNamespace(config={'microphone': 9, 'microphone_name': MIC},
        _using_default_mic=True, tray_icon=Mock(),
        _mic_boot_ready=True)  # 901: fallback wording after startup grace.
    recovery.set_state(app, False)
    recovery.set_state(app, False)
    assert 'chosen microphone not connected' in app.tray_icon.title
    app.tray_icon.notify_warning.assert_called_once()
    assert app.config['microphone'] == 9


def test_slow_duck_is_bounded_and_late_completion_releases_only_its_token():
    from samsara import microphone_recovery as recovery
    release, finished = threading.Event(), threading.Event()
    app = types.SimpleNamespace(_close_hands_free_capture_duck=Mock(side_effect=lambda _: finished.set()))
    def slow(*args):
        release.wait(2)
        return -1, True
    try:
        with pytest.raises(TimeoutError):
            recovery._bounded_open_owned(app, -1, object(), slow, timeout=0.02)
    finally:
        release.set()
    assert finished.wait(2)
    app._close_hands_free_capture_duck.assert_called_with(-1)


def test_duck_cleanup_and_media_failures_cannot_escape_the_hold(monkeypatch):
    from samsara import microphone_recovery as recovery
    import time
    monkeypatch.setattr(recovery, '_open_hold_duck', Mock(side_effect=RuntimeError('cleanup failed')))
    app = types.SimpleNamespace(config={})
    recorder = types.SimpleNamespace(record=Mock())
    recovery.open_hold_duck(app, object(), recorder, time)
    assert app._hold_capture_duck_confirmed_at is None
    assert app._hold_capture_onset.policy == 'strict'
    recovery.duck_audio(app, types.SimpleNamespace(start_recording_media=Mock(side_effect=RuntimeError('no output'))))


def test_missing_state_reaches_both_displays_and_survives_idle(qapp, tmp_path):
    from samsara import microphone_recovery as recovery
    from samsara.live_surface.model import LiveSurfaceModel, CaptureState
    from samsara.live_surface.controller import LiveSurfaceIndicatorAdapter
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    from samsara.ui.listening_indicator import ListeningIndicator
    model = LiveSurfaceModel()
    controller = types.SimpleNamespace(post=model.consume)
    full = LiveSurfaceIndicatorAdapter(controller)
    app = types.SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}},
        listening_indicator=full, _schedule_ui=lambda fn, *args: fn(*args),
        _update_tray_tooltip=Mock(),
        _mic_boot_ready=True)  # 901: both displays still report later outages.
    recovery.set_state(app, True)
    assert model.view().capture is CaptureState.UNAVAILABLE
    full_widget = LiveSurfaceWidget(model.view(), reduced_motion=True)
    compact = ListeningIndicator()
    try:
        full_widget.show()
        app.listening_indicator = compact
        recovery.set_state(app, True)
        compact.hide()
        assert compact.isVisible()
        assert compact._active_chip() == ('mic lost', 'error')
        qapp.processEvents()
        assert full_widget.grab().save(str(tmp_path / 'Full-no-microphone.png'))
        assert compact.grab().save(str(tmp_path / 'Compact-no-microphone.png'))
        recovery.set_state(app, False)
        full.set_device_lost(False)
        assert model.view().capture is not CaptureState.UNAVAILABLE
    finally:
        compact.destroy()
        full_widget.close()
        full_widget.deleteLater()


class FakeClock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


def scenario(monkeypatch, *, devices=(), lost=True):
    from samsara import microphone_recovery as recovery
    audio = Audio(devices, default=0 if devices else -1)
    monkeypatch.setitem(sys.modules, 'sounddevice', audio)
    clock = FakeClock()
    monkeypatch.setattr(recovery.time, 'monotonic', clock)
    app = recovery_app(audio)
    app._no_microphone = lost
    app._sound_stream = object()
    app.stop_sound_stream = Mock()
    app._start_sound_stream = Mock()
    speaking = [False]
    app.tts_engine = types.SimpleNamespace(is_speaking=lambda: speaking[0],
        cancel_all=Mock(), shutdown=Mock(), restart_stream=Mock())
    state = [frozenset((str(i), d['name']) for i, d in enumerate(devices))]
    render = ['speaker']
    changes = recovery.DeviceChanges(app, clock=clock, endpoints=lambda: state[0],
                                     render_id=lambda: render[0])
    changes.prime()
    ready = []
    def start():
        app._ace_engine = engine(monkeypatch, audio, before_reopen=app._prepare_audio_rescan,
            on_recovery_success=lambda: ready.append('ready'))
        app._ace_engine.start()
        recovery.set_state(app, False)
        ready.append('ready')
    app._start_ace_engine = start
    if devices:
        start()
        ready.clear()
    return audio, app, clock, changes, state, render, speaking, ready


def test_finish1_missing_sixty_seconds_never_rescans_or_cancels(monkeypatch):
    audio, app, clock, changes, state, render, speaking, ready = scenario(monkeypatch)
    sound = app._sound_stream
    snapshot = Mock(side_effect=changes.endpoints)
    changes.endpoints = snapshot
    speaking[0] = True
    for _ in range(120):
        clock.advance(0.5)
        changes.tick()
    assert audio.events == []
    snapshot.assert_not_called()
    app.tts_engine.cancel_all.assert_not_called()
    app.tts_engine.shutdown.assert_not_called()
    app.stop_sound_stream.assert_not_called()
    assert app._sound_stream is sound


def test_finish1_engine_fast_and_slow_retries_do_not_run_before_reopen(monkeypatch):
    from samsara.audio_engine import engine as mod
    audio = Audio()
    before = Mock()
    capture = engine(monkeypatch, audio, before_reopen=before)
    capture._running = capture._device_lost = True
    clock = FakeClock()
    monkeypatch.setattr(mod.time, 'monotonic', clock)
    class Wait:
        def wait(self, timeout):
            clock.advance(timeout)
            if clock.now >= 80:
                capture._running = False
            return False
        def clear(self): pass
    capture._reconnect_now = Wait()
    capture._recovery_loop()
    assert clock.now >= 80
    before.assert_not_called()
    assert audio.events == []


def test_finish1_added_input_one_rescan_one_ready(monkeypatch):
    audio, app, clock, changes, state, render, speaking, ready = scenario(monkeypatch)
    audio.pending = [dev(MIC)]
    audio.default.device = (0, -1)
    state[0] = frozenset({('usb', MIC)})
    changes.notify()
    clock.advance(1)
    changes.tick()
    for _ in range(60):
        clock.advance(1)
        changes.tick()
    assert audio.events == ['terminate', 'initialize']
    assert audio.opened == [0]
    assert ready == ['ready']
    app.tts_engine.cancel_all.assert_not_called()
    app._ace_engine.stop()


def test_finish1_speech_defers_beyond_bound_then_resumes_without_cancellation(monkeypatch):
    audio, app, clock, changes, state, render, speaking, ready = scenario(monkeypatch)
    speaking[0] = True
    audio.pending = [dev(MIC)]
    audio.default.device = (0, -1)
    state[0] = frozenset({('usb', MIC)})
    changes.notify()
    for _ in range(40):
        clock.advance(1)
        changes.tick()
    assert audio.events == []
    app.stop_sound_stream.assert_not_called()
    app.tts_engine.cancel_all.assert_not_called()
    app.tts_engine.shutdown.assert_not_called()
    speaking[0] = False
    for _ in range(12):
        clock.advance(1)
        changes.tick()
    assert audio.events == ['terminate', 'initialize']
    assert ready == ['ready']
    app._ace_engine.stop()


@pytest.mark.parametrize('explicit_output', [False, True])
def test_finish1_unrelated_notification_keeps_chosen_capture_and_output_only_route(monkeypatch, explicit_output):
    audio, app, clock, changes, state, render, speaking, ready = scenario(monkeypatch, devices=[dev(MIC)], lost=False)
    stream = app._ace_engine.rescan_stream
    app.wake_word_active = app.command_mode_active = True
    app.output_device = 8 if explicit_output else None
    state[0] = state[0] | {('unrelated', 'Webcam microphone')}
    changes.notify()
    clock.advance(1)
    changes.tick()
    assert app._ace_engine.rescan_stream is stream
    assert audio.events == []
    render[0] = 'monitor'
    changes.notify()
    clock.advance(1)
    changes.tick()
    assert app._ace_engine.rescan_stream is stream
    assert audio.events == []
    assert app.stop_sound_stream.call_count == (0 if explicit_output else 1)
    assert app.tts_engine.restart_stream.call_count == (0 if explicit_output else 1)
    app._ace_engine.stop()


@pytest.mark.parametrize('lane', ['wake_word_active', 'continuous_active', 'command_mode_active', 'ava_mode_active'])
def test_finish1_live_session_defers_return_to_preferred_input(monkeypatch, lane):
    audio, app, clock, changes, state, render, speaking, ready = scenario(monkeypatch, devices=[dev('Built-in')], lost=False)
    setattr(app, lane, True)
    app._using_default_mic = True
    stream = app._ace_engine.rescan_stream
    audio.pending = [dev('Built-in'), dev(MIC)]
    state[0] = state[0] | {('usb', MIC)}
    changes.notify()
    clock.advance(1)
    changes.tick()
    assert app._ace_engine.rescan_stream is stream
    assert audio.events == []
    setattr(app, lane, False)
    clock.advance(1)
    changes.tick()
    assert audio.events == ['terminate', 'initialize']
    app._ace_engine.stop()


def test_finish1_hold_during_deliberate_switch_never_reports_missing(monkeypatch):
    capture = engine(monkeypatch, Audio([dev(MIC)], default=0))
    capture.start()
    capture.stop()  # The intentional stop/start interval inside switch_microphone.
    app = types.SimpleNamespace(_ace_engine=capture, _dictation_consumer=object(),
        _set_microphone_state=Mock(), _show_outcome_chip=Mock(), tray_icon=Mock())
    assert app_method('_start_recording_impl')(app) is False
    app._set_microphone_state.assert_not_called()
    app._show_outcome_chip.assert_not_called()
    app.tray_icon.notify_warning.assert_not_called()


def test_finish1_wizard_meter_blocks_rescan_until_its_worker_closes(monkeypatch):
    from samsara import microphone_recovery as recovery
    from samsara.portaudio_guard import install, RescanDeferred
    import numpy as np
    # Exercise the real wizard worker method with a registered fake PortAudio base.
    class Base:
        def __init__(self, **kw): self.closed = False
        def start(self): pass
        def stop(self): pass
        def close(self): self.closed = True
        def read(self, frames): return np.zeros((frames, 1)), False
    audio = Audio([dev(MIC)], default=0)
    del audio._samsara_stream_guard
    audio._StreamBase = audio.InputStream = Base
    guard = install(audio)
    assert install(audio) is guard
    app = recovery_app(audio)
    app.stop_sound_stream = Mock()
    wizard = types.SimpleNamespace(_selected_device=0, _stream=None,
        _stream_lock=threading.Lock(), _ring_engine=lambda: None)
    checked = []
    def sample(_frame):
        with pytest.raises(RescanDeferred, match='Another audio stream'):
            recovery.prepare_rescan(app, audio)
        assert audio.events == []
        app.stop_sound_stream.assert_not_called()
        checked.append(wizard._stream)
    wizard._on_capture_frame = sample
    tree = ast.parse((ROOT / 'samsara/ui/mic_setup_wizard_qt.py').read_text(encoding='utf-8'))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_read_own_stream')
    ns = dict(sd=audio, np=np, logger=logging.getLogger('wizard-test'),
        _detect_capture_rate=lambda _: 16000, _capture_channels=lambda _: 1,
        NativeRateFrames=lambda _: types.SimpleNamespace(native_rate=16000, blocksize=1600, convert=lambda x: x))
    exec(compile(ast.Module(body=[method], type_ignores=[]), 'mic_setup_wizard_qt.py', 'exec'), ns)
    ns['_read_own_stream'](wizard, lambda: not checked)
    assert len(checked) == 1 and checked[0].closed
    assert not guard.streams
    recovery.prepare_rescan(app, audio)
    assert audio.events == ['terminate', 'initialize']


def test_finish1_new_stream_open_cannot_race_global_rescan():
    from samsara.portaudio_guard import install
    entered = threading.Event()
    class Base:
        def __init__(self): entered.set()
        def close(self): pass
    sd = types.SimpleNamespace(_StreamBase=Base)
    guard = install(sd)
    streams = []
    with guard.reserve():
        worker = threading.Thread(target=lambda: streams.append(Base()))
        worker.start()
        assert not entered.wait(0.05)
    worker.join(1)
    assert entered.is_set() and guard.streams
    streams[0].close()
    assert not guard.streams



def test_finish2_earcon_drops_before_device_lookup_while_rescan_owns_audio():
    # Keep the rescan lock held until AFTER playback returns: blocking cannot pass.
    lock = threading.Lock()
    app = types.SimpleNamespace(config={}, _audio_refresh_lock=lock,
        _resolve_playback_output=Mock(side_effect=AssertionError('must not query audio')))
    finished = threading.Event()
    errors = []
    def play():
        try:
            app_method('play_sound')(app, 'start')
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
    with lock:
        worker = threading.Thread(target=play)
        worker.start()
        returned_while_locked = finished.wait(1)
    worker.join(1)
    assert returned_while_locked
    assert not errors
    app._resolve_playback_output.assert_not_called()


@pytest.mark.parametrize('failure', [False, True])
def test_finish2_earcon_releases_rescan_lock_on_early_return_or_error(failure):
    lock = threading.RLock()
    app = types.SimpleNamespace(config={}, _audio_refresh_lock=lock,
        _resolve_playback_output=Mock(side_effect=RuntimeError('device lookup')) if failure else lambda: 7,
        _sound_stream_device=7, _sound_cache={'start': None})
    play = app_method('play_sound')
    if failure:
        with pytest.raises(RuntimeError, match='device lookup'):
            play(app, 'start')
    else:
        play(app, 'start')
    acquired = []
    def probe():
        acquired.append(lock.acquire(blocking=False))
        if acquired[-1]:
            lock.release()
    worker = threading.Thread(target=probe)
    worker.start()
    worker.join(1)
    assert acquired == [True]


def test_finish2_bounded_duck_open_uses_registered_spawn(monkeypatch):
    from samsara import microphone_recovery as recovery
    from samsara.runtime import thread_registry
    real_spawn = thread_registry.spawn
    spawn = Mock(side_effect=real_spawn)
    monkeypatch.setattr(thread_registry, 'spawn', spawn)
    app = types.SimpleNamespace(_close_hands_free_capture_duck=Mock())
    assert recovery._bounded_open_owned(app, -1, object(), lambda *args: ('opened', True)) == ('opened', True)
    spawn.assert_called_once()
    assert spawn.call_args.args[0] == 'hold-duck-open'
    assert spawn.call_args.kwargs == {'daemon': True}
    app._close_hands_free_capture_duck.assert_not_called()
