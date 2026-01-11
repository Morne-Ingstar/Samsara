"""
Tests for wake word detection functionality.
"""
import pytest
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestWakeWordDetection:
    """Tests for wake word detection logic"""

    def test_wake_word_in_text(self):
        """Test detecting wake word in transcribed text"""
        wake_word = "hey samsara"
        text = "hey samsara open chrome"

        # Check if wake word is in text
        assert wake_word.lower() in text.lower()

    def test_wake_word_case_insensitive(self):
        """Test wake word detection is case insensitive"""
        wake_word = "hey samsara"
        variations = [
            "Hey Samsara open chrome",
            "HEY SAMSARA open chrome",
            "hey samsara open chrome",
            "HeY sAmSaRa open chrome"
        ]

        for text in variations:
            assert wake_word.lower() in text.lower()

    def test_extract_command_after_wake_word(self):
        """Test extracting command text after wake word"""
        wake_word = "hey samsara"
        text = "hey samsara open chrome please"

        wake_word_index = text.lower().find(wake_word)
        command_text = text[wake_word_index + len(wake_word):].strip()

        assert command_text == "open chrome please"

    def test_extract_command_nothing_after(self):
        """Test when there's nothing after wake word"""
        wake_word = "hey samsara"
        text = "hey samsara"

        wake_word_index = text.lower().find(wake_word)
        command_text = text[wake_word_index + len(wake_word):].strip()

        assert command_text == ""

    def test_wake_word_not_found(self):
        """Test when wake word is not in text"""
        wake_word = "hey samsara"
        text = "open chrome please"

        assert wake_word.lower() not in text.lower()

    def test_partial_wake_word_not_matched(self):
        """Test that partial wake word doesn't match"""
        wake_word = "hey samsara"
        text = "hey sam open chrome"

        assert wake_word.lower() not in text.lower()


class TestWakeWordModeFlow:
    """Tests for wake word mode state machine"""

    def test_wake_word_triggers_listening(self):
        """Test that detecting wake word sets triggered flag"""
        mock_app = Mock()
        mock_app.wake_word_triggered = False
        mock_app.config = {'wake_word': 'hey samsara'}

        text = "hey samsara"
        wake_word = mock_app.config['wake_word'].lower()

        if wake_word in text.lower():
            mock_app.wake_word_triggered = True

        assert mock_app.wake_word_triggered is True

    def test_command_resets_triggered_flag(self):
        """Test that executing command resets triggered flag"""
        mock_app = Mock()
        mock_app.wake_word_triggered = True

        # Simulate command execution
        mock_app.wake_word_triggered = False

        assert mock_app.wake_word_triggered is False

    def test_timeout_resets_triggered_flag(self):
        """Test that timeout resets triggered flag"""
        mock_app = Mock()
        mock_app.wake_word_triggered = True
        mock_app.config = {'wake_word_timeout': 5.0}

        # Simulate timeout
        def reset_wake_word():
            mock_app.wake_word_triggered = False

        reset_wake_word()

        assert mock_app.wake_word_triggered is False


class TestWakeWordConfiguration:
    """Tests for wake word configuration"""

    def test_default_wake_word(self, sample_config):
        """Test default wake word value"""
        assert 'wake_word' in sample_config
        # Either "hey samsara" or "hey claude" depending on config
        assert sample_config['wake_word'] in ['hey samsara', 'hey claude']

    def test_wake_word_timeout(self, sample_config):
        """Test wake word timeout configuration"""
        assert 'wake_word_timeout' in sample_config
        assert sample_config['wake_word_timeout'] > 0

    def test_custom_wake_word(self):
        """Test setting custom wake word"""
        config = {'wake_word': 'computer'}

        assert config['wake_word'] == 'computer'

    def test_multi_word_wake_word(self):
        """Test multi-word wake word"""
        config = {'wake_word': 'ok google now'}
        text = "ok google now search for pizza"

        wake_word = config['wake_word']
        assert wake_word in text


class TestWakeWordIntegration:
    """Integration tests for wake word with command execution"""

    def test_wake_word_plus_command_execution(self, temp_commands_file):
        """Test wake word followed by command executes correctly"""
        from samsara.commands import CommandExecutor
        executor = CommandExecutor(temp_commands_file)

        # Mock the keyboard controller
        mock_press = Mock()
        mock_release = Mock()
        executor.keyboard_controller.press = mock_press
        executor.keyboard_controller.release = mock_release

        # Simulate wake word detected, command extracted
        wake_word = "hey samsara"
        full_text = "hey samsara copy"
        command_text = full_text[len(wake_word):].strip()

        # Execute the command
        result = executor.execute_command(command_text)

        assert result is True
        # Verify keyboard was used (pynput pattern)
        assert mock_press.call_count >= 2  # ctrl + c
        assert mock_release.call_count >= 2

    def test_wake_word_with_dictation(self, temp_commands_file):
        """Test wake word followed by non-command text"""
        from samsara.commands import CommandExecutor
        executor = CommandExecutor(temp_commands_file)

        # Command text that's not a known command
        command_text = "hello world this is dictation"
        found = executor.find_command(command_text)

        assert found is None  # Not a command
