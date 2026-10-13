"""Watcher lifetime and device-less startup; no desktop bootstrap or audio IO."""
import logging
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import microphone_recovery as recovery
from samsara.audio_engine.engine import AudioCaptureEngine
from tests.test_audio_hotplug_recovery import Audio, app_method
from tools.ci_smoke import KNOWN_BENIGN_MARKERS, LogScanner


class StopAfter:
    def __init__(self, passes, before=lambda n: None):
        self.passes, self.before, self.calls = passes, before, 0

    def wait(self, timeout):
        self.calls += 1
        self.before(self.calls)
        return self.calls > self.passes


def watcher_app(stop):
    app = SimpleNamespace(_output_watcher_stop=stop, _no_microphone=True,
                          output_device=None)
    app._microphone_changes = recovery.DeviceChanges(app, endpoints=lambda: frozenset())
    return app


@pytest.mark.parametrize('failure', ['none', 'import', 'initialize', 'register', 'pump', 'cleanup'])
def test_com_ownership_and_setup_fallback(monkeypatch, caplog, failure):
    com = SimpleNamespace(CoInitializeEx=Mock(), CoUninitialize=Mock())
    close = Mock()
    register = Mock(return_value=close)
    pump = Mock(side_effect=OSError('Dispatch failed') if failure == 'pump' else None)
    monkeypatch.setitem(sys.modules, 'comtypes.client', SimpleNamespace(PumpEvents=pump))
    if failure == 'initialize':
        com.CoInitializeEx.side_effect = OSError('COM unavailable')
    if failure == 'register':
        register.side_effect = OSError('No endpoint service')
    if failure == 'cleanup':
        close.side_effect = OSError('Endpoint service stopped')
    monkeypatch.setitem(sys.modules, 'comtypes', None if failure == 'import' else com)
    monkeypatch.setattr(recovery, 'register_notifications', register)
    stop = StopAfter(3)
    app = watcher_app(stop)
    tick = Mock(wraps=app._microphone_changes.tick)
    app._microphone_changes.tick = tick
    recovery.watch(app, lambda: None)
    assert stop.calls == 4 and tick.call_count == 3
    assert app._microphone_changes.notifications == (failure in ('none', 'cleanup'))
    assert com.CoUninitialize.call_count == (failure not in ('import', 'initialize'))
    if failure != 'import':
        com.CoInitializeEx.assert_called_once_with()  # Match comtypes' import apartment.
    assert close.call_count == (failure in ('none', 'pump', 'cleanup'))
    assert register.call_count == (failure not in ('import', 'initialize'))
    assert sum('notifications unavailable' in r.message for r in caplog.records) == (
        failure in ('import', 'initialize', 'register', 'pump'))
    assert pump.call_count == (3 if failure in ('none', 'cleanup') else int(failure == 'pump'))
    assert all(r.exc_info is None for r in caplog.records)


def test_bad_output_reads_and_ticks_do_not_end_watcher_or_flood_log(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, 'comtypes', None)
    stop = StopAfter(4)
    app = watcher_app(stop)
    app._microphone_changes.tick = Mock(side_effect=[ValueError(), ValueError(), None, None])
    render = Mock(side_effect=[None, OSError(), OSError(), 'new', 'new'])
    recovery.watch(app, render)
    assert stop.calls == 5
    assert app._microphone_changes.tick.call_count == 4
    assert app._microphone_changes._render == 'new'
    assert sum('Default output observation unavailable' in r.message for r in caplog.records) == 1
    assert sum('Device watcher pass failed' in r.message for r in caplog.records) == 1
    assert all(r.exc_info is None for r in caplog.records)


@pytest.mark.parametrize('busy', ['speech', 'cue'])
@pytest.mark.parametrize('explicit', [False, True])
def test_comparing_mode_tracks_output_without_capture_query_and_waits_for_idle(
        monkeypatch, busy, explicit):
    monkeypatch.setitem(sys.modules, 'comtypes', None)
    now = [0.0]
    restarts = []
    app = watcher_app(None)
    app.output_device = 7 if explicit else None
    app._audio_refresh_lock = threading.RLock()
    app._buffer_lock = threading.RLock()
    app._playback_buffer = [1] if busy == 'cue' else []
    speaking = [busy == 'speech']
    app.tts_engine = SimpleNamespace(is_speaking=lambda: speaking[0],
                                    restart_stream=lambda: restarts.append('tts'))
    app.stop_sound_stream = lambda: restarts.append('stop')
    app._start_sound_stream = lambda: restarts.append('start')
    changes = app._microphone_changes
    changes.clock = lambda: now[0]
    changes.endpoints = Mock(side_effect=OSError('Capture enumeration unavailable'))
    def before(n):
        now[0] = float(n)
        if n == 2:
            assert restarts == []  # The first output change occurred while busy.
            speaking[0] = False
            app._playback_buffer = []
    app._output_watcher_stop = StopAfter(3, before)
    recovery.watch(app, lambda: 'old' if now[0] == 0 else 'new')
    assert restarts == ([] if explicit else ['stop', 'start', 'tts'])
    assert changes._input_pending is False
    assert changes.notifications is False


