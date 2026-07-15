"""Windows integration coverage for the generated updater helper script.

Both the success path and the rollback path run the actual generated
PowerShell in disposable sibling install/staging directories under
``tmp_path`` -- neither test ever touches a real Samsara installation.

The rollback branch normally opens a blocking Windows MessageBox on failure.
``SAMSARA_UPDATE_TEST_NO_DIALOG=1`` (read by the generated script's
``Show-UpdateFailure`` function) suppresses that dialog so a forced-rollback
test can run unattended without risking a hung, invisible modal stranding
pytest.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from samsara.updater import PreparedUpdate, _helper_script, _system_powershell_path


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows helper only")


def _write_runner(path):
    handshake = path.with_name("handshake.ps1")
    handshake.write_text(
        "$root = Split-Path $PSScriptRoot -Parent\n"
        "$p = Join-Path $root 'updates\\last_update.json'\n"
        "$j = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json\n"
        "$j.state = 'installed'\n"
        "$j.message = 'healthy test startup'\n"
        "$j | ConvertTo-Json -Compress | "
        "Set-Content -LiteralPath $p -Encoding UTF8\n",
        encoding="utf-8-sig",
    )
    path.write_text(
        "@echo off\r\n"
        "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        "-File \"%~dp0handshake.ps1\"\r\n"
        "exit /b %ERRORLEVEL%\r\n",
        encoding="utf-8",
    )


def _write_failing_runner(path):
    """A deliberately failing staged executable: it exits immediately and
    never reports a healthy startup, so the helper's confirmation loop must
    see it exit and trigger rollback."""
    path.write_text("@exit /b 1\r\n", encoding="utf-8")


def _write_relaunch_marker_runner(path, marker_path):
    """A restored/old executable that proves it was actually relaunched by
    writing a marker file -- Start-Process on the rollback branch is
    fire-and-forget, so the marker is the only observable signal."""
    path.write_text(
        '@echo off\r\n'
        f'echo relaunched > "{marker_path}"\r\n'
        'exit /b 0\r\n',
        encoding="utf-8",
    )


def test_generated_helper_swaps_waits_for_health_and_cleans_staging(tmp_path):
    install = tmp_path / "Samsara"
    workspace = tmp_path / ".Samsara-update-v0.22.1-integration"
    staged = workspace / "payload"
    rollback = tmp_path / ".Samsara-rollback-v0.22.1-integration"
    updates = tmp_path / "updates"
    status = updates / "last_update.json"
    helper = updates / "install-v0.22.1-integration.ps1"

    install.mkdir()
    staged.mkdir(parents=True)
    updates.mkdir()
    (install / "Samsara.exe").write_bytes(b"old executable placeholder")
    (install / "old-version.txt").write_text("old", encoding="utf-8")
    (install / "runner.cmd").write_text("@exit /b 0\r\n", encoding="utf-8")
    (staged / "Samsara.exe").write_bytes(b"new executable placeholder")
    (staged / "new-version.txt").write_text("new", encoding="utf-8")
    _write_runner(staged / "runner.cmd")

    prepared = PreparedUpdate(
        version="0.22.1",
        tag="v0.22.1",
        install_dir=install,
        staged_dir=staged,
        rollback_dir=rollback,
        workspace_dir=workspace,
        status_path=status,
        helper_path=helper,
        executable_name="runner.cmd",
    )
    helper.write_text(
        _helper_script(prepared, current_pid=2_000_000_000),
        encoding="utf-8-sig",
    )

    completed = subprocess.run(
        [
            str(_system_powershell_path()),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(helper),
        ],
        cwd=updates,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    payload = json.loads(status.read_text(encoding="utf-8-sig"))
    assert payload["state"] == "installed"
    assert payload["message"] == "healthy test startup"
    assert (install / "new-version.txt").read_text(encoding="utf-8") == "new"
    assert not (install / "old-version.txt").exists()
    assert not rollback.exists()
    assert not workspace.exists()


def test_rollback_integration_restores_and_relaunches_old_installation(tmp_path):
    install = tmp_path / "Samsara"
    workspace = tmp_path / ".Samsara-update-v0.22.1-integration"
    staged = workspace / "payload"
    rollback = tmp_path / ".Samsara-rollback-v0.22.1-integration"
    updates = tmp_path / "updates"
    status = updates / "last_update.json"
    helper = updates / "install-v0.22.1-integration.ps1"
    relaunch_marker = tmp_path / "old_relaunched.marker"

    install.mkdir()
    staged.mkdir(parents=True)
    updates.mkdir()
    (install / "Samsara.exe").write_bytes(b"old executable placeholder")
    (install / "old-version.txt").write_text("old", encoding="utf-8")
    _write_relaunch_marker_runner(install / "runner.cmd", relaunch_marker)
    (staged / "Samsara.exe").write_bytes(b"new executable placeholder")
    (staged / "new-version.txt").write_text("new", encoding="utf-8")
    _write_failing_runner(staged / "runner.cmd")

    prepared = PreparedUpdate(
        version="0.22.1",
        tag="v0.22.1",
        install_dir=install,
        staged_dir=staged,
        rollback_dir=rollback,
        workspace_dir=workspace,
        status_path=status,
        helper_path=helper,
        executable_name="runner.cmd",
    )
    helper.write_text(
        _helper_script(prepared, current_pid=2_000_000_000),
        encoding="utf-8-sig",
    )

    env = dict(os.environ)
    env["SAMSARA_UPDATE_TEST_NO_DIALOG"] = "1"

    completed = subprocess.run(
        [
            str(_system_powershell_path()),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(helper),
        ],
        cwd=updates,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        check=False,
    )

    # exit 2 == "rollback attempted and succeeded" (see _helper_script).
    assert completed.returncode == 2, completed.stdout

    # The old installation was restored -- not left as the failing build.
    assert (install / "old-version.txt").exists()
    assert not (install / "new-version.txt").exists()

    # Staging and rollback directories were cleaned up, not leaked.
    assert not rollback.exists()
    assert not workspace.exists()

    # The status file reports a correct terminal outcome. cleanup_pending is
    # also acceptable: it's the same "restored, but retry the leftover
    # delete" outcome the success path can hit under a transient file lock.
    payload = json.loads(status.read_text(encoding="utf-8-sig"))
    assert payload["state"] in {"rolled_back", "cleanup_pending"}
    assert "previous version was restored" in payload["message"]

    # The restored old installation was actually relaunched (Start-Process is
    # fire-and-forget, so poll briefly for the marker the relaunched process
    # writes rather than assuming it has already run).
    deadline = time.time() + 10
    while not relaunch_marker.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert relaunch_marker.exists(), (
        f"the restored old installation was not relaunched:\n{completed.stdout}"
    )

    # No GUI dialog: SAMSARA_UPDATE_TEST_NO_DIALOG made Show-UpdateFailure
    # return before ever touching PresentationFramework/MessageBox, and the
    # process exited cleanly within the timeout above rather than hanging
    # behind a modal -- both are asserted implicitly by reaching this point.
