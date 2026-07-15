"""Tests for samsara.formatting_tokens: inline spoken-formatting substitution
applied to DICTATE output ("new line" -> \\n, "new paragraph" -> \\n\\n,
"insert tab" -> \\t, "bullet"/"bullet point" -> "\\n• ").
"""
import pytest

from samsara.formatting_tokens import (
    apply_formatting_tokens,
    apply_formatting_tokens_if_enabled,
)


class TestEachTokenSubstitutes:
    def test_new_line(self):
        assert apply_formatting_tokens("hello new line world") == "hello\nworld"

    def test_new_paragraph(self):
        assert apply_formatting_tokens("hello new paragraph world") == "hello\n\nworld"

    def test_tab(self):
        assert apply_formatting_tokens("hello insert tab world") == "hello\tworld"

    def test_bullet(self):
        assert apply_formatting_tokens("hello bullet world") == "hello\n• world"

    def test_bullet_point(self):
        assert apply_formatting_tokens("hello bullet point world") == "hello\n• world"


class TestCaseInsensitivity:
    @pytest.mark.parametrize("text,expected", [
        ("NEW LINE", "\n"),
        ("New Line", "\n"),
        ("hello NEW PARAGRAPH world", "hello\n\nworld"),
        ("hello INSERT TAB world", "hello\tworld"),
        ("hello Bullet Point world", "hello\n• world"),
    ])
    def test_case_insensitive_match(self, text, expected):
        assert apply_formatting_tokens(text) == expected


class TestLongestMatchFirst:
    def test_bullet_point_not_split_into_bullet_plus_literal_point(self):
        # If "bullet" were tried first, this would produce
        # "\n•  point" (leftover literal "point"). Must match the
        # full "bullet point" phrase instead.
        assert apply_formatting_tokens("notes bullet point") == "notes\n• "

    def test_new_paragraph_not_split_into_new_line_plus_literal(self):
        assert apply_formatting_tokens("notes new paragraph") == "notes\n\n"

    def test_bullet_alone_still_matches_when_point_does_not_follow(self):
        assert apply_formatting_tokens("notes bullet one") == "notes\n• one"


class TestLiteralTabPreservation:
    @pytest.mark.parametrize("text", [
        "tab",
        "the word tab should remain visible",
        "hello tab world",
        "open a new tab",
        "switch to the next tab",
        "go back to the previous tab",
        "open browser tab",
        "close the tab",           # preceded by "the"
        "open a tab",              # preceded by "a"
        "press the tab key",       # followed by "key"
        "hit tab key to switch",   # followed by "key"
    ])
    def test_ordinary_tab_stays_literal(self, text):
        assert apply_formatting_tokens(text) == text

    def test_explicit_insert_tab_substitutes(self):
        assert apply_formatting_tokens("hello insert tab world") == "hello\tworld"

    @pytest.mark.parametrize("text", [
        "enter", "return", "space", "escape", "backspace", "delete",
        "home", "page up", "page down", "shift", "control", "alt",
    ])
    def test_other_keyboard_words_are_not_formatting_tokens(self, text):
        assert apply_formatting_tokens(text) == text

    @pytest.mark.parametrize("text", ["tablet", "tabbed", "tabs"])
    def test_words_containing_tab_are_unchanged(self, text):
        assert apply_formatting_tokens(text) == text


class TestSpaceCleanup:
    def test_single_space_removed_on_both_sides(self):
        assert apply_formatting_tokens("hello new line world") == "hello\nworld"

    def test_no_double_space_left_around_tab(self):
        assert apply_formatting_tokens("col1 insert tab col2") == "col1\tcol2"

    def test_no_double_space_left_around_bullet_mid_utterance(self):
        assert apply_formatting_tokens("notes bullet first item") == "notes\n• first item"


class TestStartOfUtterance:
    def test_leading_bullet_has_no_preceding_newline(self):
        assert apply_formatting_tokens("bullet first item") == "• first item"

    def test_leading_bullet_point_has_no_preceding_newline(self):
        assert apply_formatting_tokens("bullet point first item") == "• first item"

    def test_leading_new_line_still_inserts_newline(self):
        # Only bullet/bullet point get the position-0 special case.
        assert apply_formatting_tokens("new line hello") == "\nhello"

    def test_trailing_token_at_utterance_end(self):
        assert apply_formatting_tokens("hello new line") == "hello\n"


class TestDisabledFlagBypasses:
    def test_disabled_returns_text_unchanged(self):
        text = "hello new line world"
        assert apply_formatting_tokens_if_enabled(text, False) == text

    def test_enabled_applies_substitution(self):
        text = "hello new line world"
        assert apply_formatting_tokens_if_enabled(text, True) == "hello\nworld"


class TestMultiTokenUtterance:
    def test_first_point_new_line_second_point(self):
        assert (apply_formatting_tokens("first point new line second point")
                == "first point\nsecond point")

    def test_multiple_different_tokens(self):
        assert (apply_formatting_tokens("intro new paragraph body insert tab indented new line end")
                == "intro\n\nbody\tindented\nend")


class TestIdentityFastPath:
    def test_no_tokens_returns_identical_object(self):
        text = "no formatting tokens in this sentence at all"
        assert apply_formatting_tokens(text) is text

    def test_empty_string_returns_identical_object(self):
        text = ""
        assert apply_formatting_tokens(text) is text

    def test_literal_tab_utterance_returns_identical_object(self):
        text = "tab"
        assert apply_formatting_tokens(text) is text
