"""Tests for samsara.stress_tests: the declarative dictation stress-test
battery used by the Stress Test Wizard.

Pure logic only -- no Qt, no audio, no real dictation. Exercises every
pass_criteria callable against synthetic DiagRecords/text, the dynamic
jargon-step builder, word-count tolerance math, and report generation.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara.diagnostics import DiagRecord
from samsara.stress_tests import (
    STEP_SHORT_WORD, STEP_SHORT_PHRASE, STEP_ACCIDENTAL_TAP, STEP_SILENT_HOLD,
    STEP_LONG_MONOLOGUE, STEP_HOMOPHONES, STEP_NUMBERS_PUNCT,
    STEP_QUIET_SPEECH, STEP_FAST_SPEECH,
    build_jargon_step, build_battery, generate_report,
    _LONG_MONOLOGUE, _HOMOPHONE_SENTENCE, _SHORT_PHRASE,
)


def _rec(**overrides) -> DiagRecord:
    kwargs = dict(
        mode="hotkey", audio_s=2.0, model_name="base", device="cpu",
        compute_type="int8", text="",
    )
    kwargs.update(overrides)
    return DiagRecord(**kwargs)


# ============================================================================
# Exact-match steps: short_word, short_phrase, numbers_punct, fast_speech
# ============================================================================

class TestExactMatchSteps:
    def test_short_word_pass(self):
        passed, reason = STEP_SHORT_WORD.pass_criteria(_rec(text="testing"), "testing")
        assert passed is True

    def test_short_word_pass_case_insensitive_and_punctuation(self):
        passed, reason = STEP_SHORT_WORD.pass_criteria(_rec(), "Testing.")
        assert passed is True

    def test_short_word_fail_wrong_word(self):
        passed, reason = STEP_SHORT_WORD.pass_criteria(_rec(), "resting")
        assert passed is False
        assert "resting" in reason

    def test_short_word_fail_no_text(self):
        passed, reason = STEP_SHORT_WORD.pass_criteria(_rec(), None)
        assert passed is False
        assert "No text captured" in reason

    def test_short_phrase_pass(self):
        passed, reason = STEP_SHORT_PHRASE.pass_criteria(_rec(), _SHORT_PHRASE)
        assert passed is True

    def test_short_phrase_fail(self):
        passed, reason = STEP_SHORT_PHRASE.pass_criteria(_rec(), "totally different words")
        assert passed is False

    def test_numbers_punct_pass(self):
        expected = "The meeting is at 3:30 PM on July 9th, room 12B."
        passed, reason = STEP_NUMBERS_PUNCT.pass_criteria(_rec(), expected)
        assert passed is True

    def test_numbers_punct_fail_reformatted_differently(self):
        passed, reason = STEP_NUMBERS_PUNCT.pass_criteria(
            _rec(), "The meeting is at three thirty on July ninth room twelve B"
        )
        assert passed is False

    def test_fast_speech_pass(self):
        passed, reason = STEP_FAST_SPEECH.pass_criteria(_rec(), _SHORT_PHRASE)
        assert passed is True

    def test_fast_speech_fail(self):
        passed, reason = STEP_FAST_SPEECH.pass_criteria(_rec(), "")
        assert passed is False


# ============================================================================
# No-output steps: accidental_tap, silent_hold
# ============================================================================

class TestNoOutputSteps:
    def test_accidental_tap_pass_nothing_happened(self):
        passed, reason = STEP_ACCIDENTAL_TAP.pass_criteria(None, "")
        assert passed is True
        assert "as expected" in reason

    def test_accidental_tap_pass_with_none_text(self):
        passed, reason = STEP_ACCIDENTAL_TAP.pass_criteria(None, None)
        assert passed is True

    def test_accidental_tap_fail_target_box_has_text(self):
        passed, reason = STEP_ACCIDENTAL_TAP.pass_criteria(None, "oops something typed")
        assert passed is False
        assert "oops something typed" in reason

    def test_accidental_tap_fail_diagnostics_recorded_hallucinated_text(self):
        rec = _rec(text="thank you for watching")
        passed, reason = STEP_ACCIDENTAL_TAP.pass_criteria(rec, "")
        assert passed is False
        assert "thank you for watching" in reason

    def test_silent_hold_shares_same_criteria_pass(self):
        passed, reason = STEP_SILENT_HOLD.pass_criteria(None, "")
        assert passed is True

    def test_silent_hold_shares_same_criteria_fail(self):
        passed, reason = STEP_SILENT_HOLD.pass_criteria(_rec(text="bloop"), "")
        assert passed is False


# ============================================================================
# long_monologue -- word-count tolerance math
# ============================================================================

class TestLongMonologueWordCountTolerance:
    def test_pass_exact_word_count(self):
        passed, reason = STEP_LONG_MONOLOGUE.pass_criteria(_rec(), _LONG_MONOLOGUE)
        assert passed is True

    def test_pass_within_40_percent_shorter(self):
        expected_words = len(_LONG_MONOLOGUE.split())
        # Truncate to ~70% of the expected words -> ~30% deviation, safely
        # inside the 40% tolerance -> must pass.
        truncated = " ".join(_LONG_MONOLOGUE.split()[: round(expected_words * 0.7)])
        passed, reason = STEP_LONG_MONOLOGUE.pass_criteria(_rec(), truncated)
        assert passed is True

    def test_fail_beyond_40_percent_shorter(self):
        expected_words = len(_LONG_MONOLOGUE.split())
        truncated = " ".join(_LONG_MONOLOGUE.split()[: int(expected_words * 0.3)])
        passed, reason = STEP_LONG_MONOLOGUE.pass_criteria(_rec(), truncated)
        assert passed is False
        assert "deviated" in reason

    def test_fail_no_text_captured(self):
        passed, reason = STEP_LONG_MONOLOGUE.pass_criteria(_rec(), None)
        assert passed is False
        assert "No text captured" in reason

    def test_fail_far_too_long(self):
        bloated = _LONG_MONOLOGUE + " " + _LONG_MONOLOGUE
        passed, reason = STEP_LONG_MONOLOGUE.pass_criteria(_rec(), bloated)
        assert passed is False


# ============================================================================
# homophones
# ============================================================================

class TestHomophones:
    def test_pass_all_correct(self):
        passed, reason = STEP_HOMOPHONES.pass_criteria(_rec(), _HOMOPHONE_SENTENCE)
        assert passed is True
        assert "correct" in reason.lower()

    def test_fail_one_homophone_swapped(self):
        wrong = "They're going to there car over their."
        passed, reason = STEP_HOMOPHONES.pass_criteria(_rec(), wrong)
        assert passed is False
        assert "Homophone" in reason

    def test_fail_no_text(self):
        passed, reason = STEP_HOMOPHONES.pass_criteria(_rec(), None)
        assert passed is False
        assert "No text captured" in reason

    def test_fail_unrelated_text(self):
        passed, reason = STEP_HOMOPHONES.pass_criteria(_rec(), "completely unrelated sentence here")
        assert passed is False


# ============================================================================
# quiet_speech -- behavioral, always "passes"
# ============================================================================

class TestQuietSpeechBehavioral:
    def test_always_passes_with_record(self):
        rec = _rec(no_speech_prob=0.05, avg_logprob=-0.2)
        passed, reason = STEP_QUIET_SPEECH.pass_criteria(rec, _SHORT_PHRASE)
        assert passed is True
        assert "no_speech_prob=0.05" in reason
        assert "avg_logprob=-0.2" in reason

    def test_always_passes_without_record(self):
        passed, reason = STEP_QUIET_SPEECH.pass_criteria(None, None)
        assert passed is True
        assert "informational" in reason.lower()


# ============================================================================
# Dynamic jargon step
# ============================================================================

class TestJargonStep:
    def test_omitted_when_no_voice_training_window(self):
        assert build_jargon_step(None) is None

    def test_omitted_when_vocab_and_corrections_empty(self):
        vt = types.SimpleNamespace(custom_vocab=[], corrections_dict={})
        assert build_jargon_step(vt) is None

    def test_built_from_custom_vocab(self):
        vt = types.SimpleNamespace(custom_vocab=["Kubernetes", "PyTorch"], corrections_dict={})
        step = build_jargon_step(vt)
        assert step is not None
        assert step.id == "jargon"
        assert "Kubernetes" in step.instruction
        assert "PyTorch" in step.instruction

    def test_built_from_corrections_values(self):
        vt = types.SimpleNamespace(custom_vocab=[], corrections_dict={"wisper": "whisper"})
        step = build_jargon_step(vt)
        assert step is not None
        assert "whisper" in step.instruction

    def test_capped_at_five_terms(self):
        vt = types.SimpleNamespace(
            custom_vocab=["one", "two", "three", "four", "five", "six", "seven"],
            corrections_dict={},
        )
        step = build_jargon_step(vt)
        # Count terms via the instruction's comma-separated list.
        term_count = step.instruction.split(":")[-1].count(",") + 1
        assert term_count == 5

    def test_dedup_case_insensitive(self):
        vt = types.SimpleNamespace(
            custom_vocab=["Kubernetes", "kubernetes"], corrections_dict={},
        )
        step = build_jargon_step(vt)
        term_count = step.instruction.split(":")[-1].count(",") + 1
        assert term_count == 1

    def test_pass_criteria_terms_present(self):
        vt = types.SimpleNamespace(custom_vocab=["Kubernetes", "PyTorch"], corrections_dict={})
        step = build_jargon_step(vt)
        passed, reason = step.pass_criteria(_rec(), "I deployed Kubernetes and PyTorch today")
        assert passed is True

    def test_pass_criteria_terms_missing(self):
        vt = types.SimpleNamespace(custom_vocab=["Kubernetes", "PyTorch"], corrections_dict={})
        step = build_jargon_step(vt)
        passed, reason = step.pass_criteria(_rec(), "I only mentioned Kubernetes")
        assert passed is False
        assert "PyTorch" in reason

    def test_pass_criteria_no_text(self):
        vt = types.SimpleNamespace(custom_vocab=["Kubernetes"], corrections_dict={})
        step = build_jargon_step(vt)
        passed, reason = step.pass_criteria(_rec(), None)
        assert passed is False


# ============================================================================
# build_battery -- ordering and dynamic insertion
# ============================================================================

class TestBuildBattery:
    def test_battery_without_vocab_omits_jargon(self):
        battery = build_battery(None)
        ids = [s.id for s in battery]
        assert "jargon" not in ids
        assert len(ids) == 9

    def test_battery_with_vocab_inserts_jargon_between_homophones_and_numbers(self):
        vt = types.SimpleNamespace(custom_vocab=["Kubernetes"], corrections_dict={})
        battery = build_battery(vt)
        ids = [s.id for s in battery]
        assert len(ids) == 10
        homophones_idx = ids.index("homophones")
        jargon_idx = ids.index("jargon")
        numbers_idx = ids.index("numbers_punct")
        assert homophones_idx < jargon_idx < numbers_idx

    def test_battery_order_matches_spec(self):
        battery = build_battery(None)
        ids = [s.id for s in battery]
        assert ids == [
            "short_word", "short_phrase", "accidental_tap", "silent_hold",
            "long_monologue", "homophones", "numbers_punct", "quiet_speech",
            "fast_speech",
        ]


# ============================================================================
# generate_report
# ============================================================================

class TestGenerateReport:
    def _result(self, step, passed, reason="reason", verdicts=None):
        return {'step': step, 'passed': passed, 'reason': reason, 'verdicts': verdicts or []}

    def test_summary_counts(self):
        results = [
            self._result(STEP_SHORT_WORD, True),
            self._result(STEP_SHORT_PHRASE, False),
            self._result(STEP_ACCIDENTAL_TAP, None),
        ]
        report = generate_report(results)
        assert "1 passed, 1 failed, 1 skipped (of 3)" in report

    def test_per_step_lines(self):
        results = [
            self._result(STEP_SHORT_WORD, True, reason="Exact match"),
            self._result(STEP_SHORT_PHRASE, False, reason="Expected X, got Y"),
            self._result(STEP_ACCIDENTAL_TAP, None, reason="Skipped"),
        ]
        report = generate_report(results)
        assert "[PASS] Single word: Exact match" in report
        assert "[FAIL] Short phrase: Expected X, got Y" in report
        assert "[SKIP] Accidental tap: Skipped" in report

    def test_verdicts_union_deduplicated_and_ok_excluded(self):
        results = [
            self._result(STEP_SHORT_WORD, True, verdicts=["OK"]),
            self._result(STEP_SHORT_PHRASE, False, verdicts=["Very low confidence", "Slow end-to-end (>3s)"]),
            self._result(STEP_LONG_MONOLOGUE, False, verdicts=["Very low confidence"]),
        ]
        report = generate_report(results)
        assert report.count("Very low confidence") == 1
        assert "Slow end-to-end (>3s)" in report
        assert "- OK" not in report

    def test_no_verdicts_message(self):
        results = [self._result(STEP_SHORT_WORD, True, verdicts=["OK"])]
        report = generate_report(results)
        assert "No non-OK diagnostics verdicts observed." in report

    def test_empty_results(self):
        report = generate_report([])
        assert "0 passed, 0 failed, 0 skipped (of 0)" in report
