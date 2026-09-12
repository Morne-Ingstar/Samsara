"""Hold-to-record while hands-free (toggle command mode) is active must
SUSPEND every hands-free lane for the hold's duration, then resume cleanly.

The reported defect: the held words landed BOTH in the hold's own paste and
in the hands-free preview box (double capture), because the lanes read the
ring independently of each other.

The suspension contract exercised here:
  * hold START  -- every lane stops reading/emitting AND discards whatever
                   partial it had accumulated (never commit half an
                   utterance);
  * during hold -- no frames accumulated, no partial decoded, no text
                   emitted; the preview box reads "Paused (hold)";
  * hold END    -- lanes resume from the CURRENT ring position, never
                   rewinding into the held audio; the box returns to its
                   normal transcript/"Listening..." state;
  * the session itself survives -- mode is untouched and the inactivity
                   timer is paused for the hold, so a long hold cannot time
                   the session out.

`hands_free.suspend_on_hold` (default true) gates all of it; false restores
the pre-fix behaviour for comparison.

Hands-free here is toggle command mode (WakeConsumer's toggle lane +
SessionModeManager), NOT the wake-word session path. No module-level
`import dictation` -- the two tests that need real DictationApp timer
methods import it inside the helper that binds them. No audio devices, no
Qt, no model.
"""
import threading
import time
import types
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.audio_engine.frame import FRAME_SIZE
from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara.session_modes import SessionMode
from samsara.streaming import DictatePreviewSession


# ---------------------------------------------------------------------------
# WakeConsumer toggle lane
# ---------------------------------------------------------------------------

def _make_consumer(*, hotkey_recording=False, suspend_on_hold=True,
                   mode=SessionMode.DICTATE):
    """Real WakeConsumer in toggle command mode, wired to a Mock engine/reader
    and an app double with EXPLICIT bools (a bare Mock() auto-creates truthy
    attributes, which would silently defeat the very guards under test)."""
    from samsara.audio_engine.ring import EMPTY

    engine = Mock()
    reader = Mock()
    reader.read_next = Mock(return_value=EMPTY)
    engine.register_consumer = Mock(return_value=reader)

    app = Mock()
    app.wake_word_active = True
    app._hotkey_recording = hotkey_recording
    app.command_mode_active = True          # hands-free session is ON
    app.ava_command_session_active = False
    app.config = {
        'command_mode': {'mode': 'toggle'},
        'hands_free': {'suspend_on_hold': suspend_on_hold},
        'wake_word_config': {'audio': {
            'speech_threshold': 0.01,
            'min_speech_duration': 0.2,
            'wake_detection_silence': 0.0,
        }},
    }
    app._session_mode_manager = types.SimpleNamespace(mode=mode)
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
    app._close_hands_free_capture_duck = Mock()
    app._open_hands_free_capture_duck = Mock(return_value=None)
    app._hands_free_capture_duck_token = None
    app._wake_session_lock = threading.RLock()

    wc = WakeConsumer(engine, app)
    return wc, reader, app


def _loud_frame(epoch=0):
    frame = Mock()
    frame.pcm = np.full(FRAME_SIZE, 20000, dtype=np.int16)
    frame.device_epoch = epoch
    frame.seq = 0
    return frame


class TestHoldStartDiscardsAccumulatedPartial:
    """Contract part 1: nothing half-captured survives the start of a hold."""

    def test_in_progress_toggle_utterance_is_discarded(self):
        wc, _reader, app = _make_consumer()
        wc._utterance_frames = [np.zeros(10, dtype=np.float32)]
        wc._buffer_rms_history = [0.1]
        app.is_speaking = True

        wc.discard_stale_wake_utterance()   # what start_recording() calls

        assert wc._utterance_frames == []
        assert wc._buffer_rms_history == []
        assert app.is_speaking is False

    def test_discard_also_empties_the_preview_snapshot_source(self):
        """The preview lane does NOT keep its own audio copy -- it snapshots
        the consumer's _utterance_frames. One discard therefore covers both
        lanes, which is what stops the held words reaching the preview box."""
        wc, _reader, app = _make_consumer()
        wc._utterance_frames = [np.full(FRAME_SIZE, 0.2, dtype=np.float32)] * 40
        assert wc.snapshot_dictate_preview_audio() is not None

        wc.discard_stale_wake_utterance()

        assert wc.snapshot_dictate_preview_audio() is None

    def test_never_flushes_the_discarded_utterance(self):
        """Discard, never commit -- a half utterance must not be dispatched."""
        wc, _reader, app = _make_consumer()
        wc._utterance_frames = [np.zeros(10, dtype=np.float32)]
        app.is_speaking = True
        wc._flush = Mock()

        wc.discard_stale_wake_utterance()

        wc._flush.assert_not_called()


