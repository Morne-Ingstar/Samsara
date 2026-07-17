import importlib
import importlib.util
import os
import shutil
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import psutil

from plugins.commands import app_lifecycle as lifecycle
from samsara import plugin_commands


class FakeProcess:
    def __init__(self, pid, *, name="python.exe", cmdline=(), cwd="C:\\"):
        self.pid = pid
        self._name = name
        self._cmdline = list(cmdline)
        self._cwd = cwd
        self.terminated = False
        self.killed = False

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline

    def cwd(self):
        return self._cwd

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _spec(tmp_path):
    project = tmp_path / "Example"
    return lifecycle.AppSpec(
        display_name="Example",
        project_dir=project,
        launchers=(project / "start.bat",),
        process_scripts=(project / "server.py",),
    )


def test_configured_voice_phrases_are_registered():
    # The global test fixture clears the plugin registry before every test;
    # reloading this one plugin recreates the registrations under test.
    importlib.reload(lifecycle)
    for phrase in (
        "start strata",
        "stop strata",
        "restart strata",
        "start ariadne",
        "stop ariadne",
        "restart ariadne",
        "start sigil server",
        "stop sigil server",
        "restart sigil server",
    ):
        assert phrase in plugin_commands._REGISTRY


def test_loader_discovers_app_lifecycle_phrases(tmp_path):
    plugin_file = tmp_path / "app_lifecycle.py"
    shutil.copyfile(lifecycle.__file__, plugin_file)
    plugin_dir = tmp_path
    plugin_commands.load_plugins(plugin_dir)

    for phrase in (
        "start strata",
        "stop strata",
        "restart strata",
        "start ariadne",
        "stop ariadne",
        "restart ariadne",
        "start sigil server",
        "stop sigil server",
        "restart sigil server",
    ):
        assert phrase in plugin_commands._REGISTRY


def test_plugin_loads_with_samsaras_dynamic_loader_contract():
    """Plugin loader executes modules without first adding them to sys.modules."""
    module_name = "samsara_plugin_app_lifecycle_contract_test"
    sys.modules.pop(module_name, None)
    path = Path(lifecycle.__file__)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    assert module.AppSpec.__name__ == "AppSpec"
    assert module_name not in sys.modules


