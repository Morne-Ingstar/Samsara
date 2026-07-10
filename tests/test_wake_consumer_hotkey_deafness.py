"""FIX 1 (2026-07-10 hotkey word-loss investigation): WakeConsumer must go
FULLY deaf during a hotkey recording -- no RMS/VAD/OWW, no onset, no
buffering, no wake transcription -- not merely pre-speech-onset (the old
bug: the guard only applied before app.is_speaking went True).

Preserves: toggle-command-mode servicing (where the always-live global
abort phrase lives, via SessionModeManager.dispatch_utterance reached
through _flush() -> _handle_command_mode_utterance, gated only on
_is_toggle_cmd(app), independent of _hotkey_recording) and AI-command-mode
servicing.

Real WakeConsumer methods are exercised directly (not reimplemented),
matching the pattern in tests/test_inactivity_chokepoint.py.
"""
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara.audio_engine.frame import FRAME_SIZE


def _make_wc(hotkey_recording=False, command_mode_active=False, cm_mode='hold',
             ai_command_mode_active=False, wake_word_active=True):
    """Real WakeConsumer wired to a Mock() engine/reader and an app double
    with EXPLICIT bool attributes -- deliberately not a bare Mock() for
    `app` itself, since Mock() auto-creates truthy attributes (e.g.
    app._hotkey_recording would be a truthy MagicMock, not a real bool),
    which would silently defeat these exact guards."""
    from samsara.audio_engine.ring import EMPTY

    engine = Mock()
    reader = Mock()
    # Default: prebuffer-replay loop (speech-onset path) sees no history,
    # so it doesn't try to .astype()/divide a Mock() as if it were a real
    # int16 pcm array. Tests exercising the onset path override this.
    reader.read_next = Mock(return_value=EMPTY)
    engine.register_consumer = Mock(return_value=reader)

    app = Mock()
    app.wake_word_active = wake_word_active
    app._hotkey_recording = hotkey_recording
    app.command_mode_active = command_mode_active
    app.ai_command_mode_active = ai_command_mode_active
    app.config = {'command_mode': {'mode': cm_mode}}
    app.is_speaking = False
    app.silence_start = None
    app._command_executed_at = None
    app._vad_available = True
    app._vad_is_speech = Mock(return_value=True)
    app._wake_detector = None
    app.app_state = 'asleep'
    app.wake_word_triggered = False
    app._tts_last_speaking = 0.0
    app.audio_coordinator = None

    wc = WakeConsumer(engine, app)
    return wc, reader, app


def _loud_frame(epoch=0):
    """A frame whose RMS clears any plausible speech threshold."""
    pcm = np.full(FRAME_SIZE, 20000, dtype=np.int16)
    frame = Mock()
    frame.pcm = pcm
    frame.device_epoch = epoch
    frame.seq = 0
    return frame


class TestFullDeafnessDuringHotkeyRecording:
    def test_no_vad_call_while_hotkey_recording(self):
        wc, reader, app = _make_wc(hotkey_recording=True)
        wc._process_frame(_loud_frame())
        app._vad_is_speech.assert_not_called()

    def test_no_utterance_buffering_while_hotkey_recording(self):
        wc, reader, app = _make_wc(hotkey_recording=True)
        wc._process_frame(_loud_frame())
        assert wc._utterance_frames == []
        assert wc._buffer_rms_history == []

    def test_is_speaking_never_set_while_hotkey_recording(self):
        wc, reader, app = _make_wc(hotkey_recording=True)
        wc._process_frame(_loud_frame())
        assert app.is_speaking is False

    def test_no_wake_transcription_dispatched_while_hotkey_recording(self):
        wc, reader, app = _make_wc(hotkey_recording=True)
        for _ in range(5):
            wc._process_frame(_loud_frame())
        app.process_wake_word_buffer = Mock()
        app._handle_command_mode_utterance = Mock()
        app.process_wake_word_buffer.assert_not_called()
        app._handle_command_mode_utterance.assert_not_called()

    def test_suppression_engaged_log_fires_once_not_per_frame(self, caplog):
        import logging
        wc, reader, app = _make_wc(hotkey_recording=True)
        with caplog.at_level(logging.DEBUG, logger="Samsara.samsara.audio_engine.wake_consumer"):
            for _ in range(5):
                wc._process_frame(_loud_frame())
        engaged = [r for r in caplog.records if "suppression ENGAGED" in r.message]
        assert len(engaged) == 1


