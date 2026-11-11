"""1206: fake devices/clock, real recovery, ACE and listener admission."""
from types import SimpleNamespace
from unittest.mock import Mock
import threading
import numpy as np

from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara.audio_engine.continuous_consumer import ContinuousConsumer
from samsara.audio_engine.ring import FrameBus
from samsara.audio_engine.frame import FRAME_SIZE
from samsara.portaudio_guard import RescanDeferred

import pytest

from samsara import microphone_recovery as recovery
from tests.test_audio_hotplug_recovery import Audio, dev, engine, recovery_app


@pytest.fixture
def mic_change(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(recovery.time, 'monotonic', lambda: now[0])
    audio = Audio([dev('Chosen microphone')], default=0)
    app = recovery_app(audio)
    app.config = dict(microphone=0, microphone_name='Chosen microphone',
                      wake_word_enabled=True, hands_free_enabled=True)
    app._no_microphone = False
    app.recording = app.is_speaking = False
    app.wake_word_active = app.continuous_active = False
    bus = FrameBus()
    listener_engine = SimpleNamespace(register_consumer=bus.new_reader)
    app._wake_consumer = WakeConsumer(listener_engine, app)
    app._continuous_consumer = ContinuousConsumer(listener_engine, app)
    # No poll threads/audio devices: explicit _poll_frame drives real admission.
    app._wake_consumer._running = app._continuous_consumer._running = True
    capture = engine(monkeypatch, audio, dict(app.config),
                     before_reopen=app._prepare_audio_rescan)
    app._ace_engine = capture
    capture.start()
    app.available_mics = app.get_available_microphones()
    endpoints = [frozenset({('chosen', 'Chosen microphone')})]
    changes = recovery.DeviceChanges(
        app, clock=lambda: now[0], endpoints=lambda: endpoints[0])
    app._microphone_changes = changes
    changes.prime()
    try:
        yield audio, app, changes, now, endpoints
    finally:
        capture.stop()


def add_microphone(audio, changes, now, endpoints):
    audio.pending = [dev('Chosen microphone'), dev('New microphone')]
    endpoints[0] |= {('new', 'New microphone')}
    changes.notify()  # Same scheduling entry point as endpoint-added callbacks.
    # Cross the debounce, the 30-second backoff boundary and two 5-second retries.
    for step in range(1, 91):
        now[0] = step * 0.5
        changes.tick()


@pytest.mark.parametrize('listener', ['wake_word_active', 'continuous_active'])
def test_idle_listener_does_not_defer_new_microphone_forever(mic_change, listener):
    audio, app, changes, now, endpoints = mic_change
    # System default makes the existing observer queue the endpoint change,
    # isolating the always-busy admission defect from the preferred-mic defect.
    app.config.update(microphone=None, microphone_name='System default')
    setattr(app, listener, True)
    selection = dict(app.config)
    add_microphone(audio, changes, now, endpoints)

    assert changes._input_pending is False, 'Idle listener still blocks refresh after 45 s'
    assert [mic['name'] for mic in app.available_mics] == [
        'Chosen microphone', 'New microphone']
    assert audio.events == ['terminate', 'initialize']
    assert app.config == selection  # Arrival must not choose the new microphone.
    assert app._capture_device == 0
    consumer = app._wake_consumer if listener == 'wake_word_active' else app._continuous_consumer
    assert getattr(consumer, 'running', getattr(consumer, '_running', False))


def test_new_input_is_listed_with_healthy_preferred_microphone(mic_change):
    audio, app, changes, now, endpoints = mic_change
    selection = dict(app.config)
    add_microphone(audio, changes, now, endpoints)

    assert [mic['name'] for mic in app.available_mics] == [
        'Chosen microphone', 'New microphone']
    assert app.config == selection
    assert audio.opened[-1] == 0


def test_same_fake_rescans_when_no_listener_blocks_it(mic_change):
    audio, app, changes, now, endpoints = mic_change
    app.config.update(microphone=None, microphone_name='System default')
    add_microphone(audio, changes, now, endpoints)

    assert changes._input_pending is False
    assert [mic['name'] for mic in app.available_mics] == [
        'Chosen microphone', 'New microphone']
    assert audio.events == ['terminate', 'initialize']
    assert audio.opened == [0, 0]


@pytest.mark.parametrize('field', ['recording', '_hotkey_recording', '_stop_in_flight',
                                  'is_speaking', 'command_mode_active',
                                  'ava_mode_active', 'ava_command_session_active'])
def test_active_capture_defers_then_completes_without_changing_selection(mic_change, field):
    audio, app, changes, now, endpoints = mic_change
    setattr(app, field, True)
    selection = dict(app.config)
    add_microphone(audio, changes, now, endpoints)
    assert changes._input_pending and not audio.events
    setattr(app, field, False)
    now[0] += 5
    changes.tick()
    assert not changes._input_pending
    assert len(app.available_mics) == 2 and app.config == selection


@pytest.mark.parametrize('name,field', [('_wake_consumer', '_utterance_frames'),
                                       ('_continuous_consumer', '_speech_frames')])
def test_uncommitted_audio_defers_even_between_phrases(mic_change, name, field):
    audio, app, changes, now, endpoints = mic_change
    consumer = getattr(app, name)
    frames = [np.ones(FRAME_SIZE, dtype=np.float32)]
    setattr(consumer, field, frames)
    add_microphone(audio, changes, now, endpoints)
    assert changes._input_pending and not audio.events
    assert getattr(consumer, field) is frames  # No stop/start/abort/drain.
    setattr(consumer, field, [])
    now[0] += 5
    changes.tick()
    assert not changes._input_pending and len(app.available_mics) == 2


@pytest.mark.parametrize('cls', [WakeConsumer, ContinuousConsumer])
def test_pause_refuses_frame_in_flight_and_preserves_speech(cls):
    app = SimpleNamespace(config={}, is_speaking=False)
    bus = FrameBus()
    consumer = cls(SimpleNamespace(register_consumer=bus.new_reader), app)
    consumer._running = True
    entered, finish = threading.Event(), threading.Event()
    speech = [np.ones(FRAME_SIZE, dtype=np.float32)]
    field = '_utterance_frames' if cls is WakeConsumer else '_speech_frames'
    def speech_begins(frame):
        entered.set()
        assert finish.wait(2)
        setattr(consumer, field, speech)
    consumer._process_frame = speech_begins
    bus.write(np.ones(FRAME_SIZE, dtype=np.int16), 0, 0)
    worker = threading.Thread(target=consumer._poll_frame, args=(threading.Event(),))
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(RescanDeferred, match='processing audio'):
            with consumer.pause_if_idle():
                pytest.fail('In-flight speech must win admission')
    finally:
        finish.set()
        worker.join(2)
    assert not worker.is_alive()
    with pytest.raises(RescanDeferred, match='speech or uncommitted'):
        with consumer.pause_if_idle():
            pytest.fail('Speech buffers must prevent refresh')
    assert getattr(consumer, field) is speech


@pytest.mark.parametrize('cls', [WakeConsumer, ContinuousConsumer])
def test_pause_preserves_unread_audio_and_resumes_same_poller(cls):
    app = SimpleNamespace(config={}, is_speaking=False)
    bus = FrameBus()
    consumer = cls(SimpleNamespace(register_consumer=bus.new_reader), app)
    consumer._running = True
    pcm = np.ones(FRAME_SIZE, dtype=np.int16)
    bus.write(pcm, 0, 0)
    with pytest.raises(RescanDeferred, match='unread audio'):
        with consumer.pause_if_idle():
            pytest.fail('Queued speech is not idle')
    seen = []
    consumer._process_frame = lambda frame: seen.append(frame.pcm.copy())
    assert consumer._poll_frame(threading.Event())
    with consumer.pause_if_idle():
        bus.write(pcm, 1, 0)
        assert not consumer._poll_frame(threading.Event())
        assert len(seen) == 1
    assert consumer._poll_frame(threading.Event())
    assert len(seen) == 2 and np.array_equal(seen[-1], pcm)
    assert consumer._running


def test_both_listeners_resume_if_second_refuses(mic_change):
    audio, app, changes, now, endpoints = mic_change
    app.wake_word_active = app.continuous_active = True
    app._continuous_consumer._rescan_lock.acquire()
    try:
        with pytest.raises(RescanDeferred):
            recovery.refresh(app)
        assert not audio.events
        assert app._wake_consumer._rescan_lock.acquire(False)
        app._wake_consumer._rescan_lock.release()
    finally:
        app._continuous_consumer._rescan_lock.release()
    recovery.refresh(app)
    assert audio.events == ['terminate', 'initialize']


def test_rescan_gap_measured_with_fake_clock(mic_change, monkeypatch, record_property):
    audio, app, changes, now, endpoints = mic_change
    app.wake_word_active = app.continuous_active = True
    wake = app._wake_consumer
    start = now[0]
    original = audio._initialize
    def initialize():
        assert wake._rescan_lock.locked()
        now[0] += 0.240  # Simulated backend close/enumerate/open interval.
        original()
    monkeypatch.setattr(audio, '_initialize', initialize)
    recovery.refresh(app)
    gap_ms = (now[0] - start) * 1000
    record_property('wake_gap_ms', gap_ms)
    assert gap_ms == pytest.approx(240)
    assert not wake._rescan_lock.locked() and wake.running
    assert not app._continuous_consumer._rescan_lock.locked()


def test_deferral_logs_once_then_completion(mic_change, caplog):
    audio, app, changes, now, endpoints = mic_change
    app.recording = True
    with caplog.at_level('INFO', logger='Samsara'):
        add_microphone(audio, changes, now, endpoints)
        app.recording = False
        now[0] += 5
        changes.tick()
    messages = [r.message for r in caplog.records if '[MIC] Input refresh' in r.message]
    assert len(messages) == 2
    assert 'deferred: Capture session is open' in messages[0]
    assert messages[1] == '[MIC] Input refresh completed'


def test_idle_removal_keeps_existing_device_lost_path(mic_change):
    audio, app, changes, now, endpoints = mic_change
    app.wake_word_active = True
    lost = Mock()
    app._ace_engine.mark_device_lost = lost
    endpoints[0] = frozenset()
    changes._observe()
    lost.assert_called_once_with()
    assert changes._input_pending


def test_poll_without_notifications_discovers_new_preferred_input(mic_change):
    audio, app, changes, now, endpoints = mic_change
    changes.notifications = False
    audio.pending = [dev('Chosen microphone'), dev('New microphone')]
    endpoints[0] |= {('new', 'New microphone')}
    now[0] = 5
    changes.tick()
    assert len(app.available_mics) == 2
    assert app.config['microphone_name'] == 'Chosen microphone'


def test_menu_requests_known_stale_list_even_during_speech(mic_change, monkeypatch):
    from samsara.ui import tray_microphones as tray
    audio, app, changes, now, endpoints = mic_change
    app.recording = True
    app._mic_refresh_blocked = lambda: True
    app.switch_microphone_and_refresh = Mock()
    changes.notify()
    menu = SimpleNamespace(setStyleSheet=Mock())
    monkeypatch.setattr(tray, 'QMenu', lambda label: menu)
    add = Mock()
    assert tray.build_microphone_menu(app, add) is menu
    assert changes._input_pending and not audio.events
    assert len(app.available_mics) == 1


@pytest.mark.parametrize('cls', [WakeConsumer, ContinuousConsumer])
def test_idle_reservation_resumes_original_live_thread(cls):
    app = SimpleNamespace(config={}, is_speaking=False, wake_word_active=True,
                          continuous_active=True)
    bus = FrameBus()
    consumer = cls(SimpleNamespace(register_consumer=bus.new_reader), app)
    seen = threading.Event()
    consumer._process_frame = lambda frame: seen.set()
    assert consumer.start()
    thread = consumer._thread
    try:
        with consumer.pause_if_idle():
            bus.write(np.ones(FRAME_SIZE, dtype=np.int16), 0, 0)
            assert not seen.wait(.02)
            assert thread.is_alive()
        assert seen.wait(2)
        assert consumer._thread is thread
    finally:
        consumer.stop()
    assert not thread.is_alive()


def test_draft_audio_prevents_refresh(mic_change):
    audio, app, changes, now, endpoints = mic_change
    audio_frames = [np.ones(FRAME_SIZE)]
    app._session_mode_manager = SimpleNamespace(_dictate_pending_audio=audio_frames)
    add_microphone(audio, changes, now, endpoints)
    assert changes._input_pending and not audio.events
    assert app._session_mode_manager._dictate_pending_audio is audio_frames
    app._session_mode_manager._dictate_pending_audio = []
    now[0] += 5
    changes.tick()
    assert not changes._input_pending



def test_speech_arriving_while_other_listener_pauses_refuses_rescan(mic_change, monkeypatch):
    from contextlib import contextmanager
    audio, app, changes, now, endpoints = mic_change
    wake, continuous = app._wake_consumer, app._continuous_consumer
    original = continuous.pause_if_idle
    @contextmanager
    def pause_with_arrival():
        with original() as check:
            wake._reader._bus.write(np.ones(FRAME_SIZE, dtype=np.int16), 0, 0)
            yield check
    monkeypatch.setattr(continuous, 'pause_if_idle', pause_with_arrival)
    with pytest.raises(RescanDeferred, match='unread audio'):
        recovery.refresh(app)
    assert not audio.events
    assert wake._reader._read_cursor == 0  # Onset has not been consumed/discarded.
    assert not wake._rescan_lock.locked() and not continuous._rescan_lock.locked()


@pytest.mark.parametrize('opened', [False, True])
def test_ava_question_retains_stream_with_wake_word_off(mic_change, opened):
    audio, app, changes, now, endpoints = mic_change
    app.wake_word_active = app.continuous_active = False
    app._ava_question = SimpleNamespace(opened=opened)
    stream = app._ace_engine.rescan_stream
    add_microphone(audio, changes, now, endpoints)
    assert changes._input_pending and not audio.events
    assert app._ace_engine.rescan_stream is stream
    app._ava_question = None
    now[0] += 5
    changes.tick()
    assert not changes._input_pending and len(app.available_mics) == 2


def test_unrelated_input_refreshes_even_with_ambiguous_capture_aliases(mic_change):
    audio, app, changes, now, endpoints = mic_change
    # The engine exposes the MME 31-character prefix, while Windows exposes
    # two distinct endpoint IDs sharing it.
    full_name = 'Microphone (Chosen USB Recording Device)'
    app._ace_engine._device_name = full_name[:31]
    endpoints[0] = frozenset({('chosen', full_name), ('alias', full_name + ' #2')})
    assert len(recovery.matching_capture_endpoints(endpoints[0], full_name[:31])) == 2
    changes.prime()
    audio.pending = [dev('Chosen microphone'), dev('New microphone')]
    endpoints[0] |= {('new', 'New microphone')}
    changes.notify()
    now[0] = 1
    changes.tick()
    assert not changes._input_pending
    assert len(app.available_mics) == 2
    assert app.config['microphone_name'] == 'Chosen microphone'
