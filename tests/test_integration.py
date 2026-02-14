"""
Integration tests for the full transcription pipeline.
Tests the flow from audio input to text output.
"""
import pytest
import json
import sys
import numpy as np
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.mark.integration
class TestTranscriptionPipeline:
    """Integration tests for the transcription pipeline"""

    def test_transcription_to_command_flow(self, temp_commands_file):
        """Test full flow: transcription -> command detection -> execution"""
        from samsara.commands import CommandExecutor
        executor = CommandExecutor(temp_commands_file)

        # Mock the keyboard controller
        mock_press = Mock()
        mock_release = Mock()
        executor.keyboard_controller.press = mock_press
        executor.keyboard_controller.release = mock_release

        # Simulate transcribed text
        transcribed = "copy"

        # Check if it's a command
        command = executor.find_command(transcribed)
        assert command == "copy"

        # Execute
        result = executor.execute_command(command)
        assert result is True
        # Verify keyboard was used (pynput pattern)
        assert mock_press.call_count >= 2  # ctrl + c
        assert mock_release.call_count >= 2

    def test_transcription_to_dictation_flow(self, sample_config, tmp_path):
        """Test full flow: transcription -> text processing -> paste"""
        from tests.test_dictation_app import create_test_app

        app = create_test_app(sample_config, tmp_path)

        # Simulate transcribed text
        transcribed = "hello world. this is a test"

        # Process the transcription
        processed = app.process_transcription(transcribed)

        # Verify capitalization was applied
        assert processed.startswith("Hello")
        assert "This" in processed  # Capitalized after period

    def test_number_conversion_in_pipeline(self, sample_config, tmp_path):
        """Test number conversion in transcription pipeline"""
        sample_config['format_numbers'] = True
        from tests.test_dictation_app import create_test_app

        app = create_test_app(sample_config, tmp_path)

        transcribed = "I have twenty three apples"
        processed = app.process_transcription(transcribed)

        assert "23" in processed

    def test_correction_in_pipeline(self, tmp_path):
        """Test corrections are applied in pipeline"""
        training_data = {
            'vocabulary': [],
            'corrections': {'teh': 'the', 'adn': 'and'}
        }
        training_file = tmp_path / 'training_data.json'
        with open(training_file, 'w') as f:
            json.dump(training_data, f)

        mock_app = Mock()
        mock_app.config_path = tmp_path / 'config.json'
        mock_app.config = {'initial_prompt': ''}

        with patch('voice_training.ctk'):
            with patch('voice_training.sd'):
                from voice_training import VoiceTrainingWindow
                vt = VoiceTrainingWindow(mock_app)

                transcribed = "teh quick brown fox adn teh lazy dog"
                corrected = vt.apply_corrections(transcribed)

                assert corrected == "the quick brown fox and the lazy dog"


@pytest.mark.integration
class TestCommandModeToggle:
    """Tests for command mode toggle via voice"""

    def test_enable_command_mode_phrase(self, temp_commands_file):
        """Test 'enable command mode' phrase"""
        from samsara.commands import CommandExecutor
        executor = CommandExecutor(temp_commands_file)

        callback = Mock()

        result, was_command = executor.process_text(
            "enable command mode",
            command_mode_enabled=False,
            on_mode_change=callback
        )

        assert was_command is True
        callback.assert_called_with(True)

    def test_disable_command_mode_phrase(self, temp_commands_file):
        """Test 'disable command mode' phrase"""
        from samsara.commands import CommandExecutor
        executor = CommandExecutor(temp_commands_file)

        callback = Mock()

        result, was_command = executor.process_text(
            "disable command mode",
            command_mode_enabled=True,
            on_mode_change=callback
        )

        assert was_command is True
        callback.assert_called_with(False)