class ZeroAudio(Audio):
    class PortAudioError(RuntimeError):
        pass

    def query_devices(self, device=None, kind=None):
        if device is None and kind is None:
            return []
        raise self.PortAudioError('Error querying device -1')

    def OutputStream(self, **kwargs):
        raise self.PortAudioError('Error querying device -1')

    def InputStream(self, **kwargs):
        raise AssertionError('No microphone must be detected before opening a stream')


def test_failed_notification_observation_is_retained_until_output_can_be_read(monkeypatch, caplog):
    app = watcher_app(None)
    app._no_microphone = False  # No missing-input polling can rescue this notification.
    changes = app._microphone_changes
    now = [0.0]
    changes.clock = lambda: now[0]
    changes._render = 'old'
    changes.render_id = Mock(side_effect=[OSError(), OSError(), 'new'])
    restart = Mock()
    monkeypatch.setattr(recovery, 'restart_outputs', restart)
    changes.notify()
    for n in (1, 2):
        now[0] = n
        changes.tick()
        assert changes._pending == 0.0
        restart.assert_not_called()
    now[0] = 3
    changes.tick()
    restart.assert_called_once_with(app)
    assert changes._pending is None
    assert sum('Endpoint observation unavailable' in r.message for r in caplog.records) == 1


def test_first_successful_capture_observation_recovers_after_failed_prime(monkeypatch):
    app = watcher_app(None)
    changes = app._microphone_changes
    changes.notifications = False
    changes.endpoints = Mock(side_effect=[OSError(), frozenset({('mic', 'Headset')})])
    with pytest.raises(OSError):
        changes.prime()
    refresh = Mock()
    monkeypatch.setattr(recovery, 'refresh', refresh)
    changes.tick()
    refresh.assert_called_once_with(app)
    assert changes._input_pending is False


def test_zero_device_startup_log_is_accepted_by_unchanged_smoke_rules(monkeypatch, caplog):
    audio = ZeroAudio()
    monkeypatch.setitem(sys.modules, 'sounddevice', audio)
    monkeypatch.setitem(sys.modules, 'comtypes', None)
    caplog.set_level(logging.DEBUG)
    app = watcher_app(StopAfter(1))
    app.config = {'microphone': None, 'threshold_mode': 'auto'}
    app._ace_engine = None
    app._audio_refresh_lock = threading.RLock()
    app._sound_stream_sr = 44100
    app._resolve_playback_output = lambda: None
    app._sound_stream_callback = Mock()
    app._on_ace_stream_death = Mock()
    app._on_ace_recovery_success = Mock()
    app._on_ace_recovery_give_up = Mock()
    app._prepare_audio_rescan = Mock()
    app._set_microphone_state = lambda lost: recovery.set_state(app, lost)
    app.capture_rate = app_method('_detect_capture_rate', sd=audio, DEFAULT_CAPTURE_RATE=16000)(app, 0)
    assert app.capture_rate == 16000
    assert app_method('_run_calibration_if_auto', sd=audio)(app, use_cache=True) == 'unavailable'
    app_method('_start_sound_stream', sd=audio)(app)
    assert app._sound_stream is None
    app_method('_start_ace_engine', AudioCaptureEngine=AudioCaptureEngine,
               FrameBus=lambda: SimpleNamespace(), time=time)(app)
    assert app._ace_engine is None
    recovery.set_state(app, True)
    recovery.watch(app, lambda: None)
    assert app._no_microphone is True
    assert app._output_watcher_stop.calls == 2
    with_tracebacks = [r for r in caplog.records if r.exc_info]
    assert [r.getMessage() for r in with_tracebacks] == [
        '[AUDIO] Failed to start sound stream: Error querying device -1']
    assert all(any(marker in r.getMessage() for marker in KNOWN_BENIGN_MARKERS)
               for r in with_tracebacks)
    scanner = LogScanner()
    formatter = logging.Formatter('%(levelname)s - %(message)s')
    scanner.feed('\n'.join(formatter.format(r) for r in caplog.records).splitlines())
    assert scanner.unexplained_crash_line is None
    assert scanner.benign_seen


def test_microphone_disappearing_before_measurement_defers_without_traceback(monkeypatch, caplog):
    audio = ZeroAudio()
    monkeypatch.setitem(sys.modules, 'sounddevice', audio)
    app = SimpleNamespace(config={}, capture_rate=16000)
    caplog.set_level(logging.INFO)
    result = app_method('_measure_speech_threshold', sd=audio,
                        DEFAULT_SPEECH_THRESHOLD=0.03)(app)
    assert result == (0.03, False)
    assert any('calibration deferred' in r.message for r in caplog.records)
    assert all(r.exc_info is None for r in caplog.records)
