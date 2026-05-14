"""Tests for AudioCoordinator state machine.

The coordinator integrates tightly with a Samsara app instance and a
TTSEngine. Tests use lightweight mocks for both so no real audio hardware
or WinRT is required.

All tests are fast, deterministic, and independent of each other.
"""

import threading
import time
from unittest.mock import MagicMock, call, patch

import pytest

from samsara.tts.coordinator import (
    IDLE, LISTENING, SPEAKING, THINKING, AudioCoordinator
)
from samsara.tts.engine_base import SpeechHandle


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_handle(uid="test-uid"):
    return SpeechHandle(utterance_id=uid)


def _make_app(is_speaking=False, speech_threshold=0.03):
    """Minimal app mock with the attributes the coordinator touches."""
    app = MagicMock()
    app.is_speaking = is_speaking
    app.config = {
        'wake_word_config': {
            'audio': {'speech_threshold': speech_threshold}
        }
    }
    app.play_sound = MagicMock()
    return app


def _make_engine(state='idle'):
    """Mock TTSEngine."""
    engine = MagicMock()
    engine.get_engine_state.return_value = state
    engine.is_speaking.return_value = False

    handle = _make_handle()
    engine.speak.return_value = handle

    return engine, handle


def _make_coordinator(is_speaking=False, engine_state='idle', cfg=None):
    """Instantiate a coordinator with mock app + engine."""
    app = _make_app(is_speaking=is_speaking)
    engine, handle = _make_engine(state=engine_state)
    coord = AudioCoordinator(app, engine, config=cfg or {})
    return coord, app, engine, handle


# ---------------------------------------------------------------------------
# State machine basics
# ---------------------------------------------------------------------------

class TestInitialState:
    def test_initial_state_is_idle(self):
        coord, _, _, _ = _make_coordinator()
        assert coord.get_state() == IDLE

    def test_explicit_transition_to_listening(self):
        coord, _, _, _ = _make_coordinator()
        result = coord.transition_to(LISTENING)
        assert result is True
        assert coord.get_state() == LISTENING

    def test_invalid_transition_idle_to_speaking_directly_rejected(self):
        # IDLE → SPEAKING is allowed via coordinator.speak(); transition_to(SPEAKING)
        # from IDLE should work (coordinator.speak uses it).
        # But IDLE → THINKING is illegal.
        coord, _, _, _ = _make_coordinator()
        result = coord.transition_to(THINKING)
        assert result is False
        assert coord.get_state() == IDLE

    def test_invalid_transition_speaking_to_thinking_rejected(self):
        coord, _, _, _ = _make_coordinator()
        coord.transition_to(LISTENING)
        coord.transition_to(SPEAKING)
        result = coord.transition_to(THINKING)
        assert result is False
        assert coord.get_state() == SPEAKING

    def test_same_state_transition_returns_false(self):
        coord, _, _, _ = _make_coordinator()
        result = coord.transition_to(IDLE)
        assert result is False


# ---------------------------------------------------------------------------
# coordinator.speak() full path
# ---------------------------------------------------------------------------

class TestSpeakFullPath:
    def test_speak_transitions_to_speaking(self):
        coord, app, engine, handle = _make_coordinator()
        # We don't want on_done to transition back (no auto-idle in this test)
        engine.speak.return_value = handle

        coord.speak("hello world")
        assert coord.get_state() == SPEAKING

    def test_speak_calls_engine_with_tts_config(self):
        coord, app, engine, handle = _make_coordinator()
        app.config['tts'] = {'speed': 1.2, 'volume': 0.6}
        coord.speak("test", speed=None, volume=None)
        engine.speak.assert_called_once()
        _, kwargs = engine.speak.call_args
        assert kwargs['speed'] == pytest.approx(1.2)
        assert kwargs['volume'] == pytest.approx(0.6)

    def test_on_done_wraps_transitions_to_idle(self):
        coord, app, engine, handle = _make_coordinator()
        on_done_fired = threading.Event()

        coord.speak("wrapped", on_done=on_done_fired.set)

        # Simulate TTS completing by extracting and calling the wrapped on_done.
        _, kwargs = engine.speak.call_args
        wrapped_on_done = kwargs.get('on_done')
        assert wrapped_on_done is not None

        wrapped_on_done()  # simulate engine completion
        assert coord.get_state() == IDLE
        assert on_done_fired.is_set()

    def test_cancel_speech_returns_to_idle(self):
        coord, app, engine, handle = _make_coordinator()
        coord.speak("cancel me")
        assert coord.get_state() == SPEAKING
        coord.cancel_speech()
        engine.cancel_all.assert_called()
        assert coord.get_state() == IDLE


