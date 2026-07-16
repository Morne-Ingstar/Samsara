"""Tests for the segment-level quality/hallucination gating that replaced
the >30s silence-splitter.

Background (2026-07-15): _split_audio_at_silences() cut long recordings
into independent chunks, stripping acoustic/semantic context. With
condition_on_previous_text=False forced on every hotkey decode (see
tests/test_transcription_params.py), each chunk was treated as a brand-new
file; isolated fragments produced degraded avg_logprob/compression_ratio
versus the same audio decoded whole, and the per-chunk quality gates
(tuned against whole-recording statistics) rejected the degraded fragments
and silently zeroed them. A 55s capture split into 24.9s+24.9s+5.2s chunks
this way produced only 198 chars; the SAME audio decoded in one call
returned the complete transcript.

dictation.py now decodes everything up to _LONG_DECODE_CEILING_S (180s, a
resource guard, not a quality boundary) in a single model.transcribe()
call, and gates quality/hallucination per SEGMENT
(dictation._apply_segment_quality_gates) instead of on the whole decode's
aggregate -- so one bad segment costs only that segment, not the entire
recording, and a decode where every segment fails quality is delivered as
low-confidence text (reusing dictation._keep_low_confidence_long_chunk's
"plausible long dictation" judgment from commit d5d6b2d) rather than
silently returning "".

Covers:
  - Real fixture regression: the 55s/96s/39s captures that used to be
    gutted by silence-splitting now decode completely in one call
    (TestLongDictationFixtureRegression -- needs a real model load).
  - No path can silently return "" on quality grounds alone
    (TestNeverSilentlyEmptyOnQualityGrounds -- pure-function, no model).
  - Confirmed hallucination still legitimately returns "" -- the floor
    does not override it (also in that class).
  - Hallucination suppression still fires: whole-decode (cross-segment
    repetition) AND per-segment (isolated single-segment garbage)
    (TestHallucinationSurvivesSegmentLevelGating).
  - Segment-level gating drops only the failing segment, not the whole
    decode (TestSegmentLevelGatingDropsOnlyFailingSegment).
  - The common short-audio case (a single clean segment) is unchanged
    (TestCommonCaseUnchanged).
  - _LONG_DECODE_CEILING_S and the >ceiling splitter fallback are both
    still present (TestLongDecodeCeilingAndSplitterPreserved).

Pure-function tests use the same duck-typed segment / import pattern as
test_hallucination_blacklist.py and test_quality_exhaustion_guard.py: no
audio, no model load, `import dictation` directly.
"""
import sys
import types
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "audio"

# Mirrors what _build_hotkey_transcribe_params() actually produces (same
# pattern as test_quality_exhaustion_guard.py's _REAL_TRANSCRIBE_PARAMS).
_REAL_TRANSCRIBE_PARAMS = {
    'log_prob_threshold': dictation._LOGPROB_THRESHOLD,
    'no_speech_threshold': dictation._NO_SPEECH_THRESHOLD,
}


def _seg(text="", avg_logprob=-0.3, compression_ratio=1.5, no_speech_prob=0.1, temperature=0.0):
    return types.SimpleNamespace(
        text=text,
        avg_logprob=avg_logprob,
        compression_ratio=compression_ratio,
        no_speech_prob=no_speech_prob,
        temperature=temperature,
    )


def _load_wav_float32(path):
    with wave.open(str(path), "rb") as wf:
        n_frames = wf.getnframes()
        rate = wf.getframerate()
        raw = wf.readframes(n_frames)
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return audio, rate


# ============================================================================
# Real fixture regression -- needs a real faster-whisper model load.
# ============================================================================

