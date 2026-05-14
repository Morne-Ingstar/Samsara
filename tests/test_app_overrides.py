"""Tests for per-app keyboard shortcut overrides.

Covers:
- _resolve_app_override: base fallback, matching override, null override
- Both override formats (commands.json dict vs. plugin decorator bare value)
- HotkeyHandler suppression via null override
- plugin @command storing app_overrides
- CommandEntry.app_overrides field
- Resolution at execute time (foreground app changes between calls)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from samsara.handlers import _resolve_app_override, HotkeyHandler, CommandContext
from samsara.command_registry import CommandEntry


# ---------------------------------------------------------------------------
# _resolve_app_override: core logic
# ---------------------------------------------------------------------------

class TestResolveAppOverride:

    def test_no_overrides_uses_base_keys(self):
        cmd = {'keys': ['ctrl', 't'], 'type': 'hotkey'}
        keys, skip = _resolve_app_override(cmd)
        assert keys == ['ctrl', 't']
        assert skip is False

    def test_no_foreground_app_uses_base_keys(self):
        cmd = {
            'keys': ['ctrl', 't'],
            'app_overrides': {'chrome.exe': {'keys': 'ctrl+t'}},
        }
        with patch('samsara.handlers._get_foreground_exe_lower', return_value=None):
            keys, skip = _resolve_app_override(cmd)
        assert keys == ['ctrl', 't']
        assert skip is False

    def test_matching_app_uses_override(self):
        cmd = {
            'keys': ['ctrl', 't'],
            'app_overrides': {'code.exe': {'keys': 'ctrl+shift+n'}},
        }
        with patch('samsara.handlers._get_foreground_exe_lower', return_value='code.exe'):
            keys, skip = _resolve_app_override(cmd)
        assert keys == ['ctrl', 'shift', 'n']
        assert skip is False

    def test_non_matching_app_uses_base(self):
        cmd = {
            'keys': ['ctrl', 't'],
            'app_overrides': {'code.exe': {'keys': None}},
        }
        with patch('samsara.handlers._get_foreground_exe_lower', return_value='chrome.exe'):
            keys, skip = _resolve_app_override(cmd)
        assert keys == ['ctrl', 't']
        assert skip is False

    def test_null_override_returns_skip(self):
        cmd = {
            'keys': ['ctrl', 't'],
            'app_overrides': {'code.exe': {'keys': None}},
        }
        with patch('samsara.handlers._get_foreground_exe_lower', return_value='code.exe'):
            keys, skip = _resolve_app_override(cmd)
        assert keys is None
        assert skip is True

    def test_dict_format_string_override_splits_on_plus(self):
        cmd = {
            'keys': ['ctrl', 'w'],
            'app_overrides': {'code.exe': {'keys': 'ctrl+f4'}},
        }
        with patch('samsara.handlers._get_foreground_exe_lower', return_value='code.exe'):
            keys, skip = _resolve_app_override(cmd)
        assert keys == ['ctrl', 'f4']
        assert skip is False

    def test_bare_string_plugin_format(self):
        cmd = {
            'keys': ['ctrl', 't'],
            'app_overrides': {'explorer.exe': 'ctrl+n'},
        }
        with patch('samsara.handlers._get_foreground_exe_lower', return_value='explorer.exe'):
            keys, skip = _resolve_app_override(cmd)
        assert keys == ['ctrl', 'n']
        assert skip is False

    def test_bare_none_plugin_format(self):
        cmd = {
            'keys': ['ctrl', 't'],
            'app_overrides': {'notepad.exe': None},
        }
        with patch('samsara.handlers._get_foreground_exe_lower', return_value='notepad.exe'):
            keys, skip = _resolve_app_override(cmd)
        assert keys is None
        assert skip is True

    def test_list_override_passes_through(self):
        cmd = {
            'keys': ['ctrl', 't'],
            'app_overrides': {'code.exe': {'keys': ['ctrl', 'shift', 'n']}},
        }
        with patch('samsara.handlers._get_foreground_exe_lower', return_value='code.exe'):
            keys, skip = _resolve_app_override(cmd)
        assert keys == ['ctrl', 'shift', 'n']
        assert skip is False

    def test_empty_overrides_dict_uses_base(self):
        cmd = {'keys': ['ctrl', 'c'], 'app_overrides': {}}
        keys, skip = _resolve_app_override(cmd)
        assert keys == ['ctrl', 'c']
        assert skip is False


# ---------------------------------------------------------------------------
# HotkeyHandler: suppression and override execution
# ---------------------------------------------------------------------------

class TestHotkeyHandlerOverrides:

    def _make_ctx(self):
        kb = MagicMock()
        ctx = CommandContext(keyboard_controller=kb)
        # Use real key map for ctrl/shift/f4
        from pynput.keyboard import Key
        ctx.key_map = {
            'ctrl': Key.ctrl, 'shift': Key.shift,
            'f4': Key.f4, 't': 't', 'n': 'n', 'w': 'w',
        }
        return ctx, kb

    def test_no_override_sends_base_keys(self):
        ctx, kb = self._make_ctx()
        cmd = {'keys': ['ctrl', 't']}
        HotkeyHandler().execute(cmd, ctx)
        from pynput.keyboard import Key
        kb.press.assert_called()
        kb.release.assert_called()

    def test_null_override_suppresses_keys(self):
        ctx, kb = self._make_ctx()
        cmd = {
            'keys': ['ctrl', 't'],
            'app_overrides': {'code.exe': {'keys': None}},
        }
        with patch('samsara.handlers._get_foreground_exe_lower', return_value='code.exe'):
            result = HotkeyHandler().execute(cmd, ctx)
        assert result is True
        kb.press.assert_not_called()
        kb.release.assert_not_called()

    def test_string_override_sends_resolved_keys(self):
        ctx, kb = self._make_ctx()
        cmd = {
            'keys': ['ctrl', 'w'],
            'app_overrides': {'code.exe': {'keys': 'ctrl+f4'}},
        }
        with patch('samsara.handlers._get_foreground_exe_lower', return_value='code.exe'):
            result = HotkeyHandler().execute(cmd, ctx)
        assert result is True
        from pynput.keyboard import Key
        # ctrl should have been pressed (first of the resolved ['ctrl', 'f4'])
        pressed_keys = [c.args[0] for c in kb.press.call_args_list]
        assert Key.ctrl in pressed_keys
        assert Key.f4 in pressed_keys

    def test_resolution_at_execute_time(self):
        """Changing foreground app between calls uses different keys each time."""
        ctx, kb = self._make_ctx()
        cmd = {
            'keys': ['ctrl', 't'],
            'app_overrides': {'code.exe': {'keys': None}},
        }

        # First call: focused on VS Code — should suppress
        with patch('samsara.handlers._get_foreground_exe_lower', return_value='code.exe'):
            r1 = HotkeyHandler().execute(cmd, ctx)
        assert r1 is True
        kb.press.assert_not_called()

        # Second call: focused on Chrome — should send ctrl+t
        kb.reset_mock()
        with patch('samsara.handlers._get_foreground_exe_lower', return_value='chrome.exe'):
            r2 = HotkeyHandler().execute(cmd, ctx)
        assert r2 is True
        kb.press.assert_called()


# ---------------------------------------------------------------------------
# plugin @command: app_overrides stored in registry
# ---------------------------------------------------------------------------

class TestPluginDecoratorStoresOverrides:

    def test_decorator_stores_app_overrides(self):
        from samsara import plugin_commands as pc
        old_registry = dict(pc._REGISTRY)
        try:
            pc._REGISTRY.clear()

            @pc.command('test action', pack='core',
                        app_overrides={'code.exe': 'ctrl+shift+p', 'notepad.exe': None})
            def _handler(app, remainder):
                return True

            entry = pc._REGISTRY['test action']
            assert entry['app_overrides']['code.exe'] == 'ctrl+shift+p'
            assert entry['app_overrides']['notepad.exe'] is None
        finally:
            pc._REGISTRY.clear()
            pc._REGISTRY.update(old_registry)

    def test_decorator_default_overrides_empty(self):
        from samsara import plugin_commands as pc
        old_registry = dict(pc._REGISTRY)
        try:
            pc._REGISTRY.clear()

            @pc.command('no override cmd', pack='core')
            def _handler(app, remainder):
                return True

            entry = pc._REGISTRY['no override cmd']
            assert entry['app_overrides'] == {}
        finally:
            pc._REGISTRY.clear()
            pc._REGISTRY.update(old_registry)


# ---------------------------------------------------------------------------
# CommandEntry: app_overrides field
# ---------------------------------------------------------------------------

class TestCommandEntryAppOverrides:

    def test_app_overrides_stored(self):
        overrides = {'chrome.exe': {'keys': 'ctrl+t'}}
        e = CommandEntry('new tab', 'builtin', 'hotkey', app_overrides=overrides)
        assert e.app_overrides == overrides

    def test_app_overrides_defaults_to_empty(self):
        e = CommandEntry('copy', 'builtin', 'hotkey')
        assert e.app_overrides == {}

    def test_app_overrides_is_a_copy(self):
        orig = {'code.exe': None}
        e = CommandEntry('test', 'builtin', 'hotkey', app_overrides=orig)
        orig['other.exe'] = 'x'
        assert 'other.exe' not in e.app_overrides


# ---------------------------------------------------------------------------
# commands.json: verify overrides were added correctly
# ---------------------------------------------------------------------------

class TestCommandsJsonOverrides:

    @pytest.fixture(scope='class')
    def commands(self):
        import json
        path = PROJECT_ROOT / 'commands.json'
        return json.loads(path.read_text(encoding='utf-8')).get('commands', {})

    def test_new_tab_has_overrides(self, commands):
        assert 'app_overrides' in commands['new tab']

    def test_new_tab_code_is_null(self, commands):
        override = commands['new tab']['app_overrides'].get('code.exe', {})
        assert override.get('keys') is None

    def test_close_tab_has_code_override(self, commands):
        override = commands['close tab']['app_overrides'].get('code.exe', {})
        assert override.get('keys') == 'ctrl+f4'

    def test_reopen_tab_has_overrides(self, commands):
        assert 'app_overrides' in commands['reopen tab']

    def test_commands_without_overrides_unaffected(self, commands):
        # next tab and previous tab should NOT have app_overrides
        assert 'app_overrides' not in commands.get('next tab', {})
        assert 'app_overrides' not in commands.get('previous tab', {})
