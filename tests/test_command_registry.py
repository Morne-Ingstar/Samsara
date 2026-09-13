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


class TestParameterizedVerbPrecedence:
    """app_verbs.py registers bare single-token verbs ("open", "focus",
    "close") that hand-parse the remainder -- this only works as a
    fallback if every existing 2+-token literal macro ("open chrome",
    "close tab", etc.) keeps precedence. Longest-match-wins (already
    exercised generically above) gives this for free; these tests pin the
    EXACT scenario down."""

    def test_explicit_open_chrome_macro_beats_parameterized_open(self):
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('open chrome', cmd_type='launch', target='chrome.exe'))
        matcher.load_plugins(_plugin_entry('open'))
        matcher.freeze()

        entry, remainder = matcher.match('open chrome')
        assert entry.source == 'builtin'
        assert entry.phrase == 'open chrome'
        assert remainder == ''

    def test_parameterized_open_still_fires_for_unlisted_names(self):
        """No literal "open notepad2000" macro exists -- the bare "open"
        verb is the one that fires, carrying the app name as remainder."""
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('open chrome', cmd_type='launch', target='chrome.exe'))
        matcher.load_plugins(_plugin_entry('open'))
        matcher.freeze()

        entry, remainder = matcher.match('open notepad2000')
        assert entry.source == 'plugin'
        assert entry.phrase == 'open'
        assert remainder == 'notepad2000'

    def test_close_tab_builtin_beats_parameterized_close(self):
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('close tab', cmd_type='hotkey', keys=['ctrl', 'w']))
        matcher.load_plugins(_plugin_entry('close'))
        matcher.freeze()

        entry, remainder = matcher.match('close tab')
        assert entry.source == 'builtin'
        assert entry.phrase == 'close tab'
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


def _ask_matcher():
    matcher = CommandMatcher()
    matcher.load_plugins(_plugin_entry('hey ava', aliases=['ask ava', 'ava']))
    matcher.load_plugins(_plugin_entry('search for'))
    matcher.load_plugins(_plugin_entry('switch to', aliases=['use']))
    matcher.freeze()
    return matcher


class TestRemainderIsTheOriginalText:
    """Astra 2026-09-12 review item 8 (PAYLOAD_NORMALIZATION), inverted: the
    matcher matches a normalised view but hands over the user's words."""

    @pytest.mark.parametrize('utterance,expected', [
        # Astra's example: quotes, apostrophe, case-sensitive filenames.
        ('ask ava Tell Claude: "Don\'t rename Foo.py to foo.py".',
         'Tell Claude: "Don\'t rename Foo.py to foo.py"'),
        # Apostrophes and case.
        ("Ask Ava what's McDonald's phone number?",
         "what's McDonald's phone number?"),
        # Case-sensitive code.
        ('ask ava rename getUserID() to get_user_id() in API.ts',
         'rename getUserID() to get_user_id() in API.ts'),
        # URLs keep their scheme, case, path and query.
        ('search for https://GitHub.com/Foo/Bar?q=A&b=2',
         'https://GitHub.com/Foo/Bar?q=A&b=2'),
        # Multi-line payloads keep their line breaks and indentation.
        ('ask ava\nFirst line.\n  - Second: "x"\nThird',
         'First line.\n  - Second: "x"\nThird'),
        # Punctuation attached to the phrase belongs to the phrase.
        ('Search for, Ergonomic Keyboards.', 'Ergonomic Keyboards'),
        # Separator-only tokens between phrase and argument are dropped.
        ('ask ava - Summarise THIS', 'Summarise THIS'),
        ('ask ava: "quoted"', '"quoted"'),
    ])
    def test_original_argument_span(self, utterance, expected):
        entry, remainder = _ask_matcher().match(utterance)
        assert entry is not None
        assert remainder == expected

    def test_alias_match_keeps_original_argument(self):
        entry, remainder = _ask_matcher().match('Use "Headphones (USB-C)"')
        assert entry.phrase == 'switch to'
        assert remainder == '"Headphones (USB-C)"'

    def test_single_word_alias_with_whisper_punctuation(self):
        entry, remainder = _ask_matcher().match('Ava, Open NOTEPAD.exe.')
        assert entry.phrase == 'hey ava'
        assert remainder == 'Open NOTEPAD.exe'

    def test_only_a_trailing_terminator_run_is_dropped(self):
        _, remainder = _ask_matcher().match('search for foo... bar!?')
        assert remainder == 'foo... bar!?'

    def test_match_detail_exposes_the_normalised_view_and_offsets(self):
        text = 'Ask Ava Tell Claude: "Don\'t".'
        detail = _ask_matcher().match_detail(text)
        assert detail.entry.phrase == 'hey ava'
        assert detail.phrase_tokens == ('ask', 'ava')
        assert text[:detail.argument_start] == 'Ask Ava'
        assert detail.remainder == 'Tell Claude: "Don\'t"'
        assert detail.normalized_remainder == 'tell claude dont'

    def test_matching_still_ignores_case_and_punctuation(self):
        matcher = CommandMatcher()
        matcher.load_plugins(_plugin_entry('yes'))
        matcher.freeze()
        entry, remainder = matcher.match('Yes.')
        assert entry.phrase == 'yes'
        assert remainder == ''


