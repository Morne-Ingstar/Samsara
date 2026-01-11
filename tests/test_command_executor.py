"""
Tests for CommandExecutor class.
Tests command parsing, matching, and execution.

Uses the modular samsara.commands.CommandExecutor which has built-in
fallback mock classes when pynput/pyautogui are not available.
"""
import pytest
import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import from the modular package
from samsara.commands import CommandExecutor


class TestCommandExecutorInit:
    """Tests for CommandExecutor initialization"""

    def test_load_commands_from_file(self, temp_commands_file):
        """Test loading commands from JSON file"""
        executor = CommandExecutor(temp_commands_file)

        assert 'open chrome' in executor.commands
        assert 'close window' in executor.commands
        assert executor.commands['open chrome']['type'] == 'launch'

    def test_load_commands_missing_file(self, tmp_path):
        """Test behavior when commands file doesn't exist"""
        missing_file = tmp_path / "nonexistent.json"
        executor = CommandExecutor(missing_file)

        assert executor.commands == {}

    def test_load_commands_invalid_json(self, tmp_path):
        """Test behavior with invalid JSON"""
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not valid json {{{")
        executor = CommandExecutor(bad_file)

        assert executor.commands == {}


class TestCommandMatching:
    """Tests for command matching logic"""

    def test_find_exact_command(self, temp_commands_file):
        """Test finding an exact command match"""
        executor = CommandExecutor(temp_commands_file)

        result = executor.find_command("copy")
        assert result == "copy"

    def test_find_command_case_insensitive(self, temp_commands_file):
        """Test case-insensitive command matching"""
        executor = CommandExecutor(temp_commands_file)

        result = executor.find_command("COPY")
        assert result == "copy"

        result = executor.find_command("Copy")
        assert result == "copy"

    def test_find_command_with_extra_whitespace(self, temp_commands_file):
        """Test command matching with extra whitespace"""
        executor = CommandExecutor(temp_commands_file)

        result = executor.find_command("  copy  ")
        assert result == "copy"

    def test_find_command_not_found(self, temp_commands_file):
        """Test when command is not found"""
        executor = CommandExecutor(temp_commands_file)

        result = executor.find_command("nonexistent command")
        assert result is None

    def test_find_multi_word_command(self, temp_commands_file):
        """Test finding multi-word commands"""
        executor = CommandExecutor(temp_commands_file)

        result = executor.find_command("open chrome")
        assert result == "open chrome"

        result = executor.find_command("close window")
        assert result == "close window"


class TestCommandExecution:
    """Tests for command execution"""

    def test_execute_hotkey_command(self, temp_commands_file):
        """Test executing a hotkey command"""
        executor = CommandExecutor(temp_commands_file)

        # Mock the keyboard controller's methods
        mock_press = Mock()
        mock_release = Mock()
        executor.keyboard_controller.press = mock_press
        executor.keyboard_controller.release = mock_release

        result = executor.execute_command("copy")

        assert result is True
        # Should have pressed and released keys
        assert mock_press.call_count >= 2  # ctrl + c
        assert mock_release.call_count >= 2

    def test_execute_press_command(self, temp_commands_file):
        """Test executing a key press command"""
        executor = CommandExecutor(temp_commands_file)

        mock_press = Mock()
        mock_release = Mock()
        executor.keyboard_controller.press = mock_press
        executor.keyboard_controller.release = mock_release

        result = executor.execute_command("new line")

        assert result is True
        mock_press.assert_called_once()
        mock_release.assert_called_once()

    def test_execute_launch_command(self, temp_commands_file, mock_subprocess):
        """Test executing a launch command"""
        executor = CommandExecutor(temp_commands_file)

        result = executor.execute_command("open chrome")

        assert result is True
        mock_subprocess.assert_called_once()

    def test_execute_text_command(self, temp_commands_file, mock_pyperclip, mock_pyautogui):
        """Test executing a text insertion command"""
        with patch('samsara.commands.HAS_CLIPBOARD', True):
            with patch('samsara.commands.pyperclip', create=True) as mock_clip:
                with patch('samsara.commands.pyautogui', create=True) as mock_pag:
                    with patch('time.sleep'):
                        executor = CommandExecutor(temp_commands_file)

                        result = executor.execute_command("period")

                        assert result is True

    def test_execute_mouse_double_click(self, temp_commands_file):
        """Test executing a mouse double-click command"""
        executor = CommandExecutor(temp_commands_file)

        mock_click = Mock()
        executor.mouse_controller.click = mock_click

        result = executor.execute_command("double click")

        assert result is True
        mock_click.assert_called_once()

    def test_execute_key_down(self, temp_commands_file):
        """Test executing a key_down command"""
        executor = CommandExecutor(temp_commands_file)

        mock_press = Mock()
        executor.keyboard_controller.press = mock_press

        result = executor.execute_command("hold shift")

        assert result is True
        mock_press.assert_called_once()
        assert 'shift' in executor.held_keys

    def test_execute_key_up(self, temp_commands_file):
        """Test executing a key_up command"""
        executor = CommandExecutor(temp_commands_file)

        # First hold the key
        executor.held_keys['shift'] = 'shift'

        mock_release = Mock()
        executor.keyboard_controller.release = mock_release

        result = executor.execute_command("release shift")

        assert result is True
        assert 'shift' not in executor.held_keys

    def test_execute_nonexistent_command(self, temp_commands_file):
        """Test executing a command that doesn't exist"""
        executor = CommandExecutor(temp_commands_file)

        result = executor.execute_command("nonexistent")

        assert result is False


class TestProcessText:
    """Tests for process_text which handles command detection in transcribed text"""

    def test_process_text_finds_command(self, temp_commands_file):
        """Test that process_text finds and executes commands"""
        executor = CommandExecutor(temp_commands_file)

        # Mock the keyboard controller
        executor.keyboard_controller.press = Mock()
        executor.keyboard_controller.release = Mock()

        result, was_command = executor.process_text("copy", command_mode_enabled=True)

        assert was_command is True

    def test_process_text_no_command(self, temp_commands_file):
        """Test that process_text returns False for non-commands"""
        executor = CommandExecutor(temp_commands_file)

        result, was_command = executor.process_text("hello world", command_mode_enabled=True)

        assert was_command is False

    def test_process_text_command_mode_toggle(self, temp_commands_file):
        """Test command mode toggle phrases"""
        executor = CommandExecutor(temp_commands_file)
        callback = Mock()

        result, was_command = executor.process_text(
            "enable command mode",
            command_mode_enabled=False,
            on_mode_change=callback
        )

        assert was_command is True
        callback.assert_called_with(True)


class TestHeldKeys:
    """Tests for key holding functionality"""

    def test_release_all_keys(self, temp_commands_file):
        """Test releasing all held keys"""
        executor = CommandExecutor(temp_commands_file)

        mock_release = Mock()
        executor.keyboard_controller.release = mock_release

        # Hold some keys first
        executor.held_keys = {'shift': 'shift_key', 'ctrl': 'ctrl_key'}

        result = executor.execute_command("release all")

        assert result is True
        assert len(executor.held_keys) == 0
        assert mock_release.call_count == 2
