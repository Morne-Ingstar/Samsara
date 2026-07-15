"""Hallucination gating for the hands-free toggle command-mode utterance path
(_handle_command_mode_utterance), which previously had none.

Root cause (confirmed 2026-07-15 against ~/.samsara/logs/samsara.log, real
production evidence): this path calls self.model.transcribe() directly and
dispatches straight to SessionModeManager.dispatch_utterance() -- it never
shared _is_hallucinated_segments/_apply_segment_quality_gates with the
hotkey path. Whisper hallucinates a trailing run of underscores on the
near-silent tail after real speech at a toggle utterance's silence
boundary, e.g. '"the __________"' (2026-07-15 13:44:55) and
'"ready for <hundreds of underscores>"' (2026-07-15 03:00:58) -- both taken
verbatim from production log lines.

Two layers under test:
  1. _trim_trailing_garbage_run -- pure-function unit tests.
  2. _handle_command_mode_utterance -- integration tests with a stubbed
     model/manager, asserting dispatch_utterance never receives underscore
     garbage and that normal utterances are byte-identical to before.
"""

import sys
import types
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation
from samsara.session_modes import DispatchOutcome, SessionMode


def _seg(text, compression_ratio=1.0, no_speech_prob=0.0):
    return types.SimpleNamespace(
        text=text, compression_ratio=compression_ratio, no_speech_prob=no_speech_prob,
    )


# =============================================================================
# _trim_trailing_garbage_run -- pure function
# =============================================================================

class TestTrimTrailingGarbageRun:

    def test_production_case_the_underscores(self):
        """Verbatim from samsara.log 2026-07-15 13:44:55."""
        assert dictation._trim_trailing_garbage_run("the __________") == "the"

    def test_production_case_ready_for_long_run(self):
        """Verbatim shape from samsara.log 2026-07-15 03:00:58 (hundreds of
        underscores in production; a shorter run here is representative)."""
        text = "ready for " + ("_" * 200)
        assert dictation._trim_trailing_garbage_run(text) == "ready for"

    def test_production_case_a_underscores(self):
        """Verbatim from samsara.log 2026-07-15 12:33:25."""
        assert dictation._trim_trailing_garbage_run("a ______________") == "a"

    def test_no_trailing_garbage_returns_unchanged(self):
        text = "please schedule the meeting for tomorrow afternoon"
        assert dictation._trim_trailing_garbage_run(text) == text

    def test_pure_garbage_trims_to_empty(self):
        assert dictation._trim_trailing_garbage_run("_" * 20) == ""

    def test_trailing_dashes_trimmed(self):
        assert dictation._trim_trailing_garbage_run("open the file------") == "open the file"

    def test_trailing_periods_trimmed(self):
        assert dictation._trim_trailing_garbage_run("start recording......") == "start recording"

    def test_five_repeats_below_threshold_not_trimmed(self):
        """Threshold is 6+ repeats -- 5 must not trip it (avoids false
        positives on things like a short emphatic '-----' that isn't the
        Whisper hallucination pattern actually observed)."""
        text = "hello -----"
        assert dictation._trim_trailing_garbage_run(text) == text

    def test_six_repeats_at_threshold_trimmed(self):
        text = "hello ------"
        assert dictation._trim_trailing_garbage_run(text) == "hello"

    def test_ellipsis_not_mistaken_for_garbage(self):
        """Three-dot ellipsis is well under the 6-repeat threshold and must
        survive untouched -- real speech legitimately trails off like this."""
        text = "so I was thinking..."
        assert dictation._trim_trailing_garbage_run(text) == text

    def test_embedded_underscore_run_not_at_end_untouched(self):
        """Only a TRAILING run is trimmed -- mid-string garbage (a
        previously-staged chunk's leftovers) is out of scope for this
        per-utterance trim and must not be mutated."""
        text = "click here __________ then continue"
        assert dictation._trim_trailing_garbage_run(text) == text


