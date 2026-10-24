"""LP01 checks that the real plugins can be imported without native imports."""

import builtins
import inspect
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
OWNED_MODULES = (
    "plugins.commands.app_verbs",
    "plugins.commands.media_keys",
    "plugins.commands.tab_finder",
    "plugins.commands.windows",
)
NATIVE_IMPORTS = ("winreg", "win32", "pyautogui")


@pytest.mark.skipif(sys.platform == "win32", reason="native-import denial is exercised on Linux CI")
def test_owned_modules_execute_with_native_imports_denied(monkeypatch):
    """Execute each real module source while a real import hook rejects native imports."""
    from samsara import plugin_commands

    original_import = builtins.__import__

    def deny_native(name, *args, **kwargs):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in NATIVE_IMPORTS):
            raise ImportError(f"denied native import: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_native)
    # Avoid registering a second copy of the live commands while still running
    # each owned module's actual source and ordinary dependency imports.
    monkeypatch.setattr(plugin_commands, "command", lambda *a, **k: lambda fn: fn)

    for module_name in OWNED_MODULES:
        relative = Path(*module_name.split(".")).with_suffix(".py")
        namespace = {
            "__name__": f"_lp01_import_probe_{relative.stem}",
            "__file__": str(ROOT / relative),
            "__package__": "plugins.commands",
        }
        source = (ROOT / relative).read_text(encoding="utf-8")
        exec(compile(source, str(ROOT / relative), "exec"), namespace)


def test_exported_handler_signatures_are_stable():
    from plugins.commands import app_verbs, media_keys, tab_finder, windows

    expected = {
        app_verbs.handle_focus: ("app", "remainder"),
        app_verbs.handle_open: ("app", "remainder"),
        app_verbs.handle_close: ("app", "remainder"),
        app_verbs.snap_named_window: ("app", "remainder", "windows_module"),
        media_keys.handle_pause_this: ("app", "remainder"),
        media_keys.handle_play_this: ("app", "remainder"),
        media_keys.handle_toggle_this: ("app", "remainder"),
        media_keys.handle_next_this: ("app", "remainder"),
        media_keys.handle_prev_this: ("app", "remainder"),
        tab_finder.find_tab: ("app", "remainder"),
        tab_finder.find_text: ("app", "remainder"),
        tab_finder.tab_number: ("app", "remainder"),
        windows.handle_snap: ("app", "remainder"),
        windows.snap_window: ("hwnd", "direction", "preflight", "target_monitor"),
    }
    for function, names in expected.items():
        assert tuple(inspect.signature(function).parameters) == names


@pytest.mark.skipif(sys.platform == "win32", reason="unsupported-platform results are Linux behavior")
def test_unsupported_effects_report_unavailable_without_native_calls():
    from plugins.commands import app_verbs, media_keys, tab_finder, windows

    assert app_verbs.do_open("example").value == "unavailable"
    assert app_verbs.handle_open(None, "example") is True
    result = media_keys._run_async(media_keys._send_action("pause"))
    assert result == (False, media_keys._UNAVAILABLE_REASON)
    assert tab_finder.find_text(None, "example") is False
    assert windows.get_monitors() == []
    result = windows.handle_snap(None, "left")
    assert result.state.value == "failed"
    assert result.detail["reason"] == windows._UNAVAILABLE_REASON
