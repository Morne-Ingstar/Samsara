"""Tests for the WakeConsumer pipeline's reason-counted lifecycle (SPARK
2026-07-18 fix): a toggle/hands-free session was silently deaf whenever
wake_word_enabled was False, because entering the session assumed the
WakeConsumer pipeline was already running -- true only as a side effect of
wake-word mode's own boot-time start_wake_word_mode() call
(dictation.py:~4407). See _ensure_wake_consumer/_release_wake_consumer's own
docstrings (dictation.py, just above start_wake_word_mode) for the full
mechanism.

Exercises the REAL bound DictationApp methods
(_ensure_wake_consumer/_release_wake_consumer, and enter_command_mode/
exit_command_mode's toggle branch) against a minimal duck-typed `self` --
same "call the actual production code path, not a hand-copied
reimplementation that could drift" philosophy as
tests/test_transcription_params.py's module docstring. A hand-copied
_MockApp-style reimplementation (as tests/test_command_mode.py's older
tests use for unrelated state-machine assertions) would not have caught
this exact bug, since the bug was in wiring that such a reimplementation
would simply not reproduce.
"""
import ast
import logging
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
import numpy as np

from samsara.audio_engine import continuous_consumer as continuous_module
from samsara.audio_engine import dictation_consumer as hold_module
from samsara.audio_engine import wake_consumer as wake_module
from samsara.audio_engine.frame import FRAME_SIZE
from samsara.audio_engine.ring import EMPTY, FrameBus
from samsara.session_modes import SessionMode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def load_app_policy_methods(names):
    """Compile named methods only: never execute dictation.py's import/startup code."""
    path = Path(__file__).resolve().parents[1] / 'dictation.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DictationApp')
    methods = [node for node in app.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in methods} == set(names)
    module = types.ModuleType('app_policy_test_double')
    module.__dict__.update(
        time=time, logger=logging.getLogger('app_policy_test_double'), SessionMode=SessionMode,
        thread_registry=types.SimpleNamespace(spawn=Mock(), timer=Mock()),
        wake_session_policy=wake_module.wake_session_policy,
        flight_recorder=wake_module.flight_recorder, np=np,
        WakeWordDetector=Mock(), resample_audio=lambda audio, *args: audio,
    )
    constants = [node for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id in {
                     '_WAKE_SESSION_CHUNK_GAP_S', '_WAKE_SESSION_SEND_WORDS',
                 } for target in node.targets)]
    exec(compile(ast.Module(body=constants + methods, type_ignores=[]), str(path), 'exec'), module.__dict__)
    module.DictationApp = type('AppPolicy', (), {node.name: module.__dict__[node.name] for node in methods})
    return module


dictation = load_app_policy_methods({
    '_ensure_wake_consumer', '_release_wake_consumer', 'enter_command_mode', 'exit_command_mode',
})


class FakeWakeConsumer:
    """Stand-in for samsara.audio_engine.wake_consumer.WakeConsumer -- only
    the surface _ensure_wake_consumer/_release_wake_consumer actually touch
    (._running, .start(), .stop())."""

    def __init__(self):
        self._running = False
        self.start_calls = 0
        self.stop_calls = 0
        self.stop_return = []

    def start(self):
        self.start_calls += 1
        self._running = True

    def stop(self):
        self.stop_calls += 1
        self._running = False
        return self.stop_return


def _make_app(wake_consumer_running=False):
    app = types.SimpleNamespace()
    app._wake_consumer = FakeWakeConsumer()
    app._wake_consumer._running = wake_consumer_running
    app._wake_consumer_reasons = set()
    app._wake_consumer_lock = threading.Lock()
    app.wake_word_triggered = False
    app.process_wake_word_buffer = Mock()
    app._ensure_wake_consumer = types.MethodType(dictation.DictationApp._ensure_wake_consumer, app)
    app._release_wake_consumer = types.MethodType(dictation.DictationApp._release_wake_consumer, app)
    return app


