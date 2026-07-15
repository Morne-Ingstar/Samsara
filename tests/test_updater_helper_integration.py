"""Windows integration coverage for the generated updater helper script.

The success path runs the actual generated PowerShell in disposable sibling
install/staging directories.  The rollback path is deliberately not executed
here: production's rollback branch opens a blocking Windows MessageBox and has
no non-interactive test escape, so an assertion failure could strand pytest
behind an invisible modal dialog.  See the module-level test below, which
keeps that safety constraint explicit until production provides such a hook.
"""

import json
import os
import subprocess
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


def test_rollback_integration_requires_a_noninteractive_failure_ui_hook():
    script = _helper_script(
        PreparedUpdate(
            version="0.22.1",
            tag="v0.22.1",
            install_dir=Path("Samsara"),
            staged_dir=Path(".Samsara-update-test/payload"),
            rollback_dir=Path(".Samsara-rollback-test"),
            workspace_dir=Path(".Samsara-update-test"),
            status_path=Path("updates/last_update.json"),
            helper_path=Path("updates/install-test.ps1"),
            executable_name="runner.cmd",
        ),
        current_pid=2_000_000_000,
    )

    assert "Show-UpdateFailure" in script
    assert "[System.Windows.MessageBox]::Show" in script
    assert "The update failed, so Samsara restored" in script
    pytest.skip(
        "Rollback writes its status and then opens a blocking Windows "
        "MessageBox. Production has no environment/argument escape to suppress "
        "that UI, so a real forced-rollback test could hang unattended pytest."
    )