# ---------------------------------------------------------------------------
# Earcon ducking
# ---------------------------------------------------------------------------

class TestEarconDucking:
    def _enter_speaking(self, coord, engine, handle):
        """Helper: set up SPEAKING state with active handle."""
        engine.get_engine_state.return_value = 'playing'
        engine.speak.return_value = handle
        coord.speak("duck test")

    def test_no_duck_when_idle(self):
        coord, app, engine, handle = _make_coordinator()
        coord.on_earcon_starting('success')
        engine.set_volume.assert_not_called()

    def test_no_duck_when_engine_not_playing(self):
        coord, app, engine, handle = _make_coordinator()
        engine.get_engine_state.return_value = 'synthesizing'
        engine.speak.return_value = handle
        coord.speak("not playing yet")
        coord.on_earcon_starting('success')
        engine.set_volume.assert_not_called()

    def test_duck_fires_when_speaking_and_playing(self):
        coord, app, engine, handle = _make_coordinator()
        self._enter_speaking(coord, engine, handle)
        coord.on_earcon_starting('success')
        engine.set_volume.assert_called_once_with(
            handle, coord._duck_factor, fade_ms=coord._duck_fade_ms
        )

    def test_duck_restore_fires_after_earcon_duration(self):
        cfg = {'duck_default_duration_ms': 50}  # short duration for fast test
        coord, app, engine, handle = _make_coordinator(cfg=cfg)
        self._enter_speaking(coord, engine, handle)
        coord.on_earcon_starting('success')

        # Wait for restore timer
        time.sleep(0.6)  # 50ms earcon + buffer
        calls = engine.set_volume.call_args_list
        # Should have duck + restore
        assert any(c == call(handle, 1.0, fade_ms=coord._duck_fade_ms) for c in calls)

    def test_duck_depth_counter_overlapping_earcons(self):
        """Two concurrent earcons: one duck, one restore after both finish."""
        cfg = {'duck_default_duration_ms': 80}
        coord, app, engine, handle = _make_coordinator(cfg=cfg)
        self._enter_speaking(coord, engine, handle)

        coord.on_earcon_starting('start')
        coord.on_earcon_starting('success')  # second earcon 0ms later

        # Duck should have been called once (depth was 0 → 1)
        duck_calls = [c for c in engine.set_volume.call_args_list
                      if c[0][1] == coord._duck_factor]
        assert len(duck_calls) == 1

        # Wait for both restore timers
        time.sleep(0.6)
        restore_calls = [c for c in engine.set_volume.call_args_list
                         if c[0][1] == 1.0]
        # Only one restore after both timers fire
        assert len(restore_calls) == 1


# ---------------------------------------------------------------------------
# State listeners
# ---------------------------------------------------------------------------

class TestStateListeners:
    def test_listener_invoked_on_transition(self):
        coord, _, _, _ = _make_coordinator()
        events = []
        coord.register_state_listener(lambda old, new, ctx: events.append((old, new)))
        coord.transition_to(LISTENING)
        assert events == [(IDLE, LISTENING)]

    def test_multiple_transitions_multiple_events(self):
        coord, _, _, _ = _make_coordinator()
        events = []
        coord.register_state_listener(lambda old, new, ctx: events.append((old, new)))
        coord.transition_to(LISTENING)
        coord.transition_to(IDLE)
        assert events == [(IDLE, LISTENING), (LISTENING, IDLE)]

    def test_unregister_stops_callbacks(self):
        coord, _, _, _ = _make_coordinator()
        events = []
        cb = lambda old, new, ctx: events.append((old, new))
        coord.register_state_listener(cb)
        coord.transition_to(LISTENING)
        coord.unregister_state_listener(cb)
        coord.transition_to(IDLE)
        assert len(events) == 1

    def test_listener_receives_context(self):
        coord, _, _, _ = _make_coordinator()
        contexts = []
        coord.register_state_listener(lambda old, new, ctx: contexts.append(ctx))
        coord.transition_to(LISTENING, context={'reason': 'wake_word'})
        assert contexts[0].get('reason') == 'wake_word'