# ============================================================================
# _ensure_wake_consumer / _release_wake_consumer: the reason-counted core.
# ============================================================================

class TestEnsureWakeConsumer:
    def test_starts_a_stopped_consumer(self):
        app = _make_app(wake_consumer_running=False)
        app._ensure_wake_consumer('toggle_session')
        assert app._wake_consumer._running is True
        assert app._wake_consumer.start_calls == 1
        assert 'toggle_session' in app._wake_consumer_reasons

    def test_no_op_when_already_running(self):
        app = _make_app(wake_consumer_running=True)
        app._ensure_wake_consumer('toggle_session')
        assert app._wake_consumer.start_calls == 0  # never called -- already running
        assert 'toggle_session' in app._wake_consumer_reasons

    def test_two_reasons_both_recorded_second_does_not_restart(self):
        app = _make_app(wake_consumer_running=False)
        app._ensure_wake_consumer('wake_word')
        app._ensure_wake_consumer('toggle_session')
        assert app._wake_consumer.start_calls == 1
        assert app._wake_consumer_reasons == {'wake_word', 'toggle_session'}

    def test_none_consumer_is_safe(self):
        app = _make_app()
        app._wake_consumer = None
        app._ensure_wake_consumer('toggle_session')  # must not raise
        assert 'toggle_session' in app._wake_consumer_reasons


class TestReleaseWakeConsumer:
    def test_releasing_the_only_reason_stops_it(self):
        app = _make_app(wake_consumer_running=True)
        app._wake_consumer_reasons = {'toggle_session'}
        app._release_wake_consumer('toggle_session')
        assert app._wake_consumer._running is False
        assert app._wake_consumer.stop_calls == 1
        assert app._wake_consumer_reasons == set()

    def test_the_actual_bug_scenario_wake_off_toggle_on_toggle_off(self):
        """The exact reported bug, end to end at this layer: wake_word_enabled
        is False (consumer starts stopped, no 'wake_word' reason ever held),
        toggle session ensures it, then releases it on exit. Consumer must
        have been running for the session and be stopped again after."""
        app = _make_app(wake_consumer_running=False)
        app._ensure_wake_consumer('toggle_session')
        assert app._wake_consumer._running is True, "toggle session must not be deaf"
        app._release_wake_consumer('toggle_session')
        assert app._wake_consumer._running is False, "must not leave an orphaned pipeline"

    def test_other_reason_still_held_does_not_stop(self):
        """wake_word_enabled=True case: releasing the toggle session's
        reason while wake detection still holds its own must leave the
        consumer running."""
        app = _make_app(wake_consumer_running=True)
        app._wake_consumer_reasons = {'wake_word', 'toggle_session'}
        app._release_wake_consumer('toggle_session')
        assert app._wake_consumer._running is True
        assert app._wake_consumer.stop_calls == 0
        assert app._wake_consumer_reasons == {'wake_word'}

    def test_releasing_a_never_held_reason_is_a_safe_no_op(self):
        app = _make_app(wake_consumer_running=True)
        app._wake_consumer_reasons = {'wake_word'}
        app._release_wake_consumer('toggle_session')  # never held
        assert app._wake_consumer._running is True
        assert app._wake_consumer.stop_calls == 0

    def test_double_release_is_safe(self):
        app = _make_app(wake_consumer_running=True)
        app._wake_consumer_reasons = {'toggle_session'}
        app._release_wake_consumer('toggle_session')
        app._release_wake_consumer('toggle_session')  # must not raise or double-stop
        assert app._wake_consumer.stop_calls == 1

    def test_none_consumer_is_safe(self):
        app = _make_app()
        app._wake_consumer = None
        app._wake_consumer_reasons = {'toggle_session'}
        app._release_wake_consumer('toggle_session')  # must not raise

    def test_flushes_remaining_frames_when_wake_word_was_triggered(self):
        app = _make_app(wake_consumer_running=True)
        app._wake_consumer_reasons = {'wake_word'}
        app._wake_consumer.stop_return = ['frame1', 'frame2']
        app.wake_word_triggered = True
        app._release_wake_consumer('wake_word')
        app.process_wake_word_buffer.assert_called_once_with(['frame1', 'frame2'], src_rate=16000)

    def test_does_not_flush_when_wake_word_was_not_triggered(self):
        app = _make_app(wake_consumer_running=True)
        app._wake_consumer_reasons = {'wake_word'}
        app._wake_consumer.stop_return = ['frame1']
        app.wake_word_triggered = False
        app._release_wake_consumer('wake_word')
        app.process_wake_word_buffer.assert_not_called()

    def test_does_not_flush_when_a_reason_still_holds_it_open(self):
        """No stop() call happened at all (another reason survives), so
        there is no 'remaining' to flush regardless of wake_word_triggered."""
        app = _make_app(wake_consumer_running=True)
        app._wake_consumer_reasons = {'wake_word', 'toggle_session'}
        app.wake_word_triggered = True
        app._release_wake_consumer('toggle_session')
        assert app._wake_consumer.stop_calls == 0
        app.process_wake_word_buffer.assert_not_called()