class TestMetadataOwnership:
    """The registry keeps every metadata field verbatim; undeclared fields are
    'unknown' -- never defaulted to safe."""

    def test_plugin_metadata_is_kept_verbatim(self):
        from samsara import plugin_commands
        from samsara.command_registry import METADATA_FIELDS

        saved = dict(plugin_commands._REGISTRY)
        plugin_commands._REGISTRY.clear()
        try:
            @plugin_commands.command(
                'wipe drive', risk_class='destructive', ai_composable=False,
                side_effects=['file'], preconditions=['confirmed_by_user'],
                voice_triggerable=False, param_schema={'drive': {'type': 'str'}},
                reversible=False, preview_template='Erase {drive}', ai_visible=False)
            def _wipe(app, remainder):
                return True

            @plugin_commands.command('say hi')
            def _hi(app, remainder):
                return True

            matcher = CommandMatcher()
            matcher.load_plugins(plugin_commands._REGISTRY)
            matcher.freeze()
        finally:
            plugin_commands._REGISTRY.clear()
            plugin_commands._REGISTRY.update(saved)

        wipe = matcher._entries['wipe drive']
        assert wipe.metadata == {
            'ai_visible': False,
            'risk_class': 'destructive',
            'ai_composable': False,
            'side_effects': ['file'],
            'preconditions': ['confirmed_by_user'],
            'voice_triggerable': False,
            'param_schema': {'drive': {'type': 'str'}},
            'reversible': False,
            'preview_template': 'Erase {drive}',
        }
        hi = matcher._entries['say hi']
        assert hi.metadata == {name: 'unknown' for name in METADATA_FIELDS}
        assert hi.risk_class == 'unknown'
        # Typed gate attributes stay closed for existing readers.
        assert hi.ai_composable is False

    def test_builtin_safety_fields_are_no_longer_dropped(self):
        matcher = CommandMatcher()
        matcher.load_builtins({
            'close window': {'type': 'hotkey', 'keys': ['alt', 'f4'],
                             'risk_class': 'destructive', 'voice_triggerable': False},
            'copy': {'type': 'hotkey', 'keys': ['ctrl', 'c']},
        })
        matcher.freeze()

        close = matcher._entries['close window']
        assert close.metadata['risk_class'] == 'destructive'
        assert close.metadata['voice_triggerable'] is False
        assert close.metadata['reversible'] == 'unknown'
        assert close.risk_class == 'destructive'
        assert close.voice_triggerable is False

        copy = matcher._entries['copy']
        assert copy.risk_class == 'unknown'
        assert set(copy.metadata.values()) == {'unknown'}

    def test_list_commands_exports_metadata(self):
        matcher = CommandMatcher()
        matcher.load_builtins(_builtin('copy'))
        matcher.freeze()
        [row] = matcher.list_commands()
        assert row['metadata']['risk_class'] == 'unknown'

    def test_dump_tool_reports_every_command(self):
        import json
        import os
        import subprocess

        root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, str(root / 'tools' / 'dump_command_metadata.py')],
            cwd=str(root), capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=180,
            env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
        )
        assert proc.returncode == 0, proc.stderr[-3000:]
        dump = json.loads(proc.stdout)
        summary = dump['summary']
        assert summary['commands'] == len(dump['commands']) > 0
        assert summary['metadata_values'] == (
            summary['declared_values'] + summary['unknown_values'])
        by_phrase = {c['phrase']: c for c in dump['commands']}
        assert by_phrase['volume up']['metadata']['risk_class'] == 'safe'
        assert all(set(c['metadata']) == set(summary['metadata_fields'])
                   for c in dump['commands'])


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
