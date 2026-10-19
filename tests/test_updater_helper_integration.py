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


def _write_runner(path, status_path):
    handshake = path.with_name("handshake.ps1")
    handshake.write_text(
        f"$p = '{status_path}'\n"
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


def _write_failing_runner(path, status_path, staged_path):
    """Pass preflight, then fail after the replacement swap."""
    handshake = path.with_name("handshake.ps1")
    handshake.write_text(
        f"$p = '{status_path}'\n"
        f"if ([IO.Path]::GetFullPath($PSScriptRoot) -ieq '{staged_path}') {{\n"
        "    $j = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json\n"
        "    $j.state = 'staged_healthy'\n"
        "    $j.message = 'healthy staged test startup'\n"
        "    $j | ConvertTo-Json -Compress | "
        "Set-Content -LiteralPath $p -Encoding UTF8\n"
        "    exit 0\n"
        "}\n",
        encoding="utf-8-sig",
    )
    path.write_text(
        "@echo off\r\n"
        "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        "-File \"%~dp0handshake.ps1\"\r\n"
        "exit /b %ERRORLEVEL%\r\n",
        encoding="utf-8",
    )


def _write_preflight_failing_runner(path):
    """Exit during staged preflight without acknowledging staged health."""
    path.write_text(
        "@echo off\r\n"
        "exit /b 1\r\n",
        encoding="utf-8",
    )


def _write_rollback_source_missing_runner(
    path, status_path, staged_path, install_path, rollback_path
):
    """Pass preflight, then remove the rollback source before failing."""
    handshake = path.with_name("handshake.ps1")
    handshake.write_text(
        f"$p = '{status_path}'\n"
        f"$staged = '{staged_path}'\n"
        f"$install = '{install_path}'\n"
        f"$rollback = '{rollback_path}'\n"
        "if ([IO.Path]::GetFullPath($PSScriptRoot) -ieq [IO.Path]::GetFullPath($staged)) {\n"
        "    $j = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json\n"
        "    $j.state = 'staged_healthy'\n"
        "    $j.message = 'healthy staged test startup'\n"
        "    $j | ConvertTo-Json -Compress | "
        "Set-Content -LiteralPath $p -Encoding UTF8\n"
        # A real staged app stays alive until the helper stops it. Exiting
        # here races the helper's status read against its HasExited check.
        "    while ($true) { Start-Sleep -Seconds 1 }\n"
        "}\n"
        "if ([IO.Path]::GetFullPath($PSScriptRoot) -ieq [IO.Path]::GetFullPath($install)) {\n"
        "    Remove-Item -LiteralPath $rollback -Recurse -Force\n"
        "    exit 1\n"
        "}\n",
        encoding="utf-8-sig",
    )
    path.write_text(
        "@echo off\r\n"
        "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        "-File \"%~dp0handshake.ps1\"\r\n"
        "exit /b %ERRORLEVEL%\r\n",
        encoding="utf-8",
    )


def _direct_handshake_processes(script):
    """Launch the fake PowerShell app itself so preflight owns its lifetime."""
    powershell = "'" + str(_system_powershell_path()).replace("'", "''") + "'"
    for location in ("$staged", "$install"):
        original = (f"Start-Process -FilePath (Join-Path {location} $executable) "
                    f"-WorkingDirectory {location} -PassThru")
        replacement = (f"Start-Process -FilePath {powershell} -ArgumentList "
                       "@('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', "
                       f"'-File', ('\"' + (Join-Path {location} 'handshake.ps1') + '\"')) "
                       f"-WorkingDirectory {location} -WindowStyle Hidden -PassThru")
        assert script.count(original) == 1
        script = script.replace(original, replacement)
    return script


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


def test_failed_staged_preflight_leaves_current_install_untouched(tmp_path):
    install = tmp_path / "Samsara"
    workspace = tmp_path / ".Samsara-update-v0.22.1-preflight-failure"
    staged = workspace / "payload"
    rollback = tmp_path / ".Samsara-rollback-v0.22.1-preflight-failure"
    updates = tmp_path / "updates"
    status = updates / "last_update.json"
    helper = updates / "install-v0.22.1-preflight-failure.ps1"

    install.mkdir()
    staged.mkdir(parents=True)
    updates.mkdir()
    (install / "Samsara.exe").write_bytes(b"old executable placeholder")
    (install / "old-version.txt").write_text("old", encoding="utf-8")
    (staged / "Samsara.exe").write_bytes(b"new executable placeholder")
    _write_preflight_failing_runner(staged / "runner.cmd")

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
        timeout=30,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        check=False,
    )

    # Exit 4 is the helper's pre-swap failure contract. The known-good tree
    # must remain in place because preflight never acknowledged staged health.
    assert completed.returncode == 4, completed.stdout
    assert (install / "old-version.txt").read_text(encoding="utf-8") == "old"
    assert not (install / "new-version.txt").exists()
    assert not (install / ".samsara-previous-version-651").exists()
    assert not rollback.exists()
    assert json.loads(status.read_text(encoding="utf-8-sig"))["state"] == "failed"


