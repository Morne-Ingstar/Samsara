"""Regression tests for the script-entry hotkey host and listener safety."""

import ast
import logging
import sys
from pathlib import Path
from types import ModuleType

from samsara import hotkeys


def test_helpers_resolve_host_registered_only_as_main(monkeypatch):
    host = ModuleType("__main__")
    host._raw_key_pressed = lambda name: name == "ctrl"
    monkeypatch.setitem(sys.modules, "__main__", host)
    monkeypatch.delitem(sys.modules, "dictation", raising=False)

    previous = hotkeys.bind_host_module(host)
    try:
        assert hotkeys._raw_key_pressed("ctrl") is True
        assert hotkeys._raw_key_pressed("shift") is False
    finally:
        hotkeys.bind_host_module(previous)


def test_listener_callback_logs_and_survives_handler_exception(caplog):
    class ListenerBoom(RuntimeError):
        pass

    def raising_handler(_key):
        raise ListenerBoom("callback failed")

    callback = hotkeys.safe_listener_callback(raising_handler, "on_press")
    with caplog.at_level(logging.ERROR, logger="Samsara"):
        assert callback(object()) is None
        assert callback(object()) is None

    records = [r for r in caplog.records if "Unhandled exception in listener callback" in r.message]
    assert len(records) == 1
    assert records[0].exc_info is not None


def test_samsara_does_not_subscript_dictation_module_name():
    root = Path(__file__).resolve().parents[1] / "samsara"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            value = node.value
            if not (isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "sys"
                    and value.attr == "modules"):
                continue
            key = node.slice.value if isinstance(node.slice, ast.Constant) else None
            assert key != "dictation", f"forbidden host lookup in {path}"
