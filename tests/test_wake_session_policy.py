"""Session admission with real policy methods, a fake clock, and no mic/models."""

import ast
import copy
import logging
from pathlib import Path
import threading
import time
from types import MethodType, ModuleType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.audio_engine import wake_consumer as wake_module
from samsara.audio_engine.frame import FRAME_MS, FRAME_SIZE, PREBUFFER_FRAMES, SAMPLE_RATE
from samsara.audio_engine.ring import FrameBus
from samsara.audio_engine.wake_consumer import WakeConsumer, wake_session_policy
from samsara.audio_engine.wake_dispatch import TranscriptionOwners, WakeDispatchQueue
from samsara.session_modes import SessionMode
from samsara import config_defaults


def load_app_policy_methods():
    """Compile real policy methods without executing the desktop app prologue."""
    path = Path(__file__).resolve().parents[1] / 'dictation.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    app = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == 'DictationApp')
    names = {
        '_confirm_wake_capture', '_start_wake_session', '_restart_wake_session_timer',
        '_expire_wake_session', '_end_wake_session', '_reset_wake_dictation',
        '_touch_session_activity', '_warn_wake_fallback_once', '_load_oww_model',
        'start_wake_word_mode', 'process_wake_word_buffer', '_decode_wake_word_buffer',
        '_request_wake_models',
    }
    methods = [node for node in app.body if isinstance(node, ast.FunctionDef)
               and node.name in names]
    assert {node.name for node in methods} == names
    module = ModuleType('wake_session_policy_methods')
    module.__dict__.update(
        time=time, logger=logging.getLogger(__name__), np=np,
        thread_registry=SimpleNamespace(timer=Mock()),
        flight_recorder=wake_module.flight_recorder,
        WakeWordDetector=Mock(), resample_audio=lambda audio, *args: audio,
        # start_wake_word_mode reads config_defaults.DEFAULTS -- this AST
        # extraction only pulls function bodies and a couple of module
        # constants (see `constants` below), never the real module's
        # top-level `from samsara import config_defaults`, so any
        # extracted method's globals must be supplied here by hand.
        config_defaults=config_defaults,
    )
    constants = [node for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id in {
                     '_WAKE_SESSION_CHUNK_GAP_S', '_WAKE_SESSION_SEND_WORDS',
                 } for target in node.targets)]
    exec(compile(ast.Module(body=constants + methods, type_ignores=[]), str(path), 'exec'),
         module.__dict__)
    module.DictationApp = type('AppPolicy', (), {
        node.name: module.__dict__[node.name] for node in methods})
    return module


dictation = load_app_policy_methods()


