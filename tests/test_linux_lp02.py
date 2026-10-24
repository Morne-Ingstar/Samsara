"""Linux import and refusal checks for the four Windows command adapters."""

import ctypes
import ctypes.wintypes as wintypes
import sys
from types import SimpleNamespace

import pytest

from plugins.commands import scroll, text_marker, volume, window_switcher


@pytest.mark.skipif(sys.platform != "win32", reason="Windows lazy binding parity")
def test_window_close_resolves_native_binding_on_first_use(monkeypatch):
    calls = []
    api = SimpleNamespace(PostMessageW=lambda *args: calls.append(args))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=api))
    monkeypatch.setattr(window_switcher, "user32", None)
    monkeypatch.setattr(window_switcher, "_resolve", lambda app, letter: (42, "Sample", False))
    monkeypatch.setattr(window_switcher, "_mapping", {"B": (42, "Sample", False)})
    monkeypatch.setattr(window_switcher, "_reset_timer", lambda: None)

    assert window_switcher.handle_window_close(None, "bravo") is True
    assert calls == [(42, window_switcher.WM_CLOSE, 0, 0)]
    assert window_switcher.user32 is api
    assert window_switcher._mapping == {}


@pytest.mark.skipif(sys.platform == "win32", reason="requires a real non-Windows ctypes runtime")
def test_command_modules_import_without_native_ctypes_bindings():
    assert not hasattr(ctypes, "windll")
    assert not hasattr(ctypes, "WINFUNCTYPE")
    assert not hasattr(ctypes, "HRESULT")
    assert scroll.user32 is None
    assert text_marker.user32 is None
    assert volume._ole32 is None
    assert window_switcher.user32 is None
    assert window_switcher.kernel32 is None
    assert window_switcher.ole32 is None


@pytest.mark.skipif(sys.platform == "win32", reason="requires native non-Windows platform behavior")
def test_unsupported_commands_refuse_without_native_activity():
    assert scroll._scroll(1, up=True) is False
    assert scroll._hscroll(1, right=True) is False
    assert scroll._send_key_combo(0x21) is False
    assert text_marker.mark_here(None, "") is False
    assert text_marker.select_to_here(None, "") is False
    assert text_marker.select_paragraph(None) is False

    core_audio = volume._CoreAudio()
    assert core_audio._ensure_init() is False
    assert core_audio.get_volume() is None
    assert core_audio.set_volume(0.5) is False
    assert core_audio.get_mute() is None
    assert core_audio.set_mute(True) is False

    assert window_switcher._get_all_windows() == []
    assert window_switcher._set_process_mute(123, True) is False
    assert window_switcher.handle_show_windows(None, "") is False
    assert window_switcher.handle_window_close(None, "A") is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows adapter parity")
def test_windows_sendinput_adapters_keep_argument_order_and_payload(monkeypatch):
    scroll_calls = []
    monkeypatch.setattr(scroll, "prepare_scroll_target", lambda config: True)
    monkeypatch.setattr(scroll, "user32", SimpleNamespace(
        SendInput=lambda *args: scroll_calls.append(args) or 1))

    assert scroll._scroll(2, up=True) is True
    count, pointer, size = scroll_calls[0]
    payload = ctypes.cast(pointer, ctypes.POINTER(scroll.INPUT)).contents
    assert count == 1
    assert size == ctypes.sizeof(scroll.INPUT)
    assert payload.type == scroll.INPUT_MOUSE
    assert payload._input.mi.dwFlags == scroll.MOUSEEVENTF_WHEEL
    assert payload._input.mi.mouseData == 240

    marker_calls = []

    def get_cursor_pos(pointer):
        point = ctypes.cast(pointer, ctypes.POINTER(wintypes.POINT)).contents
        point.x, point.y = 20, 30
        return 1

    marker_api = SimpleNamespace(
        GetCursorPos=get_cursor_pos,
        GetSystemMetrics=lambda index: 1920 if index == text_marker.SM_CXVIRTUALSCREEN else 1080,
        GetForegroundWindow=lambda: 99,
        SendInput=lambda *args: marker_calls.append(args) or args[0],
    )
    monkeypatch.setattr(text_marker, "user32", marker_api)
    monkeypatch.setattr(text_marker, "_marker_set", False)
    assert text_marker.mark_here(None, "") is True

    count, pointer, size = marker_calls[0]
    payload = ctypes.cast(pointer, ctypes.POINTER(text_marker.INPUT * 2)).contents
    assert count == 2
    assert size == ctypes.sizeof(text_marker.INPUT)
    assert [payload[index]._input.mi.dwFlags & (text_marker.MOUSEEVENTF_LEFTDOWN |
                                                text_marker.MOUSEEVENTF_LEFTUP)
            for index in range(2)] == [text_marker.MOUSEEVENTF_LEFTDOWN,
                                       text_marker.MOUSEEVENTF_LEFTUP]