class TestNoCaptureDuringHold:
    """Contract part 2: the toggle lane is fully deaf while the hold runs."""

    def test_no_frames_accumulated_and_no_vad_run(self):
        wc, _reader, app = _make_consumer(hotkey_recording=True)

        for _ in range(5):
            wc._process_frame(_loud_frame())

        assert wc._utterance_frames == []
        assert wc._buffer_rms_history == []
        assert app.is_speaking is False
        app._vad_is_speech.assert_not_called()

    def test_no_text_emitted_during_the_hold(self):
        """Nothing reaches the session dispatch path while suspended."""
        wc, _reader, app = _make_consumer(hotkey_recording=True)
        app._handle_command_mode_utterance = Mock()
        app.process_wake_word_buffer = Mock()

        for _ in range(8):
            wc._process_frame(_loud_frame())

        app._handle_command_mode_utterance.assert_not_called()
        app.process_wake_word_buffer.assert_not_called()

    def test_preview_snapshot_stays_empty_for_the_whole_hold(self):
        wc, _reader, app = _make_consumer(hotkey_recording=True)

        for _ in range(10):
            wc._process_frame(_loud_frame())

        assert wc.snapshot_dictate_preview_audio() is None


class TestResumeAfterHold:
    """Contract part 3: resume from the CURRENT position, no rewind."""

    def test_resumes_capturing_once_the_hold_ends(self):
        """First post-hold frame is the speech onset (its prebuffer replay
        reads the ring, which this fixture leaves empty); every frame after
        that accumulates normally again."""
        wc, _reader, app = _make_consumer(hotkey_recording=True)
        wc._process_frame(_loud_frame())
        assert wc._utterance_frames == []

        app._hotkey_recording = False
        wc._process_frame(_loud_frame())   # onset
        wc._process_frame(_loud_frame())   # accumulates

        assert app._vad_is_speech.call_count == 2
        assert app.is_speaking is True
        assert len(wc._utterance_frames) == 1, "toggle lane did not resume"

    def test_resumed_buffer_contains_no_pre_hold_audio(self):
        """The held audio must never be rewound into: the post-hold buffer
        holds exactly what the post-hold frames delivered -- the identical
        count a from-scratch capture produces, with no pre-hold carry-over."""
        wc, _reader, app = _make_consumer()
        # Pre-hold speech accumulates (onset + 2 = 2 frames buffered)...
        for _ in range(3):
            wc._process_frame(_loud_frame())
        assert len(wc._utterance_frames) == 2

        # ...hold starts: discarded, and nothing accrues while held.
        app._hotkey_recording = True
        wc.discard_stale_wake_utterance()
        for _ in range(5):
            wc._process_frame(_loud_frame())
        assert wc._utterance_frames == []

        # ...hold ends: the same 3 frames buffer the same 2, not 4.
        app._hotkey_recording = False
        for _ in range(3):
            wc._process_frame(_loud_frame())

        assert len(wc._utterance_frames) == 2


class TestSuspendOnHoldDisabledRestoresOldBehaviour:
    """`hands_free.suspend_on_hold: false` == the pre-fix double capture,
    kept only for comparison."""

    def test_toggle_lane_keeps_capturing_during_hold(self):
        wc, _reader, app = _make_consumer(hotkey_recording=True,
                                          suspend_on_hold=False)

        wc._process_frame(_loud_frame())

        app._vad_is_speech.assert_called_once()
        assert app.is_speaking is True

    def test_in_progress_utterance_is_not_discarded(self):
        wc, _reader, app = _make_consumer(suspend_on_hold=False)
        wc._utterance_frames = [np.zeros(10, dtype=np.float32)]
        app.is_speaking = True

        wc.discard_stale_wake_utterance()

        assert len(wc._utterance_frames) == 1
        assert app.is_speaking is True


