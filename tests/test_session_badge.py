"""Tests for the session mode badge: lives on the listening indicator pill,
never on samsara.ui.status_overlay (the Reminders & Alarms window).

An earlier version wired the badge into status_overlay.py, which meant every
session mode transition popped/hid the user's Reminders & Alarms window as a
side effect, and left it in a broken hide/reopen state. This file locks in
the fix at two layers:

  - ListeningIndicator.set_session_mode() -- pure widget-state tests.
  - DictationApp.enter_command_mode / exit_command_mode / _update_mode_overlay
    -- the REAL dictation.py methods, bound onto a lightweight stub object
    (not a reimplementation -- this exercises the actual code under test).
"""
import sys
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# ListeningIndicator.set_session_mode()
# ---------------------------------------------------------------------------

class TestListeningIndicatorSessionMode:
    @pytest.fixture(autouse=True)
    def indicator(self, qapp):
        from samsara.ui.listening_indicator import ListeningIndicator
        ind = ListeningIndicator()
        yield ind
        ind.destroy()

    def test_initial_state_no_session_badge(self, indicator):
        assert indicator._session_mode_name is None
        assert indicator._session_mode_color is None

    def test_set_session_mode_updates_state(self, indicator):
        indicator.set_session_mode("COMMAND", "#5EEAD4")
        assert indicator._session_mode_name == "COMMAND"
        assert indicator._session_mode_color == "#5EEAD4"

    def test_clear_session_mode(self, indicator):
        indicator.set_session_mode("DICTATE", "#f59e0b")
        indicator.set_session_mode(None, None)
        assert indicator._session_mode_name is None
        assert indicator._session_mode_color is None

    def test_session_mode_dominates_command_mode_display(self, indicator):
        """While a session badge is set, it wins over the generic CMD
        indicator -- COMMAND/DICTATE/AVA is strictly more informative."""
        indicator.set_command_mode(True)
        indicator.set_session_mode("AVA", "#A78BFA")
        _, fg, label, _ = indicator._resolve_colors()
        assert label == "AVA"
        assert fg == "#A78BFA"

    def test_no_session_badge_falls_through_to_command_mode(self, indicator):
        indicator.set_command_mode(True)
        _, _, label, _ = indicator._resolve_colors()
        assert label == "CMD"

    def test_no_session_badge_falls_through_to_idle(self, indicator):
        _, _, label, _ = indicator._resolve_colors()
        assert label == "Hold"


# ---------------------------------------------------------------------------
# DictationApp session wiring -- real methods on a lightweight stub
# ---------------------------------------------------------------------------

def _session_mode_enum():
    from samsara.session_modes import SessionMode
    return SessionMode


def _make_stub(mode='toggle', enabled=True, listening_indicator_enabled=False):
    """Binds the REAL dictation.py methods under test onto a lightweight
    stub -- everything else (locks, config, listening_indicator) is a
    minimal stand-in, matching the existing _MockApp pattern in
    test_command_mode.py but for the pieces this file needs."""
    import dictation as _d

    class _Stub:
        enter_command_mode = _d.DictationApp.enter_command_mode
        exit_command_mode = _d.DictationApp.exit_command_mode
        _update_mode_overlay = _d.DictationApp._update_mode_overlay
        _MODE_OVERLAY = _d.DictationApp._MODE_OVERLAY

        def __init__(self):
            self.command_mode_active = False
            self.ava_mode_active = False
            self._command_mode_lock = threading.Lock()
            self._command_mode_miss_count = 0
            self._command_mode_session_start = 0.0
            self._command_mode_ghost_tap = False
            self._command_mode_inactivity_timer = None
            self.recording = False
            self.config = {
                'command_mode': {
                    'enabled': enabled,
                    'mode': mode,
                    'button': 'rctrl',
                    'enter_debounce_ms': 0,
                    'exit_earcon': False,
                    'inactivity_timeout_s': 30,
                },
                'listening_indicator_enabled': listening_indicator_enabled,
            }
            self.listening_indicator = Mock()
            self._session_mode_manager = None
            self._sounds = []

        def _schedule_ui(self, func, *args):
            # Synchronous for test determinism -- real dictation.py marshals
            # via QTimer.singleShot(0, ...); irrelevant to the logic under test.
            func(*args)

        def play_sound(self, name, **_kwargs):
            self._sounds.append(name)

        def _do_enter_command_mode(self):
            pass  # background-thread debounce/earcon -- not under test here

        def start_recording(self, **_kwargs):
            self.recording = True

        def stop_recording(self):
            self.recording = False

        def _reset_command_mode_inactivity_timer(self, timeout_s):
            pass

        def _cancel_command_mode_inactivity_timer(self):
            pass

        def _ensure_session_mode_manager(self):
            if self._session_mode_manager is None:
                self._session_mode_manager = Mock()
            return self._session_mode_manager

    return _Stub()


