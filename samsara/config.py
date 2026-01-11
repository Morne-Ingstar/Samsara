"""
Samsara Configuration Management

Handles loading, saving, and managing application configuration.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional


class Config:
    """Configuration manager with defaults and persistence."""

    DEFAULT_CONFIG: Dict[str, Any] = {
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
        "command_mode_enabled": False,
        "wake_word": "hey claude",
        "wake_word_timeout": 5.0,
        "show_all_audio_devices": False,
        "audio_feedback": True,
        "sound_volume": 0.5,
        "first_run_complete": True,
    }

    def __init__(self, config_path: Optional[Path] = None):
        """
        Initialize configuration manager.

        Args:
            config_path: Path to config file. If None, uses default location.
        """
        if config_path is None:
            config_path = Path(__file__).parent.parent / "config.json"
        self.config_path = Path(config_path)
        self._config: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        """Load configuration from file, applying defaults for missing keys."""
        self._config = self.DEFAULT_CONFIG.copy()

        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    self._config.update(loaded)
            except (json.JSONDecodeError, IOError) as e:
                print(f"[WARN] Could not load config: {e}")

    def save(self) -> None:
        """Save current configuration to file."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self._config, f, indent=2)
        except IOError as e:
            print(f"[ERROR] Could not save config: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration value.

        Args:
            key: Configuration key
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        return self._config.get(key, default)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        """
        Set a configuration value.

        Args:
            key: Configuration key
            value: Value to set
            save: Whether to save to file immediately
        """
        self._config[key] = value
        if save:
            self.save()

    def update(self, values: Dict[str, Any], save: bool = True) -> None:
        """
        Update multiple configuration values.

        Args:
            values: Dictionary of key-value pairs to update
            save: Whether to save to file immediately
        """
        self._config.update(values)
        if save:
            self.save()

    def __getitem__(self, key: str) -> Any:
        """Allow dict-like access: config['key']"""
        return self._config[key]

    def __setitem__(self, key: str, value: Any) -> None:
        """Allow dict-like assignment: config['key'] = value"""
        self.set(key, value)

    def __contains__(self, key: str) -> bool:
        """Allow 'in' operator: 'key' in config"""
        return key in self._config

    def keys(self):
        """Return config keys."""
        return self._config.keys()

    def items(self):
        """Return config items."""
        return self._config.items()

    def to_dict(self) -> Dict[str, Any]:
        """Return a copy of the configuration as a dictionary."""
        return self._config.copy()

    @property
    def needs_first_run(self) -> bool:
        """Check if first-run wizard is needed."""
        if not self.config_path.exists():
            return True
        return self.get('first_run_complete') is False

    def reset_to_defaults(self) -> None:
        """Reset configuration to defaults."""
        self._config = self.DEFAULT_CONFIG.copy()
        self.save()
