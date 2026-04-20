"""Tests for samsara.command_registry: CommandMatcher longest-match semantics."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara.command_registry import CommandMatcher, CommandEntry


def _builtin(phrase, cmd_type='hotkey', **data):
    """Shape a commands.json-style dict for a single built-in."""
    return {phrase: {'type': cmd_type, **data}}


def _plugin_entry(phrase, aliases=None, func=None):
    """Shape a plugin_commands._REGISTRY-style entry dict."""
    entry = {
        'func': func or (lambda app, remainder: True),
        'phrase': phrase,
        'aliases': aliases or [],
        'source': 'test_plugin',
    }
    registry = {phrase: entry}
    for alias in entry['aliases']:
        registry[alias] = entry
    return registry


class TestLongestMatch:
    def test_longest_match_wins(self):
        """2-token plugin beats 1-token builtin on prefix overlap."""
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('find'))
        matcher.load_plugins(_plugin_entry('find tab'))
        matcher.freeze()

        entry, remainder = matcher.match('find tab github')
        assert entry is not None
        assert entry.phrase == 'find tab'
        assert entry.source == 'plugin'
        assert remainder == 'github'

    def test_short_phrase_alone_hits_builtin(self):
        """With a 2-token plugin shadowing, the bare 1-token phrase still fires."""
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('find'))
        matcher.load_plugins(_plugin_entry('find tab'))
        matcher.freeze()

        entry, remainder = matcher.match('find')
        assert entry is not None
        assert entry.phrase == 'find'
        assert entry.source == 'builtin'
        assert remainder == ''


class TestExactMatchBuiltinWins:
    def test_exact_match_builtin_wins(self):
        """Same phrase registered by both: builtin takes precedence."""
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('copy'))
        matcher.load_plugins(_plugin_entry('copy'))
        matcher.freeze()

        entry, remainder = matcher.match('copy')
        assert entry.source == 'builtin'
        assert entry.phrase == 'copy'
        assert remainder == ''


class TestPluginAliases:
    def test_plugin_aliases_match(self):
        """Aliases resolve to the same canonical entry."""
        matcher = CommandMatcher()
        matcher.load_plugins(_plugin_entry('greet me',
                                           aliases=['say hello', 'hello']))
        matcher.freeze()

        for phrase in ('greet me', 'say hello', 'hello'):
            entry, _ = matcher.match(phrase)
            assert entry is not None, phrase
            assert entry.phrase == 'greet me', phrase

    def test_plugin_alias_remainder(self):
        """Alias hit still extracts remainder correctly."""
        matcher = CommandMatcher()
        matcher.load_plugins(_plugin_entry('switch to', aliases=['use']))
        matcher.freeze()

        entry, remainder = matcher.match('use headphones')
        assert entry.phrase == 'switch to'
        assert remainder == 'headphones'


class TestRemainder:
    def test_remainder_extraction(self):
        matcher = CommandMatcher()
        matcher.load_plugins(_plugin_entry('search for'))
        matcher.freeze()

        entry, remainder = matcher.match('search for best ergonomic keyboard')
        assert entry.phrase == 'search for'
        assert remainder == 'best ergonomic keyboard'

    def test_exact_match_has_empty_remainder(self):
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('copy'))
        matcher.freeze()

        entry, remainder = matcher.match('copy')
        assert entry.phrase == 'copy'
        assert remainder == ''


class TestNoMatch:
    def test_no_match_returns_none(self):
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('copy'))
        matcher.freeze()

        entry, remainder = matcher.match('unrelated text here')
        assert entry is None
        assert remainder == ''

    def test_empty_text_returns_none(self):
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('copy'))
        matcher.freeze()

        assert matcher.match('') == (None, '')
        assert matcher.match('   ') == (None, '')


class TestCollisionDetection:
    def test_collision_detection_logs_warning(self, capsys):
        """detect_collisions emits a warning for prefix overlaps."""
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('find'))
        matcher.load_plugins(_plugin_entry('find tab'))
        matcher.freeze()

        collisions = matcher.detect_collisions()
        assert ('find', 'find tab') in collisions

        out = capsys.readouterr().out
        assert 'Prefix overlap' in out
        assert "'find'" in out and "'find tab'" in out

    def test_no_collision_when_phrases_distinct(self, capsys):
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('copy'))
        matcher.load_plugins(_plugin_entry('paste'))
        matcher.freeze()

        collisions = matcher.detect_collisions()
        assert collisions == []


class TestFreezeLock:
    def test_freeze_prevents_further_loading(self):
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('copy'))
        matcher.freeze()

        with pytest.raises(RuntimeError):
            matcher.load_builtins(_builtin('cut'))
        with pytest.raises(RuntimeError):
            matcher.load_plugins(_plugin_entry('go to'))

    def test_match_before_freeze_returns_none(self):
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('copy'))
        # No freeze() yet
        assert matcher.match('copy') == (None, '')


class TestBuiltinPluginMix:
    """Sanity checks that exercise the combined flow end-to-end."""

    def test_builtin_longer_than_plugin_wins(self):
        matcher = CommandMatcher()
        matcher.load_builtins({'open chrome': {'type': 'launch', 'target': 'chrome.exe'}})
        matcher.load_plugins(_plugin_entry('open'))  # shorter
        matcher.freeze()

        entry, remainder = matcher.match('open chrome')
        assert entry.phrase == 'open chrome'
        assert entry.source == 'builtin'

    def test_plugin_shadowed_phrase_logged(self, capsys):
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('copy'))
        matcher.load_plugins(_plugin_entry('copy'))
        matcher.freeze()

        out = capsys.readouterr().out
        assert "Plugin 'copy' shadowed by built-in" in out
