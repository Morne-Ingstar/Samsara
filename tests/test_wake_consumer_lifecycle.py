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
import sys
import threading
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation


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