# ---------------------------------------------------------------------------
# Inactivity timer: paused for the hold, resumed with the time that was left
# ---------------------------------------------------------------------------

def _make_timer_stub(*, active=True):
    """Real DictationApp hold-pause/resume methods bound onto a light stub.

    dictation is imported HERE, not at module scope, so merely collecting
    this file never imports the live app module."""
    import dictation as _d

    class _Stub:
        _pause_command_mode_inactivity_for_hold = (
            _d.DictationApp._pause_command_mode_inactivity_for_hold)
        _resume_command_mode_inactivity_after_hold = (
            _d.DictationApp._resume_command_mode_inactivity_after_hold)
        _cancel_command_mode_inactivity_timer_locked = (
            _d.DictationApp._cancel_command_mode_inactivity_timer_locked)

        def __init__(self):
            self.command_mode_active = active
            self._command_mode_timer_lock = threading.RLock()
            self._command_mode_inactivity_timer = None
            self._command_mode_inactivity_deadline = None
            self._command_mode_inactivity_remaining_on_hold = None
            self.reset_calls = []

        def _reset_command_mode_inactivity_timer(self, timeout_s):
            self.reset_calls.append(timeout_s)

    return _Stub()


class TestInactivityTimerPausedForHold:
    def test_pause_records_remaining_time_and_cancels_the_timer(self):
        stub = _make_timer_stub()
        timer = Mock()
        stub._command_mode_inactivity_timer = timer
        # 300s window with 270s still to run.
        stub._command_mode_inactivity_deadline = time.monotonic() + 270.0

        stub._pause_command_mode_inactivity_for_hold()

        timer.cancel.assert_called_once()
        assert stub._command_mode_inactivity_timer is None
        assert stub._command_mode_inactivity_remaining_on_hold == pytest.approx(270.0, abs=1.0)

    def test_resume_rearms_with_the_time_that_was_left_not_a_fresh_window(self):
        """A 30s hold against a 300s timeout resumes with ~270s left."""
        stub = _make_timer_stub()
        stub._command_mode_inactivity_timer = Mock()
        stub._command_mode_inactivity_deadline = time.monotonic() + 270.0
        stub._pause_command_mode_inactivity_for_hold()

        stub._resume_command_mode_inactivity_after_hold()

        assert len(stub.reset_calls) == 1
        assert stub.reset_calls[0] == pytest.approx(270.0, abs=1.0)
        assert stub.reset_calls[0] < 300.0, "resumed with a fresh full window"
        assert stub._command_mode_inactivity_remaining_on_hold is None

    def test_pause_with_no_timer_running_is_a_safe_noop(self):
        stub = _make_timer_stub()

        stub._pause_command_mode_inactivity_for_hold()
        stub._resume_command_mode_inactivity_after_hold()

        assert stub.reset_calls == []

    def test_resume_after_session_ended_does_not_revive_the_timer(self):
        stub = _make_timer_stub()
        stub._command_mode_inactivity_timer = Mock()
        stub._command_mode_inactivity_deadline = time.monotonic() + 120.0
        stub._pause_command_mode_inactivity_for_hold()
        stub.command_mode_active = False   # session ended during the hold

        stub._resume_command_mode_inactivity_after_hold()

        assert stub.reset_calls == []


class TestConsumerDrivesTheTimerPause:
    """The consumer's suppression engage/release is what calls the pause and
    resume, so a hold that outlives the inactivity window cannot end the
    session."""

    def test_engaging_suppression_pauses_the_timer(self):
        wc, _reader, app = _make_consumer(hotkey_recording=True)

        wc._process_frame(_loud_frame())
        wc._process_frame(_loud_frame())   # still held: pause fires once

        app._pause_command_mode_inactivity_for_hold.assert_called_once_with()

    def test_releasing_suppression_resumes_the_timer(self):
        wc, _reader, app = _make_consumer(hotkey_recording=True)
        wc._process_frame(_loud_frame())

        app._hotkey_recording = False
        wc._process_frame(_loud_frame())

        app._resume_command_mode_inactivity_after_hold.assert_called_once_with()

    def test_session_mode_is_never_ended_by_a_hold(self):
        wc, _reader, app = _make_consumer(hotkey_recording=True)

        for _ in range(5):
            wc._process_frame(_loud_frame())

        app.exit_command_mode.assert_not_called()
        assert app.command_mode_active is True

    def test_disabled_setting_never_touches_the_timer(self):
        wc, _reader, app = _make_consumer(hotkey_recording=True,
                                          suspend_on_hold=False)

        wc._process_frame(_loud_frame())

        app._pause_command_mode_inactivity_for_hold.assert_not_called()


