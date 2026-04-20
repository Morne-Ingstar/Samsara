"""
Tests for DictationApp class.
Tests settings management, history, text processing, and hotkey parsing.
"""
import pytest
import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================================
# Helper to create a minimal DictationApp for testing
# ============================================================================

def create_test_app(config, tmp_path):
    """Create a DictationApp instance with mocked dependencies"""
    config_file = tmp_path / "config.json"
    with open(config_file, 'w') as f:
        json.dump(config, f)

    commands_file = tmp_path / "commands.json"
    with open(commands_file, 'w') as f:
        json.dump({"commands": {}}, f)

    # We need to mock many things before importing
    patches = [
        patch('dictation.SplashScreen'),
        patch('dictation.FirstRunWizard'),
        patch('dictation.SettingsWindow'),
        patch('dictation.VoiceTrainingWindow'),
        patch('dictation.HistoryWindow'),
        patch('dictation.CommandExecutor'),
        # dictation imports pynput.keyboard as pynput_keyboard; the bare
        # "keyboard" module is a different library with no Listener.
        patch('dictation.pynput_keyboard.Listener'),
        patch('dictation.sd.query_devices', return_value=[]),
        patch('dictation.WhisperModel'),
        patch('dictation.pystray'),
        patch('dictation.winsound'),
    ]

    for p in patches:
        p.start()

    from dictation import DictationApp

    # Monkey-patch the config path
    original_init = DictationApp.__init__

    def patched_init(self, splash=None):
        self.config_path = config_file
        self.commands_path = commands_file
        self.sounds_dir = tmp_path / "sounds"
        self.sounds_dir.mkdir(exist_ok=True)
        self.history_path = tmp_path / "history.json"
        # Don't call original init, just set up what we need
        self.config = config
        self.history = []
        self.max_history = 100
        self.model_loaded = False
        self.recording = False

    DictationApp.__init__ = patched_init

    app = DictationApp()

    # Restore
    DictationApp.__init__ = original_init

    for p in patches:
        p.stop()

    return app


# ============================================================================
# Config/Settings Tests
# ============================================================================

class TestConfigManagement:
    """Tests for configuration loading and saving"""

    def test_load_config_creates_defaults(self, tmp_path):
        """Test that missing config keys get default values"""
        partial_config = {"hotkey": "ctrl+shift"}
        config_file = tmp_path / "config.json"
        with open(config_file, 'w') as f:
            json.dump(partial_config, f)

        with patch('dictation.SplashScreen'):
            with patch('dictation.pynput_keyboard.Listener'):
                with patch('dictation.sd.query_devices', return_value=[]):
                    # Import the default config to check against
                    from dictation import DictationApp

                    # Create a mock app to test load_config
                    app = Mock()
                    app.config_path = config_file
                    app.config = {}

                    # Call load_config
                    DictationApp.load_config(app)

                    # Check defaults were applied
                    assert app.config['hotkey'] == 'ctrl+shift'
                    assert 'mode' in app.config
                    assert 'language' in app.config

    def test_save_config(self, tmp_path, sample_config):
        """Test saving configuration to file"""
        config_file = tmp_path / "config.json"

        with patch('dictation.SplashScreen'):
            from dictation import DictationApp

            app = Mock()
            app.config_path = config_file
            app.config = sample_config

            DictationApp.save_config(app)

            # Verify file was written
            assert config_file.exists()
            with open(config_file) as f:
                saved = json.load(f)
            assert saved['hotkey'] == sample_config['hotkey']
            assert saved['mode'] == sample_config['mode']


# ============================================================================
# Text Processing Tests
# ============================================================================