# ============================================================================
# enter_command_mode / exit_command_mode toggle branch: the actual call
# sites, exercised end to end through the real bound methods.
# ============================================================================

def _make_toggle_session_app(monkeypatch, wake_consumer_running=False, existing_reasons=None):
    app = _make_app(wake_consumer_running=wake_consumer_running)
    if existing_reasons:
        app._wake_consumer_reasons = set(existing_reasons)

    app.command_mode_active = False
    app.ava_mode_active = False
    # enter_command_mode() now exits an active Ava command session first
    # (2026-07-19 incident fix) -- always False here, so that branch is a
    # no-op, but the attribute must exist for the check itself.
    app.ava_command_session_active = False
    app._command_mode_lock = threading.Lock()
    app._command_mode_miss_count = 0
    app._command_mode_session_start = 0.0
    app._command_mode_ghost_tap = False
    app.recording = False
    app._session_mode_manager = None
    app.config = {
        'command_mode': {
            'mode': 'toggle',
            'inactivity_timeout_s': 300,
            'enter_debounce_ms': 0,
            'exit_earcon': False,
        },
    }
    app._reset_command_mode_inactivity_timer = lambda timeout_s: None
    app._cancel_command_mode_inactivity_timer = lambda: None
    app._ensure_session_mode_manager = lambda: types.SimpleNamespace(reset=lambda **kw: None)
    app._update_mode_overlay = lambda mode: None
    # SPARK streaming-preview feature (2026-07-18): enter/exit_command_mode's
    # toggle branch now also drives the DICTATE-lane preview overlay
    # lifecycle -- irrelevant to this file's WakeConsumer-wiring assertions,
    # no-op it the same way _update_mode_overlay already is.
    app._update_streaming_preview = lambda mode: None
    app._release_streaming_preview = lambda: None
    app.play_sound = lambda *a, **k: None
    app.stop_recording = lambda: None
    # Real enter_command_mode spawns _do_enter_command_mode on a worker
    # thread purely for the debounced earcon -- irrelevant to the
    # synchronous _ensure_wake_consumer call under test here, and a real
    # background thread would just add nondeterminism. No-op it (the
    # attribute reference is still evaluated as thread_registry.spawn's
    # argument even though the mocked spawn never calls it).
    app._do_enter_command_mode = lambda: None
    monkeypatch.setattr(dictation.thread_registry, 'spawn', lambda *a, **k: None)

    app.enter_command_mode = types.MethodType(dictation.DictationApp.enter_command_mode, app)
    app.exit_command_mode = types.MethodType(dictation.DictationApp.exit_command_mode, app)
    return app


