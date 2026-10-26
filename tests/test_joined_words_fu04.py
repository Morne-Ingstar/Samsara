"""Joined-word recognition stays exact and limited to command matching."""

from samsara.command_catalog import compact_command_match
from samsara.command_registry import CommandMatcher
from samsara.phonetic_wash import _DEFAULT_WORD_CORRECTIONS, apply_phonetic_wash


def _matcher(commands):
    matcher = CommandMatcher()
    matcher.load_builtins(commands)
    matcher.freeze()
    return matcher


def test_compact_exact_forms_resolve_openlog_scrolldown_and_newline():
    matcher = _matcher({
        "open log": {"type": "hotkey", "keys": ["ctrl", "o"]},
        "scroll down": {"type": "hotkey", "keys": ["pagedown"]},
        "new line": {"type": "hotkey", "keys": ["enter"]},
    })

    assert compact_command_match("Openlog", matcher)[0].phrase == "open log"
    assert compact_command_match("scrolldown", matcher)[0].phrase == "scroll down"
    assert compact_command_match("newline", matcher)[0].phrase == "new line"


def test_ambiguous_compact_form_is_refused():
    matcher = _matcher({
        "red-green blue": {"type": "hotkey", "keys": ["a"]},
        "red green-blue": {"type": "hotkey", "keys": ["b"]},
    })

    assert compact_command_match("redgreenblue", matcher) is None


def test_compact_match_is_exact_and_default_correction_is_shipped():
    matcher = _matcher({"open log": {"type": "hotkey", "keys": ["ctrl", "o"]}})

    assert compact_command_match("openlog please", matcher) is None
    assert _DEFAULT_WORD_CORRECTIONS["openlog"] == "open log"
    assert apply_phonetic_wash("openlog") == "open log"

