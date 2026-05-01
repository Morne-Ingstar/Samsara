"""Tests for samsara.handlers: per-type handler dispatch with mock contexts."""
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara.handlers import (
    CommandContext,
    HotkeyHandler,
    PressHandler,
    KeyDownHandler,
    KeyUpHandler,
    ReleaseAllHandler,
    MouseHandler,
    LaunchHandler,
    TextHandler,
    MacroHandler,
    MethodHandler,
    get_handler,
    build_handler_registry,
)


def _ctx(**overrides):
    """Build a CommandContext with mock controllers and sensible defaults."""
    defaults = dict(
        keyboard_controller=Mock(),
        mouse_controller=Mock(),
        held_keys={},
        key_map={
            'ctrl': 'CTRL', 'shift': 'SHIFT', 'alt': 'ALT',
            'enter': 'ENTER', 'tab': 'TAB',
        },
        app=None,
    )
    defaults.update(overrides)
    return CommandContext(**defaults)


class TestCommandContext:
    def test_get_key_from_map(self):
        ctx = _ctx()
        assert ctx.get_key('ctrl') == 'CTRL'

    def test_get_key_single_char_lowercased(self):
        ctx = _ctx()
        assert ctx.get_key('A') == 'a'

    def test_get_key_unknown_multi_char_passthrough(self):
        ctx = _ctx()
        assert ctx.get_key('f13') == 'f13'

    def test_held_keys_default_empty_dict(self):
        ctx = CommandContext(keyboard_controller=Mock(), mouse_controller=Mock())
        assert ctx.held_keys == {}

    def test_held_keys_passed_by_reference(self):
        external = {}
        ctx = _ctx(held_keys=external)
        ctx.held_keys['a'] = 1
        assert external == {'a': 1}


class TestHotkeyHandler:
    def test_press_release_sequence(self):
        ctx = _ctx()
        HotkeyHandler().execute({'keys': ['ctrl', 'c']}, ctx)
        # Ctrl pressed, then c pressed+released, then ctrl released
        seq = ctx.keyboard.method_calls
        assert seq[0][0] == 'press' and seq[0][1] == ('CTRL',)
        assert seq[1][0] == 'press' and seq[1][1] == ('c',)
        assert seq[2][0] == 'release' and seq[2][1] == ('c',)
        assert seq[3][0] == 'release' and seq[3][1] == ('CTRL',)


class TestPressHandler:
    def test_single_key(self):
        ctx = _ctx()
        assert PressHandler().execute({'key': 'enter'}, ctx) is True
        ctx.keyboard.press.assert_called_once_with('ENTER')
        ctx.keyboard.release.assert_called_once_with('ENTER')


class TestKeyDownHandler:
    def test_records_held_key(self):
        ctx = _ctx()
        KeyDownHandler().execute({'key': 'shift'}, ctx)
        assert 'shift' in ctx.held_keys
        ctx.keyboard.press.assert_called_once()


class TestKeyUpHandler:
    def test_releases_recorded_key(self):
        ctx = _ctx(held_keys={'shift': 'SHIFT'})
        KeyUpHandler().execute({'key': 'shift'}, ctx)
        assert 'shift' not in ctx.held_keys
        ctx.keyboard.release.assert_called_once_with('SHIFT')

    def test_noop_when_not_held(self):
        ctx = _ctx()
        KeyUpHandler().execute({'key': 'nothing'}, ctx)
        ctx.keyboard.release.assert_not_called()


class TestReleaseAllHandler:
    def test_releases_everything_then_clears(self):
        ctx = _ctx(held_keys={'shift': 'SHIFT', 'ctrl': 'CTRL'})
        ReleaseAllHandler().execute({}, ctx)
        assert ctx.held_keys == {}
        assert ctx.keyboard.release.call_count == 2


class TestMouseHandler:
    def test_single_click(self):
        ctx = _ctx()
        MouseHandler().execute({'action': 'click', 'button': 'left'}, ctx)
        ctx.mouse.click.assert_called_once()

    def test_double_click(self):
        ctx = _ctx()
        MouseHandler().execute({'action': 'double_click'}, ctx)
        ctx.mouse.click.assert_called_once()
        # Second positional arg should be 2 (double click count)
        args = ctx.mouse.click.call_args
        assert args[0][1] == 2