class TestToggleSessionWakeConsumerWiring:
    def test_entering_toggle_with_wake_word_off_starts_the_consumer(self, monkeypatch):
        app = _make_toggle_session_app(monkeypatch, wake_consumer_running=False)
        app.enter_command_mode()
        assert app._wake_consumer._running is True
        assert 'toggle_session' in app._wake_consumer_reasons

    def test_exiting_stops_the_consumer_when_wake_word_was_off(self, monkeypatch):
        app = _make_toggle_session_app(monkeypatch, wake_consumer_running=False)
        app.enter_command_mode()
        app.exit_command_mode()
        assert app._wake_consumer._running is False, \
            "must not leave an orphaned pipeline running after the session ends"

    def test_entering_with_wake_word_already_on_leaves_it_running_on_exit(self, monkeypatch):
        """The other half of the acceptance criteria: wake_word_enabled=True
        means the pipeline was already running for the 'wake_word' reason
        before the toggle session ever starts. Exiting the toggle session
        must NOT stop wake detection's own consumer."""
        app = _make_toggle_session_app(
            monkeypatch, wake_consumer_running=True, existing_reasons={'wake_word'},
        )
        app.enter_command_mode()
        assert app._wake_consumer._running is True
        app.exit_command_mode()
        assert app._wake_consumer._running is True, \
            "toggle session exit must not stop wake detection's own consumer"
        assert app._wake_consumer_reasons == {'wake_word'}

    def test_double_enter_is_safe(self, monkeypatch):
        """command_mode_active's own idempotency guard makes the second
        enter_command_mode() call a full no-op -- locking in that this
        doesn't somehow double-register the reason or double-start."""
        app = _make_toggle_session_app(monkeypatch, wake_consumer_running=False)
        app.enter_command_mode()
        app.enter_command_mode()
        assert app._wake_consumer.start_calls == 1
        assert app._wake_consumer_reasons == {'toggle_session'}

    def test_double_exit_is_safe(self, monkeypatch):
        app = _make_toggle_session_app(monkeypatch, wake_consumer_running=False)
        app.enter_command_mode()
        app.exit_command_mode()
        app.exit_command_mode()  # must not raise or double-stop
        assert app._wake_consumer.stop_calls == 1

    def test_rapid_toggle_on_off_on_off_never_leaves_it_stuck_running(self, monkeypatch):
        app = _make_toggle_session_app(monkeypatch, wake_consumer_running=False)
        for _ in range(3):
            app.enter_command_mode()
            assert app._wake_consumer._running is True
            app.exit_command_mode()
            assert app._wake_consumer._running is False
        assert app._wake_consumer_reasons == set()


@pytest.fixture
def consumer_rig(monkeypatch):
    """Real ring and consumers, with a fully fake app and no audio engine."""
    monkeypatch.setattr(wake_module.flight_recorder, 'record', Mock())

    def make(kind='wake', on_fatal=None):
        app = types.SimpleNamespace(
            config={}, app_state='asleep', _hotkey_recording=False,
            command_mode_active=False, ava_command_session_active=False,
            wake_word_active=True, wake_word_triggered=False, continuous_active=True,
            _oww_wake_detected=False, _wake_detector=None, _tts_last_speaking=-100.0,
            _command_executed_at=None, audio_coordinator=None,
            echo_canceller=types.SimpleNamespace(is_active=False),
            is_speaking=False, silence_start=None, _vad_available=False,
            _vad_is_speech=Mock(return_value=True), _vad_reset=Mock(),
            _dictation_silence_timeout=None, _session_mode_manager=None,
            _wake_rearm_needs_onset=False, _wake_consumer_reasons={'wake_word'},
            _open_hands_free_capture_duck=Mock(return_value=41),
            _close_hands_free_capture_duck=Mock(), play_sound=Mock(),
            _expire_wake_session=Mock(), _end_wake_session=Mock(),
            exit_command_mode=Mock(), exit_ava_command_session=Mock(),
            set_app_state=Mock(),
        )
        bus = FrameBus()
        reader = bus.new_reader()
        engine = types.SimpleNamespace(register_consumer=lambda name: reader,
                                       unregister_consumer=Mock())
        cls = {'wake': wake_module.WakeConsumer, 'continuous': continuous_module.ContinuousConsumer,
               'hold': hold_module.DictationSessionConsumer}[kind]
        consumer = cls(engine, app) if kind == 'hold' else cls(engine, app, on_fatal=on_fatal)

        def feed(value, epoch=0):
            bus.write(np.full(FRAME_SIZE, value, dtype=np.int16),
                      t_capture=time.monotonic(), device_epoch=epoch)
            consumer._process_frame(reader.read_next())

        return types.SimpleNamespace(app=app, consumer=consumer, reader=reader,
                                     engine=engine, bus=bus, feed=feed)
    return make


