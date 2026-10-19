"""One retained version and early startup recovery (651).

Importing this module does no I/O and loads neither Qt nor app backends.
The detached update helper owns all install-folder mutations.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import sys
import time
import uuid

log = logging.getLogger('Samsara')
WINDOW_SECONDS = 600
CAPABILITY_FILE = '.samsara-previous-version-651'
PERSISTENT_FIELDS = ('previous', 'previous_source', 'obsolete_previous')


def read_record(path):
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 64 * 1024:
            return {}
        data = json.loads(raw.decode('utf-8-sig'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_record(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(data, sort_keys=True), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def preserve_status(path, payload):
    old = read_record(path)
    for key in PERSISTENT_FIELDS:
        if key in old:
            payload.setdefault(key, old[key])


def _plain_tree(path, parent):
    """Confine before traversing; reject junctions as well as symlinks."""
    path, parent = Path(path), Path(parent).resolve()
    if path.parent.resolve() != parent:
        raise ValueError('The previous version is outside the install folder parent.')
    for ancestor in (parent, path):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Previous-version paths may not be links or junctions.')
    for directory, dirs, names in os.walk(path, followlinks=False):
        for name in [*dirs, *names]:
            info = (Path(directory) / name).lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('The previous version contains a link or junction.')


def previous_info(*, status_path=None, install_dir=None, verify=False):
    """Cheap UI read by default; full tree/hash verification runs in a worker."""
    from samsara.paths import samsara_home_dir
    install = Path(install_dir or Path(sys.executable).parent).resolve()
    status_path = status_path or samsara_home_dir() / 'updates' / 'last_update.json'
    previous = read_record(status_path).get('previous')
    if not isinstance(previous, dict):
        return None
    try:
        path = Path(previous['path'])
        if (not path.is_absolute() or path.parent.resolve() != install.parent
                or not path.name.startswith(f'.{install.name}-rollback-')):
            return None
        if not all(isinstance(previous[k], str) and previous[k]
                   for k in ('version', 'tag', 'kept_at', 'sha256')):
            return None
        if previous['tag'] != 'v' + previous['version']:
            return None
        if len(previous['sha256']) != 64 or any(c not in '0123456789abcdef' for c in previous['sha256'].lower()):
            return None
        for candidate in (install.parent, path, path / 'Samsara.exe', path / CAPABILITY_FILE):
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                return None
        if not (path / 'Samsara.exe').is_file():
            return None
        # Bind the capability to these executable bytes: an in-place installer
        # may leave an old marker behind when replacing the executable.
        if (path / CAPABILITY_FILE).read_text(encoding='utf-8').strip().lower() != previous['sha256'].lower():
            return None
        if verify:
            _plain_tree(path, install.parent)
            with (path / 'Samsara.exe').open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual.lower() != previous['sha256'].lower():
                raise ValueError('The kept Samsara.exe changed; going back was cancelled.')
        return dict(previous)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def process_alive(pid):
    """Query only: do not signal a process (including os.kill(pid, 0))."""
    if not isinstance(pid, int) or pid <= 0 or os.name != 'nt':
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5  # access denied is not evidence of a crash
    try:
        code = wintypes.DWORD()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel.CloseHandle(handle)


def begin_start(path, version, *, now=None, pid=None, is_running=process_alive):
    """Count the preceding unacknowledged start, then mark this one pending.

    A third start sees two failures; the first launch never counts itself.
    Concurrent shortcut launches leave the original pending record alone.
    """
    now, pid = time.time() if now is None else now, os.getpid() if pid is None else pid
    old = read_record(path)
    if is_running(old.get('pending_pid')):
        return False
    data = dict(version=version, failures=0, first_failed_at=None)
    try:
        if old.get('version') == version and old.get('pending_pid'):
            first = old.get('first_failed_at')
            first = old['started_at'] if first is None else first
            if 0 <= now - float(first) <= WINDOW_SECONDS:
                data.update(failures=max(0, int(old.get('failures', 0))) + 1,
                            first_failed_at=first)
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    data.update(pending_pid=pid, started_at=now)
    write_record(path, data)
    return data['failures'] >= 2


def mark_healthy(path=None, *, pid=None):
    from samsara.paths import samsara_home_dir
    path = Path(path or samsara_home_dir() / 'updates' / 'startup_failures.json')
    try:
        if read_record(path).get('pending_pid') == (os.getpid() if pid is None else pid):
            path.unlink(missing_ok=True)
    except OSError:
        log.warning('[UPDATE] Could not clear startup failure counter')


def another_instance_running():
    """Read mutex existence only; never acquire ownership or activate the app."""
    import ctypes
    from ctypes import wintypes
    from samsara.single_instance import DEFAULT_MUTEX_NAME
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenMutexW(0x00100000, False, DEFAULT_MUTEX_NAME)
    if not handle:
        return ctypes.get_last_error() == 5
    kernel.CloseHandle(handle)
    return True


def early_startup():
    """Called after maintenance dispatch, before heavy/native imports."""
    if (not getattr(sys, 'frozen', False) or os.name != 'nt'
            or any(arg in {'--restart-helper', '--fetch-components', '--migrate-update-customizations', '--update-helper'} for arg in sys.argv[1:])
            or os.environ.get('SAMSARA_DUCKING_HOST') == '1'
            or os.environ.get('SAMSARA_HOME_DIR')):
        return
    if another_instance_running():
        return  # A normal second shortcut launch is not a failed app startup.
    from samsara import __version__
    from samsara.paths import samsara_home_dir
    updates = samsara_home_dir() / 'updates'
    payload = read_record(updates / 'last_update.json')
    # A helper owns preflight/relaunch health and rollback; do not interfere.
    from samsara.update_installer_customizations import _helper_owns_update
    if _helper_owns_update(payload):
        return
    try:
        offer = begin_start(updates / 'startup_failures.json', __version__)
        previous = previous_info()
        if offer and previous:
            from samsara.ui.update_qt import startup_previous_offer
            if startup_previous_offer(previous, __version__):
                mark_healthy()
                raise SystemExit(0)
    except Exception:
        log.exception('[UPDATE] Early previous-version recovery unavailable; continuing startup')


def prepare_revert(*, status_path=None, install_dir=None, expected=None):
    """Verify first; create only an empty helper workspace beside the install."""
    from dataclasses import dataclass
    from samsara import updater
    from samsara.paths import samsara_home_dir
    install = Path(install_dir or Path(sys.executable).parent).resolve()
    status = Path(status_path or samsara_home_dir() / 'updates' / 'last_update.json')
    previous = previous_info(status_path=status, install_dir=install, verify=True)
    if not previous:
        raise updater.UpdateError('No verified previous version is available.')
    if expected is not None and any(previous.get(key) != expected.get(key) for key in ('path', 'sha256', 'version')):
        raise updater.UpdateError('The previous version changed. Open Updates and try again.')
    if not (install / 'Samsara.exe').is_file():
        raise updater.UpdateError('The current Samsara.exe is missing.')
    _plain_tree(install, install.parent)
    suffix = 'revert-' + uuid.uuid4().hex[:12]
    workspace = install.parent / f'.{install.name}-update-{suffix}'
    workspace.mkdir()

    @dataclass(frozen=True)
    class RevertUpdate(updater.PreparedUpdate):
        reverse: bool = True

    return RevertUpdate(previous['version'], previous['tag'], install, Path(previous['path']),
                        install.parent / f'.{install.name}-rollback-{suffix}', workspace,
                        status, status.parent / f'install-{suffix}.ps1')


def launch_revert(prepared, *, current_pid=None, process_runner=None):
    from samsara import updater
    from samsara.update_installer import launch_helper
    # Reverts are local, network-free and work for portable and registered copies.
    if updater.update_unavailable_reason() is not None:
        raise updater.UpdateNotSupported(updater.update_unavailable_reason())
    launch_helper(prepared, os.getpid() if current_pid is None else current_pid,
                  updater.subprocess.Popen if process_runner is None else process_runner)


# Shared by forward installer/ZIP, reverse swap, and interrupted-cleanup retry.
KEEP_PREVIOUS_PS = r'''
function Assert-PreviousCapability([string]$path, [string]$expectedHash) {
    $marker = Join-Path $path '__CAPABILITY_FILE__'
    Assert-NotReparsePoint $marker $installParent
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf) -or
        [IO.File]::ReadAllText($marker).Trim() -ine $expectedHash) {
        throw 'This previous version does not support going back safely.'
    }
}
function Mark-CurrentCapability {
    # This helper is rendered by a 651-capable running build. Never stamp its
    # incoming target or a retained legacy copy; only mark the outgoing build.
    $marker = Join-Path $install '__CAPABILITY_FILE__'
    Assert-NotReparsePoint $marker $installParent
    $hash = Get-PreviousHash (Join-Path $install 'Samsara.exe')
    [IO.File]::WriteAllText($marker, $hash)
    Write-HelperLog 'marked current previous-version capability'
}
function Get-PreviousHash([string]$path) {
    $stream = [IO.File]::OpenRead($path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose(); $stream.Dispose() }
}
function Assert-PreviousPath([string]$path) {
    $full = [IO.Path]::GetFullPath($path)
    if ([IO.Path]::GetDirectoryName($full) -ine [IO.Path]::GetFullPath($installParent) -or
        -not [IO.Path]::GetFileName($full).StartsWith('.' + [IO.Path]::GetFileName($install) + '-rollback-', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Unsafe previous-version path.'
    }
    Assert-NotReparsePoint $full $installParent
    if (Test-Path -LiteralPath $full) {
        foreach ($item in (Get-ChildItem -LiteralPath $full -Recurse -Force)) {
            Assert-NotReparsePoint $item.FullName $installParent
        }
    }
}
function Remove-ObsoletePrevious {
    if ($script:obsoletePrevious) {
        Assert-PreviousPath $script:obsoletePrevious
        if ($script:obsoletePrevious -ieq $script:previous.path) { throw 'Refusing to delete the retained version.' }
        if (Test-Path -LiteralPath $script:obsoletePrevious) {
            Write-HelperLog 'remove older kept copy'
            Remove-Item -LiteralPath $script:obsoletePrevious -Recurse -Force -ErrorAction Stop
        }
        $script:obsoletePrevious = $null
    }
}
function Keep-PreviousVersion {
    Assert-PreviousPath $rollback
    if (-not (Test-Path -LiteralPath $rollback -PathType Container)) { return }
    Remove-ObsoletePrevious
    if ($script:previous.path -ine $rollback) {
        $oldExe = Join-Path $rollback 'Samsara.exe'
        $hash = Get-PreviousHash $oldExe
        $size = (Get-ChildItem -LiteralPath $rollback -Recurse -File -Force | Measure-Object -Property Length -Sum).Sum
        $older = $script:previous.path
        if ($older) { Assert-PreviousPath $older }
        $script:previous = @{version=$sourceVersion; tag=$sourceTag; kept_at=[DateTime]::UtcNow.ToString('o'); path=$rollback; sha256=$hash; size_bytes=$size}
        $script:obsoletePrevious = $older
        Write-HelperLog "keep previous $sourceTag at $rollback ($size bytes)"
        $confirmed = Get-Content -LiteralPath $status -Raw | ConvertFrom-Json
        Write-UpdateStatus $confirmed.state $confirmed.message
    }
    Remove-ObsoletePrevious
}
'''.replace('__CAPABILITY_FILE__', CAPABILITY_FILE)


def _preserve_customization_status(script):
    """Retaining a healthy copy must keep the startup recovery receipt."""
    return script.replace('$data | ConvertTo-Json', r'''if (($state -in @('installed', 'reported', 'cleanup_pending')) -and (Test-Path -LiteralPath $status)) {
        $saved = Get-Content -LiteralPath $status -Raw | ConvertFrom-Json
        if ($saved.tag -eq $tag -and $saved.install_dir -eq $install) {
            foreach ($property in $saved.PSObject.Properties) {
                if ($property.Name.StartsWith('customization_')) { $data[$property.Name] = $property.Value }
            }
        }
    }
    $data | ConvertTo-Json''', 1)


def decorate_helper(script, prepared):
    """Add retention to the existing logged helper, keeping its transaction."""
    from samsara import __version__, updater
    init = f'''$sourceVersion = {updater._ps_literal(__version__)}
$sourceTag = {updater._ps_literal('v' + __version__)}
$previous = $null
$obsoletePrevious = $null
if (Test-Path -LiteralPath $status) {{
    $savedPrevious = Get-Content -LiteralPath $status -Raw | ConvertFrom-Json
    $previous = $savedPrevious.previous
    $obsoletePrevious = $savedPrevious.obsolete_previous
}}
'''
    script = script.replace('function Write-UpdateStatus', init + KEEP_PREVIOUS_PS + '\nfunction Write-UpdateStatus', 1)
    # The ZIP transaction stamps only after staged health and quit.
    snapshot = '    Move-Item -LiteralPath $install -Destination $rollback'
    script = script.replace(snapshot, '    Mark-CurrentCapability\n' + snapshot, 1)
    script = script.replace('state=$state; message=$message; tag=$tag;',
                            'state=$state; message=$message; tag=$tag; previous=$script:previous; obsolete_previous=$script:obsoletePrevious; previous_source=@{version=$sourceVersion; tag=$sourceTag};', 1)
    script = script.replace('Assert-NotReparsePoint $rollback $installParent; Remove-Item -LiteralPath $rollback -Recurse -Force -ErrorAction Stop',
                            'Keep-PreviousVersion', 1)
    # The installer uses a multiline success cleanup block.
    script = script.replace('Assert-NotReparsePoint $rollback $installParent\n                Remove-Item -LiteralPath $rollback -Recurse -Force',
                            'Keep-PreviousVersion', 1)
    return _preserve_customization_status(script)


def decorate_cleanup(script):
    """Retries must retain the successful rollback, even after helper death."""
    script = script.replace('function Write-CleanupStatus', '''$installParent = $expectedParent
$executable = 'Samsara.exe'
$savedPrevious = Get-Content -LiteralPath $status -Raw | ConvertFrom-Json
$previous = $savedPrevious.previous
$obsoletePrevious = $savedPrevious.obsolete_previous
$sourceVersion = $savedPrevious.previous_source.version
$sourceTag = $savedPrevious.previous_source.tag
function Write-HelperLog([string]$message) {
    try { [IO.File]::AppendAllText((Join-Path ([IO.Path]::GetDirectoryName($status)) 'helper-retention-cleanup.log'), ([DateTime]::UtcNow.ToString('o') + ' ' + $message + [Environment]::NewLine)) } catch { }
}
''' + KEEP_PREVIOUS_PS + '''
function Write-UpdateStatus([string]$state, [string]$message) { Write-CleanupStatus $state $message }
function Write-CleanupStatus''', 1)
    script = script.replace('state=$state; message=$message; tag=$tag;',
                            'state=$state; message=$message; tag=$tag; previous=$script:previous; obsolete_previous=$script:obsoletePrevious; previous_source=@{version=$sourceVersion; tag=$sourceTag}; install_dir=$install; rollback_dir=$rollback; workspace_dir=$workspace;', 1)
    script = script.replace('foreach ($candidate in @($rollbackFull, $workspaceFull)) {', '''if ($sourceVersion -and $sourceTag) { Keep-PreviousVersion }
    Remove-ObsoletePrevious
    foreach ($candidate in @($rollbackFull, $workspaceFull)) {
        if ($candidate -ieq $script:previous.path) { continue }''', 1)
    return _preserve_customization_status(script)


def reverse_helper(prepared, current_pid):
    from samsara import updater
    base = updater._archive_helper_script(prepared, current_pid)
    prefix = base[:base.index('$oldMoved = $false')]
    prefix = prefix.replace("[IO.Path]::Combine($workspaceFull, 'payload')", "[IO.Path]::GetFullPath([string]$previous.path)")
    # Validate the retained source before acknowledging startup/waiting for quit.
    prefix = prefix.replace('    $script:pathsValidated = $true', '''    Assert-PreviousPath $staged
    Assert-PreviousCapability $staged $previous.sha256
    if ((Get-PreviousHash (Join-Path $staged 'Samsara.exe')) -ine $previous.sha256) { throw 'The previous executable changed.' }
    $script:pathsValidated = $true''', 1)
    from samsara import update_installer
    info = update_installer.installation(prepared.install_dir)
    registry = '$registrySnapshot = $null\n$registryName = $null\n'
    if info.kind == 'installed':
        registry += '$registryName = ' + updater._ps_literal(update_installer.UNINSTALL_ROOT + '\\' + update_installer.APP_KEY) + '\n'
        registry += '$registryView = [Microsoft.Win32.RegistryView]::' + ('Registry32' if info.view == 32 else 'Registry64') + '\n'
        registry += update_installer.INSTALLER_TRANSACTION.split('# Recovery metadata', 1)[0]
    return prefix + registry + REVERSE_TRANSACTION_PS


REVERSE_TRANSACTION_PS = r'''
$oldMoved = $false
$targetMoved = $false
$newProcess = $null
$originalPrevious = $previous
try {
    if ($registryName) { $registrySnapshot = Read-InstallRegistry }
    Assert-PreviousPath $staged
    Assert-PreviousCapability $staged $originalPrevious.sha256
    if ((Get-PreviousHash (Join-Path $staged 'Samsara.exe')) -ine $originalPrevious.sha256) { throw 'The previous executable changed while waiting.' }
    Assert-NotReparsePoint $install $installParent
    Mark-CurrentCapability
    Write-HelperLog 'reverse swap current to rollback'
    Move-Item -LiteralPath $install -Destination $rollback
    $oldMoved = $true
    Write-HelperLog 'reverse swap previous to current'
    Move-Item -LiteralPath $staged -Destination $install
    $targetMoved = $true
    if ($registryName) {
        $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser,$registryView)
        $key = $baseKey.OpenSubKey($registryName, $true)
        try { $key.SetValue('DisplayVersion', $tag.TrimStart('v')) }
        finally { if ($key) { $key.Close() }; $baseKey.Close() }
        Assert-InstalledMetadata
    }
    # The previous path has moved; never advertise a missing copy.
    $previous = $null
    Write-UpdateStatus 'awaiting_confirmation' 'Waiting for the previous version to start.'
    $newProcess = Start-Process -FilePath (Join-Path $install $executable) -WorkingDirectory $install -WindowStyle Hidden -PassThru
    $healthDeadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $healthDeadline) {
        $current = Get-Content -LiteralPath $status -Raw | ConvertFrom-Json
        if ($current.state -in @('installed', 'reported')) {
            Write-HelperLog 'reverse health confirmed'
            try {
                Keep-PreviousVersion
                Assert-NotReparsePoint $workspace $installParent
                Remove-Item -LiteralPath $workspace -Recurse -Force
            } catch { Write-UpdateStatus 'cleanup_pending' "The version was restored; keeping/cleanup will retry: $($_.Exception.Message)" }
            exit 0
        }
        if ($newProcess.HasExited) { throw 'The previous version closed before confirming startup.' }
        Start-Sleep -Milliseconds 250
        $newProcess.Refresh()
    }
    throw 'The previous version did not confirm startup.'
} catch {
    $problem = $_.Exception.Message
    Write-HelperLog "reverse failure $problem"
    try {
        if ($newProcess -and -not $newProcess.HasExited) {
            $null = $newProcess.CloseMainWindow()
            if (-not $newProcess.WaitForExit(10000)) { throw 'The launched copy is still open; both copies were retained.' }
        }
        if ($targetMoved) {
            Assert-PreviousPath $staged
            Assert-NotReparsePoint $install $installParent
            Move-Item -LiteralPath $install -Destination $staged
        }
        if ($oldMoved) {
            Assert-PreviousPath $rollback
            Move-Item -LiteralPath $rollback -Destination $install
        }
        if ($registrySnapshot) { Restore-InstallRegistry $registrySnapshot }
        $previous = $originalPrevious
        Write-UpdateStatus 'rolled_back' "Going back failed; the current version was restored. $problem"
        Start-Process -FilePath (Join-Path $install $executable) -WorkingDirectory $install -WindowStyle Hidden
        Show-UpdateFailure "Going back failed. The current version was restored. $problem"
        exit 2
    } catch {
        Write-UpdateStatus 'failed' "Going back failed; runnable copies were retained. $problem / $($_.Exception.Message)"
        if (-not $newProcess -or $newProcess.HasExited) {
            foreach ($copy in @($install, $rollback, $staged)) {
                try {
                    Assert-NotReparsePoint $copy $installParent
                    $copyExe = Join-Path $copy $executable
                    Assert-NotReparsePoint $copyExe $installParent
                    if (Test-Path -LiteralPath $copyExe -PathType Leaf) {
                        Write-HelperLog "relaunch retained copy $copy"
                        Start-Process -FilePath $copyExe -WorkingDirectory $copy -WindowStyle Hidden
                        break
                    }
                } catch { }
            }
        }
        Show-UpdateFailure "Going back failed. Both folders were retained. $problem / $($_.Exception.Message)"
        exit 3
    }
}
'''