def test_missing_rollback_source_preserves_current_installation(tmp_path):
    install = tmp_path / "Samsara"
    workspace = tmp_path / ".Samsara-update-v0.22.1-rollback-source"
    staged = workspace / "payload"
    rollback = tmp_path / ".Samsara-rollback-v0.22.1-rollback-source"
    updates = tmp_path / "updates"
    status = updates / "last_update.json"
    helper = updates / "install-v0.22.1-rollback-source.ps1"

    install.mkdir()
    staged.mkdir(parents=True)
    updates.mkdir()
    (install / "Samsara.exe").write_bytes(b"old executable placeholder")
    (install / "old-version.txt").write_text("old", encoding="utf-8")
    (staged / "Samsara.exe").write_bytes(b"new executable placeholder")
    (staged / "new-version.txt").write_text("new", encoding="utf-8")
    _write_rollback_source_missing_runner(
        staged / "runner.cmd", status, staged, install, rollback
    )

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
        _direct_handshake_processes(_helper_script(prepared, current_pid=2_000_000_000)),
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

    # _helper_script documents exit 3 for a failed rollback. The guard runs
    # before Remove-Item $install, so the failing current tree is preserved.
    receipt = json.loads(status.read_text(encoding="utf-8-sig"))
    assert completed.returncode == 3, (completed.stdout, receipt)
    assert receipt["state"] == "failed"
    assert "known-good rollback directory is missing" in receipt["message"]
    assert (install / "new-version.txt").read_text(encoding="utf-8") == "new"
    assert not (install / "old-version.txt").exists()
    assert not rollback.exists()


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
    _write_runner(staged / "runner.cmd", status)

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
    # 651 retains the outgoing copy after health success (formerly deleted).
    assert (rollback / "old-version.txt").read_text(encoding="utf-8") == "old"
    assert not (rollback / "new-version.txt").exists()
    previous = payload["previous"]
    assert Path(previous["path"]) == rollback
    assert previous["sha256"] == hashlib.sha256(b"old executable placeholder").hexdigest()
    assert (rollback / ".samsara-previous-version-651").read_text() == previous["sha256"]
    assert previous["size_bytes"] == sum(p.stat().st_size for p in rollback.rglob("*") if p.is_file())
    assert previous["tag"] == "v" + previous["version"]
    assert previous["kept_at"]
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
    _write_failing_runner(staged / "runner.cmd", status, staged)

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


# 554: real PowerShell transaction, fake registry and installer only.
import hashlib
from unittest.mock import Mock
from samsara import updater as up
from samsara import update_installer as ins
requires_installer = pytest.mark.skipif(not hasattr(ins,'PreparedInstaller'), reason='installer route absent from guard-only patch')

def owner(folder,**values):
    return dict(InstallLocation=str(folder),registry_name=ins.APP_KEY,
                **{'Inno Setup: Selected Components':'core,cuda_pack,wake_word_models'},**values)

def installed(tmp_path,monkeypatch):
    folder=tmp_path/'Samsara';folder.mkdir()
    (folder/'Samsara.exe').write_bytes(b'old')
    (folder/'unins000.exe').write_bytes(b'uninstaller')
    (folder/'unins000.dat').write_bytes(b'uninstaller-data')
    (folder/'components').mkdir();(folder/'components'/'keep.dat').write_bytes(b'keep')
    info=ins.installation(folder,entries=[owner(folder)])
    monkeypatch.setattr(ins,'installation',lambda folder=None,**kw: info)
    monkeypatch.setattr(up,'_require_update_eligible',lambda: None)
    monkeypatch.setattr(up,'_frozen_install_dir',lambda: folder)
    monkeypatch.setattr(up,'samsara_home_dir',lambda: tmp_path/'profile')
    return folder

def runner(path,body):
    script=path.with_suffix('.ps1');script.write_text(body,encoding='utf-8-sig')
    path.write_text('@echo off\r\n"'+str(up._system_powershell_path())+'" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dpn0.ps1"\r\nexit /b %ERRORLEVEL%\r\n')