@pytest.fixture
def poll_threads(monkeypatch):
    """Use actual threads with deterministic Events, bounded cleanup and short joins."""
    threads = []
    releases = []
    stops = []

    def spawn(name, target, args=(), kwargs=None, daemon=True):
        stops.extend(arg for arg in args if isinstance(arg, threading.Event))
        thread = threading.Thread(name=name, target=target, args=args, kwargs=kwargs or {}, daemon=True)
        threads.append(thread)
        thread.start()
        return thread

    monkeypatch.setattr(wake_module.thread_registry, 'spawn', spawn)
    for module in (wake_module, continuous_module, hold_module):
        monkeypatch.setattr(module, '_THREAD_JOIN_TIMEOUT_S', 0.02)
    yield types.SimpleNamespace(threads=threads, releases=releases)
    for event in stops + releases:
        event.set()
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive(), thread.name


def test_rms_quiet_to_speech_rearms_once_without_silero(consumer_rig, caplog):
    r = consumer_rig()
    r.consumer._await_wake_onset = True
    r.feed(12000)  # No observed quiet run: an existing monologue cannot rearm.
    assert r.consumer._await_wake_onset is True
    r.feed(0)
    r.feed(12000)
    assert r.consumer._await_wake_onset is False
    r.app._vad_is_speech.assert_not_called()
    r.consumer._await_wake_onset = True
    r.feed(12000)
    assert r.consumer._await_wake_onset is True
    r.feed(0)
    r.feed(12000)
    assert r.consumer._await_wake_onset is False
    warnings = [record for record in caplog.records if 'rearmed on RMS onset' in record.message]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING


def test_vad_disabling_after_errors_can_rearm_on_rms(consumer_rig):
    r = consumer_rig()
    r.consumer._await_wake_onset = True
    r.app._vad_available = True
    r.app._vad_consec_errors = 49
    r.app._vad_is_speech.side_effect = RuntimeError('VAD unavailable')
    r.feed(0)
    assert r.app._vad_available is False
    r.feed(12000)
    assert r.consumer._await_wake_onset is False


def test_start_resets_onset_and_admission_state(consumer_rig, poll_threads, monkeypatch):
    r = consumer_rig()
    r.consumer._await_wake_onset = True
    r.consumer._silero_was_speech = True
    r.consumer._rms_was_speech = True
    r.consumer._wake_admission = (1.0, 'discard', 150)
    r.app._wake_rearm_needs_onset = True
    monkeypatch.setattr(r.consumer, '_poll_loop', lambda stop: stop.wait(1))
    assert r.consumer.start() is True
    assert r.consumer._await_wake_onset is False
    assert r.consumer._silero_was_speech is False
    assert r.consumer._rms_was_speech is None
    assert r.consumer._wake_admission is None
    assert r.app._wake_rearm_needs_onset is False
    assert r.consumer.stop().stopped is True


