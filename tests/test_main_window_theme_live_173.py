"""Live main-window theme regression (queues 173 and 173b)."""

from __future__ import annotations

import collections
from contextlib import ExitStack
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from samsara.ui import main_window_qt, theme
from samsara.ui.tray_qt import MarkFrame


class _HistoryStore:
    def query(self, **_kwargs):
        return []

    def words_typed_since(self, *_args):
        return 0


class _App:
    """Safe app surface for the hub and the real Settings picker path."""

    def __init__(self):
        self.config = {
            "mode": "hold", "hotkey": "ctrl+shift", "microphone": 7,
            "wake_word_enabled": True, "wake_word_configured": True,
            "wake_word_config": {"phrase": "Samsara"},
            "command_mode": {"mode": "toggle"}, "ollama": {"enabled": True},
            "ava_declined": True, "ui": {"theme": "dark"},
        }
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(commands={}, find_command=lambda _phrase: None)
        self.hints = self.alarm_manager = None
        self.available_mics = []
        self.recording = self.snoozed = self.wake_word_active = False
        self.continuous_active = self.command_mode_active = False
        self.ava_command_session_active = self.ava_mode_active = False
        self.toggle_active = False
        self.audio_coordinator = object()
        self._outcome_ring = collections.deque(maxlen=8)
        self.history_store = _HistoryStore()
        self._wake_consumer = None
        self._hands_free_fault = None
        self._wake_start_pending = False
        self.tray_theme_refreshes = 0

    def wake_ready_state(self):
        return "ready"

    def create_icon_image(self):
        return MarkFrame("idle", "off")

    def _push_tray_icon(self):
        self.tray_theme_refreshes += 1

    def open_settings(self):
        pass

    def save_config(self):
        pass

    def switch_microphone(self, microphone):
        self.config["microphone"] = microphone

    def switch_output_device(self, device, name):
        self.config["output_device"] = device
        self.config["output_device_name"] = name

    def play_sound(self, *_args, **_kwargs):
        pass

    def load_commands(self):
        return {}

    def load_training_data(self):
        pass

    def _load_sound_cache(self):
        pass


@pytest.fixture
def dark_again(qapp, qt_widget_scope, monkeypatch):
    previous = theme.active_theme()
    previous_sheet = qapp.styleSheet()
    try:
        with ExitStack() as owners:
            yield lambda widget: owners.enter_context(qt_widget_scope(widget))
    finally:
        theme.set_theme(previous, refresh=False)
        qapp.setStyleSheet(previous_sheet)


def _pump(qapp):
    for _ in range(12):
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)


def _visible_background(widget) -> QColor:
    """Sample rendered background coverage; one pixel may land on text ink."""
    image = widget.grab().toImage().scaled(64, 64)
    colours = collections.Counter(image.pixelColor(x, y).name()
                                 for y in range(image.height()) for x in range(image.width()))
    return QColor(colours.most_common(1)[0][0])


def test_signal_is_created_on_the_existing_qapplication_thread(qapp):
    signal = theme.theme_change_signal()

    assert signal.parent() is qapp
    assert signal.thread() == qapp.thread()


def test_picker_rethemes_every_constructed_hub_page_without_recreation(qapp, dark_again, monkeypatch):
    """The user path updates real pixels without replacing page widgets."""
    from samsara.ui.settings_qt import _SettingsWindow

    theme.set_theme("dark", refresh=False)
    app = _App()
    hub = dark_again(main_window_qt._MainWindow(app))
    hub._poll_timer.stop()
    hub.resize(1100, 780)
    hub.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    hub.show()
    _pump(qapp)

    # Populate every lazy hub page. Settings itself is a separate top-level
    # window and owns its existing queue-162 runtime coverage.
    for name in main_window_qt.NAV_ORDER:
        if name != "Settings":
            hub._activate(name)
    page_ids = {name: id(page) for name, page in hub._panel_cache.items()}

    monkeypatch.setattr(_SettingsWindow, '_replace_tts_runtime', lambda *_: None)
    settings = dark_again(_SettingsWindow(app))
    settings.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    settings.show()
    _pump(qapp)
    # Theme refresh belongs to this test's windows; prior tests may retain
    # wrappers whose owners have already been destroyed.
    monkeypatch.setattr(QApplication, 'topLevelWidgets', staticmethod(lambda: [hub, settings]))
    combo = settings._widgets["theme_combo"]
    combo.setCurrentIndex(combo.findData("light"))
    settings._apply_and_close()  # real picker -> save -> set_theme path
    _pump(qapp)

    assert theme.active_theme() == "light"
    assert app.config["ui"]["theme"] == "light"
    assert app.tray_theme_refreshes == 1
    assert {name: id(page) for name, page in hub._panel_cache.items()} == page_ids
    assert theme.PALETTES["light"]["BG0"] in hub.styleSheet()
    assert theme.PALETTES["dark"]["BG0"] not in hub.styleSheet()

    light_surfaces = {QColor(theme.PALETTES["light"][key]).name() for key in ("BG0", "BG1", "BG2")}
    dark_surfaces = {QColor(theme.PALETTES["dark"][key]).name() for key in ("BG0", "BG1", "BG2")}
    for name, page in hub._panel_cache.items():
        hub._stack.setCurrentWidget(page)
        _pump(qapp)
        colour = _visible_background(page).name()
        assert colour in light_surfaces, f"{name} retained {colour} instead of a light surface"
        assert colour not in dark_surfaces

    # dark_again destroys both owners once, before the patches expire.
