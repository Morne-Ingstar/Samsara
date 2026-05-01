"""
Tests for samsara.command_parser -- wake word command parsing.
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara.command_parser import (
    normalize_command_text,
    strip_fillers,
    parse_wake_command,
    strip_wake_echoes,
)


class TestNormalizeCommandText:
    def test_empty(self):
        assert normalize_command_text("") == ""

    def test_leading_punctuation(self):
        assert normalize_command_text(", dictate hello") == "dictate hello"

    def test_leading_dash(self):
        assert normalize_command_text("-- dictate hello") == "dictate hello"

    def test_collapse_whitespace(self):
        assert normalize_command_text("  dictate   hello  ") == "dictate hello"

    def test_case_folding(self):
        assert normalize_command_text("DICTATE Hello") == "dictate hello"

    def test_interior_colon_preserved(self):
        assert normalize_command_text("dictate: hello") == "dictate: hello"


class TestStripFillers:
    def test_leading(self):
        assert strip_fillers("please dictate hello") == "dictate hello"

    def test_trailing(self):
        assert strip_fillers("dictate hello please") == "dictate hello"

    def test_both(self):
        assert strip_fillers("um dictate hello please") == "dictate hello"

    def test_multiple_leading(self):
        assert strip_fillers("uh um please dictate") == "dictate"

    def test_only_fillers(self):
        assert strip_fillers("please") == ""

    def test_interior_preserved(self):
        assert strip_fillers("I like cats") == "I like cats"

    def test_interior_please_preserved(self):
        assert strip_fillers("dictate please call me") == "dictate please call me"

    def test_empty(self):
        assert strip_fillers("") == ""


class TestParseWakeCommandDictation:
    def test_bare_dictate(self):
        r = parse_wake_command("dictate")
        assert r["type"] == "dictation"
        assert r["name"] == "long_dictation"
        assert r["content"] is None

    def test_dictate_with_content(self):
        r = parse_wake_command("dictate hello world")
        assert r["type"] == "dictation"
        assert r["name"] == "long_dictation"
        assert r["content"] == "hello world"

    def test_dictation_synonym(self):
        r = parse_wake_command("dictation hello")
        assert r["type"] == "dictation"
        assert r["name"] == "long_dictation"
        assert r["content"] == "hello"

    def test_leading_punctuation(self):
        r = parse_wake_command(", dictate hello")
        assert r["type"] == "dictation"
        assert r["name"] == "long_dictation"
        assert r["content"] == "hello"

    def test_colon_separator(self):
        r = parse_wake_command("dictate: hello world")
        assert r["type"] == "dictation"
        assert r["name"] == "long_dictation"
        assert r["content"] == "hello world"

    def test_dash_separator(self):
        r = parse_wake_command("dictate - hello")
        assert r["type"] == "dictation"
        assert r["name"] == "long_dictation"
        assert r["content"] == "hello"

    def test_long_dictate(self):
        r = parse_wake_command("long dictate")
        assert r["type"] == "dictation"
        assert r["name"] == "long_dictation"
        assert r["content"] is None

    def test_short_dictate_with_content(self):
        r = parse_wake_command("short dictate hello")
        assert r["type"] == "dictation"
        assert r["name"] == "quick_dictation"
        assert r["content"] == "hello"

    def test_quick_dictate(self):
        r = parse_wake_command("quick dictate")
        assert r["type"] == "dictation"
        assert r["name"] == "quick_dictation"
        assert r["content"] is None

    def test_filler_stripped(self):
        r = parse_wake_command("please dictate hello")
        assert r["type"] == "dictation"
        assert r["name"] == "long_dictation"
        assert r["content"] == "hello"

    def test_trailing_filler_stripped_from_content(self):
        r = parse_wake_command("dictate hello world please")
        assert r["type"] == "dictation"
        assert r["name"] == "long_dictation"
        assert r["content"] == "hello world"


class TestParseWakeCommandText:
    def test_regular_command(self):
        r = parse_wake_command("copy that")
        assert r["type"] == "command_text"
        assert r["name"] is None
        assert r["content"] == "copy that"

    def test_launch_command(self):
        r = parse_wake_command("open chrome")
        assert r["type"] == "command_text"
        assert r["name"] is None
        assert r["content"] == "open chrome"


class TestParseWakeCommandUnknown:
    def test_empty(self):
        r = parse_wake_command("")
        assert r["type"] == "unknown"

    def test_punctuation_only(self):
        r = parse_wake_command(",")
        assert r["type"] == "unknown"

    def test_dots(self):
        r = parse_wake_command("...")
        assert r["type"] == "unknown"

    def test_single_char(self):
        r = parse_wake_command("a")
        assert r["type"] == "unknown"


class TestParseWakeCommandPreservesRaw:
    def test_raw_preserved(self):
        r = parse_wake_command(", Dictate: Hello World")
        assert r["raw"] == ", Dictate: Hello World"
        assert r["type"] == "dictation"


class TestStripWakeEchoes:
    def test_doubled_wake_word(self):
        cleaned, count = strip_wake_echoes("jarvis jarvis open chrome", "jarvis")
        assert normalize_command_text(cleaned) == "open chrome"
        assert count == 1

    def test_doubled_with_punctuation(self):
        cleaned, count = strip_wake_echoes("jarvis, jarvis, dictate hello", "jarvis")
        assert normalize_command_text(cleaned) == "dictate hello"
        assert count == 1

    def test_mid_command_echo(self):
        cleaned, count = strip_wake_echoes("jarvis open jarvis chrome", "jarvis")
        assert normalize_command_text(cleaned) == "open chrome"
        assert count == 1

    def test_partial_word_preserved(self):
        cleaned, count = strip_wake_echoes("jarvis jarvison settings", "jarvis")
        assert normalize_command_text(cleaned) == "jarvison settings"
        assert count == 0

    def test_no_echoes(self):
        cleaned, count = strip_wake_echoes("open chrome", "jarvis")
        assert normalize_command_text(cleaned) == "open chrome"
        assert count == 0
