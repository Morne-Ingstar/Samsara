"""Restart regressions: disposable files, fake installer/launches, no app starts."""
from dataclasses import fields
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from samsara import single_instance, update_installer as ins, updater as up
from tests.test_update_helper_648 import prepared_update, powershell


def prepared(root, monkeypatch, route):
    result = prepared_update(root, monkeypatch)
    if route == 'portable':
        result = up.PreparedUpdate(**{f.name: getattr(result, f.name)
                                     for f in fields(up.PreparedUpdate)})
        monkeypatch.setattr(ins, 'installation', lambda *a, **kw: ins.Installation(result.install_dir))
    return result


@pytest.mark.parametrize('route', ['installer', 'portable'])
@pytest.mark.parametrize('profile', [None, '', 'absolute', 'relative'])
def test_helper_gets_fresh_runtime_and_same_profile(tmp_path, monkeypatch, route, profile):
    # Unset/empty profile cases still snapshot: keep their default home disposable.
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'synthetic-user'))
    p = prepared(tmp_path, monkeypatch, route)
    monkeypatch.chdir(tmp_path)
    original = None if profile is None else '' if profile == '' else str(
        tmp_path / "profile with space's" if profile == 'absolute' else Path('relative profile'))
    if original is None:
        monkeypatch.delenv('SAMSARA_HOME_DIR', raising=False)
    else:
        monkeypatch.setenv('SAMSARA_HOME_DIR', original)
    monkeypatch.setenv('PYINSTALLER_RESET_ENVIRONMENT', '0')
    calls = []
    up.launch_prepared_update(p, current_pid=2_000_000_000,
                             process_runner=lambda *args, **kw: calls.append(kw))
    assert len(calls) == 1
    child = calls[0]['env']
    assert child['PYINSTALLER_RESET_ENVIRONMENT'] == '1'
    assert child.get('SAMSARA_HOME_DIR') == (str(Path(original).resolve()) if original else original)
    assert single_instance.mutex_name_for_profile(child.get('SAMSARA_HOME_DIR')) == (
        single_instance.mutex_name_for_profile(original))
    assert os.environ.get('SAMSARA_HOME_DIR') == original
    assert os.environ['PYINSTALLER_RESET_ENVIRONMENT'] == '0'


# Execute the actual generated transaction and filesystem rollback. Only the
# registry, maintenance command and process launcher are replaced. The launcher
# records what the app would receive without executing any Samsara binary.
TRANSACTION_SEAMS = r'''
function Read-InstallRegistry { return @{} }
function Restore-InstallRegistry($saved) { }
function Assert-InstalledMetadata { }
function Invoke-CustomizationMigration { }
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, [switch]$PassThru)
    $kind = if ($FilePath -eq $installer) { 'installer' } elseif ($WorkingDirectory -eq $staged) { 'preflight' } else { 'app' }
    $version = if ($kind -eq 'installer') { '' } else { [IO.File]::ReadAllText($FilePath) }
    @{ kind=$kind; path=$FilePath; version=$version; style=[string]$WindowStyle;
       profile=$env:SAMSARA_HOME_DIR; reset=$env:PYINSTALLER_RESET_ENVIRONMENT } |
        ConvertTo-Json -Compress | Add-Content -LiteralPath $events -Encoding UTF8
    $process = [pscustomobject]@{ ExitCode=0; HasExited=$false }
    $process | Add-Member ScriptMethod WaitForExit { param($timeout) $this.HasExited=$true; return $true }
    $process | Add-Member ScriptMethod Refresh { }
    if ($kind -eq 'installer') {
        [IO.File]::WriteAllText((Join-Path $install $executable), 'new synthetic copy')
        if ($failure -eq 'installer') { $process.ExitCode=2 }
    } elseif ($kind -eq 'preflight') {
        if ($failure -eq 'preflight') { throw 'synthetic preflight failure' }
        Write-UpdateStatus 'staged_healthy' 'synthetic preflight healthy'
        $process.HasExited=$true
    } elseif ($version -eq 'new synthetic copy') {
        if ($failure -eq 'launch') { throw 'synthetic new launch failure' }
        Write-UpdateStatus 'installed' 'synthetic startup healthy'
    }
    if ($PassThru) { return $process }
}
'''


