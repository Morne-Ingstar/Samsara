"""2026-07-23 G3 live-test finding: the Ava command session's own TTS
(miss feedback, CONFIRM lines, Ava's conversational replies -- anything
routed through AudioCoordinator.speak()) was being captured by the SAME
wake-word-style listener as if it were the next user utterance --
log-confirmed: an utterance transcribed as ", open up, I'm listening,
open up tab", where "I'm listening" is the session's own TTS bleeding
into the surrounding real speech's buffer.

Mirrors FIX 1's hotkey_suppress pattern (tests/test_wake_consumer_hotkey_
deafness.py): full deafness (no RMS/VAD/OWW/onset/buffering) while
AudioCoordinator.is_speaking is True for the Ava command session
specifically, plus a short tail (_AVA_CMD_TTS_TAIL_S) after playback ends,
then capture arms fresh. Real WakeConsumer methods are exercised directly
against a Mock() engine/reader and an explicit-attribute app double, same
convention as test_wake_consumer_hotkey_deafness.py (Mock() auto-creates
truthy attributes, which would silently defeat these exact guards).
"""
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.audio_engine import wake_consumer as wc_module
from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara.audio_engine.frame import FRAME_SIZE


class _FakeClock:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _make_wc(monkeypatch, ava_command_session_active=True, tts_speaking=False):
    from samsara.audio_engine.ring import EMPTY

    engine = Mock()
    reader = Mock()
    reader.read_next = Mock(return_value=EMPTY)
    engine.register_consumer = Mock(return_value=reader)

    app = Mock()
    app.wake_word_active = True
    app._hotkey_recording = False
    app.command_mode_active = False
    app.ava_command_session_active = ava_command_session_active
    app.config = {'command_mode': {'mode': 'hold'}}
    app.is_speaking = False  # VAD-detected USER speech onset -- unrelated flag
    app.silence_start = None
    app._command_executed_at = None
    app._vad_available = True
    app._vad_is_speech = Mock(return_value=True)
    app._wake_detector = None
    app.app_state = 'asleep'
    app.wake_word_triggered = False

    coordinator = Mock()
    coordinator.is_speaking = tts_speaking
    app.audio_coordinator = coordinator

    # Start well away from 0.0: a separate, pre-existing TTS guard further
    # down _process_frame (line ~500) compares time.monotonic() against
    # app._tts_last_speaking, which defaults to 0.0 -- starting the fake
    # clock at 0.0 too would make that unrelated guard misfire (elapsed
    # looks like 0.0s < 0.3s "just finished speaking") for every test here.
    clock = _FakeClock(start=1000.0)
    monkeypatch.setattr(wc_module.time, 'monotonic', clock)
    app._tts_last_speaking = 0.0

    wc = WakeConsumer(engine, app)
    return wc, reader, app, coordinator, clock


def _loud_frame(epoch=0):
    """A frame whose RMS clears any plausible speech threshold."""
    pcm = np.full(FRAME_SIZE, 20000, dtype=np.int16)
    frame = Mock()
    frame.pcm = pcm
    frame.device_epoch = epoch
    frame.seq = 0
    return frame


class TestFullDeafnessWhileSessionTtsSpeaking:
    def test_no_vad_call_while_tts_speaking(self, monkeypatch):
        wc, reader, app, coordinator, clock = _make_wc(monkeypatch, tts_speaking=True)
        wc._process_frame(_loud_frame())
        app._vad_is_speech.assert_not_called()

    def test_no_utterance_buffering_while_tts_speaking(self, monkeypatch):
        wc, reader, app, coordinator, clock = _make_wc(monkeypatch, tts_speaking=True)
        wc._process_frame(_loud_frame())
        assert wc._utterance_frames == []
        assert wc._buffer_rms_history == []

    def test_is_speaking_flag_never_set_while_tts_speaking(self, monkeypatch):
        """app.is_speaking here means VAD-detected onset -- must stay
        False, not get set from the discarded frame."""
        wc, reader, app, coordinator, clock = _make_wc(monkeypatch, tts_speaking=True)
        wc._process_frame(_loud_frame())
        assert app.is_speaking is False

    def test_suppression_engaged_log_fires_once_not_per_frame(self, monkeypatch, caplog):
        import logging
        wc, reader, app, coordinator, clock = _make_wc(monkeypatch, tts_speaking=True)
        with caplog.at_level(logging.DEBUG, logger="Samsara.samsara.audio_engine.wake_consumer"):
            for _ in range(5):
                wc._process_frame(_loud_frame())
        engaged = [r for r in caplog.records if 'suppression ENGAGED' in r.message]
        assert len(engaged) == 1