class TestNoHotkeyRecordingIsUnaffected:
    def test_vad_still_called_when_not_hotkey_recording(self):
        wc, reader, app = _make_wc(hotkey_recording=False)
        wc._process_frame(_loud_frame())
        app._vad_is_speech.assert_called_once()

    def test_speech_onset_buffers_normally(self):
        wc, reader, app = _make_wc(hotkey_recording=False)
        wc._process_frame(_loud_frame())
        assert app.is_speaking is True


class TestToggleCommandModeStillServicesDuringHotkeyRecording:
    def test_vad_still_called_in_toggle_mode_even_while_hotkey_recording(self):
        wc, reader, app = _make_wc(
            hotkey_recording=True, command_mode_active=True, cm_mode='toggle',
        )
        wc._process_frame(_loud_frame())
        app._vad_is_speech.assert_called_once()

    def test_toggle_mode_buffers_speech_even_while_hotkey_recording(self):
        wc, reader, app = _make_wc(
            hotkey_recording=True, command_mode_active=True, cm_mode='toggle',
        )
        wc._process_frame(_loud_frame())
        assert app.is_speaking is True

    def test_no_engaged_log_when_toggle_mode_exempts_the_frame(self, caplog):
        import logging
        wc, reader, app = _make_wc(
            hotkey_recording=True, command_mode_active=True, cm_mode='toggle',
        )
        with caplog.at_level(logging.DEBUG, logger="Samsara.samsara.audio_engine.wake_consumer"):
            wc._process_frame(_loud_frame())
        engaged = [r for r in caplog.records if "suppression ENGAGED" in r.message]
        assert engaged == []


class TestAiCommandModeStillServicesDuringHotkeyRecording:
    def test_vad_still_called_in_ai_command_mode_even_while_hotkey_recording(self):
        wc, reader, app = _make_wc(
            hotkey_recording=True, ai_command_mode_active=True,
        )
        wc._process_frame(_loud_frame())
        app._vad_is_speech.assert_called_once()


class TestDiscardStaleWakeUtterance:
    def test_discards_in_progress_wake_mode_utterance(self):
        wc, reader, app = _make_wc()
        wc._utterance_frames = [np.zeros(10, dtype=np.float32)]
        wc._buffer_rms_history = [0.1]
        app.is_speaking = True
        wc.discard_stale_wake_utterance()
        assert wc._utterance_frames == []
        assert wc._buffer_rms_history == []
        assert app.is_speaking is False

    def test_never_flushes_the_discarded_utterance(self):
        wc, reader, app = _make_wc()
        wc._utterance_frames = [np.zeros(10, dtype=np.float32)]
        app.is_speaking = True
        wc._flush = Mock()
        wc.discard_stale_wake_utterance()
        wc._flush.assert_not_called()

    def test_noop_when_toggle_command_mode_owns_the_utterance(self):
        wc, reader, app = _make_wc(command_mode_active=True, cm_mode='toggle')
        wc._utterance_frames = [np.zeros(10, dtype=np.float32)]
        app.is_speaking = True
        wc.discard_stale_wake_utterance()
        assert len(wc._utterance_frames) == 1  # untouched
        assert app.is_speaking is True  # untouched

    def test_noop_when_ai_command_mode_owns_the_utterance(self):
        wc, reader, app = _make_wc(ai_command_mode_active=True)
        wc._utterance_frames = [np.zeros(10, dtype=np.float32)]
        app.is_speaking = True
        wc.discard_stale_wake_utterance()
        assert len(wc._utterance_frames) == 1
        assert app.is_speaking is True

    def test_safe_to_call_with_nothing_in_progress(self):
        wc, reader, app = _make_wc()
        wc.discard_stale_wake_utterance()  # must not raise
        assert wc._utterance_frames == []
