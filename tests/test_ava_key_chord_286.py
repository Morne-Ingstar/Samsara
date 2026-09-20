"""Queue 286: Ava Command Session's release-edge chord binding."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


ROOT = Path(__file__).parent.parent


@pytest.fixture
def app(monkeypatch):
    from samsara import hotkeys

    monkeypatch.setattr(hotkeys.ai_preferences, "ava_master_enabled", lambda _cfg: True)
    # The separate hold-to-Ava-mode resolver belongs to dictation's legacy
    # host seam.  This chord suite never routes through that mode.
    monkeypatch.setattr(hotkeys, "_get_pynput_command_key", lambda _name: None)
    subject = SimpleNamespace(
        config={
            "command_mode": {"enabled": False},
            "ava_mode_key": "right_alt",
            "ava_command_session": {"enabled": True, "key": "left_alt"},
        },
        ava_command_session_active=False,
        command_mode_active=False,
        ava_mode_active=False,
        _ava_cmd_key_held=False,
        _ava_cmd_key_press_time=0.0,
    )
    subject.transitions = []

    def enter():
        subject.transitions.append("enter")
        subject.ava_command_session_active = True

    def exit():
        subject.transitions.append("exit")
        subject.ava_command_session_active = False

    subject.enter_ava_command_session = enter
    subject.exit_ava_command_session = exit
    return subject


def _event(name=None, char=None):
    return SimpleNamespace(name=name, char=char)


def _send(app, name=None, char=None, *, pressed):
    from samsara.hotkeys import HotkeyCluster

    HotkeyCluster._check_command_mode_key(app, _event(name, char), pressed)


def _clock(monkeypatch):
    from samsara import hotkeys

    values = iter((10.0, 10.1, 20.0, 20.1, 30.0, 30.1))
    monkeypatch.setattr(hotkeys.time, "monotonic", lambda: next(values))


def test_legacy_single_key_toggles_on_release(app, monkeypatch):
    _clock(monkeypatch)
    _send(app, "alt_l", pressed=True)
    _send(app, "alt_l", pressed=False)
    assert app.transitions == ["enter"]


def test_ctrl_alt_a_latches_when_final_key_releases_with_modifiers_held(app, monkeypatch):
    _clock(monkeypatch)
    app.config["ava_command_session"]["key"] = "ctrl+alt+a"
    _send(app, "ctrl_l", pressed=True)
    _send(app, "alt_l", pressed=True)
    _send(app, char="a", pressed=True)
    _send(app, char="a", pressed=False)
    assert app.transitions == ["enter"]


def test_releasing_a_modifier_before_the_final_key_does_not_toggle(app, monkeypatch):
    _clock(monkeypatch)
    app.config["ava_command_session"]["key"] = "ctrl+alt+a"
    _send(app, "ctrl_l", pressed=True)
    _send(app, "alt_l", pressed=True)
    _send(app, char="a", pressed=True)
    _send(app, "ctrl_l", pressed=False)
    _send(app, char="a", pressed=False)
    assert app.transitions == []


def test_other_key_drops_a_latched_chord_session(app, monkeypatch):
    _clock(monkeypatch)
    app.config["ava_command_session"]["key"] = "ctrl+alt+a"
    app.ava_command_session_active = True
    _send(app, "ctrl_l", pressed=True)
    _send(app, "alt_l", pressed=True)
    _send(app, char="a", pressed=True)
    _send(app, char="b", pressed=True)
    assert app.transitions == ["exit"]


class _SettingsApp:
    """Small headless settings seam; it deliberately never imports dictation."""

    def __init__(self):
        self.config = {}
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(commands={}, find_command=lambda _phrase: None)
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


def test_editor_rejects_a_dictation_hotkey_collision(qapp):
    from samsara.ui.settings_qt import _SettingsWindow

    win = _SettingsWindow(_SettingsApp())
    try:
        ava_key = win._widgets["ava_cmd_key"]
        original = ava_key.combo
        ava_key._combo = win._widgets["command_hotkey"].combo
        ava_key._on_change()
        assert ava_key.combo == original
        assert "Command only" in win._widgets["modes_collision_warn"].text()
    finally:
        win.close()
        win.deleteLater()