@pytest.mark.parametrize('kind', ['wake', 'continuous'])
def test_stop_timeout_retains_thread_and_refuses_restart(
    kind, consumer_rig, poll_threads, monkeypatch, caplog,
):
    r = consumer_rig(kind)
    entered, release = threading.Event(), threading.Event()
    poll_threads.releases.append(release)

    def blocked_poll(stop):
        entered.set()
        release.wait(1)
        stop.wait(1)

    monkeypatch.setattr(r.consumer, '_poll_loop', blocked_poll)
    assert r.consumer.start() is True
    assert entered.wait(1)
    original = r.consumer._thread
    original_stop = r.consumer._stop_event
    result = r.consumer.stop()
    assert result.stopped is False if kind == 'wake' else result == []
    assert original.is_alive()
    assert r.consumer._thread is original
    assert r.consumer.start() is False
    assert r.consumer._running is False
    assert r.consumer._stop_event is original_stop and original_stop.is_set()
    assert len(poll_threads.threads) == 1
    assert any(original.name in record.message and record.levelno == logging.ERROR
               for record in caplog.records)
    release.set()
    original.join(1)
    assert not original.is_alive()
    assert r.consumer.start() is True
    assert len(poll_threads.threads) == 2
    r.consumer.stop()


@pytest.mark.parametrize('stop_method,next_start', [
    ('drain', 'activate'), ('cancel', 'activate'), ('drain', 'activate_streaming'),
    ('stop_streaming', 'activate_streaming'), ('stop_streaming', 'activate'),
])
def test_timed_out_drain_cannot_be_resurrected(
    stop_method, next_start, consumer_rig, poll_threads, monkeypatch,
):
    r = consumer_rig('hold')
    entered, release = threading.Event(), threading.Event()
    poll_threads.releases.append(release)
    calls = []

    def blocked_read():
        calls.append(threading.current_thread().name)
        entered.set()
        release.wait(1)
        return EMPTY

    r.consumer._reader = types.SimpleNamespace(read_next=blocked_read, snap_to_head=Mock(), rewind=Mock())
    streaming = stop_method == 'stop_streaming'
    assert getattr(r.consumer, 'activate_streaming' if streaming else 'activate')() is True
    assert entered.wait(1)
    thread_attr = '_streaming_thread' if streaming else '_drain_thread'
    stop_attr = '_streaming_stop' if streaming else '_drain_stop'
    original = getattr(r.consumer, thread_attr)
    original_stop = getattr(r.consumer, stop_attr)
    assert not getattr(r.consumer, stop_method)()
    assert original.is_alive()
    assert getattr(r.consumer, thread_attr) is original
    assert getattr(r.consumer, next_start)() is False
    assert getattr(r.consumer, stop_attr) is original_stop and original_stop.is_set()
    assert len(poll_threads.threads) == 1
    assert len(calls) == 1  # drain() did not attempt a competing synchronous read.
    assert r.consumer._reader.snap_to_head.call_count == 1
    # Prove the loop is bound to its own stop event, independent of the guard.
    setattr(r.consumer, stop_attr, threading.Event())
    release.set()
    original.join(1)
    assert not original.is_alive()
    assert len(calls) == 1
    assert getattr(r.consumer, next_start)() is True
    r.consumer.cancel()
    r.consumer.stop_streaming()


