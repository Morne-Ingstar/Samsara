"""Tests for samsara.formatters case-formatting transforms."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from samsara.formatters import apply_case_formatter, FORMATTERS


# ---------------------------------------------------------------------------
# Individual formatter transforms
# ---------------------------------------------------------------------------

def test_camel():
    assert apply_case_formatter("camel my variable name") == "myVariableName"


def test_camel_single_word_remainder():
    assert apply_case_formatter("camel hello") == "hello"


def test_pascal():
    assert apply_case_formatter("pascal my class name") == "MyClassName"


def test_pascal_single_word():
    assert apply_case_formatter("pascal result") == "Result"


def test_snake():
    assert apply_case_formatter("snake my variable name") == "my_variable_name"


def test_constant():
    assert apply_case_formatter("constant max retries") == "MAX_RETRIES"


def test_kebab():
    assert apply_case_formatter("kebab my class name") == "my-class-name"


def test_dotted():
    assert apply_case_formatter("dotted my path name") == "my.path.name"


def test_title():
    assert apply_case_formatter("title the great gatsby") == "The Great Gatsby"


def test_say():
    assert apply_case_formatter("say hello world") == "hello world"


# ---------------------------------------------------------------------------
# No-match cases: caller should receive None and use original text
# ---------------------------------------------------------------------------

def test_no_match_normal_speech():
    assert apply_case_formatter("hello world") is None


def test_no_match_keyword_alone_no_remainder():
    # User says just "camel" with nothing after -- not a formatter trigger
    assert apply_case_formatter("camel") is None


def test_no_match_empty_string():
    assert apply_case_formatter("") is None


def test_no_match_none():
    assert apply_case_formatter(None) is None


def test_no_match_keyword_in_middle():
    # First-token guard: "snake" in position 2 must NOT trigger
    assert apply_case_formatter("use snake case here") is None


def test_no_match_unknown_word():
    assert apply_case_formatter("compress my data") is None


# ---------------------------------------------------------------------------
# Robustness: Whisper may capitalize or punctuate the first word
# ---------------------------------------------------------------------------

def test_case_insensitive_camel():
    assert apply_case_formatter("Camel my variable name") == "myVariableName"


def test_case_insensitive_snake():
    assert apply_case_formatter("Snake my variable name") == "my_variable_name"


def test_keyword_with_trailing_comma():
    # Whisper may attach punctuation: "Snake, my variable name"
    assert apply_case_formatter("snake, my variable name") == "my_variable_name"


def test_keyword_with_trailing_period():
    # "pascal." -> keyword "pascal"; "MyClassName" is one word -> lowercased then pascal-cased
    assert apply_case_formatter("pascal. MyClassName") == "Myclassname"


def test_remainder_with_trailing_period():
    # Whisper may punctuate: "camel my variable name."
    assert apply_case_formatter("camel my variable name.") == "myVariableName"


# ---------------------------------------------------------------------------
# Multi-word edge cases
# ---------------------------------------------------------------------------

def test_camel_four_words():
    assert apply_case_formatter("camel get http response body") == "getHttpResponseBody"


def test_constant_three_words():
    assert apply_case_formatter("constant default timeout ms") == "DEFAULT_TIMEOUT_MS"


def test_kebab_hyphenated_output():
    result = apply_case_formatter("kebab content type header")
    assert result == "content-type-header"


def test_dotted_four_segments():
    assert apply_case_formatter("dotted com example my app") == "com.example.my.app"


# ---------------------------------------------------------------------------
# Table completeness
# ---------------------------------------------------------------------------

def test_all_expected_formatters_registered():
    expected = {"camel", "pascal", "snake", "constant", "kebab", "dotted", "title", "say"}
    assert expected.issubset(FORMATTERS.keys())