def run_helper(path):
    env=dict(os.environ,SAMSARA_UPDATE_TEST_NO_DIALOG='1')
    result=subprocess.run([str(up._system_powershell_path()),'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(path)],
        env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=40,
        creationflags=subprocess.CREATE_NO_WINDOW)
    return result


@pytest.mark.parametrize('installer_exit,health',[(0,True),(4,True),(0,False)])
@requires_installer
def test_real_installer_helper_transaction_with_fake_registry_and_installer(tmp_path,monkeypatch,installer_exit,health):
    folder=installed(tmp_path,monkeypatch)
    workspace=tmp_path/'.Samsara-update-v0.31.0-helper';staged=workspace/'payload'
    import shutil
    shutil.copytree(folder,staged)
    updates=tmp_path/'profile'/'updates';updates.mkdir(parents=True)
    status=updates/'last_update.json';status.write_text('{}')
    registry=tmp_path/'fake-registry.json';registry.write_text('{"DisplayVersion":"0.30.0"}')
    old_marker=tmp_path/'old-relaunched'
    runner(folder/'runner.cmd',f'Set-Content -LiteralPath {up._ps_literal(old_marker)} -Value old')
    shutil.copy2(folder/'runner.cmd',staged/'runner.cmd');shutil.copy2(folder/'runner.ps1',staged/'runner.ps1')
    installer=workspace/'SamsaraSetup-0.31.0.exe';installer.write_bytes(b'fake installer marker')
    prepared=ins.PreparedInstaller('0.31.0','v0.31.0',folder,staged,tmp_path/'.Samsara-rollback-v0.31.0-helper',workspace,status,
        updates/'install-v0.31.0-helper.ps1',executable_name='runner.cmd',installer_path=installer,
        installer_sha256=hashlib.sha256(installer.read_bytes()).hexdigest())
    health_script=(f'$j=Get-Content -LiteralPath {up._ps_literal(status)} -Raw | ConvertFrom-Json; $j.state="installed"; $j | ConvertTo-Json | Set-Content -LiteralPath {up._ps_literal(status)}' if health else 'exit 1')
    (workspace/'new-runner.ps1').write_text(health_script,encoding='utf-8-sig')
    fake=workspace/'fake-installer.cmd'
    body=f'Copy-Item -LiteralPath {up._ps_literal(workspace/"new-runner.ps1")} -Destination {up._ps_literal(folder/"runner.ps1")} -Force\n'
    body+=f'Set-Content -LiteralPath {up._ps_literal(folder/"Samsara.exe")} -Value new\n'
    body+=f'Set-Content -LiteralPath {up._ps_literal(registry)} -Value \'{{"DisplayVersion":"0.31.0"}}\'\nexit {installer_exit}'
    runner(fake,body)
    script=up._helper_script(prepared,2_000_000_000)
    # Override only external seams; the actual transaction and filesystem code run.
    shim=fr'''
function Read-InstallRegistry {{ return @{{ version='0.30.0' }} }}
function Restore-InstallRegistry($saved) {{ Set-Content -LiteralPath {up._ps_literal(registry)} -Value ('{{"DisplayVersion":"'+$saved.version+'"}}') }}
function Assert-InstalledMetadata {{ if ((Get-Content -LiteralPath {up._ps_literal(registry)} -Raw | ConvertFrom-Json).DisplayVersion -ne '0.31.0') {{ throw 'fake installer registry version mismatch' }} }}
function Invoke-CustomizationMigration {{ }} # Covered with a real migration by test_installer_customizations_560.py.
function Start-Process {{
    param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle,[switch]$PassThru)
    if ($FilePath -eq $installer) {{ $FilePath={up._ps_literal(fake)}; $ArgumentList=$null }}
    $params=@{{FilePath=$FilePath;WindowStyle='Hidden'}}
    if ($WorkingDirectory) {{ $params.WorkingDirectory=$WorkingDirectory }}
    if ($ArgumentList) {{ $params.ArgumentList=$ArgumentList }}
    if ($PassThru) {{ $params.PassThru=$true }}
    Microsoft.PowerShell.Management\Start-Process @params
}}
'''
    script=script.replace('$installerProcess = $null',shim+'\n$installerProcess = $null',1)
    prepared.helper_path.write_text(script,encoding='utf-8-sig')
    completed=run_helper(prepared.helper_path)
    assert completed.returncode==(0 if installer_exit==0 and health else 2),completed.stdout
    outcome=json.loads(status.read_text(encoding='utf-8-sig'))
    if installer_exit == 0 and health:
        previous = outcome['previous']
        retained = Path(previous['path'])
        assert retained == prepared.rollback_dir
        assert previous['sha256'] == hashlib.sha256(b'old').hexdigest()
        assert (retained / '.samsara-previous-version-651').read_text() == previous['sha256']
    if installer_exit:
        assert 'code 4' in outcome['message']
    elif not health:
        assert 'closed before confirming startup' in outcome['message']
    assert (folder/'unins000.exe').read_bytes()==b'uninstaller'
    assert (folder/'unins000.dat').read_bytes()==b'uninstaller-data'
    assert (folder/'components'/'keep.dat').read_bytes()==b'keep'
    assert json.loads(registry.read_text(encoding='utf-8-sig'))['DisplayVersion']==('0.31.0' if installer_exit==0 and health else '0.30.0')