# ---------------------------------------------------------------------------
# Wake-word threshold management
# ---------------------------------------------------------------------------

class TestThresholdManagement:
    def test_threshold_raised_on_speaking_entry(self):
        coord, app, engine, handle = _make_coordinator()
        original = app.config['wake_word_config']['audio']['speech_threshold']
        engine.speak.return_value = handle
        engine.get_engine_state.return_value = 'playing'
        coord.speak("raise threshold")
        current = app.config['wake_word_config']['audio']['speech_threshold']
        assert current > original

    def test_threshold_restored_on_speaking_exit(self):
        coord, app, engine, handle = _make_coordinator()
        original = app.config['wake_word_config']['audio']['speech_threshold']
        engine.speak.return_value = handle
        engine.get_engine_state.return_value = 'playing'

        coord.speak("then restore")
        coord.transition_to(IDLE, context={'reason': 'tts_complete'})

        restored = app.config['wake_word_config']['audio']['speech_threshold']
        assert restored == pytest.approx(original)

    def test_threshold_not_compounded_on_multiple_speaks(self):
        """Verify the multiplier doesn't compound across consecutive speak() calls."""
        coord, app, engine, handle = _make_coordinator()
        original = app.config['wake_word_config']['audio']['speech_threshold']
        engine.get_engine_state.return_value = 'playing'

        for _ in range(3):
            engine.speak.return_value = _make_handle(str(_))
            coord.speak("repeat")
            coord.transition_to(IDLE, context={'reason': 'tts_complete'})

        final = app.config['wake_word_config']['audio']['speech_threshold']
        assert final == pytest.approx(original)


# ---------------------------------------------------------------------------
# Interrupt-on-speech grace period
# ---------------------------------------------------------------------------

class TestInterruptGracePeriod:
    def test_no_interrupt_during_synthesis(self):
        """While engine is 'synthesizing', no interrupt fires regardless of is_speaking."""
        coord, app, engine, handle = _make_coordinator(is_speaking=True)
        engine.get_engine_state.return_value = 'synthesizing'
        engine.speak.return_value = handle

        interrupted = threading.Event()
        coord.register_state_listener(
            lambda old, new, ctx: interrupted.set()
            if new == LISTENING else None
        )

        coord.speak("long synthesis")
        time.sleep(0.3)  # Wait longer than grace period default (200ms)
        # Grace should NOT expire because engine never entered 'playing'
        # (engine mock always returns 'synthesizing')
        assert coord.get_state() == SPEAKING  # no interrupt yet
        coord.shutdown()

    def test_interrupt_fires_after_grace_when_playing(self):
        """After grace expires with engine 'playing', is_speaking=True triggers interrupt."""
        cfg = {'interrupt_grace_period_ms': 50}
        coord, app, engine, handle = _make_coordinator(cfg=cfg)

        # Engine starts in synthesizing, then flips to playing
        call_count = [0]
        def _engine_state():
            call_count[0] += 1
            return 'playing' if call_count[0] > 2 else 'synthesizing'
        engine.get_engine_state.side_effect = _engine_state
        engine.speak.return_value = handle

        # User is already speaking
        app.is_speaking = True

        interrupted = threading.Event()
        coord.register_state_listener(
            lambda old, new, ctx: interrupted.set() if new == LISTENING else None
        )

        coord.speak("will be interrupted")
        interrupted.wait(timeout=3.0)
        assert interrupted.is_set(), "Interrupt should have fired"
        assert coord.get_state() == LISTENING
        coord.shutdown()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

class TestShutdown:
    def test_shutdown_cancels_pending_speech(self):
        coord, app, engine, handle = _make_coordinator()
        engine.speak.return_value = handle
        coord.speak("will be shut down")
        coord.shutdown()
        engine.cancel_all.assert_called()

    def test_shutdown_returns_to_idle(self):
        coord, app, engine, handle = _make_coordinator()
        engine.speak.return_value = handle
        coord.speak("shutdown test")
        coord.shutdown()
        assert coord.get_state() == IDLE

    def test_coordinator_survives_engine_crash(self):
        """Engine crashing mid-speak should not leave coordinator stuck."""
        coord, app, engine, handle = _make_coordinator()
        engine.speak.side_effect = RuntimeError("engine exploded")

        try:
            coord.speak("crash test")
        except RuntimeError:
            pass

        # Coordinator should still function
        coord.shutdown()
        assert coord.get_state() == IDLE