class TestTextProcessing:
    """Tests for transcription text processing (auto-capitalize, numbers)"""

    def test_auto_capitalize_first_letter(self, sample_config, tmp_path):
        """Test that first letter is capitalized"""
        sample_config['auto_capitalize'] = True
        sample_config['format_numbers'] = False

        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription("hello world")
        assert result == "Hello world"

    def test_auto_capitalize_after_period(self, sample_config, tmp_path):
        """Test capitalization after periods"""
        sample_config['auto_capitalize'] = True
        sample_config['format_numbers'] = False

        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription("hello. world")
        assert result == "Hello. World"

    def test_auto_capitalize_after_exclamation(self, sample_config, tmp_path):
        """Test capitalization after exclamation marks"""
        sample_config['auto_capitalize'] = True
        sample_config['format_numbers'] = False

        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription("wow! amazing")
        assert result == "Wow! Amazing"

    def test_auto_capitalize_after_question(self, sample_config, tmp_path):
        """Test capitalization after question marks"""
        sample_config['auto_capitalize'] = True
        sample_config['format_numbers'] = False

        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription("what? really")
        assert result == "What? Really"

    def test_auto_capitalize_disabled(self, sample_config, tmp_path):
        """Test that capitalization can be disabled"""
        sample_config['auto_capitalize'] = False
        sample_config['format_numbers'] = False

        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription("hello world")
        assert result == "hello world"

    def test_format_numbers_single_digits(self, sample_config, tmp_path):
        """Test formatting single digit numbers"""
        sample_config['auto_capitalize'] = False
        sample_config['format_numbers'] = True

        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription("I have five apples")
        assert result == "I have 5 apples"

    def test_format_numbers_teens(self, sample_config, tmp_path):
        """Test formatting teen numbers"""
        sample_config['auto_capitalize'] = False
        sample_config['format_numbers'] = True

        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription("there are fifteen items")
        assert result == "there are 15 items"

    def test_format_numbers_compound(self, sample_config, tmp_path):
        """Test formatting compound numbers like twenty-one"""
        sample_config['auto_capitalize'] = False
        sample_config['format_numbers'] = True

        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription("I am twenty one years old")
        assert result == "I am 21 years old"

    def test_format_numbers_with_hyphen(self, sample_config, tmp_path):
        """Test formatting hyphenated compound numbers"""
        sample_config['auto_capitalize'] = False
        sample_config['format_numbers'] = True

        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription("twenty-five dollars")
        assert result == "25 dollars"

    def test_format_numbers_disabled(self, sample_config, tmp_path):
        """Test that number formatting can be disabled"""
        sample_config['auto_capitalize'] = False
        sample_config['format_numbers'] = False

        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription("I have five apples")
        assert result == "I have five apples"

    def test_format_numbers_preserves_punctuation(self, sample_config, tmp_path):
        """Test that punctuation is preserved with number formatting"""
        sample_config['auto_capitalize'] = False
        sample_config['format_numbers'] = True

        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription("five, six, seven.")
        assert "5" in result
        assert "6" in result
        assert "7" in result
        assert "," in result
        assert "." in result

    def test_combined_capitalize_and_numbers(self, sample_config, tmp_path):
        """Test both auto-capitalize and number formatting together"""
        sample_config['auto_capitalize'] = True
        sample_config['format_numbers'] = True

        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription("i have twenty one apples. there are five left")
        assert result.startswith("I have 21")
        assert "5 left" in result

    def test_process_empty_text(self, sample_config, tmp_path):
        """Test processing empty text"""
        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription("")
        assert result == ""

    def test_process_none_text(self, sample_config, tmp_path):
        """Test processing None"""
        app = create_test_app(sample_config, tmp_path)

        result = app.process_transcription(None)
        assert result is None


# ============================================================================
# History Tests
# ============================================================================

class TestHistoryManagement:
    """Tests for dictation history"""

    def test_add_to_history(self, sample_config, tmp_path):
        """Test adding items to history"""
        app = create_test_app(sample_config, tmp_path)
        app.save_history = Mock()  # Don't actually save

        app.add_to_history("hello world", is_command=False)

        assert len(app.history) == 1
        assert app.history[0][1] == "hello world"
        assert app.history[0][2] is False

    def test_add_command_to_history(self, sample_config, tmp_path):
        """Test adding commands to history"""
        app = create_test_app(sample_config, tmp_path)
        app.save_history = Mock()

        app.add_to_history("copy", is_command=True)

        assert len(app.history) == 1
        assert app.history[0][1] == "copy"
        assert app.history[0][2] is True

    def test_history_max_limit(self, sample_config, tmp_path):
        """Test that history respects max limit"""
        app = create_test_app(sample_config, tmp_path)
        app.max_history = 5
        app.save_history = Mock()

        # Add more than max
        for i in range(10):
            app.add_to_history(f"item {i}")

        assert len(app.history) == 5
        # Should have last 5 items
        assert app.history[0][1] == "item 5"
        assert app.history[4][1] == "item 9"

    def test_history_timestamp_format(self, sample_config, tmp_path):
        """Test that history timestamps are formatted correctly"""
        app = create_test_app(sample_config, tmp_path)
        app.save_history = Mock()

        app.add_to_history("test")

        timestamp = app.history[0][0]
        # Should be YYYY-MM-DD HH:MM:SS format
        assert len(timestamp) == 19
        assert timestamp[4] == '-'
        assert timestamp[10] == ' '

    def test_save_history_to_file(self, sample_config, tmp_path):
        """Test saving history to JSON file"""
        app = create_test_app(sample_config, tmp_path)
        app.history = [("2024-01-01 12:00:00", "test", False)]

        # Import the real method
        from dictation import DictationApp
        DictationApp.save_history(app)

        assert app.history_path.exists()
        with open(app.history_path) as f:
            saved = json.load(f)
        assert len(saved) == 1
        assert saved[0][1] == "test"

    def test_load_history_from_file(self, sample_config, tmp_path):
        """Test loading history from JSON file"""
        history_data = [
            ["2024-01-01 12:00:00", "hello", False],
            ["2024-01-01 12:01:00", "copy", True]
        ]
        history_file = tmp_path / "history.json"
        with open(history_file, 'w') as f:
            json.dump(history_data, f)

        app = create_test_app(sample_config, tmp_path)
        app.history_path = history_file

        from dictation import DictationApp
        result = DictationApp.load_history(app)

        assert len(result) == 2
        assert result[0][1] == "hello"
        assert result[1][1] == "copy"

    def test_load_history_missing_file(self, sample_config, tmp_path):
        """Test loading history when file doesn't exist"""
        app = create_test_app(sample_config, tmp_path)
        app.history_path = tmp_path / "nonexistent.json"

        from dictation import DictationApp
        result = DictationApp.load_history(app)

        assert result == []


