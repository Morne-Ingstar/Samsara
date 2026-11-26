"""Exercise the integrity verdict used by the warning with fake Win32 APIs."""

import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import injection_safety as safety


@pytest.fixture
def win32(monkeypatch):
    target_hwnd = 123
    target_pid = 456
    registered_message = 0xC123
    last_error = 0

    def set_error(value):
        nonlocal last_error
        last_error = value

    def find_pid(hwnd, output):
        assert hwnd == target_hwnd
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32))[0] = target_pid
        return 99

    def post_message(hwnd, message, wparam, lparam):
        assert (hwnd, message, wparam, lparam) == (target_hwnd, registered_message, 0, 0)
        assert last_error == 0
        return True

    api = SimpleNamespace(
        wt=SimpleNamespace(DWORD=ctypes.c_uint32),
        user32=SimpleNamespace(
            GetForegroundWindow=Mock(return_value=target_hwnd),
            GetWindowThreadProcessId=Mock(side_effect=find_pid),
            RegisterWindowMessageW=Mock(return_value=registered_message),
            PostMessageW=Mock(side_effect=post_message),
        ),
        kernel32=SimpleNamespace(
            GetCurrentProcessId=Mock(return_value=1),
            OpenProcess=Mock(return_value=789),
            CloseHandle=Mock(),
        ),
        hwnd=target_hwnd,
        pid=target_pid,
        token_rid=Mock(return_value=(None, "token unavailable")),
        own_rid=Mock(return_value=0x2000),
        set_error=set_error,
    )
    monkeypatch.setattr(safety, "_win32", lambda: api)
    monkeypatch.setattr(safety, "own_integrity_rid", api.own_rid)
    monkeypatch.setattr(safety, "_token_rid", api.token_rid)
    monkeypatch.setattr(ctypes, "set_last_error", set_error, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)
    return api


@pytest.mark.parametrize(
    ("own_rid", "target_rid", "expected"),
    [(0x2000, 0x3000, safety.HIGHER), (0x2000, 0x2000, safety.OK),
     (0x3000, 0x2000, safety.OK), (0x3000, 0x3000, safety.OK)],
)
def test_readable_token_wins_without_posting(win32, own_rid, target_rid, expected):
    win32.own_rid.return_value = own_rid
    win32.token_rid.return_value = target_rid, ""

    result = safety.window_integrity(win32.hwnd)

    assert (result.state, result.method) == (expected, "token")
    assert result.blocked == (expected == safety.HIGHER)
    assert (result.own_rid, result.target_rid) == (own_rid, target_rid)
    win32.user32.GetForegroundWindow.assert_not_called()
    win32.kernel32.OpenProcess.assert_called_once_with(0x1000, False, win32.pid)
    win32.kernel32.CloseHandle.assert_called_once_with(789)
    win32.user32.RegisterWindowMessageW.assert_not_called()
    win32.user32.PostMessageW.assert_not_called()


def test_unreadable_target_uses_a_registered_message_without_a_payload(win32):
    result = safety.window_integrity()

    assert (result.state, result.method) == (safety.OK, "uipi_probe")
    assert result.confirmed_ok
    win32.user32.RegisterWindowMessageW.assert_called_once_with("Samsara.WindowIntegrityProbe")
    win32.user32.PostMessageW.assert_called_once_with(win32.hwnd, 0xC123, 0, 0)
    win32.kernel32.CloseHandle.assert_called_once_with(789)


@pytest.mark.parametrize(("post_error", "expected"), [(5, safety.HIGHER), (1400, safety.UNKNOWN)])
def test_unreadable_target_only_blocks_on_uipi_denial(win32, post_error, expected):
    def rejected_post(*_args):
        win32.set_error(post_error)
        return False

    win32.kernel32.OpenProcess.return_value = None
    win32.user32.PostMessageW.side_effect = rejected_post

    result = safety.window_integrity()

    assert (result.state, result.method) == (expected, "uipi_probe")
    assert result.blocked == (post_error == 5)
    assert not result.confirmed_ok
    win32.kernel32.CloseHandle.assert_not_called()


def test_registration_failure_is_unknown_and_posts_nothing(win32):
    win32.user32.RegisterWindowMessageW.return_value = 0
    win32.set_error(5)

    result = safety.window_integrity()

    assert result.state == safety.UNKNOWN
    assert not result.blocked
    win32.user32.PostMessageW.assert_not_called()


def test_no_foreground_window_posts_nothing(win32):
    win32.user32.GetForegroundWindow.return_value = None

    assert safety.window_integrity().state == safety.UNKNOWN
    win32.user32.RegisterWindowMessageW.assert_not_called()
    win32.user32.PostMessageW.assert_not_called()
