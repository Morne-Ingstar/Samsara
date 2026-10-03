"""Restart handoff tests without importing the live application."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import ctypes
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_relaunch_command_uses_one_executable_and_absolute_source_script():
    from samsara.app_restart import build_relaunch

    argv, cwd = build_relaunch(
        executable="C:/Python/python.exe",
        argv=["dictation.py", "--quiet"],
        frozen=False,
        source_script=Path("C:/project/dictation.py"),
    )
    assert argv == ["C:/Python/python.exe", "C:\\project\\dictation.py", "--quiet"]
    assert cwd == "C:\\project"


def test_frozen_relaunch_does_not_duplicate_executable():
    from samsara.app_restart import build_relaunch

    argv, cwd = build_relaunch(
        executable="C:/Samsara/Samsara.exe",
        argv=["C:/Samsara/Samsara.exe", "--quiet"],
        frozen=True,
    )
    assert argv == ["C:/Samsara/Samsara.exe", "--quiet"]
    assert cwd == "C:\\Samsara"


def test_parent_exit_precedes_replacement_start(tmp_path):
    """A real dummy owner/helper/replacement chain must cross the process boundary."""
    events = tmp_path / "events.txt"
    replacement = tmp_path / "replacement.py"
    replacement.write_text(
        "import ctypes, os, pathlib, sys, time\n"
        "k = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "k.OpenProcess.restype = ctypes.c_void_p\n"
        "h = k.OpenProcess(0x00100000, False, int(sys.argv[2]))\n"
        "dead = bool(h) and k.WaitForSingleObject(ctypes.c_void_p(h), 0) == 0\n"
        "if h: k.CloseHandle(ctypes.c_void_p(h))\n"
        "pathlib.Path(sys.argv[1]).open('a').write(f'replacement {os.getpid()} parent_exited={dead}\\n')\n"
        "time.sleep(3.3)\n",
        encoding="utf-8",
    )
    owner = tmp_path / "owner.py"
    owner.write_text(
        "import os, pathlib, sys, time\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from samsara.app_restart import start_handoff\n"
        "events = pathlib.Path(sys.argv[1])\n"
        "events.open('a').write(f'owner {os.getpid()}\\n')\n"
        "start_handoff([sys.executable, sys.argv[2], str(events), str(os.getpid())], cwd=str(events.parent), "
        "parent_pid=os.getpid(), helper_argv=[sys.executable, '-m', 'samsara.app_restart'])\n"
        "events.open('a').write('owner-exit\\n')\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["SAMSARA_RESTART_TEST_NO_DIALOG"] = "1"
    owner_process = subprocess.Popen(
        [sys.executable, str(owner), str(events), str(replacement)],
        cwd=str(Path(__file__).resolve().parents[1]), env=env,
    )
    assert owner_process.wait(timeout=15) == 0
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        lines = events.read_text(encoding="utf-8").splitlines()
        if any(line.startswith("replacement ") for line in lines):
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"replacement never started: {lines}")
    assert lines[1] == "owner-exit"
    assert len([line for line in lines if line.startswith("replacement ")]) == 1
    assert lines[2].endswith("parent_exited=True")
    replacement_pid = int(lines[2].split()[1])
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, replacement_pid)
    if handle:
        try:
            assert kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 8000) == 0
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))


def test_helper_timeout_never_starts_replacement(tmp_path, monkeypatch):
    from samsara import app_restart

    calls = []
    monkeypatch.setattr(app_restart, "_open_parent", lambda _pid: None)
    monkeypatch.setattr(app_restart, "_wait_parent", lambda *_args: (_ for _ in ()).throw(
        TimeoutError("bounded shutdown timeout")))
    monkeypatch.setattr(app_restart, "_notify_failure", calls.append)
    monkeypatch.setattr(app_restart.subprocess, "Popen", lambda *_a, **_k: pytest.fail(
        "replacement must not launch"))
    assert app_restart.run_helper(123, tmp_path / "ready", str(tmp_path),
                                  ["dummy.exe"], timeout=0.01) == 1
    assert "bounded shutdown timeout" in calls[0]


def test_dummy_handoff_missing_target_exits_with_failure(tmp_path):
    events = tmp_path / "failure.txt"
    owner = tmp_path / "owner_failure.py"
    owner.write_text(
        "import os, pathlib, sys, time\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from samsara.app_restart import start_handoff\n"
        "events = pathlib.Path(sys.argv[1])\n"
        "helper = start_handoff([str(events.parent / 'missing-replacement.exe')], "
        "cwd=str(events.parent), parent_pid=os.getpid())\n"
        "events.write_text(str(helper.pid))\n"
        "while not events.with_suffix('.go').exists(): time.sleep(0.01)\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["SAMSARA_RESTART_TEST_NO_DIALOG"] = "1"
    owner_process = subprocess.Popen([sys.executable, str(owner), str(events)],
                                     cwd=str(Path(__file__).resolve().parents[1]), env=env)
    deadline = time.monotonic() + 10
    while not events.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert events.exists()
    helper_pid = int(events.read_text(encoding="utf-8"))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, helper_pid)
    assert handle
    try:
        events.with_suffix(".go").write_text("exit", encoding="ascii")
        assert owner_process.wait(timeout=15) == 0
        assert kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 8000) == 0
        code = ctypes.c_uint32()
        assert kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        assert code.value == 1
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def test_tray_launch_failure_keeps_old_app_and_can_retry(monkeypatch):
    from samsara import app_restart
    from samsara.ui.tray_qt import SamsaraTrayQt

    quit_calls = []
    messages = []
    app = SimpleNamespace(
        _resolve_parked_draft_before_quit=lambda: True,
        quit_app=lambda **kwargs: quit_calls.append(kwargs),
    )
    tray = SimpleNamespace(_app=app, _tray=SimpleNamespace(
        showMessage=lambda *args: messages.append(args)))
    monkeypatch.setattr(app_restart, "start_handoff", lambda *_a, **_k: (
        _ for _ in ()).throw(OSError("dummy launch blocked")))
    SamsaraTrayQt._restart_samsara(tray)
    assert quit_calls == []
    assert tray._restart_pending is False
    assert "dummy launch blocked" in messages[0][1]


def test_repeated_restart_request_arms_one_helper(monkeypatch):
    from samsara import app_restart
    from samsara.ui.tray_qt import SamsaraTrayQt

    launches = []
    quits = []
    app = SimpleNamespace(
        _resolve_parked_draft_before_quit=lambda: True,
        quit_app=lambda **kwargs: quits.append(kwargs),
    )
    tray = SimpleNamespace(_app=app)
    monkeypatch.setattr(app_restart, "start_handoff", lambda *_a, **_k: launches.append(1))
    SamsaraTrayQt._restart_samsara(tray)
    SamsaraTrayQt._restart_samsara(tray)
    assert len(launches) == len(quits) == 1


def test_cancelled_draft_does_not_arm_restart(monkeypatch):
    from samsara import app_restart
    from samsara.ui.tray_qt import SamsaraTrayQt

    app = SimpleNamespace(
        _resolve_parked_draft_before_quit=lambda: False,
        quit_app=lambda **_kwargs: pytest.fail("cancelled exit must not quit"),
    )
    tray = SimpleNamespace(_app=app)
    monkeypatch.setattr(app_restart, "start_handoff", lambda *_a, **_k: pytest.fail(
        "cancelled exit must not arm helper"))
    SamsaraTrayQt._restart_samsara(tray)
    assert tray._restart_pending is False