# ---------------------------------------------------------------------------
# DictatePreviewSession: paused state, no decoding, clean resume
# ---------------------------------------------------------------------------

class _FakeOverlay:
    def __init__(self):
        self.transcripts = []
        self.paused = []

    def set_transcript(self, finalized, partial):
        self.transcripts.append((list(finalized), partial))

    def set_paused(self, finalized):
        self.paused.append(list(finalized))

    def show(self):
        pass

    def close(self):
        pass


def _make_preview(*, hotkey_recording=False, suspend_on_hold=True):
    app = types.SimpleNamespace(
        _hotkey_recording=hotkey_recording,
        config={'hands_free': {'suspend_on_hold': suspend_on_hold}},
    )
    preview = DictatePreviewSession.__new__(DictatePreviewSession)
    preview.app = app
    preview._closed = False
    preview._finalized = ["already dictated"]
    preview._generation = 0
    preview._stop_event = threading.Event()
    preview._overlay = _FakeOverlay()
    return preview, app


class TestPreviewSuspension:
    def test_suspended_only_while_holding_and_only_when_enabled(self):
        preview, app = _make_preview()
        assert preview._suspended_for_hold() is False

        app._hotkey_recording = True
        assert preview._suspended_for_hold() is True

        app.config['hands_free']['suspend_on_hold'] = False
        assert preview._suspended_for_hold() is False

    def test_entering_the_hold_shows_paused_once_and_bumps_generation(self):
        preview, _app = _make_preview(hotkey_recording=True)
        before = preview._generation

        was = preview._enter_hold_pause(False)
        was = preview._enter_hold_pause(was)   # still held: no second render

        assert was is True
        assert preview._overlay.paused == [["already dictated"]]
        assert preview._generation == before + 1

    def test_partial_decoded_during_a_hold_is_discarded_not_rendered(self):
        """The race this fix closes: the tick started its decode BEFORE the
        hold, and the loop is single-threaded, so it cannot notice the
        suspension until that (slow, model-lock-bound) decode returns. The
        resulting text belongs to pre-hold audio the consumer has ALREADY
        discarded, so emitting it would paint held-audio words over a box
        that must read "Paused (hold)"."""
        preview, app = _make_preview()
        preview._is_control_phrase = lambda text: False

        def _decode_then_hold_starts():
            app._hotkey_recording = True      # hold began mid-decode
            return "half an utterance"

        preview._transcribe_partial = _decode_then_hold_starts
        preview._stop_event.set()             # one tick only, then exit

        # FIRST_CHUNK_S wait is skipped because the stop event is already
        # set, so drive the post-decode path directly the way _loop does.
        text = preview._transcribe_partial()
        suspended_after_decode = preview._suspended_for_hold()

        assert text == "half an utterance"
        assert suspended_after_decode is True, "hold not observed after decode"
        # _loop must take the suspend branch here, never set_transcript.
        assert preview._overlay.transcripts == []

    def test_resume_returns_to_the_normal_transcript(self):
        preview, app = _make_preview(hotkey_recording=True)
        was = preview._enter_hold_pause(False)
        assert was is True

        app._hotkey_recording = False
        # _loop's resume branch.
        preview._overlay.set_transcript(list(preview._finalized), "")

        assert preview._overlay.transcripts[-1] == (["already dictated"], "")

    def test_empty_transcript_resume_reads_listening(self):
        """With nothing dictated yet the box returns to "Listening...", which
        is what StreamingOverlayQt renders for an empty transcript."""
        from samsara.streaming import StreamingOverlayQt

        overlay = StreamingOverlayQt(dim=False)
        captured = []
        overlay.update_text = lambda text, state=None: captured.append(text)

        overlay.set_transcript([], "")

        assert captured[-1] == "Listening..."
