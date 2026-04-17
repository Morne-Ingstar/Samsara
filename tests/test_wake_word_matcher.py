"""
Tests for samsara.wake_word_matcher.match_wake_phrase.
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara.wake_word_matcher import match_wake_phrase


class TestExactMatch:
    def test_exact(self):
        assert match_wake_phrase("samsara", "samsara") == (True, "exact", 0)

    def test_exact_case_insensitive(self):
        assert match_wake_phrase("Samsara", "samsara") == (True, "exact", 0)

    def test_exact_with_whitespace(self):
        assert match_wake_phrase("  samsara  ", "samsara") == (True, "exact", 0)


class TestPrefixMatch:
    def test_prefix_with_space(self):
        m, t, i = match_wake_phrase("samsara dictate hello", "samsara")
        assert m is True and t == "prefix" and i == 0

    def test_prefix_with_comma(self):
        m, t, i = match_wake_phrase("samsara, do something", "samsara")
        assert m is True and t == "prefix" and i == 0


class TestSuffixMatch:
    def test_suffix(self):
        m, t, i = match_wake_phrase("hey samsara", "samsara")
        assert m is True and t == "suffix"

    def test_suffix_with_period(self):
        m, t, i = match_wake_phrase("hello samsara.", "samsara")
        assert m is True


class TestTokenMatch:
    def test_middle(self):
        m, t, i = match_wake_phrase("hey samsara dictate", "samsara")
        assert m is True and t == "token" and i == 4

    def test_comma_bounded(self):
        m, t, i = match_wake_phrase("hey samsara, dictate", "samsara")
        assert m is True and t == "token"

    def test_multi_word(self):
        m, t, i = match_wake_phrase("ok hey samsara open chrome", "hey samsara")
        assert m is True and t == "token" and i == 3


class TestSubstringReject:
    """Substring-only matches must NOT trigger (matched=False)."""

    def test_hyphenated_compound(self):
        m, t, i = match_wake_phrase("samsara-like thing", "samsara")
        assert m is False and t == "substring"

    def test_possessive(self):
        m, t, i = match_wake_phrase("samsara's great", "samsara")
        assert m is False and t == "substring"

    def test_concatenated(self):
        m, t, i = match_wake_phrase("prosamsara mode", "samsara")
        assert m is False and t == "substring"


class TestNoMatch:
    def test_absent(self):
        assert match_wake_phrase("hello world", "samsara") == (False, "none", -1)

    def test_empty_text(self):
        assert match_wake_phrase("", "samsara") == (False, "none", -1)

    def test_empty_phrase(self):
        assert match_wake_phrase("hello", "") == (False, "none", -1)
