"""A headless stand-in for the running app, so a window can be built in a test.

Same shape as tests/test_settings.py's _StubApp and tools/settings_screenshots
.py's: every _build_*_tab reads config through .get(key, default), so an empty
dict is enough. It deliberately never touches ~/.samsara/config.json and never
needs a live DictationApp -- Samsara may be running while these tests are.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace


class StubApp:
    def __init__(self, config: dict | None = None):
        self.config = dict(config or {})
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(
            commands={}, find_command=lambda phrase: None)
        self.hints = None
        self.alarm_manager = None

    def play_sound(self, *args, **kwargs):
        pass

    def save_config(self):
        pass

    def load_commands(self):
        return {}

    def load_training_data(self):
        pass

    def _load_sound_cache(self):
        pass

    def get_available_microphones(self):
        """The mic wizard asks the app for devices; a headless build has
        none, which is also the state the wizard has to render correctly."""
        return []

    def get_current_microphone(self):
        return None


def build_settings_window(config: dict | None = None):
    from samsara.ui.settings_qt import _SettingsWindow

    return _SettingsWindow(StubApp(config))
