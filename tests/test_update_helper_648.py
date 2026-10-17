"""649: synthetic update transactions only; no app, live registry or installer."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from samsara import update_installer as ins, updater as up


def prepared_update(root, monkeypatch):
    install = root / 'Samsara'
    install.mkdir()
    for name in ('Samsara.exe', 'unins000.exe', 'unins000.dat'):
        (install / name).write_bytes(b'synthetic old copy')
    workspace = root / '.Samsara-update-v0.31.0-test'
    staged = workspace / 'payload'
    shutil.copytree(install, staged)
    updates = root / 'profile/updates'
    updates.mkdir(parents=True)
    installer = workspace / 'SamsaraSetup-0.31.0.exe'
    installer.write_bytes(b'synthetic installer, never executed')
    result = ins.PreparedInstaller('0.31.0', 'v0.31.0', install, staged,
        root / '.Samsara-rollback-v0.31.0-test', workspace,
        updates / 'last_update.json', updates / 'install-v0.31.0-test.ps1',
        installer_path=installer, installer_sha256=hashlib.sha256(installer.read_bytes()).hexdigest())
    monkeypatch.setattr(ins, 'installation', lambda *a, **kw: ins.Installation(install, ins.APP_KEY, kind='installed'))
    monkeypatch.setattr(up, '_require_update_eligible', lambda: None)
    monkeypatch.setattr(up, '_frozen_install_dir', lambda: install)
    monkeypatch.setattr(up, 'samsara_home_dir', lambda: updates.parent)
    monkeypatch.setattr(up, 'update_unavailable_reason', lambda: None)
    monkeypatch.setattr(ins, 'repair_notice', lambda: None)
    return result


def powershell(script, root):
    path = root / 'proof.ps1'
    path.write_text(script, encoding='utf-8-sig')
    return subprocess.run([str(up._system_powershell_path()), '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', str(path)], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=25,
        creationflags=subprocess.CREATE_NO_WINDOW,
        env=dict(os.environ, SAMSARA_UPDATE_TEST_NO_DIALOG='1'))


@pytest.mark.parametrize('suffix', ['\\', '/', '\\/', ''])
def test_python_detector_accepts_trailing_separator(tmp_path, suffix):
    (tmp_path / 'unins000.exe').write_bytes(b'fake')
    record = dict(registry_name=ins.APP_KEY, InstallLocation=str(tmp_path)+suffix)
    assert ins.installation(tmp_path, entries=[record]).kind == 'installed'
    record['InstallLocation'] = str(tmp_path / 'other')+suffix
    assert ins.installation(tmp_path, entries=[record]).kind == 'portable'


@pytest.mark.parametrize('location,expected', [('C:/Synthetic/Samsara\\', 0),
    ('c:/synthetic/SAMSARA/', 0), ('C:/Synthetic/Samsara-other\\', 7)])
def test_real_powershell_registry_checks(tmp_path, location, expected):
    functions = ins.INSTALLER_TRANSACTION.split('# Recovery metadata')[0]
    functions = functions.replace(
        '[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser,$registryView)', '$script:fakeBase')
    fake = r'''
$ErrorActionPreference='Stop'
$install='C:/Synthetic/Samsara'
$tag='v0.31.0'
$registryName='unused'
$script:key = [pscustomobject]@{}
$key | Add-Member ScriptMethod GetValue { param($name) if ($name -eq 'InstallLocation') { return $script:location }; return '0.31.0' }
$key | Add-Member ScriptMethod GetValueNames { return @() }
$key | Add-Member ScriptMethod Close { }
$script:fakeBase = [pscustomobject]@{}
$fakeBase | Add-Member ScriptMethod OpenSubKey { param($name) return $script:key }
$fakeBase | Add-Member ScriptMethod Close { }
'''
    # Both call sites execute the generated normalizer; no real key is opened.
    for function in ('Read-InstallRegistry', 'Assert-InstalledMetadata'):
        result = powershell(fake+'\n$script:location='+up._ps_literal(location)+'\n'+functions+
            f'\ntry {{ {function} | Out-Null }} catch {{ Write-Output $_.Exception.Message; exit 7 }}', tmp_path)
        assert result.returncode == expected, result.stdout


def test_rendered_helper_normalizes_both_registry_checks_and_logs_first(tmp_path, monkeypatch):
    prepared = prepared_update(tmp_path, monkeypatch)
    script = up._helper_script(prepared, 2_000_000_000)
    assert script.count("(Get-InstallFolder ([string]$key.GetValue('InstallLocation')))") == 2
    assert ".TrimEnd([char[]]'\\/')" in script
    assert script.index("' start'") < script.index("$ErrorActionPreference")
    assert 'helper_pid=$PID; helper_started_filetime=' in script


@pytest.mark.parametrize('failure', [None, 'paths', 'hash', 'registry', 'installer', 'migration', 'health', 'restore'])
def test_helper_step_log_success_and_failures(tmp_path, monkeypatch, failure):
    p = prepared_update(tmp_path, monkeypatch)
    script = up._helper_script(p, 2_000_000_000)
    # Only side effects are faked. The real transaction, catches, status and
    # append log run; no executable is started and no registry key is touched.
    shim = r'''
function Read-InstallRegistry { if ($failure -eq 'registry') { throw 'fake registry failure' }; return @{} }
function Assert-InstalledMetadata { }
function Restore-InstallRegistry($saved) { if ($failure -eq 'restore') { throw 'fake restore failure' } }
function Invoke-CustomizationMigration {
    Write-HelperLog 'migration started'
    if ($failure -eq 'migration') { throw 'fake migration failure' }
    Write-HelperLog 'migration completed'
}
function Start-Process {
    param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle,[switch]$PassThru)
    $process = [pscustomobject]@{ ExitCode=0; HasExited=$false }
    if ($FilePath -eq $installer) {
        if ($failure -eq 'installer') { $process.ExitCode=8 }
    } elseif ($failure -in @('health','restore')) { throw 'fake health failure' }
    else { Write-UpdateStatus 'installed' 'fake health confirmed' }
    $process | Add-Member ScriptMethod WaitForExit { param($timeout) $this.HasExited=$true; return $true }
    return $process
}
function Start-RecoveryCopy($copies) { Write-HelperLog 'relaunch recovery copy' }
'''
    script = script.replace('$installerProcess = $null', '$failure='+up._ps_literal(failure or '')+'\n'+shim+'\n$installerProcess = $null', 1)
    if failure == 'hash':
        p.installer_path.write_bytes(b'tampered')
    if failure == 'paths':
        (p.staged_dir / 'Samsara.exe').unlink()
    result = powershell(script, tmp_path)
    assert result.returncode == (0 if failure is None else 5 if failure == 'paths' else 3 if failure == 'restore' else 2), result.stdout
    log = ins.helper_log_path(p).read_text(encoding='utf-8')
    assert ' start\n' in log
    assert all(line[:4].isdigit() and 'T' in line[:20] for line in log.splitlines())
    if failure == 'paths':
        assert 'Update path validation failed' in log
    else:
        assert 'paths ok' in log and 'waiting' in log
        if failure:
            assert 'failure ' in log
        else:
            for step in ('installer started', 'installer exit code 0', 'migration completed', 'relaunch updated copy', 'health confirmed'):
                assert step in log
        if failure in ('installer', 'migration', 'health', 'restore'):
            assert 'rollback started' in log
            assert ('rollback failure' if failure == 'restore' else 'rollback completed') in log


@pytest.mark.parametrize('state', ['ready', 'waiting'])
@pytest.mark.parametrize('age,alive,notice', [(179, False, False), (181, False, True), (901, True, False)])
def test_stale_launch_notice_once(tmp_path, monkeypatch, state, age, alive, notice):
    from samsara import update_installer_customizations as migration
    p = prepared_update(tmp_path, monkeypatch)
    monkeypatch.setattr(migration, '_helper_owns_update', lambda payload: alive)
    up._write_status(p.status_path, state, 'pending', p.tag, p)
    now = time.time()
    payload = json.loads(p.status_path.read_text())
    payload['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now-age))
    p.status_path.write_text(json.dumps(payload))
    result = up.reconcile_update_on_startup(now=now)
    if notice:
        assert result.state == 'failed' and "didn't install. Try again from Updates." in result.message
        assert up.reconcile_update_on_startup(now=now) is None
        assert p.workspace_dir.exists()  # no startup deletion
    else:
        assert result.state == state


def test_launch_breakaway_denied_falls_back_with_owned_stdio(tmp_path, monkeypatch):
    p = prepared_update(tmp_path, monkeypatch)
    calls = []
    def runner(command, **options):
        calls.append(options)
        if len(calls) == 1:
            exc = OSError('synthetic job forbids breakaway')
            exc.winerror = 5
            raise exc
        up._write_status(p.status_path, 'waiting', 'fake acknowledged', p.tag, p, {'helper_pid': 4242})
        return SimpleNamespace(pid=4242, poll=lambda: None)
    up.launch_prepared_update(p, process_runner=runner)
    assert len(calls) == 2
    assert calls[0]['creationflags'] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert not calls[0]['creationflags'] & subprocess.DETACHED_PROCESS
    assert not calls[1]['creationflags'] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert calls[0]['stdin'] == subprocess.DEVNULL
    assert calls[0]['stdout'] is calls[0]['stderr']
    assert 'job breakaway denied' in ins.launch_log_path(p).read_text()


def test_dead_helper_prevents_successful_launch(tmp_path, monkeypatch):
    p = prepared_update(tmp_path, monkeypatch)
    dead = SimpleNamespace(pid=4242, poll=lambda: 9, returncode=9)
    with pytest.raises(up.UpdateError, match='exited before startup'):
        up.launch_prepared_update(p, process_runner=lambda *a, **kw: dead)
    assert json.loads(p.status_path.read_text())['state'] == 'failed'
    assert 'launch failed' in ins.launch_log_path(p).read_text()


def _kill_on_close_job():
    """A disposable parent-owned job, explicitly allowing helper breakaway."""
    import ctypes
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
            ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
            ('active', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
            ('process_mem', ctypes.c_size_t), ('job_mem', ctypes.c_size_t),
            ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, w.LPCWSTR)
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = (w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD)
    kernel.AssignProcessToJobObject.argtypes = (w.HANDLE, w.HANDLE)
    kernel.GetCurrentProcess.restype = w.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, handle  # keep the handle owned by this parent until exit


@pytest.mark.parametrize('exit_kind', ['atexit', 'hard', 'job-hard', 'job-control', 'no-stdio-hard'])
def test_real_helper_outlives_parent_exit_and_ducking_tree_kill(tmp_path, monkeypatch, exit_kind):
    p = prepared_update(tmp_path, monkeypatch)
    # Stop at the transaction boundary, after the REAL generated prefix has
    # waited for the launching parent's disappearance. No install is run.
    marker = tmp_path / 'helper-survived'
    real_script = up._helper_script(p, 1)
    prefix = real_script[:real_script.index('function Get-InstallFolder')]
    parent = tmp_path / 'fake-parent.py'
    parent.write_text(
        'import atexit, os, subprocess, sys\nfrom pathlib import Path\n'
        f'sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n'
        'from samsara import updater as up\nfrom samsara import update_installer as ins\n'
        'from samsara.audio_ducking import _kill_process_tree\nfrom samsara.process_exit import hard_exit\n'
        'from tests.test_update_helper_648 import _kill_on_close_job\n'
        f'p=ins.PreparedInstaller("0.31.0", "v0.31.0", *map(Path, {list(map(str, (p.install_dir,p.staged_dir,p.rollback_dir,p.workspace_dir,p.status_path,p.helper_path)))!r}))\n'
        'up._require_update_eligible=lambda:None\nup._validate_prepared_update=lambda p:None\n'
        f'up._helper_script=lambda p,pid: {prefix!r}.replace("Get-Process -Id 1 ", "Get-Process -Id "+str(pid)+" ")+"\\n[IO.File]::WriteAllText("+up._ps_literal({str(marker)!r})+", \'survived\')\\n"\n'
        'duck=subprocess.Popen([sys.executable,"-B","-c","import time; time.sleep(20)"],creationflags=subprocess.CREATE_NO_WINDOW)\n'
        + ('job=_kill_on_close_job()\n' if exit_kind.startswith('job-') else '')
        + ('subprocess.CREATE_BREAKAWAY_FROM_JOB=0\n' if exit_kind == 'job-control' else '')
        + ('import ctypes\nk=ctypes.WinDLL("kernel32"); [k.SetStdHandle(n,0) for n in (-10,-11,-12)]\n' if exit_kind == 'no-stdio-hard' else '')
        +
        'up.launch_prepared_update(p, current_pid=os.getpid())\n'
        '_kill_process_tree(duck)\n'
        + ('atexit.register(lambda: None)\n' if exit_kind == 'atexit' else 'hard_exit(0)\n'), encoding='utf-8')
    result = subprocess.run([sys.executable, '-B', str(parent)], capture_output=True, text=True,
        timeout=25, creationflags=subprocess.CREATE_NO_WINDOW,
        env=dict(os.environ, SAMSARA_UPDATE_TEST_NO_DIALOG='1'))
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic()+10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    if exit_kind == 'job-control':
        assert not marker.exists()  # confirms the parent-exit failure mechanism
    else:
        assert marker.read_text() == 'survived'
    assert 'helper startup acknowledged' in ins.launch_log_path(p).read_text()


def test_running_version_labels(qapp, monkeypatch):
    from PySide6.QtWidgets import QLabel
    from samsara import __version__, cuda_detect
    from samsara.ui import home_qt, settings_qt
    from tests.test_home_qt import _FakeSettings, _app
    from tests.test_settings_pages import _StubApp, _schema_defaults
    from samsara import autostart
    monkeypatch.setattr(home_qt, 'QSettings', _FakeSettings)
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **kw: None)
    monkeypatch.setattr(cuda_detect, 'request_cuda_status', lambda callback: None)
    home = home_qt.HomePage(_app())
    settings = settings_qt._SettingsWindow(_StubApp(_schema_defaults()))
    try:
        for page in (home, settings._pages['General']):
            assert page.findChild(QLabel, 'runningVersion').text() == f'v{__version__}'
    finally:
        home.deleteLater()
        settings.deleteLater()
