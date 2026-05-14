"""Tests for the command pack system.

Verifies that:
- disabled-pack commands are skipped by CommandMatcher.match()
- a disabled longer match falls through to a shorter enabled match
- core (always_on) pack ignores the config disable flag
- default_enabled values apply when config has no command_packs key
- every built-in command in commands.json has a non-empty pack field
- every plugin command has a non-empty pack field
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---- Paths ------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from samsara.command_registry import CommandEntry, CommandMatcher
from samsara.command_packs import PACKS, get_enabled_packs, default_pack_config


# ---- Helpers ----------------------------------------------------------------

def _make_matcher(enabled_packs=None):
    m = CommandMatcher()
    if enabled_packs is not None:
        m.set_enabled_packs(enabled_packs)
    return m


def _register_builtin(matcher, phrase, pack='core'):
    matcher._entries[phrase] = CommandEntry(
        phrase=phrase, source='builtin', cmd_type='hotkey',
        data={'type': 'hotkey', 'pack': pack}, pack=pack)


def _register_plugin(matcher, phrase, pack='core', aliases=None):
    handler = MagicMock(return_value=True)
    matcher._entries[phrase] = CommandEntry(
        phrase=phrase, source='plugin', cmd_type='plugin',
        handler=handler, aliases=aliases or [], pack=pack)
    for alias in (aliases or []):
        matcher._entries[alias] = matcher._entries[phrase]
    return handler


# =============================================================================
# Core matching tests
# =============================================================================

class TestPackFiltering:

    def test_disabled_pack_commands_not_matched(self):
        """Commands from a disabled pack should not be returned by match()."""
        m = _make_matcher(enabled_packs={'core'})
        _register_builtin(m, 'open chrome', pack='browsers')
        _register_builtin(m, 'escape', pack='core')
        m.freeze()

        entry, _ = m.match('open chrome')
        assert entry is None, "Disabled-pack command should not match"

        entry2, _ = m.match('escape')
        assert entry2 is not None
        assert entry2.phrase == 'escape'

    def test_fallthrough_on_disabled_match(self):
        """If the longest match is disabled, fall through to a shorter enabled match."""
        m = _make_matcher(enabled_packs={'core', 'browsers'})
        # 'find tab' is browsers (enabled)
        _register_plugin(m, 'find tab', pack='browsers')
        # 'find' is in a disabled pack
        _register_builtin(m, 'find', pack='text-editing')
        m.freeze()

        # text-editing disabled -> 'find' not active
        # browsers enabled -> 'find tab' should still match
        entry, remainder = m.match('find tab github')
        assert entry is not None
        assert entry.phrase == 'find tab'
        assert remainder == 'github'

    def test_disabled_longer_falls_to_shorter_enabled(self):
        """A disabled 3-token phrase should not block a 1-token enabled phrase."""
        m = _make_matcher(enabled_packs={'core'})
        # 'open chrome browser' is browsers (disabled)
        _register_plugin(m, 'open chrome browser', pack='browsers')
        # 'open' is core (enabled)
        _register_builtin(m, 'open', pack='core')
        m.freeze()

        entry, remainder = m.match('open chrome browser')
        assert entry is not None
        assert entry.phrase == 'open'
        assert remainder == 'chrome browser'

    def test_all_packs_enabled_when_none_set(self):
        """When set_enabled_packs is never called, all packs match."""
        m = _make_matcher()   # no pack filter
        _register_builtin(m, 'open chrome', pack='browsers')
        m.freeze()

        entry, _ = m.match('open chrome')
        assert entry is not None

    def test_exact_match_disabled_falls_through_to_prefix(self):
        """Exact match from disabled pack should fall through to prefix from enabled pack."""
        m = _make_matcher(enabled_packs={'core'})
        _register_builtin(m, 'escape key', pack='gaming')   # disabled exact match
        _register_builtin(m, 'escape', pack='core')         # enabled prefix
        m.freeze()

        entry, remainder = m.match('escape key')
        assert entry is not None
        assert entry.phrase == 'escape'
        assert remainder == 'key'


# =============================================================================
# Core always_on
# =============================================================================

class TestCoreAlwaysOn:

    def test_core_always_enabled_even_when_config_says_false(self):
        """get_enabled_packs must always include 'core' regardless of config."""
        config = {'command_packs': {'core': False, 'browsers': False}}
        enabled = get_enabled_packs(config)
        assert 'core' in enabled, "core is always_on and must always be enabled"

    def test_core_commands_match_when_pack_config_excludes_all(self):
        """CommandMatcher must match core commands even if all other packs disabled."""
        # Simulate a user who has disabled everything except core
        m = _make_matcher(enabled_packs={'core'})
        _register_builtin(m, 'escape', pack='core')
        m.freeze()

        entry, _ = m.match('escape')
        assert entry is not None and entry.phrase == 'escape'

    def test_always_on_meta_flag(self):
        """PACKS['core']['always_on'] must be True."""
        assert PACKS['core']['always_on'] is True


# =============================================================================
# Default enabled values
# =============================================================================

class TestDefaultEnabled:

    def test_default_enabled_values_with_empty_config(self):
        """With no command_packs in config, packs default to default_enabled."""
        enabled = get_enabled_packs({})
        for pack_id, meta in PACKS.items():
            if meta['always_on']:
                assert pack_id in enabled
            elif meta['default_enabled']:
                assert pack_id in enabled, f"Pack '{pack_id}' should be enabled by default"
            else:
                assert pack_id not in enabled, f"Pack '{pack_id}' should be disabled by default"

    def test_user_config_overrides_default(self):
        """User config should override default_enabled."""
        # text-editing is default_enabled=True; disable it
        config = {'command_packs': {'text-editing': False}}
        enabled = get_enabled_packs(config)
        assert 'text-editing' not in enabled

    def test_default_pack_config_covers_all_packs(self):
        """default_pack_config() should return an entry for every pack."""
        cfg = default_pack_config()
        for pack_id in PACKS:
            assert pack_id in cfg


# =============================================================================
# commands.json pack coverage
# =============================================================================

class TestBuiltinPackAssignment:

    @pytest.fixture(scope='class')
    def commands(self):
        path = PROJECT_ROOT / 'commands.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        return data.get('commands', data)

    def test_every_builtin_has_pack(self, commands):
        """Every entry in commands.json must have a non-empty 'pack' field."""
        missing = [name for name, v in commands.items()
                   if not v.get('pack')]
        assert not missing, f"Commands missing pack: {missing}"

    def test_builtin_packs_are_known(self, commands):
        """Every pack referenced in commands.json must exist in PACKS."""
        unknown = {v['pack'] for v in commands.values()
                   if v.get('pack') and v['pack'] not in PACKS}
        assert not unknown, f"Unknown packs in commands.json: {unknown}"


# =============================================================================
# Plugin pack coverage
# =============================================================================

class TestPluginPackAssignment:

    @pytest.fixture(scope='class')
    def plugin_entries(self):
        """Load plugins and return the registry entries (deduplicated)."""
        from samsara import plugin_commands as _pc
        # Clear and reload so this test is isolated
        old_registry = dict(_pc._REGISTRY)
        _pc._REGISTRY.clear()
        try:
            _pc.load_plugins(str(PROJECT_ROOT / 'plugins' / 'commands'))
            seen = set()
            entries = []
            for entry_data in _pc._REGISTRY.values():
                eid = id(entry_data)
                if eid not in seen:
                    seen.add(eid)
                    entries.append(entry_data)
            return entries
        finally:
            _pc._REGISTRY.clear()
            _pc._REGISTRY.update(old_registry)

    def test_every_plugin_command_has_pack(self, plugin_entries):
        """Every registered plugin command must have a non-empty 'pack' field."""
        missing = [e['phrase'] for e in plugin_entries if not e.get('pack')]
        assert not missing, f"Plugin commands missing pack: {missing}"

    def test_plugin_packs_are_known(self, plugin_entries):
        """Every pack referenced in plugin commands must exist in PACKS."""
        unknown = {e['pack'] for e in plugin_entries
                   if e.get('pack') and e['pack'] not in PACKS}
        assert not unknown, f"Unknown packs in plugin commands: {unknown}"


# =============================================================================
# CommandEntry pack attribute
# =============================================================================

class TestCommandEntryPack:

    def test_pack_defaults_to_core(self):
        e = CommandEntry('my command', 'builtin', 'hotkey')
        assert e.pack == 'core'

    def test_pack_stored_correctly(self):
        e = CommandEntry('open chrome', 'builtin', 'launch', pack='browsers')
        assert e.pack == 'browsers'

    def test_none_pack_coerces_to_core(self):
        e = CommandEntry('some command', 'builtin', 'hotkey', pack=None)
        assert e.pack == 'core'
