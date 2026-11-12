"""1217: replay real admission, persistent outputs, and registered stream lives."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest

from samsara import microphone_recovery as recovery
from samsara.audio_engine import engine as engine_module
from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara.audio_engine.continuous_consumer import ContinuousConsumer
from samsara.audio_engine.ring import FrameBus
from samsara.portaudio_guard import RescanDeferred, install
from tests.test_audio_hotplug_recovery import Audio, dev, engine, recovery_app

CHOSEN = 'Focusrite USB'
HEADSET = 'Microphone (Arctis Nova Pro Wireless)'


class BusyGuard:
    def __init__(self, guard):
        self.guard, self.streams = guard, guard.streams
        self.remaining = 0
        self.attempts = 0

    @contextmanager
    def reserve(self, owned=()):
        if owned:
            self.attempts += 1
            if self.remaining:
                self.remaining -= 1
                raise RescanDeferred('wizard meter')
        with self.guard.reserve(owned):
            yield


@pytest.fixture
def sequence(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(recovery.time, 'monotonic', lambda: now[0])
    # Prevent recovery daemons in the intentional backend-failure case.
    monkeypatch.setattr(engine_module.thread_registry, 'spawn', Mock())
    audio = Audio([dev(CHOSEN)], default=0)
    del audio._samsara_stream_guard
    lives = []

    class Base:
        def __init__(self, **kwargs):
            self.closed = False
            self.active = False
            self.device = kwargs.get('device')
            lives.append(('open', type(self).__name__, now[0]))
        def start(self): self.active = True
        def stop(self): self.active = False
        def close(self):
            self.closed = True
            lives.append(('close', type(self).__name__, now[0]))

    class InputStream(Base): pass
    class OutputStream(Base): pass
    audio._StreamBase, audio.InputStream, audio.OutputStream = Base, InputStream, OutputStream
    guard = BusyGuard(install(audio))
    audio._samsara_stream_guard = guard
    app = recovery_app(audio)
    app.config = dict(microphone=0, microphone_name=CHOSEN,
                      wake_word_enabled=True, hands_free_enabled=True)
    app._no_microphone = False
    app.recording = app.is_speaking = False
    app.wake_word_active = app.continuous_active = True
    app.audio_coordinator = SimpleNamespace(_dispatch_lock=threading.RLock())
    local = SimpleNamespace(is_speaking=lambda: False, _tts_stream=None)
    def open_tts():
        local._tts_stream = audio.OutputStream()
        local._tts_stream.start()
    def close_tts():
        local._tts_stream.close()
        local._tts_stream = None
    open_tts()
    local._close_persistent_stream = close_tts
    # The missing 1206 fake: Edge wraps the local voice with a plain Lock.
    app.tts_engine = SimpleNamespace(_worker_lock=threading.Lock(),
        _fallback_engine=local, is_speaking=lambda: False, restart_stream=open_tts)
    def start_sound():
        app._sound_stream = audio.OutputStream()
        app._sound_stream.start()
    def stop_sound():
        app._sound_stream.close()
        app._sound_stream = None
    app.stop_sound_stream, app._start_sound_stream = stop_sound, start_sound
    start_sound()
    bus = FrameBus()
    listener_engine = SimpleNamespace(register_consumer=bus.new_reader)
    app._wake_consumer = WakeConsumer(listener_engine, app)
    app._continuous_consumer = ContinuousConsumer(listener_engine, app)
    app._wake_consumer._running = app._continuous_consumer._running = True
    capture = engine(monkeypatch, audio, dict(app.config), before_reopen=app._prepare_audio_rescan)
    app._ace_engine = capture
    capture.start()
    app.available_mics = app.get_available_microphones()
    endpoints = [frozenset({('chosen', CHOSEN)})]
    changes = recovery.DeviceChanges(app, clock=lambda: now[0], endpoints=lambda: endpoints[0])
    app._microphone_changes = changes
    changes.prime()
    audio.pending = [dev(CHOSEN), dev(HEADSET)]
    endpoints[0] |= {('headset', HEADSET)}
    try:
        yield audio, app, changes, now, endpoints, guard, lives
    finally:
        capture.stop()
        stop_sound()
        close_tts()


def audits(caplog):
    return [record.message for record in caplog.records
            if record.message.startswith('[MIC] Input refresh:')]


@pytest.mark.parametrize('busy_attempts', [0, 1, 4])
def test_busy_then_real_rescan_keeps_capture_and_selection(sequence, caplog, busy_attempts):
    audio, app, changes, now, endpoints, guard, lives = sequence
    guard.remaining = busy_attempts
    original_stream = app._ace_engine.rescan_stream
    selection = dict(app.config)
    lives.clear()
    app._rescan_idle_since = None  # First of the owner's five lines.
    changes.notify()
    with caplog.at_level('INFO'):
        for step in range(1, 4 + busy_attempts):
            now[0] = step * .5
            changes.tick()
            if not audio.events:
                assert changes._input_pending
                assert app._ace_engine.rescan_stream is original_stream
                assert not app._no_microphone and not app._ace_engine.device_lost
                assert not lives  # Includes persistent output closes.
                assert not audits(caplog)
                assert not any('completed' in r.message for r in caplog.records)
        assert audio.events == ['terminate', 'initialize']
    assert not changes._input_pending and not changes._input_stale
    assert [m['name'] for m in app.available_mics] == [CHOSEN, HEADSET]
    assert app.config == selection
    assert app._ace_engine.capture_name == CHOSEN
    assert len(guard.streams) == 3  # ACE, sound cue, local TTS, all reopened.
    assert app._rescan_owner is None
    assert app.tts_engine._worker_lock.acquire(False)
    app.tts_engine._worker_lock.release()
    assert not any('Capture state: unavailable' in r.message for r in caplog.records)
    assert len(audits(caplog)) == 1
    assert 'rescanned=True devices_before=1 devices_after=2' in audits(caplog)[0]
    assert f'added={[HEADSET]}' in audits(caplog)[0]
    assert 'selection_changed=False' in audits(caplog)[0]
    assert len([r for r in caplog.records if 'Input refresh completed' in r.message]) == 1


def test_busy_forever_gives_up_without_touching_any_stream(sequence, caplog):
    audio, app, changes, now, endpoints, guard, lives = sequence
    guard.remaining = 10000
    changes.request_input()
    lives.clear()
    with caplog.at_level('INFO'):
        for step in range(251):
            now[0] = step * .5
            changes.tick()
    assert not changes._input_pending and changes._input_stale
    assert not audio.events and not lives
    assert [m['name'] for m in app.available_mics] == [CHOSEN]
    assert not app._no_microphone
    assert guard.attempts < 85  # Backoff, not a tight retry loop.
    assert len(audits(caplog)) == 1
    assert 'rescanned=False' in audits(caplog)[0]
    assert 'busy_owner=wizard meter' in audits(caplog)[0]
    assert 'waited_ms=120000' in audits(caplog)[0]
    assert len([r for r in caplog.records if 'Input refresh deferred:' in r.message]) == 1
    assert any('given up after 2 minutes' in r.message for r in caplog.records)
    assert not any('completed' in r.message for r in caplog.records)


def test_actual_tts_worker_busy_retries_before_capture_closes(sequence, caplog):
    audio, app, changes, now, endpoints, guard, lives = sequence
    changes.request_input()
    stream = app._ace_engine.rescan_stream
    app.tts_engine._worker_lock.acquire()
    try:
        with caplog.at_level('INFO'):
            changes.tick()
        assert changes._input_pending and not audio.events
        assert app._ace_engine.rescan_stream is stream
        assert any('Audio owner is busy: TTS worker' in r.message for r in caplog.records)
    finally:
        app.tts_engine._worker_lock.release()
    now[0] = .5
    changes.tick()
    assert not changes._input_pending and len(app.available_mics) == 2


def test_backend_failure_never_logs_completed(sequence, monkeypatch, caplog):
    audio, app, changes, now, endpoints, guard, lives = sequence
    def fail():
        assert any(event[0] == 'close' for event in lives)
        audio.events.append('terminate-failed')
        raise RuntimeError('backend failure')
    monkeypatch.setattr(audio, '_terminate', fail)
    changes.request_input()
    with caplog.at_level('INFO'):
        changes.tick()
    assert changes._input_pending
    assert app._no_microphone  # Only after the admitted real rescan attempt.
    assert audio.events == ['terminate-failed']
    assert not audits(caplog)
    assert not any('completed' in r.message for r in caplog.records)


def test_ace_false_return_cannot_clear_request(sequence, monkeypatch, caplog):
    audio, app, changes, now, endpoints, guard, lives = sequence
    monkeypatch.setattr(app._ace_engine, 'refresh', lambda *a, **kw: False)
    changes.request_input()
    with caplog.at_level('INFO'):
        changes.tick()
    assert changes._input_pending and not audio.events
    assert len(app.available_mics) == 1
    assert not audits(caplog)
    assert not any('completed' in r.message for r in caplog.records)


def test_lost_notification_is_caught_with_notifications_enabled(sequence):
    audio, app, changes, now, endpoints, guard, lives = sequence
    assert changes.notifications
    now[0] = 5
    changes.tick()  # Deliberately no notify().
    assert len(app.available_mics) == 2 and not changes._input_pending


def test_tray_next_open_has_new_list_and_can_retry_after_giveup(sequence, monkeypatch):
    from samsara.ui import tray_microphones as tray
    audio, app, changes, now, endpoints, guard, lives = sequence
    app._mic_refresh_blocked = lambda: True
    app.switch_microphone_and_refresh = Mock()
    monkeypatch.setattr(tray, 'QMenu', lambda label: SimpleNamespace(setStyleSheet=Mock()))
    changes._input_stale = True  # A previous bounded attempt gave up.
    first = []
    tray.build_microphone_menu(app, lambda menu, label, *a, **kw: first.append(label))
    assert changes._input_pending and len(first) == 2  # Default + cached Focusrite.
    now[0] = .5
    changes.tick()
    second = []
    tray.build_microphone_menu(app, lambda menu, label, *a, **kw: second.append(label))
    assert len(second) == 3 and any(HEADSET in name for name in second)


def test_new_windows_default_does_not_change_named_selection(sequence):
    audio, app, changes, now, endpoints, guard, lives = sequence
    audio.default.device = (1, -1)
    changes.input_id = lambda: 'headset'
    changes.notify()
    now[0] = 1
    changes.tick()
    assert app.config['microphone_name'] == CHOSEN
    assert app._capture_device == 0


def test_system_default_selection_follows_new_default(sequence):
    audio, app, changes, now, endpoints, guard, lives = sequence
    app.config.update(microphone=None, microphone_name='System default')
    audio.default.device = (1, -1)
    changes.request_input()
    changes.tick()
    assert app.config['microphone_name'] == 'System default'
    assert app._capture_device == 1


def test_ui_and_watcher_cannot_run_the_same_request_twice(sequence, monkeypatch, caplog):
    audio, app, changes, now, endpoints, guard, lives = sequence
    original = recovery.refresh
    def refresh_with_simultaneous_ui(app):
        # Same synchronization refusal applies to a second thread, without
        # scheduling-dependent timing or touching a real audio device.
        assert recovery.request_refresh(app) is app.available_mics
        return original(app)
    monkeypatch.setattr(recovery, 'refresh', refresh_with_simultaneous_ui)
    changes.request_input()
    with caplog.at_level('INFO'):
        changes.tick()
    assert audio.events == ['terminate', 'initialize']
    assert len(audits(caplog)) == 1 and not changes._input_pending
