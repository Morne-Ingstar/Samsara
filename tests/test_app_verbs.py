"""Tests for plugins.commands.app_verbs: resolve_window() (live windows,
title + process stem, sharing app_index's scorer) and the do_focus/do_open/
do_close action functions. Win32 enumeration is mocked -- no real window
manager / process dependency in tests.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# psutil is a real installed dependency -- never stub it (see
# test_window_manager.py's own note on this).
_win32_mocks = {}
for _mod in ('win32api', 'win32con', 'win32gui', 'win32process'):
    if _mod not in sys.modules:
        _win32_mocks[_mod] = MagicMock()
        sys.modules[_mod] = _win32_mocks[_mod]

import importlib
import plugins.commands.app_verbs as av
importlib.reload(av)


def _fake_process(name):
    proc = MagicMock()
    proc.name.return_value = name
    return proc


class TestResolveWindow:
    def test_matches_on_title(self):
        windows = [(111, "Claude - Anthropic", 1001), (222, "Untitled - Notepad", 1002)]
        with patch("plugins.commands.windows.get_all_movable_windows", return_value=windows), \
             patch("psutil.Process", side_effect=lambda pid: _fake_process(
                 "claude.exe" if pid == 1001 else "notepad.exe")):
            result = av.resolve_window("claude")
        assert result is not None
        hwnd, title, proc = result
        assert hwnd == 111
        assert title == "Claude - Anthropic"

    def test_matches_on_process_stem(self):
        windows = [(111, "Some Generic Title", 1001)]
        with patch("plugins.commands.windows.get_all_movable_windows", return_value=windows), \
             patch("psutil.Process", return_value=_fake_process("chrome.exe")):
            result = av.resolve_window("chrome")
        assert result is not None
        hwnd, title, proc = result
        assert proc == "chrome.exe"

    def test_no_windows_returns_none(self):
        with patch("plugins.commands.windows.get_all_movable_windows", return_value=[]):
            assert av.resolve_window("chrome") is None

    def test_below_floor_returns_none(self):
        windows = [(111, "Untitled - Notepad", 1002)]
        with patch("plugins.commands.windows.get_all_movable_windows", return_value=windows), \
             patch("psutil.Process", return_value=_fake_process("notepad.exe")):
            result = av.resolve_window("flurbotron")
        assert result is None

    def test_empty_name_returns_none(self):
        assert av.resolve_window("") is None
        assert av.resolve_window(None) is None

    def test_process_lookup_failure_does_not_crash(self):
        """psutil.Process raising (process exited mid-enumeration) must not
        blow up resolution -- that candidate just scores on title alone."""
        windows = [(111, "Claude - Anthropic", 1001)]
        with patch("plugins.commands.windows.get_all_movable_windows", return_value=windows), \
             patch("psutil.Process", side_effect=Exception("no such process")):
            result = av.resolve_window("claude")
        assert result is not None


class TestDoFocus:
    def test_focuses_running_window(self):
        with patch.object(av, "resolve_window", return_value=(111, "Claude", "claude.exe")), \
             patch.object(av, "_force_focus") as mock_focus:
            result = av.do_focus("claude")
        assert result is av.ActionResult.DONE
        mock_focus.assert_called_once_with(111)

    def test_not_running_when_installed_but_no_window(self):
        with patch.object(av, "resolve_window", return_value=None), \
             patch.object(av, "get_app_index") as mock_get_idx:
            mock_get_idx.return_value.resolve.return_value = MagicMock()  # installed
            result = av.do_focus("claude")
        assert result is av.ActionResult.NOT_RUNNING

    def test_not_found_when_neither_window_nor_app_index_matches(self):
        with patch.object(av, "resolve_window", return_value=None), \
             patch.object(av, "get_app_index") as mock_get_idx:
            mock_get_idx.return_value.resolve.return_value = None
            result = av.do_focus("flurbotron")
        assert result is av.ActionResult.NOT_FOUND

    def test_never_launches(self):
        """Focus must never auto-launch, even if the app is installed."""
        with patch.object(av, "resolve_window", return_value=None), \
             patch.object(av, "get_app_index") as mock_get_idx, \
             patch.object(av, "launch_app") as mock_launch:
            mock_get_idx.return_value.resolve.return_value = MagicMock()
            av.do_focus("claude")
        mock_launch.assert_not_called()


class TestDoOpen:
    def test_focuses_if_already_running(self):
        with patch.object(av, "resolve_window", return_value=(111, "Claude", "claude.exe")), \
             patch.object(av, "_force_focus") as mock_focus, \
             patch.object(av, "launch_app") as mock_launch:
            result = av.do_open("claude")
        assert result is av.ActionResult.DONE
        mock_focus.assert_called_once_with(111)
        mock_launch.assert_not_called()

    def test_launches_if_not_running_but_installed(self):
        app_entry = MagicMock()
        with patch.object(av, "resolve_window", return_value=None), \
             patch.object(av, "get_app_index") as mock_get_idx, \
             patch.object(av, "launch_app") as mock_launch:
            mock_get_idx.return_value.resolve.return_value = app_entry
            result = av.do_open("notepad")
        assert result is av.ActionResult.DONE
        mock_launch.assert_called_once_with(app_entry)

    def test_not_found_when_neither_matches(self):
        with patch.object(av, "resolve_window", return_value=None), \
             patch.object(av, "get_app_index") as mock_get_idx:
            mock_get_idx.return_value.resolve.return_value = None
            result = av.do_open("flurbotron")
        assert result is av.ActionResult.NOT_FOUND

    def test_launch_override_wins_for_netflix(self):
        """LAUNCH_OVERRIDES intercepts "netflix" BEFORE app_index is ever
        consulted (case-insensitive) -- the actual bug this override fixes:
        app_index would otherwise resolve the Windows UWP Netflix app."""
        with patch.object(av, "resolve_window", return_value=None), \
             patch.object(av, "_resolve_exe_path", return_value=r"C:\fake\brave.exe") as mock_resolve_exe, \
             patch("subprocess.Popen") as mock_popen, \
             patch.object(av, "get_app_index") as mock_get_idx:
            result = av.do_open("NetFlix")
        assert result is av.ActionResult.DONE
        mock_resolve_exe.assert_called_once_with("brave.exe")
        mock_popen.assert_called_once_with([
            r"C:\fake\brave.exe",
            "--app=https://www.netflix.com",
            "--user-data-dir=C:\\Temp\\remote_profiles\\netflix",
        ])
        mock_get_idx.return_value.resolve.assert_not_called()

    def test_launch_override_falls_back_when_exe_missing(self):
        """brave.exe not resolvable -> do_open() falls back to the generic
        app_index launcher instead of silently failing."""
        app_entry = MagicMock()
        with patch.object(av, "resolve_window", return_value=None), \
             patch.object(av, "_resolve_exe_path", return_value=None), \
             patch.object(av, "get_app_index") as mock_get_idx, \
             patch.object(av, "launch_app") as mock_launch:
            mock_get_idx.return_value.resolve.return_value = app_entry
            result = av.do_open("netflix")
        assert result is av.ActionResult.DONE
        mock_launch.assert_called_once_with(app_entry)

    def test_launch_override_does_not_shadow_other_targets(self):
        """"launch notepad" (no override defined for it) must still resolve
        generically via app_index -- LAUNCH_OVERRIDES is an exact-name dict
        lookup, so it can never shadow an unrelated launch target."""
        app_entry = MagicMock()
        with patch.object(av, "resolve_window", return_value=None), \
             patch("subprocess.Popen") as mock_popen, \
             patch.object(av, "get_app_index") as mock_get_idx, \
             patch.object(av, "launch_app") as mock_launch:
            mock_get_idx.return_value.resolve.return_value = app_entry
            result = av.do_open("notepad")
        assert result is av.ActionResult.DONE
        mock_launch.assert_called_once_with(app_entry)
        mock_popen.assert_not_called()


class TestDoClose:
    def test_closes_running_window_gracefully(self):
        with patch.object(av, "resolve_window", return_value=(111, "Notepad", "notepad.exe")):
            result = av.do_close("notepad")
        assert result is av.ActionResult.DONE
        # WM_CLOSE via PostMessage, never TerminateProcess.
        sys.modules['win32gui'].PostMessage.assert_called_once()
        args = sys.modules['win32gui'].PostMessage.call_args.args
        assert args[0] == 111
        assert args[1] == sys.modules['win32con'].WM_CLOSE

    def test_not_found_when_no_window(self):
        with patch.object(av, "resolve_window", return_value=None):
            result = av.do_close("flurbotron")
        assert result is av.ActionResult.NOT_FOUND


class TestVoiceCommandHandlers:
    def _app(self):
        app = MagicMock()
        app.play_sound = MagicMock()
        app.audio_coordinator = MagicMock()
        return app

    def test_handle_focus_success_no_speech(self):
        app = self._app()
        with patch.object(av, "do_focus", return_value=av.ActionResult.DONE):
            result = av.handle_focus(app, "claude")
        assert result is True
        app.play_sound.assert_not_called()
        app.audio_coordinator.speak.assert_not_called()

    def test_handle_focus_not_running_speaks_and_earcons(self):
        app = self._app()
        with patch.object(av, "do_focus", return_value=av.ActionResult.NOT_RUNNING):
            av.handle_focus(app, "claude")
        app.play_sound.assert_called_once_with("scratch_refuse")
        app.audio_coordinator.speak.assert_called_once()
        assert "not running" in app.audio_coordinator.speak.call_args.args[0]

    def test_handle_focus_not_found_speaks_and_earcons(self):
        app = self._app()
        with patch.object(av, "do_focus", return_value=av.ActionResult.NOT_FOUND):
            av.handle_focus(app, "flurbotron")
        app.play_sound.assert_called_once_with("scratch_refuse")
        assert "No app called" in app.audio_coordinator.speak.call_args.args[0]

    def test_handle_focus_empty_remainder_is_a_miss(self):
        app = self._app()
        assert av.handle_focus(app, "") is False

    def test_handle_open_success(self):
        app = self._app()
        with patch.object(av, "do_open", return_value=av.ActionResult.DONE):
            result = av.handle_open(app, "notepad")
        assert result is True
        app.play_sound.assert_not_called()

    def test_handle_close_success(self):
        app = self._app()
        with patch.object(av, "do_close", return_value=av.ActionResult.DONE):
            result = av.handle_close(app, "notepad")
        assert result is True

    def test_handle_close_not_found(self):
        app = self._app()
        with patch.object(av, "do_close", return_value=av.ActionResult.NOT_FOUND):
            av.handle_close(app, "flurbotron")
        app.play_sound.assert_called_once_with("scratch_refuse")
