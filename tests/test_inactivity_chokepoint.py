"""Tests for the unified session's single activity chokepoint
(DictationApp._touch_session_activity) and idle-robustness hardening:

- Every lane's dispatch outcome (except a discarded "empty" near-silence
  buffer) resets the SAME inactivity timer through one method, replacing
  the old scattered per-lane resets that let DICTATE/AVA starve the timer.
- threading.Timer hygiene: every reset cancels the prior timer -- no leak.
- Zombie-proofing: an exception inside the timeout callback still ends the
  session; an exception inside per-utterance handling still earcons and
  leaves the session alive in its current mode.
- WakeConsumer._poll_loop fails LOUD (earcon + forced session end) if its
  own loop body raises outside the per-frame guard, instead of silently
  going deaf while the session stays latched.

Real dictation.py / wake_consumer.py methods are bound onto lightweight
stub objects (not reimplementations), matching the existing pattern in
tests/test_session_badge.py and tests/test_ava_substance_gate.py.
"""
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# _touch_session_activity -- the single chokepoint
# ---------------------------------------------------------------------------

def _make_touch_stub(mode='toggle', active=True):
    import dictation as _d

    class _Stub:
        _touch_session_activity = _d.DictationApp._touch_session_activity

        def __init__(self):
            self.command_mode_active = active
            self._session_recovery_pause = False
            self.config = {'command_mode': {'mode': mode, 'inactivity_timeout_s': 30}}
            self.reset_calls = []

        def _reset_command_mode_inactivity_timer(self, timeout_s):
            self.reset_calls.append(timeout_s)

    return _Stub()


class TestTouchSessionActivity:
    def test_resets_timer_when_active_and_toggle(self):
        stub = _make_touch_stub(mode='toggle', active=True)
        stub._touch_session_activity()
        assert stub.reset_calls == [30]

    def test_noop_when_command_mode_inactive(self):
        stub = _make_touch_stub(mode='toggle', active=False)
        stub._touch_session_activity()
        assert stub.reset_calls == []

    def test_noop_when_hold_mode(self):
        stub = _make_touch_stub(mode='hold', active=True)
        stub._touch_session_activity()
        assert stub.reset_calls == []


# ---------------------------------------------------------------------------
# AVA agent-response completion (on_done) also touches activity
# ---------------------------------------------------------------------------

def _make_ava_done_stub():
    import collections
    import dictation as _d

    class _Stub:
        _on_ava_session_request_done = _d.DictationApp._on_ava_session_request_done

        def __init__(self):
            self._ava_session_dispatch_lock = threading.Lock()
            self._ava_session_dispatch_queue = collections.deque(maxlen=3)
            self._ava_session_request_in_flight = True
            self.touch_calls = 0
            self.started_with = []

        def _touch_session_activity(self):
            self.touch_calls += 1

        def _start_ava_session_worker(self, text):
            self.started_with.append(text)

    return _Stub()


class TestAvaOnDoneTouchesActivity:
    def test_on_done_touches_activity_before_draining_queue(self):
        stub = _make_ava_done_stub()
        stub._on_ava_session_request_done()
        assert stub.touch_calls == 1
        assert stub._ava_session_request_in_flight is False

    def test_on_done_touches_activity_even_when_draining_next(self):
        stub = _make_ava_done_stub()
        stub._ava_session_dispatch_queue.append("next utterance")
        stub._on_ava_session_request_done()
        assert stub.touch_calls == 1
        assert stub.started_with == ["next utterance"]


# ---------------------------------------------------------------------------
# threading.Timer hygiene -- real Timer objects, no mocking of threading
# ---------------------------------------------------------------------------

def _make_timer_stub():
    import dictation as _d

    class _Stub:
        _reset_command_mode_inactivity_timer = _d.DictationApp._reset_command_mode_inactivity_timer
        _cancel_command_mode_inactivity_timer = _d.DictationApp._cancel_command_mode_inactivity_timer
        _cancel_command_mode_inactivity_timer_locked = _d.DictationApp._cancel_command_mode_inactivity_timer_locked
        _on_command_mode_inactivity = _d.DictationApp._on_command_mode_inactivity

        def __init__(self):
            self._command_mode_inactivity_timer = None
            self._command_mode_timer_lock = threading.Lock()
            self.fire_count = 0

        def exit_command_mode(self):
            self.fire_count += 1

        def play_sound(self, name, **_kwargs):
            pass

    return _Stub()


class TestTimerHygiene:
    def test_fifty_rapid_resets_leave_exactly_one_live_timer(self):
        stub = _make_timer_stub()
        timers = []
        for _ in range(50):
            stub._reset_command_mode_inactivity_timer(5.0)
            timers.append(stub._command_mode_inactivity_timer)
        for t in timers[:-1]:
            assert t.finished.is_set(), "a superseded timer was never cancelled -- leak"
        assert not timers[-1].finished.is_set()
        stub._cancel_command_mode_inactivity_timer()

    def test_timeout_still_fires_when_genuinely_idle(self):
        stub = _make_timer_stub()
        stub._reset_command_mode_inactivity_timer(0.05)
        time.sleep(0.2)
        assert stub.fire_count == 1

    def test_cancel_prevents_fire(self):
        stub = _make_timer_stub()
        stub._reset_command_mode_inactivity_timer(0.05)
        stub._cancel_command_mode_inactivity_timer()
        time.sleep(0.2)
        assert stub.fire_count == 0