# =============================================================================
# _drop_trailing_garbage_segments -- pure function
# =============================================================================

class TestDropTrailingGarbageSegments:

    def test_pure_garbage_segment_dropped(self):
        segs = [_seg("hello there"), _seg("_" * 30)]
        kept = dictation._drop_trailing_garbage_segments(segs)
        assert kept == [segs[0]]

    def test_mixed_real_and_garbage_segment_also_dropped(self):
        """A single segment mixing real words with a trailing garbage run
        (e.g. Whisper's actual "ready for <underscores>" production case)
        must be dropped from the telemetry list entirely -- its own
        compression_ratio reflects the UNTRIMMED text, so it can't be
        trusted even though it contains some real words."""
        segs = [_seg("ready for " + "_" * 200, compression_ratio=10.0)]
        kept = dictation._drop_trailing_garbage_segments(segs)
        assert kept == []

    def test_clean_trailing_segment_kept(self):
        segs = [_seg("hello there")]
        kept = dictation._drop_trailing_garbage_segments(segs)
        assert kept == segs

    def test_only_trailing_segments_dropped_not_earlier_ones(self):
        """A garbage segment sandwiched between real speech is out of
        scope -- only a trailing run of garbage segments is dropped."""
        segs = [_seg("_" * 30), _seg("real words here")]
        kept = dictation._drop_trailing_garbage_segments(segs)
        assert kept == segs


# =============================================================================
# _handle_command_mode_utterance -- integration
# =============================================================================

def _make_app(seg_list, *, mode=SessionMode.DICTATE, dispatch_outcome=None):
    """Minimal DictationApp stand-in wired just enough to drive
    _handle_command_mode_utterance end to end with a fake Whisper decode."""
    from dictation import DictationApp

    app = DictationApp.__new__(DictationApp)
    app._wake_transcription_in_progress = False
    app.model_rate = 16000
    app.model_lock = Mock()
    app.model_lock.__enter__ = Mock(return_value=None)
    app.model_lock.__exit__ = Mock(return_value=False)
    app.model = Mock()
    app.model.transcribe = Mock(return_value=(seg_list, types.SimpleNamespace()))
    app.get_transcription_params = Mock(return_value={})
    app.voice_training_window = Mock()
    app.voice_training_window.apply_corrections = Mock(side_effect=lambda t: t)
    app._command_mode_ghost_tap = False
    app._compute_switch_gate_signals = Mock(return_value=Mock())
    app._handle_session_dispatch_outcome = Mock()
    app.play_sound = Mock()
    app._vad_reset = Mock()

    manager = Mock()
    manager.mode = mode
    manager.dispatch_utterance = Mock(
        return_value=dispatch_outcome or DispatchOutcome(kind="dictate_staged", detail={}),
    )
    app._ensure_session_mode_manager = Mock(return_value=manager)

    return app, manager


def _buffer_for(duration_s=1.0, rate=16000):
    return [np.zeros(int(duration_s * rate), dtype=np.float32)]