class TestLaunchHandler:
    def test_calls_subprocess(self):
        ctx = _ctx()
        with patch('samsara.handlers.subprocess.Popen') as mock_popen:
            assert LaunchHandler().execute({'target': 'notepad.exe'}, ctx) is True
            mock_popen.assert_called_once()

    def test_exception_returns_false(self):
        ctx = _ctx()
        with patch('samsara.handlers.subprocess.Popen', side_effect=OSError('nope')):
            assert LaunchHandler().execute({'target': 'bogus'}, ctx) is False


class TestTextHandler:
    def test_graceful_failure_without_pyautogui(self):
        """If pyautogui/pyperclip can't be imported, handler returns False."""
        ctx = _ctx()
        with patch.dict('sys.modules', {'pyautogui': None}):
            # pyautogui set to None means `import pyautogui` raises ImportError
            result = TextHandler().execute({'text': 'hi'}, ctx)
        assert result is False

    def test_empty_text_noop_but_success(self):
        ctx = _ctx()
        # pyperclip/pyautogui ARE available in the test env; with empty text
        # the handler short-circuits and still returns True.
        result = TextHandler().execute({'text': ''}, ctx)
        assert result is True


class TestMacroHandler:
    def test_empty_steps_returns_false(self):
        ctx = _ctx()
        assert MacroHandler().execute({'steps': []}, ctx) is False

    def test_multi_step_sequence(self):
        ctx = _ctx()
        cmd = {
            'steps': [
                {'action': 'press', 'key': 'enter', 'delay_after': 0},
                {'action': 'hotkey', 'keys': ['ctrl', 'a'], 'delay_after': 0},
            ]
        }
        assert MacroHandler().execute(cmd, ctx) is True
        # Step 1: press+release Enter; step 2: Ctrl+A combo
        # Total keyboard ops: 2 (step1) + 4 (step2) = 6
        total_calls = ctx.keyboard.press.call_count + ctx.keyboard.release.call_count
        assert total_calls == 6

    def test_unknown_action_logged_not_fatal(self):
        ctx = _ctx()
        cmd = {'steps': [{'action': 'explode', 'delay_after': 0}]}
        assert MacroHandler().execute(cmd, ctx) is True  # continues past unknown


class TestMethodHandler:
    def test_calls_named_method(self):
        app = Mock()
        app.undo_last_dictation = Mock(return_value=None)
        ctx = _ctx(app=app)
        assert MethodHandler().execute({'method': 'undo_last_dictation'}, ctx) is True
        app.undo_last_dictation.assert_called_once()

    def test_missing_method_returns_false(self):
        app = Mock(spec=[])  # no attributes
        ctx = _ctx(app=app)
        assert MethodHandler().execute({'method': 'does_not_exist'}, ctx) is False

    def test_no_app_returns_false(self):
        ctx = _ctx(app=None)
        assert MethodHandler().execute({'method': 'anything'}, ctx) is False

    def test_method_exception_returns_false(self):
        app = Mock()
        app.broken = Mock(side_effect=RuntimeError('boom'))
        ctx = _ctx(app=app)
        assert MethodHandler().execute({'method': 'broken'}, ctx) is False


class TestRegistry:
    def test_get_handler_for_each_type(self):
        for cmd_type in ['hotkey', 'press', 'key_down', 'key_up',
                         'release_all', 'mouse', 'launch', 'text',
                         'macro', 'method']:
            assert get_handler(cmd_type) is not None, cmd_type

    def test_get_handler_unknown_returns_none(self):
        assert get_handler('bogus') is None
        assert get_handler(None) is None

    def test_build_registry_returns_copy(self):
        reg1 = build_handler_registry()
        reg1['bogus'] = 'poisoned'
        reg2 = build_handler_registry()
        assert 'bogus' not in reg2
