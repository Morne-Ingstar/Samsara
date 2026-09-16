"""Regression tests for raising moved and restored windows from worker threads."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

for _module in ("win32api", "win32con", "win32gui", "win32process"):
    if _module not in sys.modules:
        sys.modules[_module] = MagicMock()

import plugins.commands.windows as wm
from plugins.commands import window_switcher as ws


MONITOR = {
    "index": 2,
    "rect": (1920, 0, 3840, 1080),
    "width": 1920,
    "height": 1080,
    "primary": False,
}


@pytest.fixture
def user32(monkeypatch):
    user = MagicMock()
    kernel = MagicMock()
    user.IsIconic.return_value = False
    user.GetForegroundWindow.side_effect = [99, 4242]
    user.GetWindowThreadProcessId.side_effect = [10, 20]
    user.AttachThreadInput.return_value = 1
    kernel.GetCurrentThreadId.return_value = 30
    monkeypatch.setattr(wm.ctypes, "windll", SimpleNamespace(user32=user, kernel32=kernel))
    monkeypatch.setattr(wm.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(wm.win32con, "SW_SHOW", 5, raising=False)
    return user


def test_move_window_to_monitor_moves_then_raises_with_activation(monkeypatch):
    set_window_pos = MagicMock()
    raise_window = MagicMock(return_value=True)
    monkeypatch.setattr(wm.win32gui, "IsIconic", lambda _hwnd: False)
    monkeypatch.setattr(wm.win32gui, "GetWindowRect", lambda _hwnd: (0, 0, 800, 600))
    monkeypatch.setattr(wm.win32gui, "GetWindowText", lambda _hwnd: "Fixture")
    monkeypatch.setattr(wm.win32gui, "SetWindowPos", set_window_pos)
    monkeypatch.setattr(wm, "raise_window", raise_window)

    wm.move_window_to_monitor(4242, MONITOR)

    assert set_window_pos.call_args_list == [
        call(4242, wm.HWND_TOP, 2480, 240, 800, 600, wm.SWP_SHOWWINDOW),
    ]
    raise_window.assert_called_once_with(4242, activate=True)


def test_restore_layout_raises_each_window_without_foreground_steal(tmp_path, monkeypatch, user32):
    layouts = tmp_path / "window_layouts.json"
    layouts.write_text(
        '{"work":{"windows":['
        '{"app":"chrome.exe","monitor_index":1,"rect":[0,0,800,600],"maximized":false},'
        '{"app":"code.exe","monitor_index":2,"rect":[1920,0,3840,1080],"maximized":false}'
        ']}}',
        encoding="utf-8",
    )
    set_window_pos = MagicMock()
    monkeypatch.setattr(wm, "_get_layouts_path", lambda: layouts)
    monkeypatch.setattr(wm, "get_monitors", lambda: [
        {**MONITOR, "index": 1, "rect": (0, 0, 1920, 1080), "primary": True}, MONITOR,
    ])
    monkeypatch.setattr(wm, "find_windows_by_app", lambda name: [101] if "chrome" in name else [202])
    monkeypatch.setattr(wm.win32gui, "IsIconic", lambda _hwnd: False)
    monkeypatch.setattr(wm.win32gui, "SetWindowPos", set_window_pos)
    monkeypatch.setattr(wm.win32gui, "ShowWindow", MagicMock())

    wm._restore_layout("work")

    assert user32.BringWindowToTop.call_args_list == [call(101), call(202)]
    user32.SetForegroundWindow.assert_not_called()
    user32.SetActiveWindow.assert_not_called()
    user32.SetFocus.assert_not_called()


def test_refused_raise_keeps_snap_command_successful(monkeypatch):
    monkeypatch.setattr(wm.win32gui, "GetForegroundWindow", lambda: 303)
    monkeypatch.setattr(wm.win32gui, "IsIconic", lambda _hwnd: False)
    monkeypatch.setattr(wm.win32gui, "GetWindowPlacement", lambda _hwnd: (0, 0))
    monkeypatch.setattr(wm.win32gui, "GetWindowText", lambda _hwnd: "Fixture")
    monkeypatch.setattr(wm.win32gui, "SetWindowPos", MagicMock())
    monkeypatch.setattr(wm, "get_monitors", lambda: [MONITOR])
    monkeypatch.setattr(wm, "_get_monitor_for_window", lambda _hwnd, _monitors: MONITOR)
    raise_window = MagicMock(return_value=False)
    monkeypatch.setattr(wm, "raise_window", raise_window)

    assert wm.handle_snap(None, "left") is True
    raise_window.assert_called_once_with(303, activate=True)


def test_window_switcher_force_focus_returns_shared_delegate_boolean(monkeypatch):
    delegate = MagicMock(return_value=False)
    monkeypatch.setattr(wm, "raise_window", delegate)

    assert ws._force_focus(88) is False
    delegate.assert_called_once_with(88, activate=True)
