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


# ---------------------------------------------------------------------------
# 53: quotes, parens, asterisks and trailing wrap modifiers
# ---------------------------------------------------------------------------

class TestQuoteAndParenSpacing:
    def test_open_and_close_quote_hug_the_quoted_words(self):
        assert apply_formatting_tokens("he said open quote hello close quote") == 'he said "hello"'

    def test_parens_hug_their_contents(self):
        assert apply_formatting_tokens("open paren like this close paren") == "(like this)"

    def test_quotes_are_straight_not_smart(self):
        out = apply_formatting_tokens("open quote hi close quote")
        assert out == '"hi"' and "\u201c" not in out and "\u201d" not in out

    def test_space_outside_the_pair_is_kept(self):
        assert apply_formatting_tokens("a open paren b close paren c") == "a (b) c"

    def test_whisper_commas_around_the_spoken_phrases_are_absorbed(self):
        assert apply_formatting_tokens("He said, open quote, hello, close quote.") == 'He said, "hello".'

    def test_token_at_the_very_start(self):
        assert apply_formatting_tokens("open quote hello there close quote") == '"hello there"'

    def test_token_at_the_very_end(self):
        assert apply_formatting_tokens("the word is open quote done close quote") == 'the word is "done"'
        assert apply_formatting_tokens("hello close quote") == 'hello"'

    def test_case_insensitive(self):
        assert apply_formatting_tokens("Open Quote hi Close Quote") == '"hi"'


class TestAsterisks:
    def test_double_asterisk_pair_makes_bold(self):
        # 53 decision: asterisks have no open/close words, so within one
        # utterance each phrase alternates open, close.
        assert apply_formatting_tokens("double asterisk bold double asterisk") == "**bold**"

    def test_single_asterisk_pair(self):
        assert apply_formatting_tokens("this is asterisk important asterisk ok") == "this is *important* ok"

    def test_unclosed_double_asterisk_just_opens(self):
        assert apply_formatting_tokens("double asterisk bold") == "**bold"

    def test_double_asterisk_is_not_split_into_double_plus_asterisk(self):
        out = apply_formatting_tokens("say double asterisk here double asterisk")
        assert out == "say **here**" and "double" not in out

    def test_single_and_double_alternate_independently(self):
        assert (apply_formatting_tokens("double asterisk a asterisk b asterisk c double asterisk")
                == "**a *b* c**")


class TestStarIsNotAToken:
    """53 decision: "star" is ordinary speech in prose ("five star", "star
    wars"); only the precise word "asterisk" inserts *."""

    @pytest.mark.parametrize("text", [
        "five star review", "star wars", "a star", "star", "starred", "the stars align",
    ])
    def test_star_stays_literal(self, text):
        assert apply_formatting_tokens(text) is text


class TestTrailingWrapModifiers:
    @pytest.mark.parametrize("text,expected", [
        ("he said hello in quotes", '"he said hello"'),
        ("he said hello in quotes.", '"he said hello"'),
        ("He said hello in quotes!", '"He said hello"'),
        ("he said hello in quotes .", '"he said hello"'),
        ("make this in asterisks", "*make this*"),
        ("make this in asterisks.", "*make this*"),
        ("the title in bold", "**the title**"),
        ("an aside in parens", "(an aside)"),
        ("an aside in parens.", "(an aside)"),
        ("IMPORTANT IN BOLD", "**IMPORTANT**"),
    ])
    def test_trailing_modifier_wraps_the_whole_utterance(self, text, expected):
        assert apply_formatting_tokens(text) == expected

    def test_no_invented_period_inside_the_wrap(self):
        assert apply_formatting_tokens("he said hello in quotes.") == '"he said hello"'

    def test_punctuation_spoken_before_the_trigger_stays_put(self):
        assert apply_formatting_tokens("wait, what? in quotes") == '"wait, what?"'

    @pytest.mark.parametrize("text", [
        "put it in quotes please",
        "the in quotes part",
        "write this in bold letters",
        "say it in parens later",
    ])
    def test_mid_utterance_trigger_is_literal(self, text):
        assert apply_formatting_tokens(text) is text

    @pytest.mark.parametrize("text", ["in quotes", "in quotes.", "In Bold", " in parens "])
    def test_trigger_only_utterance_is_left_literal(self, text):
        # Nothing to wrap: typing the words is visible and undoable, unlike
        # an empty "" pair or silently dropping the utterance.
        assert apply_formatting_tokens(text) == text

    def test_only_one_modifier_the_earlier_one_is_literal(self):
        assert apply_formatting_tokens("hello in bold in quotes") == '"hello in bold"'

    def test_inline_tokens_substitute_first_then_the_wrap_goes_around_the_text(self):
        # The bullet marker is structure, so it stays outside the wrap.
        assert apply_formatting_tokens("bullet one in quotes") == '• "one"'
        assert apply_formatting_tokens("intro bullet one in quotes") == '"intro\n• one"'

    def test_edge_line_breaks_stay_outside_the_wrap(self):
        assert apply_formatting_tokens("hello new line in quotes") == '"hello"\n'
        assert apply_formatting_tokens("new line hello in bold") == "\n**hello**"

    def test_inline_quotes_inside_a_trailing_paren_wrap(self):
        assert apply_formatting_tokens("he said open quote hi close quote in parens") == '(he said "hi")'


class TestExistingTokensUnchangedBy53:
    """Byte-identical outputs for the original four tokens, recorded from the
    module before 53 (reports/53/artifacts/golden_before.json)."""

    @pytest.mark.parametrize("text,expected", [
        ("hello new line world", "hello\nworld"),
        ("hello new paragraph world", "hello\n\nworld"),
        ("hello insert tab world", "hello\tworld"),
        ("hello bullet world", "hello\n• world"),
        ("bullet first item", "• first item"),
        ("new line hello", "\nhello"),
        ("hello new line", "hello\n"),
        ("intro new paragraph body insert tab indented new line end", "intro\n\nbody\tindented\nend"),
        ("notes bullet point", "notes\n• "),
        ("  hello  new line  world ", "  hello \n world "),
        ("a new line new line b", "a\n\nb"),
        ("x bullet point bullet y", "x\n• \n• y"),
        ("NEW LINE", "\n"),
        ("hello\tinsert tab\tworld", "hello\t\t\tworld"),
        ("hello, new line, world.", "hello,\n, world."),
    ])
    def test_golden(self, text, expected):
        assert apply_formatting_tokens(text) == expected
