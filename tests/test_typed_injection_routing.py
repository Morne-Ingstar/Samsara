"""Routing tests for typed-vs-clipboard delivery (2026-09-05).

Typed Unicode injection is opt-in only: the default allowlist is empty, so
_foreground_wants_typed_injection must return False without consulting
Win32 at all. With an explicit config allowlist, the focused process name
decides. These tests never call the real SendInput.
"""
from types import SimpleNamespace

import pytest

import dictation


def _app(config):
    app = SimpleNamespace(
        config=config,
        _TYPED_INJECTION_PROCESSES=dictation.DictationApp._TYPED_INJECTION_PROCESSES,
    )
    app._foreground_wants_typed_injection = (
        dictation.DictationApp._foreground_wants_typed_injection.__get__(app)
    )
    return app


def test_default_allowlist_is_empty():
    assert dictation.DictationApp._TYPED_INJECTION_PROCESSES == frozenset()


def test_default_routes_to_clipboard_without_touching_win32(monkeypatch):
    import ctypes
    calls = []
    monkeypatch.setattr(ctypes.windll.user32, "GetForegroundWindow",
                        lambda: calls.append("fg") or 1)
    assert _app({})._foreground_wants_typed_injection() is False
    assert calls == []  # short-circuit: no foreground lookup at all


def _fake_foreground(monkeypatch, exe_name):
    import ctypes
    import psutil
    monkeypatch.setattr(ctypes.windll.user32, "GetForegroundWindow", lambda: 777)

    def _pid(_hwnd, pid_ref):
        pid_ref._obj.value = 4242
        return 1
    monkeypatch.setattr(ctypes.windll.user32, "GetWindowThreadProcessId", _pid)
    monkeypatch.setattr(psutil, "Process",
                        lambda pid: SimpleNamespace(name=lambda: exe_name))


def test_config_allowlist_opts_in_matching_process(monkeypatch):
    _fake_foreground(monkeypatch, "Brave.exe")
    app = _app({"typed_injection_processes": ["brave.exe"]})
    assert app._foreground_wants_typed_injection() is True


def test_config_allowlist_rejects_other_process(monkeypatch):
    _fake_foreground(monkeypatch, "claude.exe")
    app = _app({"typed_injection_processes": ["brave.exe"]})
    assert app._foreground_wants_typed_injection() is False


def test_win32_failure_fails_toward_clipboard(monkeypatch):
    import ctypes
    def _boom():
        raise OSError("no foreground")
    monkeypatch.setattr(ctypes.windll.user32, "GetForegroundWindow", _boom)
    app = _app({"typed_injection_processes": ["brave.exe"]})
    assert app._foreground_wants_typed_injection() is False
