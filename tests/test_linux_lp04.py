"""Import hygiene for optional desktop input consumers (LP04)."""

import builtins
import importlib
from types import ModuleType


def test_desktop_modules_import_without_display_backend(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)

    real_import = builtins.__import__
    attempted = []

    def blocked_import(name, *args, **kwargs):
        if name == "pyautogui" or name == "pynput" or name.startswith("pynput."):
            attempted.append(name)
            raise ImportError("desktop backend intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    import samsara.key_macros as key_macros
    import samsara.streaming as streaming

    importlib.reload(key_macros)
    importlib.reload(streaming)

    # These command modules have registration decorators; load them normally
    # if present, then verify their actual lazy proxy seams without a backend.
    from plugins.commands import macros, word

    for module in (macros, word):
        assert module.pyautogui.__class__.__name__ == "_PyAutoGUIProxy"
    assert streaming.pyautogui.__class__.__name__ == "_PyAutoGUIProxy"
    assert key_macros.get_key_name(type("Key", (), {"char": "A"})()) == "a"
    assert key_macros.get_default_macro_config()["enabled"] is False
    manager = key_macros.KeyMacroManager({"key_macros": {"enabled": False}})
    assert manager.keyboard is None
    assert attempted == []


def test_missing_key_macro_desktop_is_unavailable_not_success(monkeypatch):
    from samsara import key_macros

    def unavailable(*args, **kwargs):
        raise ImportError("desktop backend intentionally unavailable")

    monkeypatch.setattr(key_macros, "Controller", unavailable)
    manager = key_macros.KeyMacroManager({"key_macros": {"enabled": True}})
    assert manager.start() is False
    assert manager.running is False
    assert manager.listener is None


def test_word_monkeypatch_seam_does_not_import_backend(monkeypatch):
    from plugins.commands import word

    calls = []
    monkeypatch.setattr(word.pyautogui, "hotkey", lambda *keys: calls.append(keys))
    # This is the same seam retained for tests and command injection.
    word.pyautogui.hotkey("ctrl", "b")
    assert calls == [("ctrl", "b")]


def test_missing_command_desktop_is_reported_and_does_not_succeed(monkeypatch, capsys):
    from plugins.commands import macros, word

    # Model an installed import whose desktop controller cannot initialize.
    # This avoids crossing the gate's intentionally forbidden native boundary.
    backend = ModuleType("pyautogui")

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("desktop backend intentionally unavailable")

    backend.hotkey = unavailable
    monkeypatch.setattr(macros, "pyautogui", backend)
    assert macros._desktop_action("hotkey", "win", "d") is False
    assert "Desktop unavailable" in capsys.readouterr().out

    monkeypatch.setattr(word, "pyautogui", backend)
    monkeypatch.setattr(word, "_require_word_focus", lambda _app: True)
    assert word._shortcut(object(), "ctrl", "b") is False
