"""
Integration tests for the wake word -> command pipeline.

Runs the full chain: lowercase -> corrections -> wake phrase matching ->
command extraction -> normalization -> intent parsing.

Uses the real modules (not mocks) to catch regressions across module boundaries.
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara.wake_word_matcher import match_wake_phrase
from samsara.wake_corrections import apply_corrections
from samsara.command_parser import parse_wake_command, normalize_command_text


def simulate_pipeline(text, wake_phrase="jarvis"):
    """Run a single utterance through the full pipeline.

    Returns (wake_matched: bool, intent: dict or None).
    """
    text_lower = text.lower().strip()
    corrected = apply_corrections(text_lower)

    matched, match_type, match_index = match_wake_phrase(corrected, wake_phrase)
    if not matched:
        return False, None

    command_raw = corrected[match_index + len(wake_phrase):]
    intent = parse_wake_command(command_raw)
    return True, intent


# ---- Test cases from pipeline_tester.py (21 cases) ----

# Format: (input_text, expected_wake_matched, expected_type, expected_name, description)
PIPELINE_CASES = [
    # Clean inputs
    ("jarvis dictate hello world",          True,  "dictation",    "dictate",       "clean dictate with content"),
    ("jarvis dictate",                      True,  "dictation",    "dictate",       "bare dictate"),
    ("jarvis long dictate",                 True,  "dictation",    "long_dictate",  "bare long dictate"),
    ("jarvis short dictate hello",          True,  "dictation",    "short_dictate", "short dictate with content"),
    ("jarvis quick dictate",               True,  "dictation",    "short_dictate", "quick dictate synonym"),

    # Punctuation noise from Whisper
    ("jarvis, dictate hello world",         True,  "dictation",    "dictate",       "comma after wake word"),
    ("jarvis - dictate hello world",        True,  "dictation",    "dictate",       "dash after wake word"),
    ("jarvis. dictate hello",               True,  "dictation",    "dictate",       "period after wake word"),

    # Weird spacing
    ("jarvis     dictate     hello world",  True,  "dictation",    "dictate",       "extra spaces"),

    # Filler words
    ("jarvis please dictate hello",         True,  "dictation",    "dictate",       "leading filler"),
    ("jarvis um dictate hello",             True,  "dictation",    "dictate",       "um filler"),

    # Non-dictation commands (should be command_text)
    ("jarvis copy that",                    True,  "command_text", None,            "regular command"),
    ("jarvis open chrome",                  True,  "command_text", None,            "launch command"),
    ("jarvis select all",                   True,  "command_text", None,            "hotkey command"),

    # Wake word positions
    ("hey jarvis dictate hello",            True,  "dictation",    "dictate",       "wake word mid-phrase"),
    ("so I said jarvis dictate hello",      True,  "dictation",    "dictate",       "wake word late in phrase"),

    # Failure cases -- no wake word
    ("hello world",                         False, None,           None,            "no wake word at all"),
    ("dictate hello world",                 False, None,           None,            "dictate without wake word"),

    # Edge cases
    ("jarvis",                              True,  "unknown",      None,            "wake word only, nothing after"),
    ("jarvis,",                             True,  "unknown",      None,            "wake word + punctuation only"),
]


@pytest.mark.parametrize(
    "text,expected_matched,expected_type,expected_name,desc",
    PIPELINE_CASES,
    ids=[c[4] for c in PIPELINE_CASES],
)
def test_pipeline(text, expected_matched, expected_type, expected_name, desc):
    matched, intent = simulate_pipeline(text)
    assert matched == expected_matched, f"wake match: expected {expected_matched}, got {matched}"

    if not expected_matched:
        assert intent is None
    else:
        assert intent is not None
        assert intent["type"] == expected_type, (
            f"type: expected '{expected_type}', got '{intent['type']}'")
        assert intent.get("name") == expected_name, (
            f"name: expected '{expected_name}', got '{intent.get('name')}'")


# ---- Additional edge cases ----

class TestPipelineEdgeCases:
    def test_uppercase_input(self):
        matched, intent = simulate_pipeline("JARVIS DICTATE HELLO")
        assert matched
        assert intent["type"] == "dictation"
        assert intent["name"] == "dictate"
        assert intent["content"] == "hello"

    def test_messy_spacing_and_punctuation(self):
        matched, intent = simulate_pipeline("jarvis  ,  dictate  hello")
        assert matched
        assert intent["type"] == "dictation"
        assert intent["name"] == "dictate"

    def test_dictation_synonym(self):
        matched, intent = simulate_pipeline("jarvis dictation hello")
        assert matched
        assert intent["type"] == "dictation"
        assert intent["name"] == "dictate"
        assert intent["content"] == "hello"

    def test_empty_string(self):
        matched, intent = simulate_pipeline("")
        assert not matched
        assert intent is None

    def test_content_preserved(self):
        matched, intent = simulate_pipeline("jarvis dictate turn on the lights")
        assert matched
        assert intent["content"] == "turn on the lights"

    def test_long_dictate_with_content(self):
        matched, intent = simulate_pipeline("jarvis long dictate once upon a time")
        assert matched
        assert intent["type"] == "dictation"
        assert intent["name"] == "long_dictate"
        assert intent["content"] == "once upon a time"

    def test_custom_wake_phrase(self):
        matched, intent = simulate_pipeline("samsara dictate hello", wake_phrase="samsara")
        assert matched
        assert intent["type"] == "dictation"

    def test_wake_phrase_not_present(self):
        matched, intent = simulate_pipeline("computer dictate hello", wake_phrase="jarvis")
        assert not matched

    def test_em_dash_separator(self):
        """Whisper sometimes outputs em dash between wake word and command."""
        matched, intent = simulate_pipeline("jarvis \u2014 dictate hello")
        assert matched
        assert intent["type"] == "dictation"
        assert intent["name"] == "dictate"