class TestTailWindowAfterPlaybackCompletes:
    def test_capture_still_suppressed_immediately_after_speaking_ends(self, monkeypatch):
        wc, reader, app, coordinator, clock = _make_wc(monkeypatch, tts_speaking=True)
        wc._process_frame(_loud_frame())  # TTS speaking -- observed and discarded

        coordinator.is_speaking = False   # playback just finished
        clock.advance(0.05)               # well inside the ~300ms tail
        wc._process_frame(_loud_frame())

        assert wc._utterance_frames == []
        app._vad_is_speech.assert_not_called()

    def test_capture_arms_fresh_once_tail_elapses(self, monkeypatch):
        wc, reader, app, coordinator, clock = _make_wc(monkeypatch, tts_speaking=True)
        wc._process_frame(_loud_frame())  # TTS speaking

        coordinator.is_speaking = False
        clock.advance(0.05)
        wc._process_frame(_loud_frame())  # observes playback ended -- tail starts NOW, still discarded
        assert wc._utterance_frames == []

        # Comfortably past _AVA_CMD_TTS_TAIL_S (0.3s) from when the tail
        # started -- not exactly 0.3s, to avoid float-accumulation landing
        # on the wrong side of the boundary.
        clock.advance(0.5)
        wc._process_frame(_loud_frame())

        app._vad_is_speech.assert_called()

    def test_suppression_released_log_fires_once_tail_elapses(self, monkeypatch, caplog):
        import logging
        wc, reader, app, coordinator, clock = _make_wc(monkeypatch, tts_speaking=True)
        wc._process_frame(_loud_frame())  # TTS speaking -- engages suppression
        coordinator.is_speaking = False
        clock.advance(0.05)
        wc._process_frame(_loud_frame())  # observes playback ended -- tail starts NOW
        clock.advance(0.5)  # comfortably past the tail from THAT observation

        with caplog.at_level(logging.DEBUG, logger="Samsara.samsara.audio_engine.wake_consumer"):
            wc._process_frame(_loud_frame())

        assert any('suppression RELEASED' in r.message for r in caplog.records)


class TestNotSuppressedWhenNotSpeaking:
    def test_normal_capture_when_tts_idle(self, monkeypatch):
        wc, reader, app, coordinator, clock = _make_wc(monkeypatch, tts_speaking=False)
        wc._process_frame(_loud_frame())
        app._vad_is_speech.assert_called()

    def test_no_audio_coordinator_does_not_crash_or_suppress(self, monkeypatch):
        wc, reader, app, coordinator, clock = _make_wc(monkeypatch, tts_speaking=False)
        app.audio_coordinator = None
        wc._process_frame(_loud_frame())
        app._vad_is_speech.assert_called()


class TestOnlyAppliesToTheAvaCommandSession:
    def test_guard_state_not_engaged_for_other_modes(self, monkeypatch):
        """This guard's own state (_ava_cmd_tts_suppressed_last) must
        never engage outside the Ava command session -- scoped strictly
        to _is_ai_cmd_mode(app). Asserts on THIS guard's own state rather
        than the VAD call: a separate, pre-existing, unrelated TTS guard
        further down _process_frame (line ~495, pre-onset-only, gated on
        AudioCoordinator.is_speaking with no mode scoping at all) ALSO
        discards a frame while TTS is speaking, for any mode -- that
        guard firing too is not evidence THIS one did."""
        wc, reader, app, coordinator, clock = _make_wc(
            monkeypatch, ava_command_session_active=False, tts_speaking=True,
        )
        wc._process_frame(_loud_frame())
        assert wc._ava_cmd_tts_suppressed_last is False
