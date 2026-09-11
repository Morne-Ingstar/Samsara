"""FIX 1 (2026-07-10 hotkey word-loss investigation): WakeConsumer must go
FULLY deaf during a hotkey recording -- no RMS/VAD/OWW, no onset, no
buffering, no wake transcription -- not merely pre-speech-onset (the old
bug: the guard only applied before app.is_speaking went True).

Preserves: toggle-command-mode servicing (where the always-live global
abort phrase lives, via SessionModeManager.dispatch_utterance reached
through _flush() -> _handle_command_mode_utterance, gated only on
_is_toggle_cmd(app), independent of _hotkey_recording).

The Ava command session is deliberately NOT preserved (2026-07-19 nag
incident, carried over verbatim through the Ava Front Door P1
consolidation): it used to share toggle-command-mode's exemption, which
let it keep transcribing and nagging "I didn't catch a command in that"
WHILE a hold-to-dictate recording was in progress. It now goes fully deaf
during a hotkey hold, same as plain wake-word mode -- see
TestAvaCommandSessionSuppressedDuringHotkeyRecording below.

Real WakeConsumer methods are exercised directly (not reimplemented),
matching the pattern in tests/test_inactivity_chokepoint.py.
"""
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.audio_engine import wake_consumer as wake_consumer_module
from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara.audio_engine.frame import FRAME_SIZE, SAMPLE_RATE


def _make_wc(
    hotkey_recording=False,
    command_mode_active=False,
    cm_mode='hold',
    ava_command_session_active=False,
    wake_word_active=True,
    hands_free_token=None,
):
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
    app.ava_command_session_active = ava_command_session_active
    app.config = {'command_mode': {'mode': cm_mode}}
    app._close_hands_free_capture_duck = Mock()
    app._open_hands_free_capture_duck = Mock(return_value=hands_free_token)
    app._hands_free_capture_duck_token = hands_free_token
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
    wc._hands_free_capture_duck_token = hands_free_token
    return wc, reader, app


def _loud_frame(epoch=0):
    """A frame whose RMS clears any plausible speech threshold."""
    pcm = np.full(FRAME_SIZE, 20000, dtype=np.int16)
    frame = Mock()
    frame.pcm = pcm
    frame.device_epoch = epoch
    frame.seq = 0
    return frame


def _process_complete_wake_utterance(wc, reader, app):
    """Feed speech followed by enough silence to exercise the real _flush."""
    from samsara.audio_engine.ring import EMPTY

    app.config['wake_word_config'] = {'audio': {
        'speech_threshold': 0.01,
        'min_speech_duration': 0.2,
        'wake_detection_silence': 0.0,
    }}
    # Install dispatch observers BEFORE the first frame. Both hotkey states
    # receive the identical input, including the silence needed to dispatch.
    app.process_wake_word_buffer = Mock()
    app._handle_command_mode_utterance = Mock()
    app._vad_is_speech.side_effect = [True] * 5 + [False] * 2
    speech = [_loud_frame() for _ in range(5)]
    # On onset the ring replay includes the current frame, not an empty ring.
    reader.read_next.side_effect = [speech[0], EMPTY]
    silence = [_loud_frame() for _ in range(2)]
    for frame in silence:
        frame.pcm = np.zeros(FRAME_SIZE, dtype=np.int16)
    frames = speech + silence
    for frame in frames:
        wc._process_frame(frame)
    return frames


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
        _process_complete_wake_utterance(wc, reader, app)
        app.process_wake_word_buffer.assert_not_called()
        app._handle_command_mode_utterance.assert_not_called()
        app._vad_is_speech.assert_not_called()
        assert wc._utterance_frames == []

    def test_suppression_engaged_log_fires_once_not_per_frame(self, caplog):
        import logging
        wc, reader, app = _make_wc(hotkey_recording=True)
        with caplog.at_level(logging.DEBUG, logger="Samsara.samsara.audio_engine.wake_consumer"):
            for _ in range(5):
                wc._process_frame(_loud_frame())
        engaged = [r for r in caplog.records if "suppression ENGAGED" in r.message]
        assert len(engaged) == 1


class TestNoHotkeyRecordingIsUnaffected:
    def test_wake_transcription_dispatched_when_not_hotkey_recording(self):
        wc, reader, app = _make_wc(hotkey_recording=False)
        frames = _process_complete_wake_utterance(wc, reader, app)
        app.process_wake_word_buffer.assert_called_once()
        args, kwargs = app.process_wake_word_buffer.call_args
        buffer, sample_rate = args
        assert sample_rate == SAMPLE_RATE
        assert kwargs == {'oww_confirmed': False, 'owner_token': None, 'tracked': False}
        assert len(buffer) == len(frames)
        for actual, frame in zip(buffer, frames):
            np.testing.assert_array_equal(actual, frame.pcm.astype(np.float32) / 32767.0)
        assert app._vad_is_speech.call_count == len(frames)
        app._handle_command_mode_utterance.assert_not_called()
        assert wc._utterance_frames == []
        assert app.is_speaking is False

    def test_vad_still_called_when_not_hotkey_recording(self):
        wc, reader, app = _make_wc(hotkey_recording=False)
        wc._process_frame(_loud_frame())
        app._vad_is_speech.assert_called_once()

    def test_speech_onset_buffers_normally(self):
        wc, reader, app = _make_wc(hotkey_recording=False)
        wc._process_frame(_loud_frame())
        assert app.is_speaking is True
        app._open_hands_free_capture_duck.assert_not_called()

    def test_speech_onset_opens_capture_duck_in_toggle_mode(self, monkeypatch):
        wc, reader, app = _make_wc(
            hotkey_recording=False, command_mode_active=True, cm_mode='toggle', hands_free_token=99
        )
        wc._process_frame(_loud_frame())
        app._open_hands_free_capture_duck.assert_called_once_with()
        assert wc._hands_free_capture_duck_token == 99


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