@pytest.mark.skipif(not FIXTURES_DIR.exists(), reason="audio fixtures not present")
class TestLongDictationFixtureRegression:
    """Decodes the real regression captures with the actual hotkey-path
    transcribe parameters (see _build_hotkey_transcribe_params), on CPU so
    this never contends with a live Samsara process's GPU. Skipped
    entirely if faster_whisper/the model isn't available."""

    @pytest.fixture(scope="class")
    def model(self):
        fw = pytest.importorskip("faster_whisper")
        try:
            return fw.WhisperModel("medium", device="cpu", compute_type="int8")
        except Exception as exc:
            pytest.skip(f"medium model unavailable: {exc}")

    def _decode(self, model, filename):
        path = FIXTURES_DIR / filename
        audio, rate = _load_wav_float32(path)
        assert rate == dictation.MODEL_SAMPLE_RATE
        audio_faded = dictation._fade_edges(audio, rate)
        params = dict(
            language="en",
            initial_prompt="",
            no_speech_threshold=dictation._NO_SPEECH_THRESHOLD,
            log_prob_threshold=dictation._LOGPROB_THRESHOLD,
            beam_size=3,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        segments, info = model.transcribe(audio_faded, **params)
        seg_list = list(segments)
        return seg_list, len(audio) / rate

    def test_55s_capture_decodes_to_substantially_complete_text(self, model):
        """The exact regression: silence-splitting cut this into
        24.9s+24.9s+5.2s chunks and produced only 198 chars total (the
        middle chunk's content vanished). A single decode, gated per
        segment, must recover it -- well above the old 198-char result."""
        seg_list, duration = self._decode(model, "long_dictation_55s.wav")
        text, low_confidence = dictation._apply_segment_quality_gates(
            seg_list, _REAL_TRANSCRIBE_PARAMS, duration,
        )
        assert len(text) > 300, f"only {len(text)} chars: {text!r}"
        # Known middle content: exactly the kind of material that fell in
        # the (silently dropped) middle chunk under the old 25s split.
        assert "different fonts" in text
        assert low_confidence is False

    def test_96s_capture_decodes_completely(self, model):
        seg_list, duration = self._decode(model, "long_dictation_96s.wav")
        text, low_confidence = dictation._apply_segment_quality_gates(
            seg_list, _REAL_TRANSCRIBE_PARAMS, duration,
        )
        assert len(text) > 600, f"only {len(text)} chars: {text!r}"
        assert "solo developer" in text
        assert low_confidence is False

    def test_39s_capture_no_longer_splits_and_decodes_completely(self, model):
        """Logged historically as "[LONG] 39.0s recording split into 2
        chunk(s) at silence boundaries" (2026-07-15 09:20:36). 39s is past
        the OLD 30s split threshold but under the new 180s ceiling, so
        this must now stay a single decode."""
        path = FIXTURES_DIR / "long_dictation_39s.wav"
        audio, rate = _load_wav_float32(path)
        duration = len(audio) / rate
        assert duration > 30.0
        assert duration <= dictation._LONG_DECODE_CEILING_S

        seg_list, _ = self._decode(model, "long_dictation_39s.wav")
        text, low_confidence = dictation._apply_segment_quality_gates(
            seg_list, _REAL_TRANSCRIBE_PARAMS, duration,
        )
        assert len(text) > 150, f"only {len(text)} chars: {text!r}"
        assert "Samsara history" in text
        assert low_confidence is False


# ============================================================================
# Never silently return "" on quality grounds -- pure function, no model.
# ============================================================================

class TestNeverSilentlyEmptyOnQualityGrounds:
    def test_all_segments_failing_quality_but_plausible_delivers_low_confidence(self):
        """Regression for the core bug this task fixes: previously an
        aggregate-quality-exhausted decode was discarded entirely (see
        commit d5d6b2d, fixed at chunk granularity; this generalizes that
        fix to the whole decode). Mirrors the real dropped-chunk incident
        (avg_logprob -1.82, compression 1.1, sustained real content) but
        split across two segments that BOTH individually fail quality."""
        text = (
            "paper bringing to make the page I guess and I just wanted you "
            "to look it over make sure"
        )
        words = text.split()
        half = len(words) // 2
        seg1 = _seg(
            text=" " + " ".join(words[:half]),
            avg_logprob=-1.82, compression_ratio=1.1, no_speech_prob=0.12,
        )
        seg2 = _seg(
            text=" " + " ".join(words[half:]),
            avg_logprob=-1.9, compression_ratio=1.1, no_speech_prob=0.12,
        )
        assert dictation._is_quality_exhausted([seg1], _REAL_TRANSCRIBE_PARAMS) is True
        assert dictation._is_quality_exhausted([seg2], _REAL_TRANSCRIBE_PARAMS) is True

        result_text, low_confidence = dictation._apply_segment_quality_gates(
            [seg1, seg2], _REAL_TRANSCRIBE_PARAMS, 10.8,
        )
        assert result_text.strip() == text
        assert low_confidence is True

    def test_floor_does_not_bypass_hard_safety_limits(self):
        """Mirrors _keep_low_confidence_long_chunk's own safety limits
        (here: compression far past the ceiling) -- not every
        quality-exhausted decode is floored into delivery, only ones
        that also look like plausible sustained dictation."""
        seg = _seg(
            text=" this is real enough text",
            avg_logprob=-5.0, compression_ratio=2.5, no_speech_prob=0.1,
        )
        assert dictation._is_quality_exhausted([seg], _REAL_TRANSCRIBE_PARAMS) is True
        text, low_confidence = dictation._apply_segment_quality_gates(
            [seg], _REAL_TRANSCRIBE_PARAMS, 10.0,
        )
        assert text == ""
        assert low_confidence is False

    def test_confirmed_hallucination_legitimately_returns_empty(self):
        """A decode that IS entirely hallucination must still return ""
        -- the never-empty floor applies only to quality rejection, never
        to confirmed hallucination."""
        seg = _seg(
            text=" click click click click",
            avg_logprob=-0.2, compression_ratio=1.0, no_speech_prob=0.9,
        )
        text, low_confidence = dictation._apply_segment_quality_gates(
            [seg], _REAL_TRANSCRIBE_PARAMS, 10.0,
        )
        assert text == ""
        assert low_confidence is False


# ============================================================================
# Hallucination detection: whole-decode (cross-segment) AND per-segment.
# ============================================================================

class TestHallucinationSurvivesSegmentLevelGating:
    def test_whole_decode_repetition_across_many_segments_still_suppressed(self):
        """Cross-segment repetition (the model echoing the same phrase
        across many segments) is only visible at the whole-decode level --
        no single segment here looks like a hallucination in isolation,
        but the reassembled text is degenerate repetition."""
        segs = [_seg(text=" the cat sat", avg_logprob=-0.2, compression_ratio=1.1,
                       no_speech_prob=0.2) for _ in range(6)]
        text, low_confidence = dictation._apply_segment_quality_gates(
            segs, _REAL_TRANSCRIBE_PARAMS, 15.0,
        )
        assert text == ""
        assert low_confidence is False

    def test_isolated_single_segment_garbage_dropped_without_losing_the_rest(self):
        """The subtle part: an isolated near-silent 'click' segment
        embedded inside an otherwise-real long recording. The whole-decode
        check alone would likely miss this (diluted by the surrounding
        real speech); per-segment detection must still catch it, and must
        drop ONLY that segment."""
        good1 = _seg(
            text=" the meeting is scheduled for tomorrow afternoon",
            avg_logprob=-0.2, compression_ratio=1.2, no_speech_prob=0.1,
        )
        bad = _seg(
            text=" click click click",
            avg_logprob=-0.3, compression_ratio=1.0, no_speech_prob=0.9,
        )
        good2 = _seg(
            text=" please confirm the room booking",
            avg_logprob=-0.25, compression_ratio=1.3, no_speech_prob=0.1,
        )
        text, low_confidence = dictation._apply_segment_quality_gates(
            [good1, bad, good2], _REAL_TRANSCRIBE_PARAMS, 20.0,
        )
        assert "meeting is scheduled" in text
        assert "confirm the room booking" in text
        assert "click" not in text
        assert low_confidence is False

    def test_isolated_bloop_near_silent_segment_dropped(self):
        """Reproduced failure mode: a near-silent hold producing
        'bloop bloop bloop' -- corroborated by high no_speech_prob, so
        real emphatic speech ('no no no') is not falsely caught (that case
        has LOW no_speech_prob and is untouched -- see
        test_hallucination_blacklist.py's Signature D documentation)."""
        bloop = _seg(
            text=" bloop bloop bloop",
            avg_logprob=-0.3, compression_ratio=1.0, no_speech_prob=0.95,
        )
        real = _seg(
            text=" no no I meant the other file",
            avg_logprob=-0.2, compression_ratio=1.1, no_speech_prob=0.05,
        )
        text, low_confidence = dictation._apply_segment_quality_gates(
            [bloop, real], _REAL_TRANSCRIBE_PARAMS, 12.0,
        )
        assert "bloop" not in text
        assert "no no I meant the other file" in text
        assert low_confidence is False


# ============================================================================
# Segment-level gating drops only the failing segment, not the whole decode.
# ============================================================================

class TestSegmentLevelGatingDropsOnlyFailingSegment:
    def test_one_quality_exhausted_segment_among_good_segments_only_drops_that_one(self):
        good1 = _seg(
            text=" the first part of this dictation is perfectly clear",
            avg_logprob=-0.2, compression_ratio=1.2, no_speech_prob=0.1,
        )
        bad = _seg(
            text=" mumbled unclear something",
            avg_logprob=-1.8, compression_ratio=1.2, no_speech_prob=0.2,
        )
        good2 = _seg(
            text=" and the last part is clear again too",
            avg_logprob=-0.25, compression_ratio=1.1, no_speech_prob=0.1,
        )
        assert dictation._is_quality_exhausted([bad], _REAL_TRANSCRIBE_PARAMS) is True

        text, low_confidence = dictation._apply_segment_quality_gates(
            [good1, bad, good2], _REAL_TRANSCRIBE_PARAMS, 20.0,
        )
        assert "first part of this dictation is perfectly clear" in text
        assert "and the last part is clear again too" in text
        assert "mumbled" not in text
        # Some good text survived, so this is a normal (not low-confidence)
        # delivery -- the floor only marks low_confidence when EVERY
        # segment failed quality.
        assert low_confidence is False


# ============================================================================
# Common short-audio case: a single clean segment is unchanged.
# ============================================================================

class TestCommonCaseUnchanged:
    def test_single_clean_segment_unchanged(self):
        """The overwhelmingly common hold-to-dictate case: one segment,
        healthy signals. Whole-decode and per-segment gating are
        mathematically identical for a single segment, so this is
        byte-identical to the pre-change short (<30s) path."""
        seg = _seg(
            text=" please schedule the meeting for tomorrow afternoon",
            avg_logprob=-0.3, compression_ratio=1.4, no_speech_prob=0.1,
        )
        text, low_confidence = dictation._apply_segment_quality_gates(
            [seg], _REAL_TRANSCRIBE_PARAMS, 4.0,
        )
        assert text == "please schedule the meeting for tomorrow afternoon"
        assert low_confidence is False

    def test_empty_segment_list_returns_empty(self):
        text, low_confidence = dictation._apply_segment_quality_gates(
            [], _REAL_TRANSCRIBE_PARAMS, 0.0,
        )
        assert text == ""
        assert low_confidence is False


# ============================================================================
# The ceiling constant and the >ceiling splitter fallback are both preserved.
# ============================================================================

class TestLongDecodeCeilingAndSplitterPreserved:
    def test_ceiling_constant_is_180_seconds(self):
        assert dictation._LONG_DECODE_CEILING_S == 180.0

    def test_split_audio_at_silences_still_present_for_ceiling_fallback(self):
        """Not deleted -- still used beyond _LONG_DECODE_CEILING_S so a
        runaway recording can't exhaust memory in one decode call."""
        assert callable(dictation._split_audio_at_silences)
        audio = np.zeros(16000 * 10, dtype=np.float32)
        chunks = dictation._split_audio_at_silences(audio, 16000)
        assert len(chunks) == 1  # under its own internal max_chunk_s, untouched

    def test_split_audio_at_silences_actually_splits_beyond_its_own_window(self):
        rng = np.random.default_rng(0)
        audio = (rng.standard_normal(16000 * 40) * 0.1).astype(np.float32)
        # Force a clean silence gap around the midpoint so there is a
        # guaranteed split point.
        audio[16000 * 19:16000 * 21] = 0.0
        chunks = dictation._split_audio_at_silences(audio, 16000)
        assert len(chunks) >= 2
        assert sum(len(c) for c in chunks) == len(audio)
