"""Tests for samsara.diagnostics_verdict.verdict(): the plain-English
health-summary function behind the Dictation Diagnostics panel's header
band (samsara/ui/diagnostics_qt.py).

Pure-function coverage only -- synthetic DiagRecords, no Qt, no real
dictation. `records` only needs to duck-type the fields verdict() reads,
so a couple of tests use plain namespaces to prove that contract.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara.diagnostics import DiagRecord
from samsara.diagnostics_verdict import (
    verdict, WINDOW_N, HIGH_NO_SPEECH_PROB, HIGH_GATED_EMPTY_RATIO,
    LOW_LOGPROB, SLOW_REALTIME_FACTOR,
)


def _rec(**overrides) -> DiagRecord:
    kwargs = dict(
        mode="hotkey", audio_s=2.0, model_name="base", device="cpu",
        compute_type="int8", text="hello world",
        no_speech_prob=0.1, avg_logprob=-0.2, outcome="ok",
        t_transcribe_ms=500,
    )
    kwargs.update(overrides)
    return DiagRecord(**kwargs)


def _healthy_batch(n=10):
    return [_rec() for _ in range(n)]


# ============================================================================
# Empty input
# ============================================================================

class TestEmpty:
    def test_empty_list(self):
        headline, detail = verdict([])
        assert "no dictation activity" in headline.lower()
        assert detail

    def test_headline_and_detail_are_both_nonempty_strings(self):
        headline, detail = verdict([])
        assert isinstance(headline, str) and headline
        assert isinstance(detail, str) and detail


# ============================================================================
# Healthy branch
# ============================================================================

class TestHealthy:
    def test_healthy_audio_and_model(self):
        headline, detail = verdict(_healthy_batch())
        assert "healthy" in headline.lower()
        assert "no jargon" not in headline.lower()  # sanity: plain-language headline
        assert "logprob" not in headline.lower()
        assert "no_speech" not in headline.lower()

    def test_headline_has_no_numbers_detail_may(self):
        headline, detail = verdict(_healthy_batch())
        assert not any(ch.isdigit() for ch in headline)


# ============================================================================
# Weak audio branch -- high no_speech_prob
# ============================================================================

class TestWeakAudioNoSpeech:
    def test_high_median_no_speech_prob_triggers_weak_audio(self):
        records = [_rec(no_speech_prob=HIGH_NO_SPEECH_PROB + 0.2) for _ in range(WINDOW_N)]
        headline, detail = verdict(records)
        assert "weak or noisy" in headline.lower()
        assert "no-speech probability" in detail.lower()

    def test_just_under_threshold_does_not_trigger(self):
        records = [_rec(no_speech_prob=HIGH_NO_SPEECH_PROB - 0.1, avg_logprob=-0.1)
                   for _ in range(WINDOW_N)]
        headline, _ = verdict(records)
        assert "weak or noisy" not in headline.lower()


# ============================================================================
# Weak audio branch -- many gated/empty outcomes
# ============================================================================

class TestWeakAudioGatedEmpty:
    def test_high_gated_empty_ratio_triggers_weak_audio_even_with_good_no_speech_prob(self):
        n_bad = int(WINDOW_N * (HIGH_GATED_EMPTY_RATIO + 0.2))
        records = (
            [_rec(outcome="gated", no_speech_prob=None, avg_logprob=None, text="")
             for _ in range(n_bad)]
            + [_rec() for _ in range(WINDOW_N - n_bad)]
        )
        headline, detail = verdict(records)
        assert "weak or noisy" in headline.lower()
        assert f"{n_bad}/{WINDOW_N}" in detail

    def test_low_gated_empty_ratio_does_not_trigger(self):
        records = [_rec(outcome="empty", text="") for _ in range(1)] + [_rec() for _ in range(9)]
        headline, _ = verdict(records)
        assert "weak or noisy" not in headline.lower()


# ============================================================================
# Model-struggling branch -- only reachable when audio is NOT weak
# ============================================================================

class TestModelStruggling:
    def test_low_median_logprob_on_clean_audio(self):
        records = [_rec(avg_logprob=LOW_LOGPROB - 0.3) for _ in range(WINDOW_N)]
        headline, detail = verdict(records)
        assert "struggling" in headline.lower()
        assert "larger model" in detail.lower()
        assert "vocabulary" in detail.lower()

    def test_just_above_threshold_does_not_trigger(self):
        records = [_rec(avg_logprob=LOW_LOGPROB + 0.2) for _ in range(WINDOW_N)]
        headline, _ = verdict(records)
        assert "struggling" not in headline.lower()


# ============================================================================
# Mixed -- weak audio takes priority over model-confidence diagnosis
# ============================================================================

class TestMixed:
    def test_weak_audio_and_low_logprob_together_reports_audio_first(self):
        records = [
            _rec(no_speech_prob=HIGH_NO_SPEECH_PROB + 0.2, avg_logprob=LOW_LOGPROB - 0.5)
            for _ in range(WINDOW_N)
        ]
        headline, _ = verdict(records)
        assert "weak or noisy" in headline.lower()
        assert "struggling" not in headline.lower()


# ============================================================================
# Window size -- only the most recent WINDOW_N records matter
# ============================================================================

class TestWindowSize:
    def test_only_last_window_n_records_considered(self):
        stale_bad = [_rec(no_speech_prob=0.95) for _ in range(50)]
        recent_healthy = _healthy_batch(WINDOW_N)
        headline, _ = verdict(stale_bad + recent_healthy)
        assert "healthy" in headline.lower()

    def test_fewer_than_window_n_records_still_analyzed(self):
        headline, _ = verdict(_healthy_batch(3))
        assert "healthy" in headline.lower()


# ============================================================================
# Latency footnote
# ============================================================================

class TestLatencyFootnote:
    def test_slow_median_rtf_appends_footnote(self):
        records = [
            _rec(audio_s=1.0, t_transcribe_ms=int((SLOW_REALTIME_FACTOR + 1.0) * 1000))
            for _ in range(WINDOW_N)
        ]
        _, detail = verdict(records)
        assert "latency note" in detail.lower()

    def test_fast_transcription_no_footnote(self):
        records = [_rec(audio_s=5.0, t_transcribe_ms=500) for _ in range(WINDOW_N)]
        _, detail = verdict(records)
        assert "latency note" not in detail.lower()


# ============================================================================
# Duck-typing -- verdict() only needs the fields it reads, not a real
# DiagRecord (documented contract in the module docstring)
# ============================================================================

class TestDuckTyping:
    def test_plain_namespace_works(self):
        ns = types.SimpleNamespace(
            no_speech_prob=0.1, outcome="ok", avg_logprob=-0.2,
            audio_s=2.0, t_transcribe_ms=500,
        )
        headline, _ = verdict([ns] * WINDOW_N)
        assert "healthy" in headline.lower()

    def test_missing_optional_fields_default_gracefully(self):
        ns = types.SimpleNamespace(no_speech_prob=None, outcome="ok", avg_logprob=None)
        headline, detail = verdict([ns])
        assert isinstance(headline, str) and headline