# ---------------------------------------------------------------------------
# Zombie-proofing: exception inside the timeout callback
# ---------------------------------------------------------------------------

def _make_zombie_timeout_stub(exit_raises=True):
    import dictation as _d

    class _Stub:
        _on_command_mode_inactivity = _d.DictationApp._on_command_mode_inactivity
        _cancel_command_mode_inactivity_timer = _d.DictationApp._cancel_command_mode_inactivity_timer
        _cancel_command_mode_inactivity_timer_locked = _d.DictationApp._cancel_command_mode_inactivity_timer_locked

        def __init__(self):
            self.command_mode_active = True
            self._command_mode_lock = threading.Lock()
            self._command_mode_inactivity_timer = None
            self._command_mode_timer_lock = threading.Lock()
            self._session_mode_manager = Mock()
            self._sounds = []

        def play_sound(self, name, **_kwargs):
            self._sounds.append(name)

        def exit_command_mode(self):
            if exit_raises:
                raise RuntimeError("boom mid-exit")
            self.command_mode_active = False

    return _Stub()


class TestTimeoutCallbackZombieProofing:
    def test_raise_inside_exit_command_mode_still_ends_session(self):
        stub = _make_zombie_timeout_stub(exit_raises=True)
        stub._on_command_mode_inactivity()  # must not raise out of this call
        assert stub.command_mode_active is False
        assert "error" in stub._sounds
        stub._session_mode_manager.reset.assert_called_once()

    def test_normal_exit_path_unaffected(self):
        stub = _make_zombie_timeout_stub(exit_raises=False)
        stub._on_command_mode_inactivity()
        assert stub.command_mode_active is False
        assert stub._sounds == []


# ---------------------------------------------------------------------------
# Zombie-proofing: exception inside per-utterance handling
# ---------------------------------------------------------------------------

def _make_utterance_stub():
    import dictation as _d

    class _Stub:
        _handle_command_mode_utterance = _d.DictationApp._handle_command_mode_utterance

        def __init__(self):
            self._wake_transcription_in_progress = False
            self.command_mode_active = True
            self._sounds = []
            self.vad_reset_calls = 0

        def play_sound(self, name, **_kwargs):
            self._sounds.append(name)

        def _vad_reset(self):
            self.vad_reset_calls += 1

    return _Stub()


class TestUtteranceLoopZombieProofing:
    def test_exception_earcons_and_leaves_session_alive_in_current_mode(self):
        stub = _make_utterance_stub()
        buffer = [np.ones(1600, dtype=np.float32) * 0.01]
        with patch('dictation.resample_audio', side_effect=RuntimeError("boom")):
            stub._handle_command_mode_utterance(buffer, 16000)  # must not raise
        assert stub.command_mode_active is True
        assert "error" in stub._sounds
        assert stub.vad_reset_calls == 1
        assert stub._wake_transcription_in_progress is False


# ---------------------------------------------------------------------------
# WakeConsumer._poll_loop fail-loud on a dead consumer thread
# ---------------------------------------------------------------------------

def _make_consumer(command_mode_active=False, mode='hold'):
    from samsara.audio_engine.wake_consumer import WakeConsumer

    engine = Mock()
    reader = Mock()
    engine.register_consumer = Mock(return_value=reader)
    app = Mock()
    app.wake_word_active = True
    app.command_mode_active = command_mode_active
    app.ai_command_mode_active = False
    app.config = {'command_mode': {'mode': mode}}
    wc = WakeConsumer(engine, app)
    return wc, reader, app


class TestWakeConsumerPollLoopFailsLoud:
    def test_loop_death_earcons_and_stops_running(self):
        wc, reader, app = _make_consumer()
        reader.read_next = Mock(side_effect=RuntimeError("ring exploded"))
        wc._running = True
        wc._poll_loop()  # exception must not propagate out of this call
        assert wc._running is False
        app.play_sound.assert_called_once_with('error')

    def test_loop_death_during_toggle_session_forces_session_end(self):
        wc, reader, app = _make_consumer(command_mode_active=True, mode='toggle')
        reader.read_next = Mock(side_effect=RuntimeError("ring exploded"))
        wc._running = True
        wc._poll_loop()
        app.exit_command_mode.assert_called_once()

    def test_per_frame_exception_does_not_kill_the_loop(self):
        """The pre-existing inner guard: a single bad frame logs and the
        loop keeps polling -- only an exception escaping THAT guard should
        ever stop the loop (covered above)."""
        wc, reader, app = _make_consumer()
        reader.read_next = Mock(return_value=Mock(device_epoch=1))
        count = {"n": 0}

        def _stop_after_a_few(frame):
            count["n"] += 1
            if count["n"] >= 3:
                wc._running = False
            raise RuntimeError("bad frame")

        wc._process_frame = _stop_after_a_few
        wc._running = True
        wc._poll_loop()  # must not propagate -- inner per-frame guard eats it
        assert count["n"] == 3
        app.play_sound.assert_not_called()