class TestModeOverlayDrivesThePill:
    """Mode transitions drive the pill's (text, color) through a mocked
    pill interface (listening_indicator is a Mock here)."""

    def test_command_mode_badge(self):
        stub = _make_stub()
        stub._update_mode_overlay(_session_mode_enum().COMMAND)
        stub.listening_indicator.set_session_mode.assert_called_once_with(
            "COMMAND", "#5EEAD4"
        )

    def test_dictate_mode_badge(self):
        stub = _make_stub()
        stub._update_mode_overlay(_session_mode_enum().DICTATE)
        stub.listening_indicator.set_session_mode.assert_called_once_with(
            "DICTATE", "#f59e0b"
        )

    def test_ava_mode_badge(self):
        stub = _make_stub()
        stub._update_mode_overlay(_session_mode_enum().AVA)
        stub.listening_indicator.set_session_mode.assert_called_once_with(
            "AVA", "#A78BFA"
        )


class TestSessionStartEndForceVisible:
    """Session start/end toggles force-visible correctly with
    listening_indicator_enabled both true and false."""

    def test_session_start_force_shows_pill_when_indicator_disabled(self):
        stub = _make_stub(mode='toggle', listening_indicator_enabled=False)
        stub.enter_command_mode()
        stub.listening_indicator.show.assert_called_once()

    def test_session_start_force_shows_pill_when_indicator_enabled_too(self):
        stub = _make_stub(mode='toggle', listening_indicator_enabled=True)
        stub.enter_command_mode()
        stub.listening_indicator.show.assert_called_once()

    def test_session_end_hides_pill_when_indicator_disabled(self):
        stub = _make_stub(mode='toggle', listening_indicator_enabled=False)
        stub.enter_command_mode()
        stub.listening_indicator.reset_mock()
        stub.exit_command_mode()
        stub.listening_indicator.hide.assert_called_once()
        stub.listening_indicator.set_session_mode.assert_any_call(None, None)

    def test_session_end_does_not_hide_pill_when_indicator_enabled(self):
        stub = _make_stub(mode='toggle', listening_indicator_enabled=True)
        stub.enter_command_mode()
        stub.listening_indicator.reset_mock()
        stub.exit_command_mode()
        stub.listening_indicator.hide.assert_not_called()
        stub.listening_indicator.set_session_mode.assert_any_call(None, None)

    def test_hold_mode_never_force_shows_or_touches_session_badge(self):
        """Hold mode never uses the unified session -- no force-show, no
        session-badge set/clear. set_command_mode still fires (the
        pre-existing, separate legacy CMD cue) -- that's untouched here."""
        stub = _make_stub(mode='hold', listening_indicator_enabled=False)
        stub.enter_command_mode()
        stub.listening_indicator.show.assert_not_called()
        stub.exit_command_mode()
        stub.listening_indicator.hide.assert_not_called()
        stub.listening_indicator.set_session_mode.assert_not_called()


class TestStatusOverlayNeverTouchedBySessionCode:
    """status_overlay receives ZERO calls from session code."""

    def test_enter_command_mode_never_calls_status_overlay(self):
        with patch('samsara.ui.status_overlay.get_overlay') as spy:
            stub = _make_stub(mode='toggle')
            stub.enter_command_mode()
            spy.assert_not_called()

    def test_exit_command_mode_never_calls_status_overlay(self):
        with patch('samsara.ui.status_overlay.get_overlay') as spy:
            stub = _make_stub(mode='toggle')
            stub.enter_command_mode()
            stub.exit_command_mode()
            spy.assert_not_called()

    def test_update_mode_overlay_never_calls_status_overlay(self):
        with patch('samsara.ui.status_overlay.get_overlay') as spy:
            stub = _make_stub()
            stub._update_mode_overlay(_session_mode_enum().DICTATE)
            spy.assert_not_called()

    def test_hold_mode_session_never_calls_status_overlay(self):
        with patch('samsara.ui.status_overlay.get_overlay') as spy:
            stub = _make_stub(mode='hold')
            stub.enter_command_mode()
            stub.exit_command_mode()
            spy.assert_not_called()