class TestAvaCommandSessionSuppressedDuringHotkeyRecording:
    """2026-07-19 nag incident: the Ava command session used to be exempted
    from the hotkey-deafness gate, so its utterance loop kept running (and
    nagging) concurrently with a hold-to-dictate recording. It now gets
    the same full deafness as plain wake-word mode."""

    def test_no_vad_call_in_ava_command_session_while_hotkey_recording(self):
        wc, reader, app = _make_wc(
            hotkey_recording=True, ava_command_session_active=True,
        )
        wc._process_frame(_loud_frame())
        app._vad_is_speech.assert_not_called()

    def test_no_utterance_buffering_in_ava_command_session_while_hotkey_recording(self):
        wc, reader, app = _make_wc(
            hotkey_recording=True, ava_command_session_active=True,
        )
        wc._process_frame(_loud_frame())
        assert wc._utterance_frames == []
        assert wc._buffer_rms_history == []

    def test_ava_command_session_still_serviced_once_hotkey_recording_ends(self):
        wc, reader, app = _make_wc(
            hotkey_recording=True, ava_command_session_active=True,
        )
        wc._process_frame(_loud_frame())
        app._hotkey_recording = False
        wc._process_frame(_loud_frame())
        app._vad_is_speech.assert_called_once()

    def test_suppression_engaged_logged_for_ava_command_session(self, caplog):
        import logging
        wc, reader, app = _make_wc(
            hotkey_recording=True, ava_command_session_active=True,
        )
        with caplog.at_level(logging.DEBUG, logger="Samsara.samsara.audio_engine.wake_consumer"):
            wc._process_frame(_loud_frame())
        engaged = [r for r in caplog.records if "suppression ENGAGED" in r.message]
        assert len(engaged) == 1

    def test_suppression_released_logged_once_hotkey_recording_ends(self, caplog):
        import logging
        wc, reader, app = _make_wc(
            hotkey_recording=True, ava_command_session_active=True,
        )
        wc._process_frame(_loud_frame())  # engage (not captured)
        app._hotkey_recording = False
        with caplog.at_level(logging.DEBUG, logger="Samsara.samsara.audio_engine.wake_consumer"):
            wc._process_frame(_loud_frame())
        released = [r for r in caplog.records if "suppression RELEASED" in r.message]
        assert len(released) == 1


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

    def test_discards_ava_command_session_utterance_too(self):
        """2026-07-19 nag incident fix: the Ava command session no longer
        gets the toggle-command-mode exemption -- its in-progress utterance
        is discarded like plain wake-word mode's would be, rather than left
        to go stale through the hotkey hold."""
        wc, reader, app = _make_wc(ava_command_session_active=True)
        wc._utterance_frames = [np.zeros(10, dtype=np.float32)]
        app.is_speaking = True
        wc.discard_stale_wake_utterance()
        assert wc._utterance_frames == []
        assert app.is_speaking is False

    def test_safe_to_call_with_nothing_in_progress(self):
        wc, reader, app = _make_wc()
        wc.discard_stale_wake_utterance()  # must not raise
        assert wc._utterance_frames == []


class TestCaptureDuckDiscardPaths:
    @staticmethod
    def _flush_to_quiet(wc, *, calls=1):
        # 100ms frame cadence means one frame per loop at FRAME_SIZE.
        # Keep this helper small and explicit for predictable timing.
        for _ in range(calls):
            wc._process_frame(_loud_frame())

    def test_too_short_utterance_discard_closes_capture_duck(self, monkeypatch):
        wc, reader, app = _make_wc(hands_free_token=11)
        monkeypatch.setattr(wake_consumer_module, "PREBUFFER_FRAMES", 1)
        app._vad_is_speech.side_effect = [True, True, False, False]
        app.config["wake_word_config"] = {
            "audio": {
                "speech_threshold": 0.01,
                "wake_detection_silence": 0.0,
                "min_speech_duration": 1.0,
            }
        }

        # One speech onset + one short frame of speech + two silence frames to
        # reach the silence gate and force the short-buffer branch.
        self._flush_to_quiet(wc, calls=4)

        app._close_hands_free_capture_duck.assert_called_once_with(11)

    def test_stuck_buffer_discard_closes_capture_duck(self, monkeypatch):
        wc, reader, app = _make_wc(hands_free_token=12)
        app._vad_is_speech = Mock(return_value=True)
        self._flush_to_quiet(wc, calls=35)

        app._close_hands_free_capture_duck.assert_called_once_with(12)

    def test_hard_cap_discard_closes_capture_duck(self, monkeypatch):
        wc, reader, app = _make_wc(hands_free_token=13, command_mode_active=True)
        app._vad_is_speech = Mock(return_value=True)
        self._flush_to_quiet(wc, calls=75)

        app._close_hands_free_capture_duck.assert_called_once_with(13)


class TestCaptureDuckOwnershipCleanup:
    def test_abort_utterance_closes_capture_duck(self):
        wc, reader, app = _make_wc(hands_free_token=14)
        wc._utterance_frames = [np.zeros(10, dtype=np.float32)]
        app.is_speaking = True
        wc.abort_utterance()
        app._close_hands_free_capture_duck.assert_called_once_with(14)

    def test_stop_closes_capture_duck(self):
        wc, reader, app = _make_wc(hands_free_token=15)
        wc._hands_free_capture_duck_token = 15
        wc.stop()
        app._close_hands_free_capture_duck.assert_called_once_with(15)