@pytest.mark.parametrize('kind', ['wake', 'continuous'])
@pytest.mark.parametrize('cleanup_raises', [False, True])
def test_frame_failure_stops_once_and_clears_latches(
    kind, cleanup_raises, consumer_rig, poll_threads, monkeypatch, caplog,
):
    fatal = Mock()
    r = consumer_rig(kind, on_fatal=fatal)
    r.app.app_state = 'wake_session'
    r.app.wake_word_triggered = True
    r.app.command_mode_active = True
    r.app.ava_command_session_active = True
    r.app.config = {'command_mode': None}  # Cleanup must not re-read broken policy.
    r.consumer._hands_free_capture_duck_token = 41
    if cleanup_raises:
        for method in ('exit_command_mode', 'exit_ava_command_session', '_end_wake_session',
                       'set_app_state', '_close_hands_free_capture_duck'):
            getattr(r.app, method).side_effect = RuntimeError('cleanup failed')
    read = Mock(side_effect=[object(), object()])
    r.consumer._reader = types.SimpleNamespace(read_next=read, snap_to_head=Mock())
    failure = RuntimeError('repeatable frame failure')
    process = Mock(side_effect=failure)
    monkeypatch.setattr(r.consumer, '_process_frame', process)
    assert r.consumer.start() is True
    r.consumer._thread.join(1)
    assert not r.consumer._thread.is_alive()
    assert r.consumer._running is False
    assert r.consumer._stop_event.is_set()
    process.assert_called_once()
    read.assert_called_once()
    fatal.assert_called_once_with(failure)
    r.consumer._handle_fatal(failure)
    fatal.assert_called_once()
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1 and errors[0].exc_info
    r.app._close_hands_free_capture_duck.assert_called_once_with(41)
    assert r.consumer._hands_free_capture_duck_token is None
    if kind == 'wake':
        assert not r.app.wake_word_active and not r.app.wake_word_triggered
        assert not r.app.command_mode_active and not r.app.ava_command_session_active
        assert r.app.app_state == 'asleep'
        assert r.consumer._utterance_frames == []
        assert r.consumer._wake_admission is None
        assert not r.consumer._await_wake_onset
        assert r.app._wake_consumer_reasons == set()
    else:
        assert r.app.continuous_active is False
        assert r.consumer._speech_frames == []
        assert not r.consumer._is_speaking and r.consumer._silence_start is None


@pytest.mark.parametrize('kind', ['wake', 'continuous'])
def test_default_fatal_notification_plays_error_once(kind, consumer_rig, poll_threads, monkeypatch):
    r = consumer_rig(kind)
    r.consumer._reader = types.SimpleNamespace(read_next=Mock(return_value=object()), snap_to_head=Mock())
    monkeypatch.setattr(r.consumer, '_process_frame', Mock(side_effect=RuntimeError('fatal')))
    r.consumer.start()
    r.consumer._thread.join(1)
    r.app.play_sound.assert_called_once_with('error')


def test_repeating_frame_logs_are_limited_to_five_seconds(consumer_rig, monkeypatch, caplog):
    r = consumer_rig()
    r.app._vad_reset.side_effect = RuntimeError('reset failed')
    caplog.set_level(logging.DEBUG, logger=wake_module.logger.name)
    clock = types.SimpleNamespace(now=100.0)
    monkeypatch.setattr(wake_module.time, 'monotonic', lambda: clock.now)
    for epoch in range(52):
        clock.now = 100.0 + epoch / 10
        r.feed(0, epoch=epoch)
    epoch_logs = [record for record in caplog.records if 'epoch change' in record.message]
    warnings = [record for record in epoch_logs if record.levelno == logging.WARNING]
    debug = [record for record in epoch_logs if record.levelno == logging.DEBUG]
    assert len(warnings) == 2
    assert len(debug) == 2


def test_no_live_app_module_imported(tmp_path):
    """This file's whole point (see module docstring and
    load_app_policy_methods above) is exercising real DictationApp policy
    methods via AST extraction WITHOUT ever importing the actual
    dictation.py module -- that module's import-time side effects are
    exactly what a lightweight consumer-lifecycle test must avoid.

    Checking sys.modules in-process is meaningless run as part of the full
    suite: pytest imports every test module's own module-level imports
    during collection, before any test executes, and numerous OTHER test
    files still do `import dictation` at module level (a pre-existing,
    known condition -- see conftest.py's hermetic-collection comment). A
    subprocess that imports only THIS file's own dependencies is the only
    way to test the actual invariant: that they don't transitively pull in
    dictation.py."""
    env = dict(os.environ, SAMSARA_HOME_DIR=str(tmp_path))
    result = subprocess.run(
        [sys.executable, '-c', (
            "import sys\n"
            "import samsara.audio_engine.continuous_consumer\n"
            "import samsara.audio_engine.dictation_consumer\n"
            "import samsara.audio_engine.wake_consumer\n"
            "import samsara.session_modes\n"
            "sys.exit(1 if 'dictation' in sys.modules else 0)\n"
        )],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        "importing this file's own dependencies pulled in dictation.py "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )


