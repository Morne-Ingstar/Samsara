"""Tests for samsara.diagnostics: per-utterance dictation pipeline diagnostics.

Pure-function coverage only -- no Qt, no audio, no real Ollama/model calls.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara.diagnostics import (
    DiagRecord, record, recent, clear, classify,
    add_one_shot_hook, remove_one_shot_hook,
)


# ============================================================================
# Helpers
# ============================================================================

def _baseline_kwargs(**overrides):
    """A DiagRecord field set with every classify() rule inactive, so a
    single overridden field triggers exactly one verdict."""
    kwargs = dict(
        mode="hotkey",
        audio_s=5.0,
        model_name="small",
        device="cpu",
        compute_type="int8",
        t_transcribe_ms=500,
        t_corrections_ms=10,
        t_smart_ms=-1,
        t_total_ms=600,
        avg_logprob=-0.3,
        compression_ratio=1.5,
        no_speech_prob=0.1,
        temperature=0.0,
        n_segments=1,
        text="hello world this is fine",
        smart_changed=False,
    )
    kwargs.update(overrides)
    return kwargs


def _rec(**overrides) -> DiagRecord:
    return DiagRecord(**_baseline_kwargs(**overrides))


def _make_app(write_jsonl=False):
    app = types.SimpleNamespace()
    app.config = {'diagnostics': {'write_jsonl': write_jsonl}}
    return app


@pytest.fixture(autouse=True)
def _clear_ring():
    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def _clear_hooks():
    import samsara.diagnostics as _diag
    _diag._one_shot_hooks.clear()
    yield
    _diag._one_shot_hooks.clear()


# ============================================================================
# classify() -- one test per rule, plus OK and multi-verdict
# ============================================================================

class TestClassify:
    def test_ok_when_nothing_fires(self):
        assert classify(_rec()) == ["OK"]

    def test_ultra_short_audio(self):
        verdicts = classify(_rec(audio_s=0.2))
        assert "Ultra-short audio — accidental hold?" in verdicts

    def test_likely_no_speech(self):
        verdicts = classify(_rec(no_speech_prob=0.9))
        assert "Likely no speech — hallucination risk" in verdicts

    def test_high_compression_ratio(self):
        verdicts = classify(_rec(compression_ratio=3.0))
        assert "High compression ratio — repetitive/hallucinated output likely" in verdicts

    def test_fallback_ladder_engaged(self):
        verdicts = classify(_rec(temperature=0.4))
        assert any("Fallback ladder engaged" in v for v in verdicts)
        assert any("0.40" in v for v in verdicts)

    def test_very_low_confidence(self):
        verdicts = classify(_rec(avg_logprob=-1.5))
        assert "Very low confidence" in verdicts

    def test_small_model_configured(self):
        for name in ("tiny", "tiny.en", "base", "base.en"):
            verdicts = classify(_rec(model_name=name))
            assert "Small model configured — accuracy limited" in verdicts

    def test_smart_corrections_is_slowest_stage(self):
        verdicts = classify(_rec(t_transcribe_ms=500, t_smart_ms=800))
        assert "Smart Corrections is the slowest stage" in verdicts

    def test_smart_slowest_not_flagged_when_smart_not_run(self):
        # t_smart_ms == -1 (not run) must never trigger the "slowest" rule.
        verdicts = classify(_rec(t_transcribe_ms=500, t_smart_ms=-1))
        assert "Smart Corrections is the slowest stage" not in verdicts

    def test_slow_end_to_end(self):
        verdicts = classify(_rec(t_total_ms=3500))
        assert "Slow end-to-end (>3s)" in verdicts

    def test_speech_produced_no_output(self):
        verdicts = classify(_rec(text="", audio_s=3.0))
        assert "Speech produced no output" in verdicts

    def test_empty_text_short_audio_not_flagged(self):
        # Empty text is expected for very short/silent buffers -- only
        # flag it when there was enough audio for speech to plausibly be in it.
        verdicts = classify(_rec(text="", audio_s=1.0))
        assert "Speech produced no output" not in verdicts

    def test_multi_verdict_case(self):
        verdicts = classify(_rec(audio_s=0.1, model_name="tiny", t_total_ms=4000))
        assert "Ultra-short audio — accidental hold?" in verdicts
        assert "Small model configured — accuracy limited" in verdicts
        assert "Slow end-to-end (>3s)" in verdicts
        assert len(verdicts) == 3


# ============================================================================
# FM3 (blank-transcription) diagnostics: outcome / path / n_segments
#
# Prior behavior: an empty hotkey transcription emitted NO diagnostics
# record at all -- the exact failure mode most needing a trail. These
# fields make it a first-class, distinctly-classified event.
# ============================================================================

class TestEmptyAndGatedOutcomes:
    def test_defaults_are_backward_compatible(self):
        """A record built the old way (no outcome/path kwargs) must still
        validate with safe defaults -- existing normal-result call sites
        are untouched by this change."""
        rec = _rec()
        assert rec.outcome == "ok"
        assert rec.path == ""

    def test_empty_outcome_zero_segments_recorded_and_classified(self):
        """Zero segments = the model returned nothing at all."""
        record(_rec(outcome="empty", path="long", n_segments=0, text=""))

        stored = recent()[0]
        assert stored.outcome == "empty"
        assert stored.path == "long"
        assert stored.n_segments == 0
        assert "Empty result — model returned zero segments" in stored.verdicts

    def test_empty_outcome_nonzero_segments_recorded_and_classified(self):
        """Non-zero segments with empty text = segments came back but were
        suppressed/blank (hallucination guard or native no_speech/log_prob
        thresholds) -- must be distinguishable from the zero-segments case."""
        record(_rec(outcome="empty", path="short", n_segments=3, text=""))

        stored = recent()[0]
        assert stored.outcome == "empty"
        assert stored.path == "short"
        assert stored.n_segments == 3
        assert "Empty result — segments present but text suppressed/blank" in stored.verdicts

    def test_gated_outcome_recorded_and_classified_separately_from_empty(self):
        """A buffer gated by _buffer_has_contiguous_speech never reached the
        model -- outcome must be "gated", never conflated with "empty"."""
        record(_rec(outcome="gated", path="", n_segments=0, text=""))

        stored = recent()[0]
        assert stored.outcome == "gated"
        assert stored.outcome != "empty"
        assert "Gated upstream — no contiguous speech detected before transcription" in stored.verdicts

    def test_generic_no_output_verdict_does_not_double_fire_for_empty_outcome(self):
        # The pre-existing generic rule would otherwise also fire here
        # (text="" and audio_s>2) -- the more specific FM3 verdict must be
        # the only one describing the empty result.
        verdicts = classify(_rec(outcome="empty", n_segments=0, text="", audio_s=5.0))
        assert "Speech produced no output" not in verdicts
        assert "Empty result — model returned zero segments" in verdicts

    def test_generic_no_output_verdict_unaffected_for_ok_outcome(self):
        # Regression guard: an "ok"-outcome record (any other empty-text
        # call site that isn't FM3-tagged) keeps the original behavior.
        verdicts = classify(_rec(text="", audio_s=5.0))
        assert "Speech produced no output" in verdicts

    def test_low_confidence_outcome_recorded_and_classified_distinctly(self):
        """dictation._apply_segment_quality_gates's never-empty floor
        (every segment failed quality but the decode was delivered
        anyway) must be distinguishable from a normal "ok" delivery --
        see the long-dictation quality rework."""
        record(_rec(
            outcome="low_confidence", path="short", n_segments=2,
            text="paper bringing to make the page I guess",
        ))

        stored = recent()[0]
        assert stored.outcome == "low_confidence"
        assert any(
            "Low-confidence delivery" in v for v in stored.verdicts
        )
        # Must not be conflated with a normal delivery's silence on outcome.
        assert stored.outcome != "ok"


# ============================================================================
# Ring buffer
# ============================================================================

class TestRingBuffer:
    def test_ring_buffer_caps_at_200_newest_retained(self):
        for i in range(250):
            record(_rec(text=f"utterance-{i}"))

        items = recent()

        assert len(items) == 200
        assert items[-1].text == "utterance-249"   # newest last
        assert items[0].text == "utterance-50"     # oldest 50 evicted

    def test_clear_empties_the_buffer(self):
        record(_rec(text="one"))
        assert len(recent()) == 1
        clear()
        assert recent() == []


# ============================================================================
# JSONL persistence -- must never affect dictation on failure
# ============================================================================

class TestJsonlWrite:
    def test_write_failure_does_not_raise_and_ring_still_appended(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))

        def _raise(*a, **kw):
            raise OSError("simulated disk failure")

        monkeypatch.setattr("builtins.open", _raise)

        app = _make_app(write_jsonl=True)
        record(_rec(text="should survive"), app=app)

        items = recent()
        assert len(items) == 1
        assert items[0].text == "should survive"

    def test_write_disabled_by_default_skips_jsonl(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        app = _make_app(write_jsonl=False)

        record(_rec(text="ring only"), app=app)

        assert not (tmp_path / "diagnostics.jsonl").exists()
        assert len(recent()) == 1

    def test_write_enabled_appends_json_line(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        app = _make_app(write_jsonl=True)

        record(_rec(text="persisted"), app=app)

        jsonl_path = tmp_path / "diagnostics.jsonl"
        assert jsonl_path.exists()
        content = jsonl_path.read_text(encoding="utf-8")
        assert "persisted" in content

    def test_record_without_app_skips_jsonl_gate(self):
        # app=None must be a valid call -- ring-buffer-only usage (e.g. tests).
        record(_rec(text="no app passed"))
        assert len(recent()) == 1


# ============================================================================
# Text truncation
# ============================================================================

class TestTextTruncation:
    def test_text_truncated_to_200_chars(self):
        long_text = "x" * 500
        record(_rec(text=long_text))

        stored = recent()[0]
        assert len(stored.text) == 200
        assert stored.text == "x" * 200

    def test_short_text_untouched(self):
        record(_rec(text="short"))
        assert recent()[0].text == "short"


# ============================================================================
# Language field (multilingual dictation Task 6)
# ============================================================================

class TestLanguageField:
    def test_defaults_to_empty_string(self):
        assert _rec().language == ""

    def test_populated_field_round_trips_through_record(self):
        record(_rec(language="de"))
        assert recent()[0].language == "de"

    def test_describe_diagnostics_language_configured_code(self):
        from samsara.languages import describe_diagnostics_language
        assert describe_diagnostics_language("de") == "de"
        assert describe_diagnostics_language("en") == "en"

    def test_describe_diagnostics_language_auto_with_detection(self):
        from samsara.languages import describe_diagnostics_language
        assert describe_diagnostics_language("auto", "fr") == "auto->fr"

    def test_describe_diagnostics_language_auto_without_detection(self):
        from samsara.languages import describe_diagnostics_language
        assert describe_diagnostics_language("auto", None) == "auto"


# ============================================================================
# One-shot completion hooks -- the Stress Test Wizard's capture tap point
# ============================================================================

class TestOneShotHooks:
    def test_hook_fires_on_next_record(self):
        calls = []
        add_one_shot_hook(calls.append)

        record(_rec(text="hello"))

        assert len(calls) == 1
        assert calls[0].text == "hello"

    def test_hook_fires_exactly_once_then_deregisters(self):
        calls = []
        add_one_shot_hook(calls.append)

        record(_rec(text="first"))
        record(_rec(text="second"))

        assert len(calls) == 1
        assert calls[0].text == "first"

    def test_remove_before_fire_prevents_call(self):
        calls = []
        add_one_shot_hook(calls.append)
        remove_one_shot_hook(calls.append)

        record(_rec(text="ignored"))

        assert calls == []

    def test_remove_is_idempotent_no_error_if_never_registered(self):
        remove_one_shot_hook(lambda rec: None)  # must not raise

    def test_remove_is_idempotent_no_error_if_already_fired(self):
        calls = []
        add_one_shot_hook(calls.append)
        record(_rec(text="fires"))
        assert len(calls) == 1

        remove_one_shot_hook(calls.append)  # already auto-deregistered -- must not raise

    def test_multiple_hooks_all_fire_for_same_record(self):
        calls_a, calls_b = [], []
        add_one_shot_hook(calls_a.append)
        add_one_shot_hook(calls_b.append)

        record(_rec(text="broadcast"))

        assert len(calls_a) == 1 and len(calls_b) == 1

    def test_hook_exception_does_not_break_record_or_other_hooks(self):
        calls = []

        def _raises(rec):
            raise RuntimeError("boom")

        add_one_shot_hook(_raises)
        add_one_shot_hook(calls.append)

        record(_rec(text="survives"))  # must not raise into the caller

        assert len(calls) == 1
        assert len(recent()) == 1
