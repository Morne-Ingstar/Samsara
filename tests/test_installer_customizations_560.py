"""Installer overwrite/migration/rollback with disposable files and fakes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from samsara import boot, update_installer as ins, updater as up
from samsara import update_installer_customizations as maintenance
from samsara.update_customizations import CustomizationMigrationError


def commands(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"commands": entries}), encoding="utf-8")


def roots(tmp_path, layout="_internal"):
    install = tmp_path / "Samsara"
    snapshot = tmp_path / ".Samsara-rollback-v0.31.0-test"
    workspace = tmp_path / ".Samsara-update-v0.31.0-test"
    for root in (install, snapshot, workspace):
        root.mkdir()
    backups = tmp_path / "profile" / "updates" / "backups"
    receipt = workspace / "customizations-migrated.json"
    commands(snapshot / layout / "commands.json", {"old": {"action": "edited"}})
    commands(install / layout / "commands.json", {"new": {"action": "new"}})
    return snapshot, install, backups, receipt


@pytest.mark.parametrize("layout", ["_internal", ""])
def test_shared_migration_preserves_commands_dropins_and_archived_collisions(tmp_path, layout):
    snapshot, install, backups, receipt = roots(tmp_path, layout)
    old_plugins = snapshot / layout / "plugins" / "commands"
    new_plugins = install / layout / "plugins" / "commands"
    old_plugins.mkdir(parents=True)
    new_plugins.mkdir(parents=True)
    (old_plugins / "custom.py").write_bytes(b"custom = True\n")
    (old_plugins / "packaged.py").write_bytes(b"old_edit = True\n")
    (new_plugins / "packaged.py").write_bytes(b"new_package = True\n")
    summary = maintenance.migrate_installer_customizations(snapshot, install, backups, receipt)
    assert json.loads((install / layout / "commands.json").read_text())["commands"] == {
        "new": {"action": "new"}, "old": {"action": "edited"}
    }
    assert (new_plugins / "custom.py").read_bytes() == b"custom = True\n"
    assert (new_plugins / "packaged.py").read_bytes() == b"new_package = True\n"
    assert (summary.backup_dir / "plugins/commands/packaged.py").read_bytes() == b"old_edit = True\n"
    shutil.rmtree(snapshot)
    shutil.rmtree(receipt.parent)
    assert (summary.backup_dir / "commands.json").is_file()
    assert (summary.backup_dir / "manifest.json").is_file()


def test_malformed_snapshot_is_rejected_before_new_commands_are_written(tmp_path):
    snapshot, install, backups, receipt = roots(tmp_path)
    (snapshot / "_internal/commands.json").write_bytes(b"{broken")
    original = (install / "_internal/commands.json").read_bytes()
    with pytest.raises(CustomizationMigrationError, match="Invalid commands file"):
        maintenance.migrate_installer_customizations(snapshot, install, backups, receipt)
    assert (install / "_internal/commands.json").read_bytes() == original
    assert not receipt.exists()


@pytest.mark.parametrize("unsafe", ["snapshot", "receipt", "backups"])
def test_maintenance_rejects_unconfined_roots(tmp_path, unsafe):
    snapshot, install, backups, receipt = roots(tmp_path)
    if unsafe == "snapshot":
        snapshot = tmp_path / "unrelated"
        snapshot.mkdir()
    elif unsafe == "receipt":
        receipt = tmp_path / "customizations-migrated.json"
    else:
        backups = install / "backups"
    with pytest.raises(CustomizationMigrationError):
        maintenance.migrate_installer_customizations(snapshot, install, backups, receipt)
    assert not receipt.exists()


def test_existing_ancestor_reparse_point_is_rejected_before_resolution(tmp_path, monkeypatch):
    snapshot, install, backups, receipt = roots(tmp_path)
    original = Path.lstat

    class ReparseStat:
        st_file_attributes = 0x400
        def __init__(self, info):
            self.info = info
        def __getattr__(self, name):
            return getattr(self.info, name)

    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        return ReparseStat(info) if path == snapshot else info

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(CustomizationMigrationError, match="reparse point"):
        maintenance.migrate_installer_customizations(snapshot, install, backups, receipt)
    assert not receipt.exists()


def test_boot_dispatch_exits_before_faulthandler_or_normal_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["Samsara.exe", maintenance.MAINTENANCE_ARGUMENT, "bad"])
    with pytest.raises(SystemExit) as error:
        boot._enable_faulthandler(tmp_path / "logs")
    assert error.value.code == 2
    assert not (tmp_path / "logs").exists()
    # Pin the entry point's ordering without importing/executing dictation.
    entry = (Path(__file__).resolve().parents[1] / "dictation.py").read_text(encoding="utf-8")
    assert entry.index("_samsara_boot._enable_faulthandler(") < entry.index("import sounddevice")


def test_command_edit_destination_is_packaged_not_home(tmp_path, monkeypatch):
    from samsara.commands import CommandExecutor
    import samsara.commands as command_module
    monkeypatch.setattr(command_module, "__file__", str(tmp_path / "_internal/samsara/commands.py"))
    # Use the constructor's AST to avoid creating input controllers/plugin discovery.
    import ast
    tree = ast.parse((Path(__file__).resolve().parents[1] / "samsara/commands.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CommandExecutor")
    constructor = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    statements = constructor.body[:2]
    from types import SimpleNamespace
    target = SimpleNamespace()
    namespace = {"commands_path": None, "Path": Path, "__file__": command_module.__file__, "self": target}
    exec(compile(ast.Module(body=statements, type_ignores=[]), "constructor-path", "exec"), namespace)
    assert target.commands_path == tmp_path / "_internal/commands.json"
    assert hasattr(CommandExecutor, "save_commands")


def runner(path, body):
    path.with_suffix(".ps1").write_text(body, encoding="utf-8-sig")
    path.write_text(
        '@echo off\r\n"' + str(up._system_powershell_path()) +
        '" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dpn0.ps1"\r\nexit /b %ERRORLEVEL%\r\n'
    )


@pytest.mark.parametrize("case", ["legacy", "already-migrated", "malformed", "portable", "foreign"])
def test_early_boot_migrates_older_installer_helper_before_confirmation(tmp_path, monkeypatch, case):
    snapshot, install, backups, receipt = roots(tmp_path)
    for marker in ("unins000.exe", "unins000.dat"):
        (snapshot / marker).write_bytes(b"old installer marker")
    status = backups.parent / "last_update.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({
        "state": "awaiting_confirmation", "install_dir": str(install),
        "rollback_dir": str(snapshot), "workspace_dir": str(receipt.parent),
    }))
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(backups.parent.parent))
    monkeypatch.setattr(sys, "executable", str(install / "Samsara.exe"))
    if case == "malformed":
        monkeypatch.setattr(maintenance, "_helper_owns_update", lambda payload: True)
        (snapshot / "_internal/commands.json").write_bytes(b"{broken")
    elif case == "already-migrated":
        maintenance.migrate_installer_customizations(snapshot, install, backups, receipt)
    elif case == "portable":
        (snapshot / "unins000.exe").unlink()
    elif case == "foreign":
        monkeypatch.setattr(sys, "executable", str(tmp_path / "another/Samsara.exe"))
    if case == "malformed":
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "argv", ["Samsara.exe"])
        with pytest.raises(SystemExit) as error:
            boot._enable_faulthandler(tmp_path / "must-not-start")
        assert error.value.code == 2
        assert not (tmp_path / "must-not-start").exists()
        assert not receipt.exists()
    else:
        assert maintenance.migrate_pending_installer_update() == (case == "legacy")
        if case in ("legacy", "already-migrated"):
            assert json.loads((install / "_internal/commands.json").read_text())["commands"] == {
                "new": {"action": "new"}, "old": {"action": "edited"}
            }
            assert len(list(backups.glob("customizations-*"))) == 1
        else:
            assert not receipt.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell transaction")
@pytest.mark.parametrize("failure", [None, "malformed-old", "malformed-new", "backup-io", "health", "legacy", "migration-timeout", "migration-timeout-reparse", "legacy-malformed", "legacy-orphan-malformed", "legacy-orphan-oversized", "migration-timeout-reparse-status", "migration-timeout-status", "migration-kill-failure", "migration-wait-failure", "migration-kill-status", "migration-timeout-dialog-failure", "restore-failure-status"])
def test_real_helper_overwrite_migration_and_rollback(tmp_path, monkeypatch, failure):
    orphan = failure in ("legacy-orphan-malformed", "legacy-orphan-oversized")
    success = failure in (None, "legacy") or orphan
    timed_out = bool(failure and (failure.startswith("migration-timeout") or failure.startswith("migration-kill") or failure == "migration-wait-failure"))
    restore_failure = failure in ("migration-timeout-reparse", "migration-timeout-reparse-status", "restore-failure-status")
    kill_failure = failure in ("migration-kill-failure", "migration-kill-status", "migration-wait-failure")
    status_failure = bool(failure and failure.endswith("-status"))
    install = tmp_path / "Samsara"
    install.mkdir()
    for name in ("Samsara.exe", "unins000.exe", "unins000.dat"):
        (install / name).write_bytes(b"old")
    old_commands = install / "_internal/commands.json"
    commands(old_commands, {"old": {"action": "edited"}})
    plugins = install / "_internal/plugins/commands"
    plugins.mkdir(parents=True)
    (plugins / "custom.py").write_bytes(b"custom = True\n")
    (plugins / "packaged.py").write_bytes(b"old_edit = True\n")
    info = ins.installation(install, entries=[{
        "registry_name": ins.APP_KEY, "InstallLocation": str(install),
    }])
    monkeypatch.setattr(ins, "installation", lambda *args, **kwargs: info)
    workspace = tmp_path / ".Samsara-update-v0.31.0-test"
    staged = workspace / "payload"
    shutil.copytree(install, staged)
    updates = tmp_path / "profile/updates"
    updates.mkdir(parents=True)
    status = updates / "last_update.json"
    status.write_text("{}")
    registry = tmp_path / "fake-registry.json"
    registry.write_text('{"version":"old"}')
    old_marker = tmp_path / "old-relaunched"
    runner(install / "runner.cmd", f'Set-Content -LiteralPath {up._ps_literal(old_marker)} -Value old')
    # A late edit proves the helper refreshes the earlier preparation snapshot.
    commands(old_commands, {"old": {"action": "late-edit"}})
    if failure in ("malformed-old", "legacy-malformed", "legacy-orphan-malformed", "restore-failure-status"):
        old_commands.write_bytes(b"{broken")
    elif failure == "legacy-orphan-oversized":
        with old_commands.open("wb") as stream:
            stream.truncate(maintenance.MAX_COMMANDS_BYTES + 1)
    expected_old_bytes = old_commands.read_bytes()
    packaged = workspace / "packaged.json"
    commands(packaged, {"new": {"action": "new"}})
    if failure == "malformed-new":
        packaged.write_bytes(b"{broken-new")
    if failure == "backup-io":
        (updates / "backups").write_bytes(b"cannot create a directory here")
    installer = workspace / "SamsaraSetup-0.31.0.exe"
    installer.write_bytes(b"fake verified installer")
    prepared = ins.PreparedInstaller(
        "0.31.0", "v0.31.0", install, staged,
        tmp_path / ".Samsara-rollback-v0.31.0-test", workspace, status,
        updates / "install-v0.31.0-test.ps1", executable_name="runner.cmd",
        installer_path=installer,
        installer_sha256=hashlib.sha256(installer.read_bytes()).hexdigest(),
    )
    healthy = workspace / "new-runner.ps1"
    healthy.write_text(
        'exit 1' if failure == "health" else
        f'$j=Get-Content -LiteralPath {up._ps_literal(status)} -Raw | ConvertFrom-Json; '
        f'$j.state="installed"; $j | ConvertTo-Json | Set-Content -LiteralPath {up._ps_literal(status)}',
        encoding="utf-8-sig",
    )
    fake_installer = workspace / "fake-installer.cmd"
    runner(fake_installer, f'''
Copy-Item -LiteralPath {up._ps_literal(packaged)} -Destination {up._ps_literal(old_commands)} -Force
Remove-Item -LiteralPath {up._ps_literal(plugins)} -Recurse -Force
New-Item -ItemType Directory -Path {up._ps_literal(plugins)} | Out-Null
Set-Content -LiteralPath {up._ps_literal(plugins/'packaged.py')} -Value 'new_package = True'
Set-Content -LiteralPath {up._ps_literal(install/'Samsara.exe')} -Value new
Copy-Item -LiteralPath {up._ps_literal(healthy)} -Destination {up._ps_literal(install/'runner.ps1')} -Force
Set-Content -LiteralPath {up._ps_literal(registry)} -Value '{{"version":"new"}}'
''')
    # Execute the real maintenance mode and shared migration through a synthetic
    # replacement executable. No real app, installer or registry is accessed.
    driver = workspace / "migration.py"
    driver.write_text(
        "import os, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        f"os.environ['SAMSARA_HOME_DIR'] = {str(updates.parent)!r}\n"
        f"sys.executable = {str(install/'Samsara.exe')!r}\n"
        "legacy = sys.argv[-1:] == ['legacy']\n"
        "if legacy:\n    sys.argv = [sys.executable]\n    sys.frozen = True\n"
        "from samsara import boot\n"
        "from samsara import update_installer_customizations as maintenance\n"
        "maintenance._show_recovery_notice = lambda message: True\n"
        f"boot._enable_faulthandler(Path({str(tmp_path/'maintenance-logs')!r}))\n"
        "if legacy:\n"
        "    import json\n"
        f"    status = Path({str(status)!r})\n"
        "    payload = json.loads(status.read_text(encoding='utf-8-sig'))\n"
        "    if payload['state'] == 'awaiting_confirmation': payload['state'] = 'installed'\n"
        "    status.write_text(json.dumps(payload), encoding='utf-8')\n",
        encoding="utf-8",
    )
    migration_runner = workspace / "migration.cmd"
    migration_runner.write_text(
        f'@echo off\r\n"{sys.executable}" -B "{driver}" %*\r\nexit /b %ERRORLEVEL%\r\n'
    )
    hanging = workspace / "hanging-maintenance.py"
    hanging.write_text(
        "import os, time\nfrom pathlib import Path\n"
        f"Path({str(tmp_path/'maintenance-pid')!r}).write_text(str(os.getpid()))\n"
        f"Path({str(old_commands)!r}).write_bytes(b'half-written commands')\n"
        "time.sleep(60)\n", encoding="utf-8",
    )
    script = up._helper_script(prepared, 2_000_000_000)
    if failure == "migration-timeout":
        script = script.replace("$migrationProcess.WaitForExit(180000)", "$migrationProcess.WaitForExit(1000)")
    if failure in ("legacy", "legacy-malformed") or orphan:
        # The previous installer helper did not invoke migration; normal
        # startup of the replacement must bridge it before the handshake.
        script = script.replace("    Invoke-CustomizationMigration\n", "")
        # Real beta.4-style status: no identity; only updated_at proves freshness.
        script = script.replace("helper_pid=$PID; helper_started_filetime=([Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().ToFileTimeUtc().ToString());", "")
        if orphan:
            script = script.replace("updated_at=(Get-Date).ToUniversalTime().ToString('o')", "updated_at=(Get-Date).AddSeconds(-201).ToUniversalTime().ToString('o')")
        healthy.write_text(
            f'& {up._ps_literal(sys.executable)} -B {up._ps_literal(driver)} legacy\nexit $LASTEXITCODE',
            encoding="utf-8-sig",
        )
    shim = fr'''
function Read-InstallRegistry {{ return @{{ version='old' }} }}
function Restore-InstallRegistry($saved) {{ Set-Content -LiteralPath {up._ps_literal(registry)} -Value '{{"version":"old"}}' }}
function Assert-InstalledMetadata {{ if ((Get-Content -LiteralPath {up._ps_literal(registry)} -Raw | ConvertFrom-Json).version -ne 'new') {{ throw 'fake registry mismatch' }} }}
function Show-UpdateFailure($message) {{ Set-Content -LiteralPath {up._ps_literal(tmp_path/'dialog-shown')} -Value $message }}
function Start-Process {{
    param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle,[switch]$PassThru)
    if ($FilePath -eq $installer) {{ $FilePath={up._ps_literal(fake_installer)}; $ArgumentList=$null }}
    elseif ($ArgumentList -like '--migrate-update-customizations *') {{
        if ({'$true' if failure == 'migration-timeout' else '$false'}) {{
            return Microsoft.PowerShell.Management\Start-Process -FilePath {up._ps_literal(sys.executable)} -ArgumentList {up._ps_literal('-B "' + str(hanging) + '"')} -WindowStyle Hidden -PassThru
        }}
        if ({'$true' if timed_out else '$false'}) {{
            $fake = [pscustomobject]@{{ HasExited=$false; ExitCode=0; Id=$PID; StartTime=([Diagnostics.Process]::GetCurrentProcess().StartTime) }}
            $fake | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {{ param($timeout) return $this.HasExited }}
            $fake | Add-Member -MemberType ScriptMethod -Name Kill -Value {{
                if ({'$true' if failure in ('migration-kill-failure','migration-kill-status') else '$false'}) {{ throw 'synthetic Kill failure' }}
                Set-Content -LiteralPath {up._ps_literal(tmp_path/'migration-killed')} -Value stopped
                $this.HasExited={'$false' if failure == 'migration-wait-failure' else '$true'}
            }}
            return $fake
        }}
        $FilePath={up._ps_literal(migration_runner)}
    }}
    $params=@{{ FilePath=$FilePath; WindowStyle='Hidden' }}
    if ($WorkingDirectory) {{ $params.WorkingDirectory=$WorkingDirectory }}
    if ($ArgumentList) {{ $params.ArgumentList=$ArgumentList }}
    if ($PassThru) {{ $params.PassThru=$true }}
    Microsoft.PowerShell.Management\Start-Process @params
}}
'''
    if restore_failure:
        # Fail exactly the restoration check after kill, then permit fallback.
        shim += fr'''
$script:checkedAfterKill=$false
$originalAssert = ${{function:Assert-NotReparsePoint}}
function Assert-NotReparsePoint($path, $root) {{
    if (({'($null -ne $migrationProcess)' if failure == 'restore-failure-status' else '(Test-Path -LiteralPath '+up._ps_literal(tmp_path/'migration-killed')+')'}) -and $path -eq $rollback -and -not $script:checkedAfterKill) {{
        $script:checkedAfterKill=$true
        throw 'synthetic reparse assertion failure in restoration'
    }}
    & $originalAssert $path $root
}}
'''
    if status_failure:
        shim += "\nfunction Write-UpdateStatus($state, $message) { throw 'synthetic status write failure' }\n"
    if failure == "migration-timeout-dialog-failure":
        shim += "\nfunction Show-UpdateFailure($message) { throw 'synthetic dialog failure' }\n"
    script = script.replace("$installerProcess = $null", shim + "\n$installerProcess = $null", 1)
    prepared.helper_path.write_text(script, encoding="utf-8-sig")
    result = subprocess.run(
        [str(up._system_powershell_path()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(prepared.helper_path)],
        env=dict(os.environ, SAMSARA_UPDATE_TEST_NO_DIALOG="1"),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=40, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == (3 if restore_failure or kill_failure else 0 if success else 2), result.stdout
    outcome = json.loads(status.read_text(encoding="utf-8-sig"))
    if failure not in ("legacy", "legacy-malformed") and not orphan:
        assert isinstance(outcome["helper_pid"], int)
        assert int(outcome["helper_started_filetime"]) > 0
    if timed_out:
        deadline = time.monotonic() + 5
        while not old_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert old_marker.is_file()
        if failure == "migration-timeout":
            pid = int((tmp_path / "maintenance-pid").read_text())
            assert maintenance._process_creation_filetime(pid) is None
        elif failure not in ("migration-kill-failure", "migration-kill-status"):
            assert (tmp_path / "migration-killed").is_file()
        if failure != "migration-timeout-dialog-failure":
            message = (tmp_path / "dialog-shown").read_text(encoding="utf-8-sig")
            assert message.startswith("Customization recovery is pending." if kill_failure else "Customization migration took too long.")
        assert outcome["state"] == ("waiting" if status_failure else "awaiting_confirmation" if restore_failure or kill_failure else "rolled_back")
        assert prepared.rollback_dir.is_dir() == (restore_failure or kill_failure)
        assert workspace.is_dir()
        if restore_failure or kill_failure:
            assert (prepared.rollback_dir / "_internal/commands.json").read_bytes() == expected_old_bytes
            if not status_failure:
                # Continue past launching the old copy: its real tray reconcile
                # must preserve the installed copy's next-start migration.
                monkeypatch.setattr(up, "update_unavailable_reason", lambda: None)
                monkeypatch.setattr(ins, "repair_notice", lambda: None)
                monkeypatch.setattr(sys, "frozen", True, raising=False)
                monkeypatch.setenv("SAMSARA_HOME_DIR", str(updates.parent))
                monkeypatch.setattr(sys, "executable", str(prepared.rollback_dir / "Samsara.exe"))
                original_status = status.read_bytes()
                assert up.reconcile_update_on_startup() is None
                assert status.read_bytes() == original_status
                monkeypatch.setattr(sys, "executable", str(install / "Samsara.exe"))
                assert maintenance.migrate_pending_installer_update() is True
                assert "old" in json.loads(old_commands.read_text())["commands"]
                assert up.reconcile_update_on_startup().state == "installed"
        else:
            assert old_commands.read_bytes() == expected_old_bytes
            assert json.loads(registry.read_text(encoding="utf-8-sig"))["version"] == "old"
        return
    if failure == "restore-failure-status":
        deadline = time.monotonic() + 5
        while not old_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert old_marker.is_file()
        assert prepared.rollback_dir.is_dir()
        assert (prepared.rollback_dir / "_internal/commands.json").read_bytes() == expected_old_bytes
        return
    if orphan:
        # 651 retains the previous install even for a reported orphan update.
        # The raw archive must also survive deletion of the workspace.
        assert outcome["state"] == "reported"
        assert prepared.rollback_dir.is_dir() and not workspace.exists()
        assert Path(outcome["previous"]["path"]) == prepared.rollback_dir
        assert (prepared.rollback_dir / "_internal/commands.json").read_bytes() == expected_old_bytes
        assert outcome["previous"]["sha256"] == hashlib.sha256(b"old").hexdigest()
        assert outcome["customization_failure"] == "orphaned_helper"
        assert outcome["customization_retry"] is False
        assert outcome["customization_state"] == "failed"
        assert outcome["customization_notice_shown"] is True
        archive = Path(outcome["customization_backup_dir"])
        assert (archive / "commands.json").read_bytes() == expected_old_bytes
        assert (archive / "plugins/commands/custom.py").read_bytes() == b"custom = True\n"
        assert (archive / "plugins/commands/packaged.py").read_bytes() == b"old_edit = True\n"
        assert json.loads(old_commands.read_text())["commands"] == {"new": {"action": "new"}}
        return
    assert outcome["state"] == ("installed" if success else "rolled_back")
    assert (tmp_path / "maintenance-logs").exists() == (failure == "legacy")
    assert (install / "unins000.exe").read_bytes() == b"old"
    assert (install / "unins000.dat").read_bytes() == b"old"
    assert prepared.rollback_dir.is_dir() == success
    if success:
        assert Path(outcome["previous"]["path"]) == prepared.rollback_dir
        assert (prepared.rollback_dir / "_internal/commands.json").read_bytes() == expected_old_bytes
        assert outcome["previous"]["sha256"] == hashlib.sha256(b"old").hexdigest()
        assert json.loads(old_commands.read_text())["commands"] == {
            "new": {"action": "new"}, "old": {"action": "late-edit"}
        }
        assert (plugins / "custom.py").read_bytes() == b"custom = True\n"
        assert "new_package" in (plugins / "packaged.py").read_text()
        archives = list((updates / "backups").glob("customizations-*"))
        assert len(archives) == 1
        assert (archives[0] / "commands.json").read_bytes() == expected_old_bytes
        assert (archives[0] / "plugins/commands/packaged.py").read_bytes() == b"old_edit = True\n"
        assert not workspace.exists()
        assert json.loads(registry.read_text(encoding="utf-8-sig"))["version"] == "new"
    else:
        assert old_commands.read_bytes() == expected_old_bytes
        assert (plugins / "packaged.py").read_bytes() == b"old_edit = True\n"
        assert (plugins / "custom.py").read_bytes() == b"custom = True\n"
        assert json.loads(registry.read_text(encoding="utf-8-sig"))["version"] == "old"
