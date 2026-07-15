"""Focused detached first-run preview launch/failure tests."""

import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import dictation
from plugins.commands import core_utils


class _Process:
    def __init__(self, return_code=0, wait_error=None):
        self.return_code = return_code
        self.wait_error = wait_error

    def wait(self, timeout):
        if self.wait_error is not None:
            raise self.wait_error
        return self.return_code


@pytest.mark.parametrize(
    "args,cwd",
    [
        ([r"F:\envs\sami\python.exe", r"C:\repo\dictation.py"], r"C:\repo"),
        ([r"C:\Program Files\Samsara\Samsara.exe"], r"C:\Program Files\Samsara"),
    ],
    ids=["source", "frozen"],
)
def test_preview_launch_isolated_detached_and_captures_diagnostics(
    tmp_path, monkeypatch, args, cwd,
):
    profile = tmp_path / ("profile-" + str(len(args)))
    profile.mkdir()
    captured = {}
    monitor_spawns = []
    process = _Process()

    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path / "primary-profile"))
    monkeypatch.setattr(dictation, "_reap_old_preview_profiles", lambda: None)
    monkeypatch.setattr("tempfile.mkdtemp", lambda **_kwargs: str(profile))
    monkeypatch.setattr(core_utils, "_build_restart_args", lambda: (args, cwd))

    def popen(actual_args, **kwargs):
        captured.update(args=actual_args, **kwargs)
        return process

    monkeypatch.setattr(dictation.subprocess, "Popen", popen)
    monkeypatch.setattr(
        dictation.thread_registry,
        "spawn",
        lambda *spawn_args, **spawn_kwargs: monitor_spawns.append(
            (spawn_args, spawn_kwargs)
        ),
    )

    app = object.__new__(dictation.DictationApp)
    app.preview_first_run()

    assert captured["args"] == args
    assert captured["cwd"] == cwd
    assert captured["env"]["SAMSARA_HOME_DIR"] == str(profile)
    assert os.environ["SAMSARA_HOME_DIR"] == str(tmp_path / "primary-profile")
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is captured["stderr"]
    assert captured["stdout"].closed is True
    assert captured["close_fds"] is True
    assert captured["creationflags"] == (
        subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    )
    diagnostic_path = profile / dictation._PREVIEW_DIAGNOSTIC_NAME
    assert diagnostic_path.exists()
    spawn_args, spawn_kwargs = monitor_spawns[0]
    assert spawn_args[:2] == (
        "preview-startup-monitor",
        dictation._monitor_preview_startup,
    )
    assert spawn_kwargs["args"] == (process, diagnostic_path)
    assert spawn_kwargs["daemon"] is True


def test_early_child_crash_logs_traceback_and_shows_visible_failure(
    tmp_path, monkeypatch,
):
    diagnostic_path = tmp_path / "preview-startup.log"
    diagnostic_path.write_text(
        "Traceback (most recent call last):\n"
        "  File 'dictation.py', line 1, in <module>\n"
        "RuntimeError: import exploded\n",
        encoding="utf-8",
    )
    logger_error = Mock()
    monkeypatch.setattr(dictation.logger, "error", logger_error)
    shown = []
    toast = Mock()
    toast.show.side_effect = lambda title, body: shown.append((title, body))
    monkeypatch.setattr(
        "samsara.ui.reminder_toast.get_toast", lambda: toast,
    )

    dictation._monitor_preview_startup(
        _Process(return_code=3), diagnostic_path, timeout=0.1,
    )

    assert "RuntimeError: import exploded" in logger_error.call_args.args[3]
    assert shown[0][0] == "Preview First-Run Failed"
    assert "code 3" in shown[0][1]
    assert "RuntimeError: import exploded" in shown[0][1]
    assert str(diagnostic_path) in shown[0][1]


def test_live_child_after_monitor_window_is_not_reported_as_failure(
    tmp_path, monkeypatch,
):
    diagnostic_path = tmp_path / "preview-startup.log"
    diagnostic_path.write_text("still starting\n", encoding="utf-8")
    report = Mock()
    monkeypatch.setattr(dictation, "_show_preview_failure", report)
    process = _Process(
        wait_error=subprocess.TimeoutExpired(cmd="preview", timeout=0.1)
    )

    dictation._monitor_preview_startup(process, diagnostic_path, timeout=0.1)

    report.assert_not_called()


def test_spawn_failure_closes_diagnostic_and_removes_unused_profile(
    tmp_path, monkeypatch,
):
    profile = tmp_path / "failed-profile"
    profile.mkdir()
    reports = []
    monkeypatch.setattr(dictation, "_reap_old_preview_profiles", lambda: None)
    monkeypatch.setattr("tempfile.mkdtemp", lambda **_kwargs: str(profile))
    monkeypatch.setattr(
        core_utils, "_build_restart_args", lambda: (["Samsara.exe"], str(tmp_path)),
    )
    monkeypatch.setattr(
        dictation.subprocess, "Popen", Mock(side_effect=OSError("spawn denied")),
    )
    monkeypatch.setattr(
        dictation,
        "_show_preview_failure",
        lambda message, diagnostic_path=None: reports.append(
            (message, diagnostic_path)
        ),
    )

    app = object.__new__(dictation.DictationApp)
    app.preview_first_run()

    assert not profile.exists()
    assert reports == [
        ("Could not launch the preview instance: spawn denied", None)
    ]
