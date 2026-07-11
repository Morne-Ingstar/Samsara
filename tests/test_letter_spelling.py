"""Torture tests for samsara.letter_spelling.parse_letters -- the heart of
the voice-teaching spelling-truth channel (see samsara/teach_patterns.py's
module docstring for why this parser exists and what it protects against).

Pure function, no app/I-O -- every case here is a direct input/output
assertion.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara.letter_spelling import parse_letters


class TestBasicLetterSequences:
    def test_bare_single_letters(self):
        assert parse_letters("M O R N E") == "Morne"

    def test_lowercase_input(self):
        assert parse_letters("m o r n e") == "Morne"

    def test_comma_separated(self):
        assert parse_letters("M, O, R, N, E") == "Morne"

    def test_mixed_comma_and_space(self):
        assert parse_letters("M, O R, N E") == "Morne"

    def test_single_letter(self):
        assert parse_letters("Q") == "Q"

    def test_full_alphabet_bare(self):
        text = "a b c d e f g h i j k l m n o p q r s t u v w x y z"
        assert parse_letters(text) == "Abcdefghijklmnopqrstuvwxyz"


class TestLetterNameHomophones:
    """Whisper's actual mis-transcription patterns for spoken letter
    names -- the whole reason this module exists instead of trusting bare
    single characters."""

    @pytest.mark.parametrize("token,letter", [
        ("ay", "a"), ("eh", "a"),
        ("bee", "b"), ("be", "b"),
        ("see", "c"), ("sea", "c"), ("si", "c"),
        ("dee", "d"),
        ("ee", "e"),
        ("eff", "f"), ("ef", "f"),
        ("gee", "g"), ("jee", "g"),
        ("aitch", "h"), ("haitch", "h"),
        ("eye", "i"), ("aye", "i"),
        ("jay", "j"),
        ("kay", "k"),
        ("el", "l"), ("ell", "l"),
        ("em", "m"),
        ("en", "n"),
        ("oh", "o"),
        ("pee", "p"), ("pea", "p"),
        ("cue", "q"), ("queue", "q"),
        ("are", "r"), ("ar", "r"),
        ("ess", "s"), ("es", "s"),
        ("tee", "t"), ("tea", "t"),
        ("you", "u"), ("yoo", "u"), ("yew", "u"),
        ("vee", "v"),
        ("ex", "x"), ("ecks", "x"),
        ("why", "y"),
        ("zee", "z"), ("zed", "z"),
    ])
    def test_single_homophone_resolves_to_letter(self, token, letter):
        assert parse_letters(token) == letter.upper()

    def test_full_homophone_name_sequence(self):
        # "em oh are en ee" -- every token is a NAME, not a bare letter.
        assert parse_letters("em oh are en ee") == "Morne"

    def test_mixed_bare_and_homophone_tokens(self):
        # Whisper renders SOME letters as bare chars and others as names
        # within the same utterance -- both must resolve consistently.
        assert parse_letters("M oh are n ee") == "Morne"

    def test_double_u_two_tokens(self):
        assert parse_letters("double u") == "W"

    def test_double_u_hyphenated_token(self):
        assert parse_letters("double-u") == "W"

    def test_double_u_single_token(self):
        assert parse_letters("doubleu") == "W"

    def test_w_bare_letter_still_works(self):
        assert parse_letters("w") == "W"

    def test_double_u_inside_a_word(self):
        assert parse_letters("d o double u n") == "Down"


class TestAsInDisambiguation:
    def test_as_in_overrides_ambiguous_letter_token(self):
        assert parse_letters("M as in Mike, O, R, N, E") == "Morne"

    def test_as_in_without_comma(self):
        assert parse_letters("M as in Mike O R N E") == "Morne"

    def test_as_in_uses_word_first_letter_not_preceding_token(self):
        # The preceding token is deliberately WRONG/garbled here -- "as in
        # <word>" must still win, proving the disambiguation word is
        # trusted OVER the isolated letter attempt, not just as a
        # tie-breaker.
        assert parse_letters("zzz as in Mike") == "M"

    def test_multiple_as_in_clauses(self):
        assert parse_letters("M as in Mike O as in Oscar R as in Romeo N as in November E as in Echo") == "Morne"

    def test_as_in_malformed_missing_word_fails(self):
        assert parse_letters("M as in") is None

    def test_as_in_word_must_be_alphabetic(self):
        assert parse_letters("M as in 123") is None


class TestCapitalMarker:
    def test_single_capital(self):
        assert parse_letters("capital M o r n e") == "Morne"

    def test_multiple_capitals_no_auto_titlecase(self):
        # Explicit capital markers anywhere -> literal casing respected
        # exactly, no auto-title-casing applied on top.
        assert parse_letters("capital M capital O r n e") == "MOrne"

    def test_capital_with_homophone(self):
        assert parse_letters("capital em o r n e") == "Morne"

    def test_capital_with_as_in(self):
        assert parse_letters("capital M as in Mike o r n e") == "Morne"

    def test_capital_missing_letter_after_fails(self):
        assert parse_letters("capital") is None

    def test_capital_alias_words(self):
        for word in ("capital", "cap", "uppercase", "upper"):
            assert parse_letters(f"{word} m o r n e") == "Morne"


class TestPunctuationWords:
    def test_hyphen(self):
        assert parse_letters("d a t a hyphen l a k e") == "Data-Lake"

    def test_dash_synonym(self):
        assert parse_letters("d a t a dash l a k e") == "Data-Lake"

    def test_apostrophe(self):
        assert parse_letters("d o n apostrophe t") == "Don't"

    def test_space_for_multiword_phrase(self):
        assert parse_letters("d a t a space l a k e") == "Data Lake"

    def test_apostrophe_not_auto_capitalized_after(self):
        # Deliberate design choice (see letter_spelling.py's docstring):
        # apostrophe is NOT a title-case boundary -- "brien" after the
        # apostrophe stays lowercase unless explicitly marked capital.
        assert parse_letters("o apostrophe b r i e n") == "O'brien"

    def test_explicit_capital_after_apostrophe_respected(self):
        assert parse_letters("capital o apostrophe capital b r i e n") == "O'Brien"


class TestCasingPolicy:
    def test_no_capital_marker_titlecases_first_letter(self):
        assert parse_letters("m o r n e") == "Morne"

    def test_titlecase_applies_per_space_separated_word(self):
        assert parse_letters("d a t a space l a k e") == "Data Lake"

    def test_titlecase_applies_across_hyphen_boundary(self):
        assert parse_letters("d a t a hyphen l a k e") == "Data-Lake"

    def test_any_explicit_capital_disables_auto_titlecase_entirely(self):
        # Even ONE "capital" marker anywhere switches the whole result to
        # literal-casing mode -- other letters stay lowercase, not
        # auto-titlecased.
        assert parse_letters("a capital B c") == "aBc"


class TestMalformedInput:
    def test_empty_string(self):
        assert parse_letters("") is None

    def test_whitespace_only(self):
        assert parse_letters("   ") is None

    def test_unrecognized_token_fails_closed(self):
        # Must NEVER guess/skip an unrecognized token -- the whole parse
        # fails so the caller re-prompts rather than silently dropping a
        # letter from the spelling.
        assert parse_letters("M O gibberish N E") is None

    def test_digits_are_not_letters(self):
        assert parse_letters("M O 5 N E") is None

    def test_random_word_salad(self):
        assert parse_letters("the quick brown fox") is None

    def test_punctuation_word_alone_without_letters(self):
        # "hyphen" with nothing around it is a degenerate but not
        # inherently malformed sequence -- it parses to a lone "-".
        # Downstream callers (teach_patterns validation) reject a
        # 0-alpha-content result on their own terms; the parser's job is
        # only to not silently misparse or crash.
        assert parse_letters("hyphen") == "-"

    def test_none_input(self):
        assert parse_letters(None) is None


class TestRealisticFullNames:
    """End-to-end sanity checks combining multiple features, modeling
    what a real Whisper transcription of a spelled name might look
    like."""

    @pytest.mark.parametrize("text,expected", [
        ("M O R N E", "Morne"),
        ("em oh are en ee", "Morne"),
        ("capital M O R N E", "Morne"),
        ("M as in Mike, O, R, N, E", "Morne"),
        ("kay you bee ee are en ee tea ee es", "Kubernetes"),
        ("k u b e r n e t e s", "Kubernetes"),
    ])
    def test_morne_and_kubernetes_variants(self, text, expected):
        assert parse_letters(text) == expected