# ============================================================================
# Hotkey Parsing Tests
# ============================================================================

class TestHotkeyParsing:
    """Tests for hotkey string parsing"""

    def test_parse_simple_hotkey(self, sample_config, tmp_path):
        """Test parsing a simple hotkey"""
        app = create_test_app(sample_config, tmp_path)

        from dictation import DictationApp
        result = DictationApp.parse_hotkey(app, "ctrl+shift")

        assert result == {'ctrl', 'shift'}

    def test_parse_single_key(self, sample_config, tmp_path):
        """Test parsing a single key"""
        app = create_test_app(sample_config, tmp_path)

        from dictation import DictationApp
        result = DictationApp.parse_hotkey(app, "escape")

        assert result == {'escape'}

    def test_parse_three_key_combo(self, sample_config, tmp_path):
        """Test parsing a three-key combination"""
        app = create_test_app(sample_config, tmp_path)

        from dictation import DictationApp
        result = DictationApp.parse_hotkey(app, "ctrl+alt+d")

        assert result == {'ctrl', 'alt', 'd'}

    def test_parse_hotkey_with_spaces(self, sample_config, tmp_path):
        """Test parsing hotkey with spaces around +"""
        app = create_test_app(sample_config, tmp_path)

        from dictation import DictationApp
        result = DictationApp.parse_hotkey(app, "ctrl + shift")

        assert result == {'ctrl', 'shift'}

    def test_parse_hotkey_case_insensitive(self, sample_config, tmp_path):
        """Test that hotkey parsing is case-insensitive"""
        app = create_test_app(sample_config, tmp_path)

        from dictation import DictationApp
        result = DictationApp.parse_hotkey(app, "CTRL+SHIFT")

        assert result == {'ctrl', 'shift'}


# ============================================================================
# Microphone Tests
# ============================================================================

class TestMicrophoneManagement:
    """Tests for microphone handling"""

    def test_get_available_microphones(self, sample_config, tmp_path):
        """Test getting list of available microphones"""
        # Real sounddevice device dicts include 'hostapi'; the enumeration code
        # filters on it, so the mock has to carry the same shape.
        mock_devices = [
            {'name': 'Microphone 1', 'max_input_channels': 2, 'index': 0, 'hostapi': 0},
            {'name': 'Microphone 2', 'max_input_channels': 1, 'index': 1, 'hostapi': 0},
            {'name': 'Speakers', 'max_input_channels': 0, 'index': 2, 'hostapi': 0},
        ]
        mock_hostapis = [{'name': 'MME', 'devices': [0, 1, 2]}]

        with patch('sounddevice.query_devices', return_value=mock_devices), \
             patch('sounddevice.query_hostapis', return_value=mock_hostapis):
            app = create_test_app(sample_config, tmp_path)

            from dictation import DictationApp
            result = DictationApp.get_available_microphones(app)

            # Should only include input devices
            input_mics = [m for m in result if m['name'] != 'Speakers']
            assert len(input_mics) >= 1
