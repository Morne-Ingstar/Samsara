"""
Shared fixtures and mocks for Samsara tests.
"""
import pytest
import json
import sys
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _isolate_plugin_registry(tmp_path, monkeypatch):
    """Keep the real plugins/ directory from leaking into tests.

    Tests that want plugin behavior can register via the @command decorator
    after this fixture clears the global registry.
    """
    from samsara import plugin_commands as _plugin_commands
    saved = dict(_plugin_commands._REGISTRY)
    _plugin_commands._REGISTRY.clear()

    original_load = _plugin_commands.load_plugins

    def _scoped_load(plugins_dir):
        plugins_dir = Path(plugins_dir)
        project_plugins = Path(__file__).parent.parent / "plugins" / "commands"
        if plugins_dir.resolve() == project_plugins.resolve():
            return 0
        return original_load(plugins_dir)

    monkeypatch.setattr(_plugin_commands, "load_plugins", _scoped_load)
    try:
        yield
    finally:
        _plugin_commands._REGISTRY.clear()
        _plugin_commands._REGISTRY.update(saved)


# ============================================================================
# Mock Classes
# ============================================================================

class MockWhisperModel:
    """Mock for faster_whisper.WhisperModel"""

    def __init__(self, model_size="base", device="cpu", compute_type="int8"):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type

    def transcribe(self, audio, language="en", beam_size=5, vad_filter=True, initial_prompt=None):
        """Return mock transcription segments"""
        mock_segment = Mock()
        mock_segment.text = "hello world"
        mock_info = Mock()
        mock_info.language = language
        return [mock_segment], mock_info


class MockAudioStream:
    """Mock for sounddevice.InputStream"""

    def __init__(self, **kwargs):
        self.callback = kwargs.get('callback')
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def close(self):
        self.running = False


class MockKeyboardListener:
    """Mock for pynput.keyboard.Listener"""

    def __init__(self, on_press=None, on_release=None):
        self.on_press = on_press
        self.on_release = on_release
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class MockTrayIcon:
    """Mock for pystray.Icon"""

    def __init__(self, name, icon=None, title=None, menu=None):
        self.name = name
        self.icon = icon
        self.title = title
        self.menu = menu
        self.visible = False

    def run(self):
        self.visible = True

    def stop(self):
        self.visible = False


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def sample_commands():
    """Sample commands for testing"""
    return {
        "commands": {
            "open chrome": {
                "type": "launch",
                "target": "chrome.exe",
                "description": "Open Google Chrome"
            },
            "close window": {
                "type": "hotkey",
                "keys": ["alt", "f4"],
                "description": "Close active window"
            },
            "copy": {
                "type": "hotkey",
                "keys": ["ctrl", "c"],
                "description": "Copy selection"
            },
            "period": {
                "type": "text",
                "text": ".",
                "description": "Insert period"
            },
            "new line": {
                "type": "press",
                "key": "enter",
                "description": "Insert new line"
            },
            "double click": {
                "type": "mouse",
                "action": "double_click",
                "description": "Double click mouse"
            },
            "hold shift": {
                "type": "key_down",
                "key": "shift",
                "description": "Hold Shift key"
            },
            "release shift": {
                "type": "key_up",
                "key": "shift",
                "description": "Release Shift key"
            },
            "release all": {
                "type": "release_all",
                "description": "Release all held keys"
            }
        }
    }


@pytest.fixture
def sample_config():
    """Sample configuration for testing"""
    return {
        "hotkey": "ctrl+shift",
        "continuous_hotkey": "ctrl+alt+d",
        "wake_word_hotkey": "ctrl+alt+w",
        "cancel_hotkey": "escape",
        "mode": "hold",
        "model_size": "base",
        "language": "en",
        "auto_paste": True,
        "add_trailing_space": True,
        "auto_capitalize": True,
        "format_numbers": True,
        "device": "auto",
        "microphone": None,
        "silence_threshold": 2.0,
        "min_speech_duration": 0.3,
        "command_mode_enabled": True,
        "wake_word": "hey samsara",
        "wake_word_timeout": 5.0,
        "audio_feedback": True,
        "sound_volume": 0.5
    }


@pytest.fixture
def temp_commands_file(tmp_path, sample_commands):
    """Create a temporary commands.json file"""
    commands_file = tmp_path / "commands.json"
    with open(commands_file, 'w') as f:
        json.dump(sample_commands, f)
    return commands_file


@pytest.fixture
def temp_config_file(tmp_path, sample_config):
    """Create a temporary config.json file"""
    config_file = tmp_path / "config.json"
    with open(config_file, 'w') as f:
        json.dump(sample_config, f)
    return config_file


@pytest.fixture
def mock_whisper():
    """Mock the WhisperModel"""
    with patch('faster_whisper.WhisperModel', MockWhisperModel):
        yield MockWhisperModel


@pytest.fixture
def mock_audio():
    """Mock sounddevice"""
    with patch('sounddevice.InputStream', MockAudioStream):
        with patch('sounddevice.query_devices', return_value=[
            {'name': 'Test Microphone', 'max_input_channels': 2, 'index': 0},
            {'name': 'Another Mic', 'max_input_channels': 1, 'index': 1},
        ]):
            yield


@pytest.fixture
def mock_keyboard():
    """Mock pynput keyboard"""
    with patch('pynput.keyboard.Listener', MockKeyboardListener):
        with patch('pynput.keyboard.Controller'):
            yield


@pytest.fixture
def mock_tray():
    """Mock pystray"""
    with patch('pystray.Icon', MockTrayIcon):
        yield


@pytest.fixture
def mock_pyautogui():
    """Mock pyautogui"""
    with patch('pyautogui.hotkey') as mock_hotkey:
        with patch('pyautogui.press') as mock_press:
            with patch('pyautogui.click') as mock_click:
                with patch('pyautogui.doubleClick') as mock_double:
                    with patch('pyautogui.keyDown') as mock_keydown:
                        with patch('pyautogui.keyUp') as mock_keyup:
                            yield {
                                'hotkey': mock_hotkey,
                                'press': mock_press,
                                'click': mock_click,
                                'doubleClick': mock_double,
                                'keyDown': mock_keydown,
                                'keyUp': mock_keyup
                            }


@pytest.fixture
def mock_pyperclip():
    """Mock pyperclip"""
    clipboard = {'content': ''}

    def mock_copy(text):
        clipboard['content'] = text

    def mock_paste():
        return clipboard['content']

    with patch('pyperclip.copy', mock_copy):
        with patch('pyperclip.paste', mock_paste):
            yield clipboard


@pytest.fixture
def mock_subprocess():
    """Mock subprocess for launch commands"""
    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = Mock()
        yield mock_popen
