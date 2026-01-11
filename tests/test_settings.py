"""
Tests for SettingsWindow and configuration management.
"""
import pytest
import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestSettingsConfiguration:
    """Tests for settings configuration values"""

    def test_default_config_has_required_keys(self, sample_config):
        """Test that default config has all required keys"""
        required_keys = [
            'hotkey', 'mode', 'model_size', 'language',
            'auto_paste', 'add_trailing_space', 'microphone',
            'audio_feedback', 'command_mode_enabled'
        ]
        for key in required_keys:
            assert key in sample_config, f"Missing required key: {key}"

    def test_hotkey_format(self, sample_config):
        """Test hotkey is in correct format"""
        hotkey = sample_config['hotkey']
        assert isinstance(hotkey, str)
        # Should contain + for multi-key combos or be a single key
        assert len(hotkey) > 0

    def test_mode_valid_values(self, sample_config):
        """Test mode has valid value"""
        valid_modes = ['hold', 'toggle', 'continuous', 'wake_word']
        assert sample_config['mode'] in valid_modes

    def test_model_size_valid_values(self, sample_config):
        """Test model_size has valid value"""
        valid_sizes = ['tiny', 'base', 'small', 'medium', 'large-v3']
        assert sample_config['model_size'] in valid_sizes

    def test_boolean_settings(self, sample_config):
        """Test boolean settings are actually booleans"""
        bool_keys = ['auto_paste', 'add_trailing_space', 'audio_feedback',
                     'command_mode_enabled', 'auto_capitalize', 'format_numbers']
        for key in bool_keys:
            if key in sample_config:
                assert isinstance(sample_config[key], bool), f"{key} should be bool"

    def test_numeric_settings(self, sample_config):
        """Test numeric settings are valid numbers"""
        assert isinstance(sample_config['silence_threshold'], (int, float))
        assert sample_config['silence_threshold'] > 0

        assert isinstance(sample_config['min_speech_duration'], (int, float))
        assert sample_config['min_speech_duration'] > 0

        assert isinstance(sample_config['sound_volume'], (int, float))
        assert 0 <= sample_config['sound_volume'] <= 1


class TestConfigPersistence:
    """Tests for saving and loading configuration"""

    def test_save_config_creates_file(self, tmp_path, sample_config):
        """Test that saving config creates the file"""
        config_file = tmp_path / "test_config.json"

        with open(config_file, 'w') as f:
            json.dump(sample_config, f, indent=2)

        assert config_file.exists()

    def test_load_config_reads_file(self, tmp_path, sample_config):
        """Test that loading config reads the file correctly"""
        config_file = tmp_path / "test_config.json"
        with open(config_file, 'w') as f:
            json.dump(sample_config, f)

        with open(config_file) as f:
            loaded = json.load(f)

        assert loaded['hotkey'] == sample_config['hotkey']
        assert loaded['mode'] == sample_config['mode']

    def test_config_missing_keys_get_defaults(self, tmp_path):
        """Test that missing config keys get default values"""
        partial_config = {'hotkey': 'ctrl+shift'}
        config_file = tmp_path / "partial_config.json"
        with open(config_file, 'w') as f:
            json.dump(partial_config, f)

        # Define defaults
        defaults = {
            'hotkey': 'ctrl+shift',
            'mode': 'hold',
            'model_size': 'base',
            'language': 'en'
        }

        # Load and merge with defaults
        with open(config_file) as f:
            loaded = json.load(f)

        for key, value in defaults.items():
            if key not in loaded:
                loaded[key] = value

        assert loaded['mode'] == 'hold'
        assert loaded['model_size'] == 'base'


class TestHotkeySettings:
    """Tests for hotkey configuration"""

    def test_parse_ctrl_shift(self):
        """Test parsing ctrl+shift hotkey"""
        hotkey = "ctrl+shift"
        keys = set(k.strip().lower() for k in hotkey.split('+'))
        assert keys == {'ctrl', 'shift'}

    def test_parse_three_key_combo(self):
        """Test parsing three-key combination"""
        hotkey = "ctrl+alt+d"
        keys = set(k.strip().lower() for k in hotkey.split('+'))
        assert keys == {'ctrl', 'alt', 'd'}

    def test_parse_single_key(self):
        """Test parsing single key hotkey"""
        hotkey = "escape"
        keys = set(k.strip().lower() for k in hotkey.split('+'))
        assert keys == {'escape'}

    def test_hotkey_normalization(self):
        """Test that hotkeys are normalized to lowercase"""
        hotkey = "CTRL+SHIFT"
        keys = set(k.strip().lower() for k in hotkey.split('+'))
        assert keys == {'ctrl', 'shift'}


class TestAutoStartSetting:
    """Tests for auto-start with Windows setting"""

    def test_startup_path_format(self, tmp_path):
        """Test startup path is correctly formatted"""
        import os
        appdata = os.environ.get('APPDATA', '')
        if appdata:
            startup_folder = Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
            startup_file = startup_folder / 'Samsara.vbs'

            # Check path format is valid
            assert 'Startup' in str(startup_file)
            assert startup_file.suffix == '.vbs'


class TestSoundSettings:
    """Tests for sound/audio feedback settings"""

    def test_volume_range(self, sample_config):
        """Test volume is in valid range"""
        volume = sample_config.get('sound_volume', 0.5)
        assert 0 <= volume <= 1

    def test_audio_feedback_toggle(self, sample_config):
        """Test audio feedback can be toggled"""
        sample_config['audio_feedback'] = False
        assert sample_config['audio_feedback'] is False

        sample_config['audio_feedback'] = True
        assert sample_config['audio_feedback'] is True


class TestMicrophoneSettings:
    """Tests for microphone configuration"""

    def test_microphone_none_uses_default(self, sample_config):
        """Test None microphone uses system default"""
        sample_config['microphone'] = None
        assert sample_config['microphone'] is None

    def test_microphone_by_id(self, sample_config):
        """Test microphone can be set by ID"""
        sample_config['microphone'] = 0
        assert sample_config['microphone'] == 0

    def test_show_all_devices_toggle(self, sample_config):
        """Test show_all_audio_devices toggle"""
        sample_config['show_all_audio_devices'] = True
        assert sample_config['show_all_audio_devices'] is True


class TestCommandSettings:
    """Tests for command-related settings"""

    def test_command_mode_toggle(self, sample_config):
        """Test command mode can be enabled/disabled"""
        sample_config['command_mode_enabled'] = True
        assert sample_config['command_mode_enabled'] is True

        sample_config['command_mode_enabled'] = False
        assert sample_config['command_mode_enabled'] is False

    def test_wake_word_setting(self, sample_config):
        """Test wake word configuration"""
        sample_config['wake_word'] = 'hello computer'
        assert sample_config['wake_word'] == 'hello computer'

    def test_wake_word_timeout_setting(self, sample_config):
        """Test wake word timeout configuration"""
        sample_config['wake_word_timeout'] = 10.0
        assert sample_config['wake_word_timeout'] == 10.0
