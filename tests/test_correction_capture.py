"""Tests for samsara.correction_capture: atomic-substitution extraction for
the correction-capture hotkey feature.

Pure-function coverage only -- no Qt, no I/O. Tribunal-mandated strictness:
these tests exist to prove the extractor never offers a rewrite, a bare
insertion/deletion, or a case/punctuation-only edit as a learnable pair.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara.correction_capture import extract_corrections


class TestSingleWordSwap:
    def test_single_word_substitution_is_learnable(self):
        result = extract_corrections("I like cats", "I like dogs")
        assert result.learnable == [("cats", "dogs")]
        assert result.rejected == []


class TestTwoWordPhraseSwap:
    def test_two_word_phrase_substitution_is_learnable(self):
        result = extract_corrections(
            "the export report is ready", "the sales summary is ready",
        )
        assert result.learnable == [("export report", "sales summary")]
        assert result.rejected == []


class TestAdjacentMerge:
    def test_replaces_separated_by_one_equal_word_are_merged(self):
        result = extract_corrections("red apple green pear", "big apple small pear")
        assert result.learnable == [("red apple green", "big apple small")]
        assert result.rejected == []

    def test_replaces_separated_by_two_equal_words_stay_independent(self):
        # Gap of 2+ equal words must NOT merge -- each replace is its own
        # independent candidate (see TestMultipleIndependentSubstitutions
        # for the fuller version of this case).
        result = extract_corrections(
            "red apple green mango pear", "big apple green cherry pear",
        )
        assert ("red", "big") in result.learnable
        assert ("mango", "cherry") in result.learnable
        assert len(result.learnable) == 2


class TestPunctuationOnlyRejected:
    def test_punctuation_only_difference_is_rejected(self):
        # Embedded in a longer sentence so the whole-text edit ratio stays
        # under the rewrite gate and the punctuation-only check is reached.
        result = extract_corrections(
            "the meeting starts at hello world today afternoon",
            "the meeting starts at hello, world! today afternoon",
        )
        assert result.learnable == []
        assert result.rejected == [
            ("hello world", "hello, world!", "punctuation-only difference"),
        ]


class TestCaseOnlyRejected:
    def test_case_only_difference_is_rejected(self):
        result = extract_corrections(
            "the meeting starts at hello world today afternoon",
            "the meeting starts at Hello World today afternoon",
        )
        assert result.learnable == []
        assert result.rejected == [
            ("hello world", "Hello World", "case-only difference"),
        ]


class TestFullRewriteYieldsZeroLearnable:
    def test_mostly_different_text_yields_no_learnable_pairs(self):
        result = extract_corrections(
            "the quick brown fox jumps over the lazy dog",
            "a slow green turtle crawls under the sleepy cat",
        )
        assert result.learnable == []
        assert result.rejected  # at least one span reported
        assert all(reason == "looks like a rewrite" for _w, _r, reason in result.rejected)

    def test_edit_ratio_below_threshold_is_not_treated_as_rewrite(self):
        # Sanity check the gate isn't overly aggressive: a single-word swap
        # in a short sentence must still be learnable.
        result = extract_corrections("I like cats", "I like dogs")
        assert result.learnable == [("cats", "dogs")]

    def test_max_edit_ratio_is_configurable(self):
        # A modest edit (1 of 4 words) exceeds a very strict custom ratio.
        result = extract_corrections("I like cats today", "I like dogs today", max_edit_ratio=0.1)
        assert result.learnable == []
        assert result.rejected


class TestPureInsertionRejected:
    def test_pure_insertion_is_rejected_not_learnable(self):
        result = extract_corrections("call me tomorrow", "please call me tomorrow")
        assert result.learnable == []
        assert result.rejected == [
            ("", "please", "insertion with no corresponding original text"),
        ]

    def test_pure_deletion_is_rejected_not_learnable(self):
        result = extract_corrections("please call me tomorrow now", "please call me now")
        assert result.learnable == []
        assert ("tomorrow", "", "deletion with no replacement text") in result.rejected


class TestMultipleIndependentSubstitutions:
    def test_two_independent_substitutions_both_learnable(self):
        result = extract_corrections(
            "I have a red car and a blue house",
            "I have a green car and a yellow house",
        )
        assert set(result.learnable) == {("red", "green"), ("blue", "yellow")}
        assert result.rejected == []


class TestFourWordCapBoundary:
    def test_four_word_phrase_is_within_cap_and_learnable(self):
        result = extract_corrections(
            "the team will review flag alpha bravo charlie the results next week",
            "the team will review mark one two three the results next week",
        )
        assert result.learnable == [("flag alpha bravo charlie", "mark one two three")]
        assert result.rejected == []

    def test_five_word_phrase_exceeds_cap_and_is_rejected(self):
        result = extract_corrections(
            "the team will review flag alpha bravo charlie delta the results next week",
            "the team will review mark one two three four the results next week",
        )
        assert result.learnable == []
        assert result.rejected == [
            ("flag alpha bravo charlie delta", "mark one two three four",
             "too long -- looks like a rewrite"),
        ]


class TestEdgeCases:
    def test_identical_text_yields_nothing(self):
        result = extract_corrections("same text here", "same text here")
        assert result.learnable == []
        assert result.rejected == []

    def test_both_empty_yields_nothing(self):
        result = extract_corrections("", "")
        assert result.learnable == []
        assert result.rejected == []

    def test_never_raises_on_none_like_empty_strings(self):
        result = extract_corrections("", "some text")
        assert result.learnable == []
        # entirely an insertion against empty original
        assert result.rejected
