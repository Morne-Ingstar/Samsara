"""Tests for samsara.cleanup.clean_text -- deterministic post-transcription
filler-word/spacing cleanup.

No prior test file existed for this module. Written 2026-07-10 alongside the
fix for the "you know" unanchored-filler defect (samsara/cleanup.py FILLERS:
r'\\byou know\\b' -> r'\\byou know\\b(?=,)') -- this was the actual root
cause of a hotkey word-loss defect ("you know what I mean" -> "What I
mean.") previously misattributed to audio-layer/decode-parameter theories.
Raw Whisper output was always correct; this cleanup rule silently deleted
the phrase from every position, filler or not.
"""
import pytest

from samsara.cleanup import clean_text, FILLERS


class TestYouKnowFillerCommaAnchored:
    """The regression this whole test file exists to guard."""

    def test_you_know_what_i_mean_survives_intact(self):
        assert clean_text("you know what I mean") == "You know what I mean."

    def test_you_know_mid_sentence_without_comma_survives(self):
        assert clean_text("do you know what I mean") == "Do you know what I mean."

    def test_you_know_with_comma_still_strips(self):
        assert clean_text("It's, you know, complicated") == "It's, complicated."

    def test_you_know_trailing_comma_filler_still_strips(self):
        assert clean_text("so I went there, you know, and left") == \
            "So I went there, and left."

    def test_you_know_at_start_with_comma_still_strips(self):
        assert clean_text("you know, that's not right") == "That's not right."


class TestOtherFillersUnchanged:
    """um/uh (unanchored, always stripped) and the comma-anchored fillers
    (like/I mean/basically/actually/so) must behave exactly as before --
    this fix touches ONLY the you-know pattern."""

    def test_um_stripped_anywhere(self):
        assert clean_text("I um went to the store") == "I went to the store."

    def test_uh_stripped_anywhere(self):
        assert clean_text("uh let's go") == "Let's go."

    def test_like_with_comma_stripped(self):
        assert clean_text("it was, like, huge") == "It was, huge."

    def test_like_meaningful_use_survives(self):
        assert clean_text("it looks like a bug") == "It looks like a bug."

    def test_i_mean_with_comma_stripped(self):
        assert clean_text("I mean, that's fair") == "That's fair."

    def test_i_mean_without_comma_survives(self):
        assert clean_text("I mean it this time") == "I mean it this time."

    def test_basically_with_comma_stripped(self):
        assert clean_text("basically, it works") == "It works."

    def test_actually_with_comma_stripped(self):
        assert clean_text("actually, no") == "No."

    def test_so_with_comma_stripped(self):
        assert clean_text("so, anyway") == "Anyway."

    def test_so_meaningful_use_survives(self):
        assert clean_text("I'm so tired") == "I'm so tired."


class TestVerbatimModeBypassesAllCleanup:
    def test_verbatim_returns_text_unchanged(self):
        text = "you know, um, like, whatever"
        assert clean_text(text, mode="verbatim") == text


class TestEmptyAndNoneInput:
    def test_empty_string_returned_unchanged(self):
        assert clean_text("") == ""

    def test_none_returned_unchanged(self):
        assert clean_text(None) is None


class TestFillersListStructure:
    def test_you_know_pattern_is_comma_anchored(self):
        assert r'\byou know\b(?=,)' in FILLERS

    def test_um_and_uh_remain_unanchored(self):
        assert r'\bum\b' in FILLERS
        assert r'\buh\b' in FILLERS