def test_stop_reports_status_and_preserves_existing_frame_flush_contract(consumer_rig):
    r = consumer_rig()
    frames = [np.full(FRAME_SIZE, .1, dtype=np.float32)]
    r.consumer._utterance_frames = frames
    r.app.wake_word_triggered = True
    result = r.consumer.stop()
    assert result.stopped is True
    assert isinstance(result, list) and len(result) == 1
    assert result[0] is frames[0]
    second = r.consumer.stop()
    assert second.stopped is True and second == []
    assert r.app.wake_word_triggered is True  # The app still decides whether to flush.


@pytest.mark.parametrize('kind', ['wake', 'continuous'])
def test_fatal_callback_can_request_stop_without_self_join(
    kind, consumer_rig, poll_threads, monkeypatch, caplog,
):
    stopped = []
    r = consumer_rig(kind, on_fatal=lambda exc: stopped.append(r.consumer.stop()))
    r.consumer._reader = types.SimpleNamespace(read_next=Mock(return_value=object()), snap_to_head=Mock())
    monkeypatch.setattr(r.consumer, '_process_frame', Mock(side_effect=RuntimeError('fatal')))
    r.consumer.start()
    r.consumer._thread.join(1)
    assert not r.consumer._thread.is_alive()
    assert len(stopped) == 1
    assert len([record for record in caplog.records if record.levelno == logging.ERROR]) == 1


def test_real_app_release_accepts_wake_stop_result(consumer_rig):
    r = consumer_rig()
    app = _make_app(wake_consumer_running=True)
    app._wake_consumer = r.consumer
    app._wake_consumer_reasons = {'wake_word'}
    app.wake_word_triggered = True
    r.consumer._running = True
    frame = np.full(FRAME_SIZE, .1, dtype=np.float32)
    r.consumer._utterance_frames = [frame]
    app._release_wake_consumer('wake_word')
    app.process_wake_word_buffer.assert_called_once()
    result = app.process_wake_word_buffer.call_args.args[0]
    assert result.stopped is True
    assert result[0] is frame


@pytest.mark.parametrize('streaming', [False, True])
def test_successful_drain_keeps_frame_already_being_read(
    streaming, consumer_rig, poll_threads, monkeypatch,
):
    r = consumer_rig('hold')
    entered, release = threading.Event(), threading.Event()
    poll_threads.releases.append(release)
    monkeypatch.setattr(hold_module, '_THREAD_JOIN_TIMEOUT_S', 0.5)
    frame = types.SimpleNamespace(seq=1, device_epoch=0, pcm=np.full(FRAME_SIZE, 8000, dtype=np.int16))

    def read_one():
        if entered.is_set():
            return EMPTY
        entered.set()
        release.wait(1)
        return frame

    r.consumer._reader = types.SimpleNamespace(read_next=read_one, snap_to_head=Mock(), rewind=Mock())
    getattr(r.consumer, 'activate_streaming' if streaming else 'activate')()
    assert entered.wait(1)
    results = []
    finish = threading.Thread(target=lambda: results.append(
        getattr(r.consumer, 'stop_streaming' if streaming else 'drain')()), daemon=True)
    finish.start()
    try:
        stop = r.consumer._streaming_stop if streaming else r.consumer._drain_stop
        assert stop.wait(1)
        release.set()
        finish.join(1)
        assert not finish.is_alive()
        np.testing.assert_allclose(results[0], frame.pcm.astype(np.float32) / 32767.0)
    finally:
        release.set()
        finish.join(1)