@pytest.fixture
def rig(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    timers = []
    events = []
    threads = []
    real_spawn = wake_module.thread_registry.spawn

    def spawn(*args, **kwargs):
        thread = real_spawn(*args, **kwargs)
        threads.append(thread)
        return thread

    def drain():
        # 10s, not 3s: under a full-suite run these FIFO worker threads
        # compete with many other tests' still-unreaped daemon threads for
        # CPU, and a tight bound has been observed to trip on a loaded
        # machine even though the thread finishes soon after -- this only
        # gives real work more wall-clock headroom, it does not change
        # what's asserted.
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive(), thread.name

    def timer(name, delay, callback, **kwargs):
        result = SimpleNamespace(name=name, delay=delay, callback=callback, cancel=Mock())
        timers.append(result)
        return result

    monkeypatch.setattr(dictation.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(dictation.time, 'perf_counter', lambda: clock.now)
    monkeypatch.setattr(dictation.time, 'time', lambda: clock.now)
    monkeypatch.setattr(dictation.time, 'sleep', lambda delay: None)
    monkeypatch.setattr(dictation.thread_registry, 'timer', timer)
    monkeypatch.setattr(wake_module.thread_registry, 'spawn', spawn)
    monkeypatch.setattr(wake_module.flight_recorder, 'record',
                        lambda name, **fields: events.append((name, fields)))

    def make(config=None, toggle=False, hold=False):
        app = SimpleNamespace(
            config=copy.deepcopy(config or {}), app_state='asleep',
            _wake_session_lock=threading.RLock(),
            _wake_dispatch_queue=WakeDispatchQueue(config or {}),
            _transcription_owners=TranscriptionOwners(),
            _hotkey_recording=hold, command_mode_active=toggle,
            ava_command_session_active=False, wake_word_active=True,
            wake_word_triggered=False, _oww_wake_detected=False,
            _wake_detector=None, _tts_last_speaking=0.0,
            _command_executed_at=None, audio_coordinator=None,
            is_speaking=False, silence_start=None, _vad_available=True,
            _vad_is_speech=Mock(return_value=True), _vad_reset=Mock(),
            _dictation_silence_timeout=None, _session_recovery_pause=False,
            _session_mode_manager=SimpleNamespace(mode=SessionMode.DICTATE),
            _duck_audio=Mock(), _restore_audio=Mock(), _indicator_reset=Mock(),
            _open_hands_free_capture_duck=Mock(return_value=1),
            _close_hands_free_capture_duck=Mock(), play_sound=Mock(),
            _reset_command_mode_inactivity_timer=Mock(),
            _output_dictation=Mock(), process_wake_word_buffer=Mock(),
            model_loaded=True, loading_model=False,
            _start_hands_free_idle_duck=Mock(), _ensure_wake_consumer=Mock(),
            set_app_state=Mock(), _request_icon_chase=Mock(),
        )
        if toggle:
            app.config.setdefault('command_mode', {}).update(mode='toggle')
        for name in (
            '_confirm_wake_capture', '_start_wake_session', '_restart_wake_session_timer',
            '_expire_wake_session', '_end_wake_session', '_reset_wake_dictation',
            '_touch_session_activity', '_warn_wake_fallback_once', '_load_oww_model',
            'start_wake_word_mode', '_decode_wake_word_buffer', '_request_wake_models',
        ):
            setattr(app, name, MethodType(getattr(dictation.DictationApp, name), app))
        bus = FrameBus()
        reader = bus.new_reader()
        engine = SimpleNamespace(register_consumer=lambda name: reader)
        consumer = WakeConsumer(engine, app)
        app._wake_consumer = consumer

        def feed(end, speech=True, value=12000, process=True):
            clock.now = end
            app._vad_is_speech.return_value = speech
            pcm = np.full(FRAME_SIZE, value, dtype=np.int16)
            bus.write(pcm, t_capture=end, device_epoch=0)
            if process:
                consumer._process_frame(reader.read_next())

        return SimpleNamespace(app=app, consumer=consumer, bus=bus, reader=reader,
                               feed=feed, clock=clock, timers=timers, events=events, drain=drain)

    yield make
    drain()


@pytest.mark.parametrize('config', [{}, {'wake_word': 'jarvis'}, {'wake_word_config': {}}])
def test_defaults_do_not_modify_config(config):
    before = copy.deepcopy(config)
    assert wake_session_policy(config) == {
        'prebuffer_policy': 'discard', 'post_wake_guard_ms': 150.0,
        'max_session_s': 20.0, 'inactivity_timeout_s': 10.0,
    }
    assert config == before


def test_canonical_keys_override_legacy_session_settings():
    config = {
        'wake_word_config': {'session': {'prebuffer_policy': 'keep', 'max_session_s': 30}},
        'wake_word': {'session': {'prebuffer_policy': 'discard', 'post_wake_guard_ms': 75}},
    }
    policy = wake_session_policy(config)
    assert policy['prebuffer_policy'] == 'discard'
    assert policy['post_wake_guard_ms'] == 75
    assert policy['max_session_s'] == 30


def test_discard_capture_starts_after_confirmation_and_guard(rig, monkeypatch, caplog):
    # Queue 44: an open session now keeps the rewind by default; this pins the
    # original discard (tests/test_wake_prebuffer_in_session.py covers retain).
    r = rig({'wake_word_config': {'session': {'retain_prebuffer_in_session': False}}})
    for i in range(PREBUFFER_FRAMES):
        r.feed(98.6 + i * .1, value=2000, process=False)
    r.reader.snap_to_head()
    r.clock.now = 100.0
    r.app._start_wake_session()
    rewind = Mock(wraps=r.reader.rewind)
    # Reader uses slots; spy on its class while retaining the actual ring logic.
    monkeypatch.setattr(type(r.reader), 'rewind', lambda self, n: rewind(n))
    caplog.set_level(logging.DEBUG, logger=wake_module.logger.name)

    r.feed(100.1, value=3000)  # Entire block is before confirmation + 150 ms.
    assert r.consumer._utterance_frames == []
    r.feed(100.2, value=4000)  # Only the last 50 ms may enter the buffer.
    first = r.consumer._utterance_frames[0]
    first_sample_time = 100.1 + (FRAME_SIZE - len(first)) / SAMPLE_RATE
    assert first_sample_time >= 100.15 - 1e-10  # Floating-point timestamp roundoff only.
    assert 799 <= len(first) <= 800
    r.feed(100.3, value=5000)
    captured = np.concatenate(r.consumer._utterance_frames)
    assert np.all(captured >= 4000 / 32767.0)
    rewind.assert_not_called()
    policy_events = [fields for name, fields in r.events if name == 'wake.capture_policy']
    assert len(policy_events) == 1
    assert policy_events[0]['policy'] == 'discard'
    assert policy_events[0]['post_wake_guard_ms'] == 150
    assert policy_events[0]['discarded_ms'] == pytest.approx(1650, abs=.1)
    logs = [record for record in caplog.records if '[WAKE-POLICY]' in record.message]
    assert len(logs) == 1
    assert logs[0].levelno == logging.DEBUG
    assert f"discarded_ms={policy_events[0]['discarded_ms']:.3f}" in logs[0].message


def test_confirmation_discards_audio_buffered_while_wake_was_decoding(rig):
    r = rig()
    r.consumer._utterance_frames = [np.full(FRAME_SIZE, .01, dtype=np.float32)]
    r.consumer._buffer_rms_history = [.01]
    r.app.is_speaking = True
    r.app._start_wake_session()
    r.feed(100.1, value=3000)
    assert r.consumer._utterance_frames == []
    r.feed(100.3, value=5000)
    assert np.all(np.concatenate(r.consumer._utterance_frames) > .1)


@pytest.mark.parametrize('lane', ['wake_keep', 'toggle_dictate'])
def test_keep_and_toggle_dictate_retain_prebuffer(rig, lane):
    config = {'wake_word_config': {'session': {'prebuffer_policy': 'keep'}}} if lane == 'wake_keep' else {}
    r = rig(config, toggle=lane == 'toggle_dictate')
    for i in range(PREBUFFER_FRAMES):
        r.feed(98.6 + i * .1, value=2000, process=False)
    r.reader.snap_to_head()
    r.clock.now = 100.0
    if lane == 'wake_keep':
        r.app._start_wake_session()
    else:
        # Even a leftover wake-confirmation marker cannot affect manual toggle.
        r.app._confirm_wake_capture()
    r.feed(100.1, value=5000)
    captured = np.concatenate(r.consumer._utterance_frames)
    assert np.any(captured == np.float32(2000 / 32767.0))
    assert np.any(captured == np.float32(5000 / 32767.0))
    expected = PREBUFFER_FRAMES if lane == 'wake_keep' else 6
    assert len(captured) == expected * FRAME_SIZE


def test_hold_capture_is_unaffected(rig):
    r = rig(hold=True)
    hold_buffer = [np.array([.25, .5], dtype=np.float32)]
    r.app.speech_buffer = hold_buffer
    r.app._confirm_wake_capture()
    r.feed(100.1)
    assert r.app.speech_buffer is hold_buffer
    np.testing.assert_array_equal(hold_buffer[0], [.25, .5])
    assert r.consumer._utterance_frames == []
    r.app._vad_is_speech.assert_not_called()


def test_max_session_closes_during_continuous_speech(rig):
    r = rig({'wake_word': {'session': {'inactivity_timeout_s': 60}}})
    r.app._start_wake_session()
    for i in range(1, 200):
        r.feed(100 + i / 10)
    assert r.app.app_state == 'wake_session'
    r.feed(120.0)
    assert r.app.app_state == 'asleep'
    assert r.consumer._utterance_frames == []
    assert any(name == 'wake.session_close' and fields['reason'] == 'max_session_s'
               for name, fields in r.events)
    # Continued monologue cannot re-arm even after consumer capture resets.
    for i in range(1, 15):
        r.feed(120 + i / 10)
    assert r.consumer._utterance_frames == []
    assert r.consumer._await_wake_onset is True
    r.feed(121.5, speech=False)
    r.feed(121.6)
    assert r.consumer._await_wake_onset is False
    assert r.app.app_state == 'asleep'  # It still needs an actual wake match.


def test_inactivity_remains_the_shorter_bound(rig):
    r = rig({'wake_word_config': {'session': {'inactivity_timeout_s': 2}}})
    r.app._start_wake_session()
    r.feed(100.2)
    expires_at = r.app._wake_session_expires_at
    r.feed(expires_at - .1)
    assert r.app.app_state == 'wake_session'
    r.feed(expires_at)
    assert r.app.app_state == 'asleep'
    assert any(fields.get('reason') == 'inactivity_timeout_s' for _, fields in r.events)


@pytest.mark.parametrize('toggle', [False, True])
def test_only_fresh_silero_onsets_extend_inactivity(rig, toggle):
    r = rig({'command_mode': {'inactivity_timeout_s': 30}}, toggle=toggle)
    if not toggle:
        r.app._start_wake_session()
    r.feed(100.2)
    if toggle:
        count = r.app._reset_command_mode_inactivity_timer.call_count
    else:
        count = len(r.timers)
    for end in (100.3, 100.4, 101.0, 102.0):
        r.feed(end)
    if toggle:
        r.app._touch_session_activity()  # Queued/decode/agent completion is not an onset.
        assert r.app._reset_command_mode_inactivity_timer.call_count == count
    else:
        r.app._restart_wake_session_timer()
        assert len(r.timers) == count
    r.feed(102.1, speech=False)
    r.feed(102.2)
    if toggle:
        assert r.app._reset_command_mode_inactivity_timer.call_count == count + 1
        r.app._reset_command_mode_inactivity_timer.assert_called_with(30)
    else:
        assert len(r.timers) == count + 1
        assert r.app._wake_session_expires_at == pytest.approx(112.2)


def test_rms_fallback_does_not_count_as_silero_onset(rig):
    r = rig(toggle=True)
    r.app._vad_available = False
    r.feed(100.2)
    r.feed(100.3, value=0)
    r.feed(100.4)
    r.app._reset_command_mode_inactivity_timer.assert_not_called()


def test_onsets_cannot_move_the_absolute_deadline(rig):
    r = rig()
    r.app._start_wake_session()
    for end in (100.2, 105.0, 110.0, 115.0, 119.0):
        r.feed(end - .1, speech=False)
        r.feed(end)
    assert r.app._wake_session_deadline == 120.0
    assert r.app._wake_session_expires_at == 120.0
    assert r.timers[-1].delay == 1.0
    r.clock.now = 120.0
    r.timers[-1].callback()  # Cap also works without another frame arriving.
    assert r.app.app_state == 'asleep'


def test_stale_timer_cannot_close_a_new_session(rig):
    r = rig()
    r.app._start_wake_session()
    old_timer = r.timers[-1]
    r.app._end_wake_session()
    r.clock.now = 110.0
    r.app._start_wake_session()
    r.clock.now = 121.0
    old_timer.callback()
    assert r.app.app_state == 'wake_session'


def test_queued_capture_cannot_dispatch_after_session_cap(rig):
    r = rig()
    r.app._start_wake_session()
    dispatch = WakeConsumer._wrap_with_duck_close(
        r.app, r.app.process_wake_word_buffer, 1, kind='wake_buffer')
    r.clock.now = 121.0
    dispatch([np.zeros(FRAME_SIZE, dtype=np.float32)])
    r.app.process_wake_word_buffer.assert_not_called()
    r.app._close_hands_free_capture_duck.assert_called_with(1)


def test_decode_finishing_after_session_cap_cannot_deliver(rig, monkeypatch):
    r = rig()
    r.app._start_wake_session()
    r.app.model_rate = SAMPLE_RATE
    r.app.model_lock = threading.Lock()
    r.app._wake_audio_is_below_gate = Mock(return_value=False)
    r.app.get_transcription_params = Mock(return_value={})
    r.app._log_history = Mock()

    def transcribe(*args, **kwargs):
        r.clock.now = 121.0
        return [SimpleNamespace(text='old television dialogue')], SimpleNamespace()

    r.app.model = SimpleNamespace(transcribe=transcribe)
    monkeypatch.setattr(dictation, 'resample_audio', lambda audio, *args: audio)
    dictation.DictationApp.process_wake_word_buffer(
        r.app, [np.full(FRAME_SIZE, .2, dtype=np.float32)], src_rate=SAMPLE_RATE)
    r.drain()
    assert r.app.app_state == 'asleep'
    r.app._output_dictation.assert_not_called()
    r.app._log_history.assert_not_called()  # No swallowed pipeline exception.


def test_flush_drops_capture_when_timer_closed_session_mid_frame(rig, monkeypatch):
    r = rig()
    r.app._start_wake_session()
    r.app._end_wake_session()
    spawn = Mock()
    monkeypatch.setattr(wake_module.thread_registry, 'spawn', spawn)
    r.consumer._flush([np.zeros(FRAME_SIZE, dtype=np.float32)], owner_token=1)
    spawn.assert_not_called()
    r.app._close_hands_free_capture_duck.assert_called_with(1)


def test_fallback_warning_once_per_app_start(rig, monkeypatch, caplog):
    monkeypatch.setattr(dictation, 'WakeWordDetector',
                        lambda *args, **kwargs: SimpleNamespace(is_available=False))
    caplog.set_level(logging.WARNING, logger=dictation.logger.name)
    r = rig({'wake_word_config': {'phrase': 'jarvis'}})
    r.app._load_oww_model()
    r.app._load_oww_model()
    r.app.start_wake_word_mode()
    r.app.start_wake_word_mode()
    warnings = [record for record in caplog.records if 'decodes all room audio' in record.message]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert "'jarvis'" in warnings[0].message
    assert [(name, fields) for name, fields in r.events if name == 'wake.fallback_active'] == [
        ('wake.fallback_active', {'phrase': 'jarvis', 'detector': 'whisper'})]
    fresh = rig({'wake_word_config': {'phrase': 'jarvis'}})
    fresh.app.start_wake_word_mode()
    assert len([record for record in caplog.records if 'decodes all room audio' in record.message]) == 2


def test_loaded_oww_has_no_fallback_warning(rig, caplog):
    r = rig()
    r.app._wake_detector = SimpleNamespace(is_available=True)
    caplog.set_level(logging.WARNING, logger=dictation.logger.name)
    r.app.start_wake_word_mode()
    assert not any('decodes all room audio' in record.message for record in caplog.records)
    assert not any(name == 'wake.fallback_active' for name, _ in r.events)