class TestCommandModeUtteranceHallucinationGating:

    def test_trailing_underscores_trimmed_real_words_dispatched(self):
        """The exact production shape: real words followed by a long
        underscore run. Must dispatch the trimmed text, never underscores."""
        seg_list = [_seg("the __________")]
        app, manager = _make_app(seg_list)

        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)

        manager.dispatch_utterance.assert_called_once()
        dispatched_text = manager.dispatch_utterance.call_args[0][0]
        assert dispatched_text == "the"
        assert "_" not in dispatched_text

    def test_pure_hallucination_no_real_words_rejected_outright(self):
        """No real preceding words -- mirrors the existing whole-decode
        hallucination test pattern (test_hallucination_blacklist.py):
        a bare blacklisted phrase must be rejected before dispatch."""
        seg_list = [_seg("Thank you for watching!")]
        app, manager = _make_app(seg_list)

        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)

        manager.dispatch_utterance.assert_not_called()

    def test_high_compression_garbage_segment_does_not_poison_whole_check(self):
        """Regression lock for the design bug caught during implementation:
        a segment that's mostly a long underscore run has a REAL, high
        compression_ratio (verified ~10x for the exact production shape
        via zlib) -- comfortably past Signature A's 3.0 threshold. If that
        segment's telemetry were left in the seg_list handed to
        _is_hallucinated_segments, Signature A would reject the WHOLE
        utterance (including "ready for") before the trim ever had a
        chance to run. Must dispatch the trimmed real words instead."""
        seg_list = [_seg("ready for " + "_" * 200, compression_ratio=10.0)]
        app, manager = _make_app(seg_list)

        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)

        manager.dispatch_utterance.assert_called_once()
        dispatched_text = manager.dispatch_utterance.call_args[0][0]
        assert dispatched_text == "ready for"
        assert "_" not in dispatched_text

    def test_pure_underscore_only_utterance_rejected_outright(self):
        """Trimming a pure-garbage utterance leaves nothing to dispatch --
        must reject, not dispatch an empty string."""
        seg_list = [_seg("_" * 30)]
        app, manager = _make_app(seg_list)

        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)

        manager.dispatch_utterance.assert_not_called()

    def test_normal_clean_utterance_unaffected(self):
        """Regression lock: a normal utterance must dispatch with
        byte-identical text to before this change -- the common case must
        never be touched."""
        seg_list = [_seg("please schedule the meeting for tomorrow afternoon")]
        app, manager = _make_app(seg_list)

        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)

        manager.dispatch_utterance.assert_called_once()
        dispatched_text = manager.dispatch_utterance.call_args[0][0]
        assert dispatched_text == "please schedule the meeting for tomorrow afternoon"

    def test_suppressed_hallucination_logged_at_info(self, caplog):
        seg_list = [_seg("Thank you for watching!")]
        app, manager = _make_app(seg_list)

        with caplog.at_level("INFO"):
            dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)

        assert any(
            "[GUARD] Suppressed hallucination" in r.message for r in caplog.records
        )

    def test_trim_logged_at_info(self, caplog):
        seg_list = [_seg("the __________")]
        app, manager = _make_app(seg_list)

        with caplog.at_level("INFO"):
            dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)

        assert any(
            "[GUARD] Trimmed trailing garbage" in r.message for r in caplog.records
        )

    def test_rejection_never_calls_dispatch_utterance(self):
        """A prior DICTATE stage lives entirely inside SessionModeManager's
        _dictate_pending_buffer, mutated only from within
        dispatch_utterance. Rejected text here never reaches
        dispatch_utterance at all, so any text already staged from earlier
        utterances in this session cannot be affected by this gate."""
        seg_list = [_seg("_" * 30)]
        app, manager = _make_app(seg_list)

        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)

        manager.dispatch_utterance.assert_not_called()
        assert manager.method_calls == []

    def test_ghost_tap_still_discards_after_gating_passes(self):
        """Existing ghost-tap handling must survive unchanged, downstream
        of the new gates."""
        seg_list = [_seg("hello there")]
        app, manager = _make_app(seg_list)
        app._command_mode_ghost_tap = True

        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)

        manager.dispatch_utterance.assert_not_called()
        assert app._command_mode_ghost_tap is False

    def test_exception_after_gating_still_earcons_and_stays_alive(self):
        """The try/except/finally zombie-proofing guarantee must survive
        this change -- an exception during dispatch still earcons and
        leaves the session alive."""
        seg_list = [_seg("hello there")]
        app, manager = _make_app(seg_list)
        manager.dispatch_utterance = Mock(side_effect=RuntimeError("boom"))

        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)

        app.play_sound.assert_called_once_with("error")
        assert app._wake_transcription_in_progress is False
        app._vad_reset.assert_called_once()