@pytest.mark.parametrize('route,failure,exit_code', [
    ('installer', '', 0), ('portable', '', 0),
    ('installer', 'installer', 2), ('installer', 'launch', 2),
    ('portable', 'launch', 2), ('portable', 'preflight', 4),
])
def test_transaction_launches_once_or_restores_old_copy(tmp_path, monkeypatch, route, failure, exit_code):
    p = prepared(tmp_path, monkeypatch, route)
    (p.staged_dir / p.executable_name).write_text('new synthetic copy')
    # Capture the real Python launch boundary, including its child environment.
    launch = []
    up.launch_prepared_update(p, current_pid=2_000_000_000,
                             process_runner=lambda *args, **kw: launch.append(kw))
    script = p.helper_path.read_text(encoding='utf-8-sig')
    events = tmp_path / 'launches.jsonl'
    seams = ('$events=' + up._ps_literal(events) + '\n$failure=' + up._ps_literal(failure) + '\n'
             + TRANSACTION_SEAMS)
    boundary = '$installerProcess = $null' if route == 'installer' else '$oldMoved = $false'
    script = script.replace(boundary, seams + '\n' + boundary, 1)
    # powershell() supplies the inherited environment of this test process.
    # Apply exactly the environment captured from the real helper launcher.
    for name in ('SAMSARA_HOME_DIR', 'PYINSTALLER_RESET_ENVIRONMENT'):
        monkeypatch.setenv(name, launch[0]['env'][name])
    result = powershell(script, tmp_path)
    assert result.returncode == exit_code, result.stdout
    calls = [json.loads(line) for line in events.read_text(encoding='utf-8-sig').splitlines()]
    apps = [call for call in calls if call['kind'] == 'app']
    new = [call for call in apps if call['version'] == 'new synthetic copy']
    old = [call for call in apps if call['version'] != 'new synthetic copy']
    assert len(new) == (0 if failure in ('installer', 'preflight') else 1)
    assert len(old) == bool(failure)
    assert len([call for call in calls if call['kind'] == 'installer']) == (route == 'installer')
    assert len([call for call in calls if call['kind'] == 'preflight']) == (route == 'portable')
    for call in calls:
        assert call['reset'] == '1'
        assert call['profile'] == launch[0]['env']['SAMSARA_HOME_DIR']
    for call in apps:
        assert Path(call['path']) == p.install_dir / p.executable_name
        assert call['style'] == ('Normal' if route == 'installer' else '')
    assert (p.install_dir / p.executable_name).read_text() == (
        'synthetic old copy' if failure else 'new synthetic copy')


@pytest.mark.parametrize('route', ['installer', 'portable'])
def test_old_process_still_running_prevents_any_launch(tmp_path, monkeypatch, route):
    p = prepared(tmp_path, monkeypatch, route)
    script = up._helper_script(p, 2_000_000_000)
    script = script.replace('$deadline = (Get-Date).AddSeconds(120)',
                            'function Get-Process { return $true }\n'
                            'function Start-Process { throw "must not launch" }\n'
                            '$deadline = (Get-Date).AddSeconds(0)')
    result = powershell(script, tmp_path)
    assert result.returncode == 1, result.stdout
    assert 'must not launch' not in result.stdout
    assert (p.install_dir / p.executable_name).read_text() == 'synthetic old copy'
    assert not p.rollback_dir.exists()


@pytest.mark.parametrize('route', ['installer', 'portable'])
def test_cancelled_quit_starts_nothing(qapp, tmp_path, monkeypatch, route):
    from samsara.ui import update_qt
    from tests.test_beta_updates_540 import _UpdateApp
    from types import SimpleNamespace
    p = prepared(tmp_path, monkeypatch, route)
    app = _UpdateApp()
    app._resolve_parked_draft_before_quit = Mock(return_value=False)
    app.quit_app = Mock()
    launch = Mock()
    monkeypatch.setattr(update_qt, 'launch_prepared_update', launch)
    dialog = update_qt._UpdateDialog(app, initial_release=SimpleNamespace(version=p.version, asset_size=1))
    try:
        dialog._on_prepare_finished(p, '')
        qapp.processEvents()
        app._resolve_parked_draft_before_quit.assert_called_once()
        launch.assert_not_called()
        app.quit_app.assert_not_called()
    finally:
        dialog.close()
        dialog.deleteLater()
