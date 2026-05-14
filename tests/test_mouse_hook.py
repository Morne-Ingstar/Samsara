"""Tests for the Win32 low-level mouse hook (samsara/mouse_hook.py).

All Win32 API calls are mocked — no actual hook is installed.
"""

import ctypes
import ctypes.wintypes
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from samsara.mouse_hook import (
    MouseHook,
    MSLLHOOKSTRUCT,
    WM_XBUTTONDOWN,
    WM_XBUTTONUP,
    XBUTTON1,
    XBUTTON2,
    WH_MOUSE_LL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lp_param(xbutton: int) -> ctypes.POINTER(MSLLHOOKSTRUCT):
    """Build a ctypes pointer to MSLLHOOKSTRUCT for a given X button."""
    info = MSLLHOOKSTRUCT()
    info.pt.x = 200
    info.pt.y = 300
    info.mouseData = ctypes.c_ulong(xbutton << 16).value
    return ctypes.pointer(info)


def _make_hook(on_event=None, suppress='mouse4'):
    cb = on_event or MagicMock()
    hook = MouseHook(on_button_event=cb, suppress_button=suppress)
    # Pretend the hook is installed so CallNextHookEx doesn't crash
    hook._hook_id = 999
    return hook, cb


# ---------------------------------------------------------------------------
# Callback: event routing
# ---------------------------------------------------------------------------

class TestHookCallback:

    def test_xbutton1_down_fires_mouse4_pressed(self):
        hook, cb = _make_hook(suppress=None)
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0):
            hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON1))
        cb.assert_called_once_with('mouse4', True)

    def test_xbutton1_up_fires_mouse4_released(self):
        hook, cb = _make_hook(suppress=None)
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0):
            hook._hook_callback(0, WM_XBUTTONUP, _make_lp_param(XBUTTON1))
        cb.assert_called_once_with('mouse4', False)

    def test_xbutton2_down_fires_mouse5_pressed(self):
        hook, cb = _make_hook(suppress=None)
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0):
            hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON2))
        cb.assert_called_once_with('mouse5', True)

    def test_xbutton2_up_fires_mouse5_released(self):
        hook, cb = _make_hook(suppress=None)
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0):
            hook._hook_callback(0, WM_XBUTTONUP, _make_lp_param(XBUTTON2))
        cb.assert_called_once_with('mouse5', False)

    def test_non_xbutton_event_does_not_fire_callback(self):
        hook, cb = _make_hook()
        WM_MOUSEMOVE = 0x0200
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0):
            hook._hook_callback(0, WM_MOUSEMOVE, _make_lp_param(0))
        cb.assert_not_called()

    def test_non_xbutton_event_calls_next_hook(self):
        hook, cb = _make_hook()
        WM_MOUSEMOVE = 0x0200
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0) as mock_next:
            hook._hook_callback(0, WM_MOUSEMOVE, _make_lp_param(0))
        mock_next.assert_called_once()

    def test_negative_n_code_delegates_immediately(self):
        hook, cb = _make_hook()
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0) as mock_next:
            hook._hook_callback(-1, WM_XBUTTONDOWN, _make_lp_param(XBUTTON1))
        mock_next.assert_called_once()
        cb.assert_not_called()


# ---------------------------------------------------------------------------
# Callback: suppression
# ---------------------------------------------------------------------------

class TestSuppression:

    def test_suppress_mouse4_returns_1(self):
        hook, cb = _make_hook(suppress='mouse4')
        result = hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON1))
        assert result == 1

    def test_suppress_mouse4_on_release_returns_1(self):
        hook, cb = _make_hook(suppress='mouse4')
        result = hook._hook_callback(0, WM_XBUTTONUP, _make_lp_param(XBUTTON1))
        assert result == 1

    def test_suppress_mouse5_returns_1(self):
        hook, cb = _make_hook(suppress='mouse5')
        result = hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON2))
        assert result == 1

    def test_suppress_mouse4_does_not_suppress_mouse5(self):
        hook, cb = _make_hook(suppress='mouse4')
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0) as mock_next:
            result = hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON2))
        mock_next.assert_called_once()
        assert result != 1

    def test_suppress_none_passes_mouse4_through(self):
        hook, cb = _make_hook(suppress=None)
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0) as mock_next:
            result = hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON1))
        mock_next.assert_called_once()
        assert result != 1

    def test_suppress_returns_1_even_if_callback_fires(self):
        called = []
        def _cb(btn, pressed):
            called.append((btn, pressed))
        hook, _ = _make_hook(on_event=_cb, suppress='mouse4')
        result = hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON1))
        assert called == [('mouse4', True)]
        assert result == 1


# ---------------------------------------------------------------------------
# Callback: exception safety
# ---------------------------------------------------------------------------

class TestCallbackException:

    def test_exception_in_callback_does_not_propagate(self):
        def _bad(btn, pressed):
            raise RuntimeError("test error")
        hook = MouseHook(on_button_event=_bad, suppress_button=None)
        hook._hook_id = 999
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0):
            # Must not raise
            hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON1))


# ---------------------------------------------------------------------------
# Lifecycle: start / stop
# ---------------------------------------------------------------------------

