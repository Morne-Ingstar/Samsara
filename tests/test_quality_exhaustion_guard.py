"""Tests for dictation._is_quality_exhausted -- the threshold-exhaustion
delivery guard added 2026-07-10.

Production incident this fixes: an 11.7s blank hotkey hold failed
faster-whisper's OWN log_prob_threshold at every temperature in its
fallback ladder (0.0/0.2/0.4/0.6/1.0) and blew past its compression_ratio
ceiling at 0.8 (7.125 vs 2.4) -- but faster-whisper returns the final
failed attempt anyway rather than nothing, and nothing downstream checked
these already-computed quality signals before delivering the text
("Thank you for watching!" -- see test_hallucination_blacklist.py for the
companion blacklist-coverage fix for that exact phrase).

Pure-function tests against a minimal duck-typed segment list -- no audio,
no model load, no full Samsara boot (same import pattern as
test_hallucination_blacklist.py: `import dictation` directly).
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation


def _seg(avg_logprob=-0.3, compression_ratio=1.5, no_speech_prob=0.1, temperature=0.0):
    return types.SimpleNamespace(
        avg_logprob=avg_logprob,
        compression_ratio=compression_ratio,
        no_speech_prob=no_speech_prob,
        temperature=temperature,
    )


# Mirrors what _build_hotkey_transcribe_params() actually produces --
# log_prob_threshold comes straight from the module-level _LOGPROB_THRESHOLD
# constant via get_transcription_params(), unmodified by the hotkey path.
_REAL_TRANSCRIBE_PARAMS = {'log_prob_threshold': dictation._LOGPROB_THRESHOLD}


class TestQualityExhaustedRejectsBadDecodes:
    def test_production_incident_values_are_rejected(self):
        """The exact numbers from the 2026-07-10 log: avg_logprob -1.46
        (worst of the temps tried) and compression_ratio 7.125 (the temp
        0.8 blowup) -- both fail, either alone would be enough."""
        seg = _seg(avg_logprob=-1.46, compression_ratio=7.125, temperature=0.8)
        assert dictation._is_quality_exhausted([seg], _REAL_TRANSCRIBE_PARAMS) is True

    def test_failing_logprob_alone_is_enough(self):
        seg = _seg(avg_logprob=-2.36, compression_ratio=1.0)
        assert dictation._is_quality_exhausted([seg], _REAL_TRANSCRIBE_PARAMS) is True

    def test_failing_compression_ratio_alone_is_enough(self):
        seg = _seg(avg_logprob=-0.1, compression_ratio=7.125)
        assert dictation._is_quality_exhausted([seg], _REAL_TRANSCRIBE_PARAMS) is True

    def test_borderline_just_past_threshold_fails(self):
        seg = _seg(avg_logprob=-1.0001, compression_ratio=1.0)
        assert dictation._is_quality_exhausted([seg], _REAL_TRANSCRIBE_PARAMS) is True

    def test_multi_segment_any_failing_segment_rejects(self):
        """Worst-across-segments semantics (matches diagnostics.
        segment_signals) -- one bad segment among several good ones still
        rejects the whole decode."""
        good = _seg(avg_logprob=-0.2, compression_ratio=1.2)
        bad = _seg(avg_logprob=-1.8, compression_ratio=1.2)
        assert dictation._is_quality_exhausted([good, bad], _REAL_TRANSCRIBE_PARAMS) is True


class TestQualityExhaustedPassesHealthyDecodes:
    def test_healthy_markers_are_not_rejected(self):
        """Delivered unchanged: a normal-confidence decode at temp 0.0."""
        seg = _seg(avg_logprob=-0.35, compression_ratio=1.4, temperature=0.0)
        assert dictation._is_quality_exhausted([seg], _REAL_TRANSCRIBE_PARAMS) is False

    def test_borderline_just_within_threshold_passes(self):
        seg = _seg(avg_logprob=-0.9999, compression_ratio=2.3999)
        assert dictation._is_quality_exhausted([seg], _REAL_TRANSCRIBE_PARAMS) is False

    def test_exactly_at_threshold_passes(self):
        """Strict inequality in both directions -- exactly-at-threshold is
        the pass boundary, matching faster-whisper's own >= acceptance."""
        seg = _seg(avg_logprob=-1.0, compression_ratio=2.4)
        assert dictation._is_quality_exhausted([seg], _REAL_TRANSCRIBE_PARAMS) is False

    def test_real_long_dictation_segments_pass(self):
        """Regression-locks the constraint: real speech's segment signals
        (comfortably confident, low compression) must never trip this,
        long or short. This is the same shape a genuine multi-chunk [LONG]
        dictation's per-chunk segments would carry."""
        chunks = [
            _seg(avg_logprob=-0.25, compression_ratio=1.3),
            _seg(avg_logprob=-0.41, compression_ratio=1.6),
            _seg(avg_logprob=-0.18, compression_ratio=1.1),
        ]
        for seg in chunks:
            assert dictation._is_quality_exhausted([seg], _REAL_TRANSCRIBE_PARAMS) is False


class TestQualityExhaustedEdgeCases:
    def test_empty_segment_list_never_fires(self):
        assert dictation._is_quality_exhausted([], _REAL_TRANSCRIBE_PARAMS) is False

    def test_missing_avg_logprob_does_not_crash_or_fire_on_that_signal(self):
        seg = types.SimpleNamespace(compression_ratio=1.0)  # no avg_logprob attr at all
        assert dictation._is_quality_exhausted([seg], _REAL_TRANSCRIBE_PARAMS) is False

    def test_missing_log_prob_threshold_key_does_not_crash(self):
        seg = _seg(avg_logprob=-5.0, compression_ratio=1.0)
        assert dictation._is_quality_exhausted([seg], {}) is False  # nothing to compare against

    def test_uses_the_actual_configured_threshold_not_a_hardcoded_one(self):
        """Reads log_prob_threshold from the passed-in params, not a
        module constant baked into the function -- a stricter/looser
        configured threshold must change the verdict."""
        seg = _seg(avg_logprob=-1.5, compression_ratio=1.0)
        assert dictation._is_quality_exhausted([seg], {'log_prob_threshold': -1.0}) is True
        assert dictation._is_quality_exhausted([seg], {'log_prob_threshold': -2.0}) is False