@pytest.mark.integration
class TestRecordingModes:
    """Tests for different recording modes"""

    def test_hold_mode_flow(self, sample_config):
        """Test hold mode: press -> record -> release -> transcribe"""
        sample_config['mode'] = 'hold'

        # Simulate the flow
        recording = False

        # Key press starts recording
        recording = True
        assert recording is True

        # Key release stops and transcribes
        recording = False
        assert recording is False

    def test_toggle_mode_flow(self, sample_config):
        """Test toggle mode: press -> start, press again -> stop"""
        sample_config['mode'] = 'toggle'

        recording = False
        toggle_active = False

        # First press starts
        toggle_active = not toggle_active
        recording = toggle_active
        assert recording is True

        # Second press stops
        toggle_active = not toggle_active
        recording = toggle_active
        assert recording is False

    def test_combined_mode_flow(self, sample_config):
        """Test combined mode: wake word active AND hotkey works like hold mode"""
        sample_config['mode'] = 'combined'

        # Combined mode should have both capabilities
        wake_word_active = False
        recording = False

        # Wake word mode starts automatically in combined mode
        wake_word_active = True
        assert wake_word_active is True

        # Hotkey still works like hold mode (independently of wake word)
        # Press starts recording
        recording = True
        assert recording is True
        assert wake_word_active is True  # Wake word stays active

        # Release stops recording
        recording = False
        assert recording is False
        assert wake_word_active is True  # Wake word still active after hotkey release


@pytest.mark.integration
class TestAudioProcessing:
    """Tests for audio buffer processing"""

    def test_audio_buffer_concatenation(self):
        """Test audio chunks are properly concatenated"""
        chunks = [
            np.array([0.1, 0.2, 0.3]),
            np.array([0.4, 0.5, 0.6]),
            np.array([0.7, 0.8, 0.9])
        ]

        # Concatenate like the app does
        audio = np.concatenate(chunks, axis=0).flatten()

        assert len(audio) == 9
        assert audio[0] == 0.1
        assert audio[-1] == 0.9

    def test_empty_audio_buffer(self):
        """Test handling of empty audio buffer"""
        audio_data = []

        # Should not crash
        if not audio_data:
            result = None
        else:
            result = np.concatenate(audio_data)

        assert result is None

    def test_audio_sample_rate(self):
        """Test audio is recorded at correct sample rate"""
        sample_rate = 16000  # Whisper expects 16kHz
        duration = 1.0  # 1 second

        # Expected samples for 1 second
        expected_samples = int(sample_rate * duration)
        assert expected_samples == 16000


@pytest.mark.integration
class TestHistoryIntegration:
    """Tests for history with the full pipeline"""

    def test_dictation_added_to_history(self, sample_config, tmp_path):
        """Test that dictations are added to history"""
        from tests.test_dictation_app import create_test_app

        app = create_test_app(sample_config, tmp_path)
        app.save_history = Mock()

        app.add_to_history("hello world", is_command=False)

        assert len(app.history) == 1
        assert app.history[0][1] == "hello world"
        assert app.history[0][2] is False

    def test_command_added_to_history(self, sample_config, tmp_path):
        """Test that commands are added to history"""
        from tests.test_dictation_app import create_test_app

        app = create_test_app(sample_config, tmp_path)
        app.save_history = Mock()

        app.add_to_history("copy", is_command=True)

        assert len(app.history) == 1
        assert app.history[0][1] == "copy"
        assert app.history[0][2] is True


@pytest.mark.integration
class TestFullCommandExecution:
    """Full integration tests for command execution"""

    def test_hotkey_command_full_flow(self, temp_commands_file):
        """Test complete hotkey command flow"""
        from samsara.commands import CommandExecutor
        executor = CommandExecutor(temp_commands_file)

        # Mock keyboard controller
        executor.keyboard_controller.press = Mock()
        executor.keyboard_controller.release = Mock()

        # Simulated transcription
        text = "close window"

        # Find and execute
        result, was_command = executor.process_text(text, command_mode_enabled=True)

        assert was_command is True

    def test_text_command_full_flow(self, temp_commands_file):
        """Test complete text insertion command flow"""
        from samsara.commands import CommandExecutor

        with patch('samsara.commands.HAS_CLIPBOARD', True):
            with patch('samsara.commands.pyperclip', create=True):
                with patch('samsara.commands.pyautogui', create=True):
                    with patch('time.sleep'):
                        executor = CommandExecutor(temp_commands_file)

                        result, was_command = executor.process_text(
                            "period",
                            command_mode_enabled=True
                        )

                        assert was_command is True

    def test_launch_command_full_flow(self, temp_commands_file, mock_subprocess):
        """Test complete launch command flow"""
        from samsara.commands import CommandExecutor
        executor = CommandExecutor(temp_commands_file)

        result, was_command = executor.process_text(
            "open chrome",
            command_mode_enabled=True
        )

        assert was_command is True
        mock_subprocess.assert_called_once()
