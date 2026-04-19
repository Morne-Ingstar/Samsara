"""
Tests for the new modular Samsara components.

Tests the samsara package modules: config, audio, speech, commands.
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestConfig:
    """Tests for the Config module."""

    def test_default_config(self, tmp_path):
        """Test config loads defaults when no file exists."""
        from samsara._stale.config import Config

        config_file = tmp_path / "config.json"
        config = Config(config_file)

        assert config.get('hotkey') == 'ctrl+shift'
        assert config.get('model_size') == 'base'
        assert config.get('language') == 'en'
        assert config.get('auto_capitalize') is True
        assert config.get('format_numbers') is True

    def test_config_load_existing(self, tmp_path):
        """Test config loads from existing file."""
        from samsara._stale.config import Config

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            'hotkey': 'ctrl+alt',
            'model_size': 'small',
        }))

        config = Config(config_file)

        assert config.get('hotkey') == 'ctrl+alt'
        assert config.get('model_size') == 'small'
        # Should still have defaults for missing keys
        assert config.get('language') == 'en'

    def test_config_save(self, tmp_path):
        """Test config saves to file."""
        from samsara._stale.config import Config

        config_file = tmp_path / "config.json"
        config = Config(config_file)
        config.set('hotkey', 'ctrl+space')

        # Reload and verify
        config2 = Config(config_file)
        assert config2.get('hotkey') == 'ctrl+space'

    def test_config_dict_access(self, tmp_path):
        """Test dict-like access to config."""
        from samsara._stale.config import Config

        config = Config(tmp_path / "config.json")

        assert config['hotkey'] == 'ctrl+shift'
        config['hotkey'] = 'ctrl+alt'
        assert config['hotkey'] == 'ctrl+alt'
        assert 'hotkey' in config

    def test_config_update(self, tmp_path):
        """Test updating multiple config values."""
        from samsara._stale.config import Config

        config = Config(tmp_path / "config.json")
        config.update({
            'hotkey': 'ctrl+alt',
            'model_size': 'large-v3',
        }, save=False)

        assert config.get('hotkey') == 'ctrl+alt'
        assert config.get('model_size') == 'large-v3'

    def test_needs_first_run(self, tmp_path):
        """Test first run detection."""
        from samsara._stale.config import Config

        # No config file - needs first run
        config_file = tmp_path / "config.json"
        config = Config(config_file)
        assert config.needs_first_run is True  # File doesn't exist yet

        # Save to create file with defaults (first_run_complete: True)
        config.save()
        config2 = Config(config_file)
        assert config2.needs_first_run is False  # File exists with first_run_complete: True

        # File with first_run_complete = False - needs first run
        config_file.write_text(json.dumps({'first_run_complete': False}))
        config3 = Config(config_file)
        assert config3.needs_first_run is True


class TestTextProcessor:
    """Tests for the TextProcessor module."""

    def test_auto_capitalize(self):
        """Test auto-capitalization."""
        from samsara._stale.speech import TextProcessor

        processor = TextProcessor(auto_capitalize=True, format_numbers=False)

        assert processor.capitalize("hello world") == "Hello world"
        assert processor.capitalize("test. another sentence") == "Test. Another sentence"
        assert processor.capitalize("hello! how are you?") == "Hello! How are you?"

    def test_number_formatting(self):
        """Test number word to digit conversion."""
        from samsara._stale.speech import TextProcessor

        processor = TextProcessor(auto_capitalize=False, format_numbers=True)

        assert "21" in processor.convert_numbers("twenty one apples")
        assert "35" in processor.convert_numbers("thirty five")
        assert "5" in processor.convert_numbers("five items")
        assert "10" in processor.convert_numbers("ten things")

    def test_corrections(self):
        """Test word corrections."""
        from samsara._stale.speech import TextProcessor

        processor = TextProcessor(
            auto_capitalize=False,
            format_numbers=False,
            corrections={'teh': 'the', 'adn': 'and'},
        )

        result = processor.apply_corrections("teh quick brown fox adn teh lazy dog")
        assert result == "the quick brown fox and the lazy dog"

    def test_full_processing(self):
        """Test full text processing pipeline."""
        from samsara._stale.speech import TextProcessor

        processor = TextProcessor(
            auto_capitalize=True,
            format_numbers=True,
            corrections={'teh': 'the'},
        )

        result = processor.process("teh answer is twenty one")
        assert result.startswith("The")  # Capitalized
        assert "21" in result  # Number formatted
        assert "teh" not in result.lower()  # Corrected


class TestCommandExecutor:
    """Tests for the CommandExecutor module."""

    @pytest.fixture
    def commands_file(self, tmp_path):
        """Create a test commands file."""
        commands = {
            'commands': {
                'copy': {'type': 'hotkey', 'keys': ['ctrl', 'c']},
                'paste': {'type': 'hotkey', 'keys': ['ctrl', 'v']},
                'period': {'type': 'text', 'text': '.'},
                'enter': {'type': 'press', 'key': 'enter'},
            }
        }
        file_path = tmp_path / 'commands.json'
        file_path.write_text(json.dumps(commands))
        return file_path

    def test_load_commands(self, commands_file):
        """Test loading commands from file."""
        # CommandExecutor has fallback mock classes when pynput is not available
        from samsara.commands import CommandExecutor

        executor = CommandExecutor(commands_file)

        assert 'copy' in executor.commands
        assert 'paste' in executor.commands
        assert 'period' in executor.commands
        assert len(executor.commands) == 4

    def test_find_command_exact(self, commands_file):
        """Test finding command with exact match."""
        from samsara.commands import CommandExecutor

        executor = CommandExecutor(commands_file)

        assert executor.find_command("copy") == "copy"
        assert executor.find_command("paste") == "paste"
        assert executor.find_command("unknown") is None

    def test_find_command_partial(self, commands_file):
        """Test finding command with partial match."""
        from samsara.commands import CommandExecutor

        executor = CommandExecutor(commands_file)

        assert executor.find_command("copy that") == "copy"
        assert executor.find_command("please copy") == "copy"

    def test_process_text_command_mode_toggle(self, commands_file):
        """Test command mode toggle detection."""
        from samsara.commands import CommandExecutor

        executor = CommandExecutor(commands_file)
        callback = Mock()

        result, was_command = executor.process_text(
            "enable command mode",
            command_mode_enabled=False,
            on_mode_change=callback,
        )

        assert was_command is True
        assert result == "command_mode_on"
        callback.assert_called_with(True)

    def test_add_remove_command(self, commands_file):
        """Test adding and removing commands."""
        from samsara.commands import CommandExecutor

        executor = CommandExecutor(commands_file)

        executor.add_command('test', 'hotkey', keys=['ctrl', 't'])
        assert 'test' in executor.commands

        executor.remove_command('test')
        assert 'test' not in executor.commands


class TestAudioCapture:
    """Tests for the AudioCapture module."""

    def test_get_devices(self):
        """Test getting audio devices."""
        mock_sd = MagicMock()
        mock_sd.query_devices.return_value = [
            {'name': 'Test Mic', 'max_input_channels': 2},
            {'name': 'Stereo Mix', 'max_input_channels': 2},  # Should be filtered
            {'name': 'Another Mic', 'max_input_channels': 1},
        ]

        with patch.dict('sys.modules', {'sounddevice': mock_sd}):
            with patch('samsara._stale.audio.sd', mock_sd):
                with patch('samsara._stale.audio.HAS_SOUNDDEVICE', True):
                    from samsara._stale.audio import AudioCapture

                    devices = AudioCapture.get_devices(show_all=False)

                    assert len(devices) == 2
                    assert any(d['name'] == 'Test Mic' for d in devices)
                    assert not any(d['name'] == 'Stereo Mix' for d in devices)

    def test_get_devices_show_all(self):
        """Test getting all audio devices."""
        mock_sd = MagicMock()
        mock_sd.query_devices.return_value = [
            {'name': 'Test Mic', 'max_input_channels': 2},
            {'name': 'Stereo Mix', 'max_input_channels': 2},
        ]

        with patch.dict('sys.modules', {'sounddevice': mock_sd}):
            with patch('samsara._stale.audio.sd', mock_sd):
                with patch('samsara._stale.audio.HAS_SOUNDDEVICE', True):
                    from samsara._stale.audio import AudioCapture

                    devices = AudioCapture.get_devices(show_all=True)

                    assert len(devices) == 2


class TestAudioPlayer:
    """Tests for the AudioPlayer module."""

    def test_volume_bounds(self, tmp_path):
        """Test volume is bounded between 0 and 1."""
        from samsara._stale.audio import AudioPlayer

        player = AudioPlayer(sounds_dir=tmp_path, volume=1.5)
        assert player.volume == 1.0

        player.set_volume(-0.5)
        assert player.volume == 0.0

        player.set_volume(0.7)
        assert player.volume == 0.7

    def test_enabled_flag(self, tmp_path):
        """Test enabled flag controls playback."""
        from samsara._stale.audio import AudioPlayer

        player = AudioPlayer(sounds_dir=tmp_path, enabled=False)
        assert player.enabled is False

        player.set_enabled(True)
        assert player.enabled is True


class TestSpeechRecognizer:
    """Tests for the SpeechRecognizer module."""

    def test_init(self):
        """Test initializing recognizer."""
        from samsara._stale.speech import SpeechRecognizer

        recognizer = SpeechRecognizer(
            model_size='base',
            device='cpu',
            language='en',
        )

        assert recognizer.model_size == 'base'
        assert recognizer.device == 'cpu'
        assert recognizer.language == 'en'
        assert recognizer.is_loaded is False
        assert recognizer.is_loading is False

    def test_set_model_size(self):
        """Test changing model size resets loaded state."""
        from samsara._stale.speech import SpeechRecognizer

        recognizer = SpeechRecognizer(model_size='base')
        recognizer._loaded = True  # Simulate loaded state

        recognizer.set_model_size('small')

        assert recognizer.model_size == 'small'
        assert recognizer.is_loaded is False

    def test_transcribe_not_loaded(self):
        """Test transcribe returns error when not loaded."""
        from samsara._stale.speech import SpeechRecognizer

        recognizer = SpeechRecognizer()
        audio = np.zeros(16000, dtype=np.float32)

        text, info = recognizer.transcribe(audio)

        assert text == ""
        assert "error" in info


class TestModuleImports:
    """Tests for module imports and package structure."""

    def test_import_package(self):
        """Test importing the main package."""
        import samsara

        assert hasattr(samsara, '__version__')
        assert hasattr(samsara, 'CommandExecutor')

    def test_import_submodules(self):
        """Test importing individual submodules."""
        from samsara._stale.config import Config
        from samsara._stale.audio import AudioCapture, AudioPlayer
        from samsara._stale.speech import SpeechRecognizer, TextProcessor
        from samsara.commands import CommandExecutor

        assert Config is not None
        assert AudioCapture is not None
        assert AudioPlayer is not None
        assert SpeechRecognizer is not None
        assert TextProcessor is not None
        assert CommandExecutor is not None