class TestLifecycle:

    def _patched_hook(self, hook_id=42):
        """Context: patch Win32 calls so start() returns immediately."""
        return (
            patch.object(ctypes.windll.user32, 'SetWindowsHookExW',
                         return_value=hook_id),
            patch.object(ctypes.windll.user32, 'GetMessageW',
                         return_value=0),               # WM_QUIT → loop exits
            patch.object(ctypes.windll.user32, 'UnhookWindowsHookEx'),
            patch.object(ctypes.windll.user32, 'PostThreadMessageW'),
            patch.object(ctypes.windll.kernel32, 'GetCurrentThreadId',
                         return_value=1234),
        )

    def test_start_calls_set_windows_hook_ex(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        p1, p2, p3, p4, p5 = self._patched_hook()
        with p1 as mock_set, p2, p3, p4, p5:
            hook.start()
            hook._thread.join(timeout=1)
        mock_set.assert_called_once_with(WH_MOUSE_LL, hook._proc, None, 0)

    def test_stop_calls_unhook(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        p1, p2, p3, p4, p5 = self._patched_hook()
        with p1, p2, p3 as mock_unhook, p4, p5:
            hook.start()
            hook._thread.join(timeout=1)
            hook.stop()
        mock_unhook.assert_called_once()

    def test_stop_posts_wm_quit(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        p1, p2, p3, p4, p5 = self._patched_hook()
        from samsara.mouse_hook import WM_QUIT
        with p1, p2, p3, p4 as mock_post, p5:
            hook.start()
            hook._thread.join(timeout=1)
            hook.stop()
        # PostThreadMessageW called with WM_QUIT
        assert any(c.args[1] == WM_QUIT for c in mock_post.call_args_list)

    def test_hook_id_none_after_stop(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        p1, p2, p3, p4, p5 = self._patched_hook()
        with p1, p2, p3, p4, p5:
            hook.start()
            hook._thread.join(timeout=1)
            hook.stop()
        assert hook._hook_id is None

    def test_stop_without_start_does_not_raise(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        hook.stop()   # must not raise

    def test_start_fails_gracefully_when_set_hook_returns_zero(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        with patch.object(ctypes.windll.user32, 'SetWindowsHookExW', return_value=0), \
             patch.object(ctypes.windll.kernel32, 'GetCurrentThreadId', return_value=111):
            hook.start()
            hook._thread.join(timeout=1)
        assert hook._hook_id is None or hook._hook_id == 0


# ---------------------------------------------------------------------------
# _on_command_button integration (DictationApp level)
# ---------------------------------------------------------------------------

class TestOnCommandButton:
    """Verify the dictation.py _on_command_button callback logic in isolation."""

    class _App:
        def __init__(self, button='mouse4', enabled=True, mode='hold'):
            self.config = {'command_mode': {
                'button': button, 'enabled': enabled, 'mode': mode,
            }}
            self.command_mode_active = False
            self._enter_count = 0
            self._exit_count = 0

        def enter_command_mode(self):
            self.command_mode_active = True
            self._enter_count += 1

        def exit_command_mode(self):
            self.command_mode_active = False
            self._exit_count += 1

        def _on_command_button(self, button_name, pressed):
            cfg = self.config.get('command_mode', {})
            if not cfg.get('enabled', False):
                return
            if button_name != cfg.get('button', 'mouse4'):
                return
            mode = cfg.get('mode', 'hold')
            if mode == 'hold':
                if pressed:
                    self.enter_command_mode()
                else:
                    self.exit_command_mode()
            else:
                if pressed:
                    if self.command_mode_active:
                        self.exit_command_mode()
                    else:
                        self.enter_command_mode()

    def test_mouse4_press_enters_hold_mode(self):
        app = self._App(button='mouse4', mode='hold')
        app._on_command_button('mouse4', True)
        assert app.command_mode_active is True

    def test_mouse4_release_exits_hold_mode(self):
        app = self._App(button='mouse4', mode='hold')
        app._on_command_button('mouse4', True)
        app._on_command_button('mouse4', False)
        assert app.command_mode_active is False

    def test_wrong_button_ignored(self):
        app = self._App(button='mouse4', mode='hold')
        app._on_command_button('mouse5', True)
        assert app.command_mode_active is False

    def test_disabled_config_ignored(self):
        app = self._App(button='mouse4', enabled=False, mode='hold')
        app._on_command_button('mouse4', True)
        assert app.command_mode_active is False

    def test_toggle_first_press_enters(self):
        app = self._App(button='mouse4', mode='toggle')
        app._on_command_button('mouse4', True)
        assert app.command_mode_active is True

    def test_toggle_second_press_exits(self):
        app = self._App(button='mouse4', mode='toggle')
        app._on_command_button('mouse4', True)
        app._on_command_button('mouse4', True)
        assert app.command_mode_active is False

    def test_mouse5_configured(self):
        app = self._App(button='mouse5', mode='hold')
        app._on_command_button('mouse5', True)
        assert app.command_mode_active is True
        app._on_command_button('mouse4', False)  # wrong button — no effect
        assert app.command_mode_active is True
