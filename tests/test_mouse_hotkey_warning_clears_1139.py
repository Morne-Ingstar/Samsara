"""1139: the mouse-hotkey warning chip clears itself once the hook recovers.

Binds the real HotkeyCluster methods onto a small stand-in; never imports
dictation (which would attach to the live log while the app runs).
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara import hotkeys  # noqa: E402
from samsara.live_surface.feedback import MOUSE_HOTKEY_OWNER, feedback_for  # noqa: E402


class _Indicator:
    """Owner-scoped like the real surface: clear_feedback only removes the
    notice when its owner matches."""

    def __init__(self):
        self.owner = None
        self.label = None

    def show_feedback(self, label, kind, ttl, feedback):
        self.label, self.owner = label, feedback.owner

    def clear_feedback(self, owner):
        if owner == self.owner:
            self.label = self.owner = None


class _Hook:
    retries_automatically = True
    recovering = True
    install_error = ''


class _App:
    def __init__(self, hotkey='mouse5'):
        self.config = {'hotkey': hotkey}
        self.listening_indicator = _Indicator()
        self._mouse_hook = _Hook()
        self._mouse_hook_released_bindings = None
        self._mouse_hotkey_disabled_reason = None
        self._main_hotkey_override = None
        self._hold_down_mouse = False

    def _schedule_ui(self, fn, *args):
        fn(*args)

    def _show_outcome_chip(self, label, kind, ttl_ms="default", *, source="", detail=None):
        fb = feedback_for(kind, source=source, detail=detail)
        self.listening_indicator.show_feedback(label, kind, ttl_ms, fb)

    def newer_message(self, text):
        self.listening_indicator.show_feedback(text, 'info', 1000, feedback_for('info'))


@pytest.fixture
def app(monkeypatch):
    a = _App()
    a._mouse_fallback_to_keyboard = hotkeys.HotkeyCluster._mouse_fallback_to_keyboard.__get__(a)
    monkeypatch.setattr(hotkeys, 'notice_for', lambda *args: SimpleNamespace(
        interrupted=lambda show, alive: show(), recovered=lambda: None))
    return a


def _recover(app):
    # What recovered() in _install_mouse_listener does for the shared state.
    app._main_hotkey_override = None
    hotkeys._clear_mouse_hotkey_warning(app)


def test_warning_owner_is_mouse_hotkey():
    assert feedback_for('warning', source='mouse_hotkey_warning').owner == MOUSE_HOTKEY_OWNER


def test_interrupted_shows_plain_warning(app):
    app._mouse_fallback_to_keyboard("mouse hotkey temporarily unavailable", "silent")
    label = app.listening_indicator.label
    assert label == "Mouse 5 stopped responding - Ctrl+Shift works meanwhile; fixing itself"
    assert "hook" not in label.lower()
    assert app._main_hotkey_override == 'ctrl+shift'


def test_recovery_clears_warning_and_restores_main_hotkey(app):
    app._mouse_fallback_to_keyboard("mouse hotkey temporarily unavailable", "silent")
    _recover(app)
    assert app.listening_indicator.label is None
    assert app._main_hotkey_override is None


def test_recovery_leaves_a_newer_message_alone(app):
    app._mouse_fallback_to_keyboard("mouse hotkey temporarily unavailable", "silent")
    app.newer_message("Typed")
    _recover(app)
    assert app.listening_indicator.label == "Typed"


def test_tray_reenable_clears_the_off_warning(app):
    app._mouse_hook.recovering = False
    app._mouse_hook.retries_automatically = False
    app._mouse_hook.install_error = 'boom'
    app._mouse_fallback_to_keyboard("mouse buttons released", "released from the tray")
    assert app.listening_indicator.label == "Mouse 5 is off - using Ctrl+Shift"
    app._mouse_hook_released_bindings = (frozenset({'mouse5'}), frozenset())
    app._mouse_hook_bindings = lambda: (frozenset(), frozenset())
    app._mouse_hook = None
    hotkeys.HotkeyCluster.refresh_mouse_hook(app, rearm=True)
    assert app.listening_indicator.label is None
