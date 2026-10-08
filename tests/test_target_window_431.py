"""Characterize the target-window leaf without bootstrapping dictation."""

import ast
import ctypes
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_resolver(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name == "_resolve_target_window")
    logger_node = next(node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "logger"
                               for target in node.targets))
    namespace = {"__name__": "__main__", "logging": logging}
    exec(compile(ast.Module(body=[logger_node, node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[node.name]


@pytest.fixture
def resolver():
    return load_resolver(ROOT / "samsara" / "target_window.py")


@pytest.fixture
def desktop(monkeypatch):
    processes = Mock(return_value=[])
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(process_iter=processes))
    windows = []
    user32 = SimpleNamespace()
    user32.IsWindowVisible = lambda hwnd: windows[hwnd]["visible"]
    user32.IsIconic = lambda hwnd: windows[hwnd]["minimized"]

    def get_pid(hwnd, pointer):
        pointer._obj.value = windows[hwnd]["pid"]

    def get_title(hwnd, buffer, size):
        buffer.value = windows[hwnd]["title"]

    def enumerate_windows(callback, argument):
        for hwnd in range(len(windows)):
            assert callback(hwnd, argument) is True

    user32.GetWindowThreadProcessId = Mock(side_effect=get_pid)
    user32.GetWindowTextLengthW = Mock(side_effect=lambda hwnd: windows[hwnd]["length"])
    user32.GetWindowTextW = Mock(side_effect=get_title)
    user32.EnumWindows = Mock(side_effect=enumerate_windows)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32))
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", lambda *args: lambda callback: callback)

    def set_processes(*infos):
        processes.return_value = [SimpleNamespace(info=info) for info in infos]

    def add_window(pid=12, title="Target", visible=True, minimized=False, length=None):
        windows.append(dict(pid=pid, title=title, visible=visible, minimized=minimized,
                            length=len(title) if length is None else length))
        return len(windows) - 1

    return SimpleNamespace(processes=processes, set_processes=set_processes,
                           add_window=add_window, user32=user32)


def test_missing_psutil_preserves_logger_name_and_message(resolver, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "psutil", None)
    with caplog.at_level(logging.DEBUG, logger="Samsara"):
        assert resolver("target.exe") is None
    record, = [r for r in caplog.records if "[WAKE-TARGET]" in r.message]
    assert record.name == "Samsara"
    assert record.message == "[WAKE-TARGET] psutil not available — cannot resolve target window"


@pytest.mark.parametrize("process_name", ["", None, "TARGET.EXE", "声.exe", "x" * 10000])
def test_empty_process_list_never_enumerates_windows(resolver, desktop, process_name):
    assert resolver(process_name) is None
    desktop.processes.assert_called_once_with(["pid", "name"])
    desktop.user32.EnumWindows.assert_not_called()


@pytest.mark.parametrize("process_name, actual_name", [
    ("target.exe", "TARGET.EXE"), ("", None), ("", ""),
    ("声.EXE", "声.exe"), ("x" * 10000, "X" * 10000),
])
def test_process_matching_and_unicode_titles(resolver, desktop, process_name, actual_name):
    desktop.set_processes({"pid": 12, "name": actual_name})
    desktop.add_window(title="声 — café")
    assert resolver(process_name) == (0, "声 — café")


def test_nonmatching_and_missing_process_names(resolver, desktop):
    desktop.set_processes({"pid": 12, "name": None}, {"pid": 13},
                          {"pid": 14, "name": "other.exe"})
    assert resolver("target.exe") is None
    desktop.user32.EnumWindows.assert_not_called()


@pytest.mark.parametrize("exclude", [None, set(), {99}, {12}])
def test_excluded_pids(resolver, desktop, exclude):
    desktop.set_processes({"pid": 12, "name": "target.exe"})
    desktop.add_window()
    assert resolver("target.exe", exclude) == (None if exclude == {12} else (0, "Target"))
    if exclude == {12}:
        desktop.user32.EnumWindows.assert_not_called()


@pytest.mark.parametrize("failure", ["enumeration", "iteration", "none", "bad_name", "missing_pid"])
def test_process_errors_keep_exception_logging(resolver, desktop, failure, caplog):
    if failure == "enumeration":
        desktop.processes.side_effect = RuntimeError("enumeration failed")
    elif failure == "iteration":
        def broken_iterator():
            yield SimpleNamespace(info={"pid": 12, "name": "target.exe"})
            raise RuntimeError("iteration failed")
        desktop.processes.return_value = broken_iterator()
    else:
        desktop.set_processes({"pid": 12, "name": 42} if failure == "bad_name"
                              else {"name": "target.exe"})
    with caplog.at_level(logging.DEBUG, logger="Samsara"):
        assert resolver(None if failure == "none" else "target.exe") is None
    record, = [r for r in caplog.records if "[WAKE-TARGET]" in r.message]
    assert record.name == "Samsara"
    assert record.message.startswith("[WAKE-TARGET] process enumeration error: ")
    assert record.exc_info is not None
    desktop.user32.EnumWindows.assert_not_called()


@pytest.mark.parametrize("visible, minimized", [(True, False), (False, True), (True, True)])
def test_visible_or_minimized_window(resolver, desktop, visible, minimized):
    desktop.set_processes({"pid": 12, "name": "target.exe"})
    desktop.add_window(visible=visible, minimized=minimized)
    assert resolver("target.exe") == (0, "Target")


def test_window_filters_and_first_match(resolver, desktop):
    desktop.set_processes({"pid": 12, "name": "target.exe"},
                          {"pid": 13, "name": "TARGET.EXE"})
    desktop.add_window(visible=False)
    desktop.add_window(pid=99)
    desktop.add_window(title="")
    desktop.add_window(title="", length=7)
    first = desktop.add_window(pid=13, title="First")
    desktop.add_window(title="Second")
    assert resolver("target.exe") == (first, "First")
    assert desktop.user32.GetWindowThreadProcessId.call_count == 5
    assert desktop.user32.GetWindowTextW.call_count == 3
    assert desktop.user32.EnumWindows.call_args.args[1] == 0


def test_no_windows_and_long_title(resolver, desktop):
    desktop.set_processes({"pid": 12, "name": "target.exe"})
    assert resolver("target.exe") is None
    desktop.add_window(title="声" * 10000)
    assert resolver("target.exe") == (0, "声" * 10000)
    assert desktop.user32.GetWindowTextW.call_args.args[2] == 10001


def test_win32_error_still_propagates(resolver, desktop):
    desktop.set_processes({"pid": 12, "name": "target.exe"})
    desktop.user32.EnumWindows.side_effect = OSError("EnumWindows failed")
    with pytest.raises(OSError, match="EnumWindows failed"):
        resolver("target.exe")


def test_dispatch_looks_up_patch_in_its_module_globals(monkeypatch):
    # The maintained gate supplies the real class without executing bootstrap.
    module = sys.modules["dictation"]
    dispatch = module.DictationApp._dispatch_wake_profile
    assert dispatch.__globals__ is module.__dict__
    replacement = Mock(return_value=None)
    monkeypatch.setattr(module, "_resolve_target_window", replacement)
    app = Mock()
    dispatch(app, {"target_process": "target.exe", "phrase": "hey target"})
    replacement.assert_called_once_with("target.exe", exclude_pids={module.os.getpid()})
    app.play_sound.assert_called_once_with("error")


def test_dispatch_patch_also_works_as_main():
    path = ROOT / "dictation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "DictationApp")
    node = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                and node.name == "_dispatch_wake_profile")
    replacement = Mock(return_value=None)
    namespace = {"__name__": "__main__", "logger": logging.getLogger("Samsara"),
                 "os": SimpleNamespace(getpid=lambda: 999),
                 "_resolve_target_window": replacement}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    app = Mock()
    namespace[node.name](app, {"target_process": "target.exe"})
    replacement.assert_called_once_with("target.exe", exclude_pids={999})
    app.play_sound.assert_called_once_with("error")