def test_dispatch_requires_the_command_to_be_the_whole_utterance(monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(lifecycle.thread_registry, "spawn", spawn)

    assert lifecycle.start_strata(None, remainder="") is True
    assert spawn.call_count == 1

    assert lifecycle.start_strata(None, remainder="please now") is False
    assert spawn.call_count == 1


def test_match_absolute_script_path(tmp_path):
    spec = _spec(tmp_path)
    proc = FakeProcess(
        123,
        cmdline=("python.exe", str(spec.process_scripts[0])),
        cwd=str(tmp_path),
    )
    assert lifecycle._matches_spec(proc, spec) is True


def test_match_relative_script_requires_project_cwd(tmp_path):
    spec = _spec(tmp_path)
    correct = FakeProcess(
        123,
        cmdline=("python.exe", "server.py"),
        cwd=str(spec.project_dir),
    )
    unrelated = FakeProcess(
        456,
        cmdline=("python.exe", "server.py"),
        cwd=str(tmp_path / "SomethingElse"),
    )

    assert lifecycle._matches_spec(correct, spec) is True
    assert lifecycle._matches_spec(unrelated, spec) is False


def test_non_python_process_never_matches(tmp_path):
    spec = _spec(tmp_path)
    proc = FakeProcess(
        123,
        name="other.exe",
        cmdline=("other.exe", str(spec.process_scripts[0])),
        cwd=str(spec.project_dir),
    )
    assert lifecycle._matches_spec(proc, spec) is False


def test_stop_terminates_only_matches_and_kills_survivor(monkeypatch, tmp_path):
    spec = _spec(tmp_path)
    match = FakeProcess(
        123,
        cmdline=("python.exe", str(spec.process_scripts[0])),
        cwd=str(spec.project_dir),
    )
    unrelated = FakeProcess(
        456,
        cmdline=("python.exe", "server.py"),
        cwd=str(tmp_path / "Elsewhere"),
    )
    monkeypatch.setattr(lifecycle.psutil, "process_iter", lambda: [match, unrelated])
    calls = []

    def fake_wait(processes, timeout):
        calls.append((list(processes), timeout))
        return ([], [match]) if len(calls) == 1 else ([match], [])

    monkeypatch.setattr(lifecycle.psutil, "wait_procs", fake_wait)

    assert lifecycle._stop(spec) == 1
    assert match.terminated is True
    assert match.killed is True
    assert unrelated.terminated is False


def test_launch_uses_existing_batch_file(monkeypatch, tmp_path):
    spec = _spec(tmp_path)
    spec.project_dir.mkdir()
    spec.launchers[0].write_text("@echo off\n", encoding="utf-8")
    popen = Mock()
    monkeypatch.setattr(lifecycle.subprocess, "Popen", popen)

    assert lifecycle._launch(spec) == spec.launchers[0]
    argv = popen.call_args.args[0]
    expected = lifecycle.subprocess.list2cmdline([str(spec.launchers[0])])
    assert argv == [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", expected]
    assert popen.call_args.kwargs["cwd"] == str(spec.project_dir)


def test_ariadne_restart_delegates_to_its_restart_launcher(monkeypatch):
    launch = Mock()
    stop = Mock()
    speak = Mock()
    app = SimpleNamespace()
    monkeypatch.setattr(lifecycle, "_launch", launch)
    monkeypatch.setattr(lifecycle, "_stop", stop)
    monkeypatch.setattr(lifecycle, "_speak", speak)

    lifecycle._run_action(app, "ariadne", "restart")

    launch.assert_called_once_with(lifecycle._APPS["ariadne"], restart=True)
    stop.assert_not_called()
    speak.assert_called_once_with(app, "Restarting Ariadne.")


def test_process_errors_are_ignored_while_scanning(monkeypatch, tmp_path):
    spec = _spec(tmp_path)
    inaccessible = FakeProcess(123)
    inaccessible.cmdline = Mock(side_effect=psutil.AccessDenied(123))
    monkeypatch.setattr(lifecycle.psutil, "process_iter", lambda: [inaccessible])
    assert lifecycle._find_processes(spec) == []


def test_frozen_process_matching_by_executable(tmp_path):
    spec = lifecycle.AppSpec(
        display_name="Example",
        project_dir=tmp_path / "Example",
        launchers=(tmp_path / "Example" / "start.cmd",),
        process_scripts=(tmp_path / "Example" / "server.py",),
        process_executables=("example.exe",),
    )
    proc = FakeProcess(
        123,
        name="example.exe",
        cmdline=("example.exe",),
        cwd=str(spec.project_dir),
    )
    assert lifecycle._matches_spec(proc, spec) is True


def test_match_script_path_with_quotes(tmp_path):
    spec = _spec(tmp_path)
    proc = FakeProcess(
        123,
        cmdline=("python.exe", '"{}"'.format(spec.process_scripts[0])),
        cwd=str(tmp_path / "Example"),
    )
    assert lifecycle._matches_spec(proc, spec) is True


def test_start_blocked_when_multiple_processes_already_running(monkeypatch):
    app = SimpleNamespace()
    speak = Mock()
    launch = Mock()

    spec = lifecycle._APPS["strata"]
    existing = [Mock(), Mock()]
    monkeypatch.setattr(lifecycle, "_find_processes", lambda _: existing)
    monkeypatch.setattr(lifecycle, "_launch", launch)
    monkeypatch.setattr(lifecycle, "_speak", speak)

    lifecycle._run_action(app, "strata", "start")

    launch.assert_not_called()
    speak.assert_called_once_with(app, f"{spec.display_name} is already running.")
