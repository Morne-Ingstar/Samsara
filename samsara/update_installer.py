"""Installer ownership detection and installed-copy update policy (554)."""
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sys
import hashlib
import json
import shutil
import tempfile
import uuid
import stat
from samsara import updater

logger = logging.getLogger('Samsara')
RELEASES_URL = 'https://github.com/Morne-Ingstar/Samsara/releases'
UNINSTALL_ROOT = r'Software\Microsoft\Windows\CurrentVersion\Uninstall'
APP_KEY = '{7C0E4D2B-5A57-4F5E-9C7E-6B1B3F2D8A34}_is1'
GUARD_MESSAGE = 'This installed copy needs the installer to update. Get a new installer.'
REPAIR_MESSAGE = 'The uninstaller is missing. Reinstall Samsara from its installer to repair it.'
_repair_reported = False


@dataclass(frozen=True)
class Installation:
    folder: Path
    registry_name: str | None = None
    components: str = 'core'
    kind: str = 'portable'
    hive: str = 'HKCU'
    view: int = 64


_detection_issue_logged = False


def _detection_issue(reason):
    global _detection_issue_logged
    if not _detection_issue_logged:
        _detection_issue_logged = True
        logger.warning('[UPDATE] Installer ownership could not be confirmed: %s',reason)


def read_uninstall_entries():
    """Read only Samsara's known key, in both hives/views; never enumerate."""
    import winreg
    entries = []
    unreadable = False
    key_path = UNINSTALL_ROOT+'\\'+APP_KEY
    for hive_name,hive in [('HKCU',winreg.HKEY_CURRENT_USER),('HKLM',winreg.HKEY_LOCAL_MACHINE)]:
        for bits,view_flag in [(64,winreg.KEY_WOW64_64KEY),(32,winreg.KEY_WOW64_32KEY)]:
            try:
                key = winreg.OpenKey(hive,key_path,0,winreg.KEY_READ|view_flag)
            except FileNotFoundError:
                continue
            except OSError:
                unreadable = True
                continue
            with key:
                record = dict(registry_name=APP_KEY,hive=hive_name,view=bits)
                for name in ('InstallLocation','Inno Setup: Selected Components'):
                    try:
                        record[name] = winreg.QueryValueEx(key,name)[0]
                    except FileNotFoundError:
                        pass
                    except OSError:
                        unreadable = True
                entries.append(record)
    if unreadable:
        entries.append({'registry_unreadable':True})
    return entries


def installation(folder=None, *, entries=None):
    try:
        target = Path(folder if folder is not None else Path(sys.executable).parent).resolve()
    except Exception:
        _detection_issue('invalid installation path')
        try:
            target = Path(os.path.abspath(os.fspath(folder)))
        except Exception:
            target = Path(os.path.dirname(sys.executable))
    try:
        uninstaller = (target/'unins000.exe').is_file()
    except Exception:
        uninstaller = False
    try:
        if entries is None:
            if not getattr(sys,'frozen',False) or os.name!='nt':
                return Installation(target)
            entries = read_uninstall_entries()
        matches = []
        unreadable = False
        for record in entries:
            if not isinstance(record,dict):
                continue
            if record.get('registry_unreadable') is True:
                unreadable = True
                continue
            name = record.get('registry_name')
            if not isinstance(name,str) or name.casefold()!=APP_KEY.casefold():
                continue
            raw = record.get('InstallLocation')
            if not isinstance(raw,str) or not raw.strip():
                continue
            path = Path(raw)
            if not path.is_absolute():
                continue
            try:
                if path.resolve()!=target:
                    continue
            except Exception:
                _detection_issue('invalid registry installation path')
                unreadable = True
                continue
            if record.get('hive','HKCU') not in ('HKCU','HKLM'):
                continue
            matches.append(record)
        # An all-users registration always wins; it must never silently reinstall per-user.
        record = next((r for r in matches if r.get('hive')=='HKLM'),matches[0] if matches else None)
        if unreadable:
            _detection_issue('registry unreadable')
            if uninstaller:
                return Installation(target,kind='installed',hive='unknown')
        if record is not None:
            if not uninstaller:
                _detection_issue('Samsara registration without uninstaller; portable pending repair')
            components = record.get('Inno Setup: Selected Components','core')
            if not isinstance(components,str):
                components = 'core'
            return Installation(target,APP_KEY,components,'installed' if uninstaller else 'damaged',
                                record.get('hive','HKCU'),32 if record.get('view')==32 else 64)
        if uninstaller:
            _detection_issue('uninstaller without matching Samsara registration; portable copy')
        return Installation(target)
    except Exception:
        _detection_issue('registry unreadable')
        return Installation(target,kind='installed' if uninstaller else 'portable',hive='unknown')


def archive_guard(folder=None):
    info = installation(folder)
    return GUARD_MESSAGE if info.kind=='installed' else None


def routing_guard():
    info = installation()
    return GUARD_MESSAGE if info.kind=='installed' and info.hive!='HKCU' else None


def dialog_notice():
    info = installation()
    return GUARD_MESSAGE if info.kind=='installed' else REPAIR_MESSAGE if info.kind=='damaged' else None


def repair_notice():
    global _repair_reported
    if not _repair_reported and installation().kind=='damaged':
        _repair_reported = True
        return updater.UpdateStatus('failed',REPAIR_MESSAGE+' '+RELEASES_URL,'repair')
    return None

def asset_name(tag):
    return f'SamsaraSetup-{tag.removeprefix("v")}.exe' if installation().kind=='installed' else f'Samsara-Windows-{tag}.zip'


@dataclass(frozen=True)
class InstallerLinkOffer:
    version: str
    tag: str
    asset_size: int = 0
    installer_link_only: bool = True


def missing_installer_offer(tag):
    return InstallerLinkOffer(tag.removeprefix('v'),tag) if installation().kind=='installed' else None


@dataclass(frozen=True)
class PreparedInstaller(updater.PreparedUpdate):
    installer_path: Path | None = None
    installer_sha256: str = ''
    components: str = 'core'
    registry_view: int = 64


def installer_arguments(folder, components):
    allowed = {'core','wake_word_models','cuda_pack','command_model','gesture_control'}
    chosen = components.split(',')
    if not chosen or any(name not in allowed for name in chosen):
        raise updater.UpdateError('The existing installer component selection is invalid.')
    return ['/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CURRENTUSER',
            '/NOCLOSEAPPLICATIONS','/NORESTARTAPPLICATIONS','/NoFetch',
            '/DIR='+str(folder),'/TYPE=custom','/COMPONENTS='+','.join(chosen)]


def prepare(release, install_dir, progress_callback, *, opener,timeout,disk_usage,allow_prerelease):
    info = installation(install_dir)
    if info.kind!='installed' or info.hive!='HKCU' or info.registry_name is None or info.registry_name.casefold()!=APP_KEY.casefold():
        raise updater.UpdateNotSupported('This installer ownership cannot be verified. '+GUARD_MESSAGE)
    if allow_prerelease:
        updater._parse_version(release.tag)
    else:
        updater._parse_stable_version(release.tag)
    if release.version!=release.tag.removeprefix('v'):
        raise updater.ReleaseMetadataError('The update version and tag do not agree.')
    folder = info.folder
    name = f'SamsaraSetup-{release.version}.exe'
    updater._validate_release_asset_url(release.asset_url,release.tag,name)
    updater._validate_release_asset_url(release.checksum_url,release.tag,name+'.sha256')
    installer_arguments(folder,info.components)
    if getattr(folder.lstat(),'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise updater.UpdateError('Installer backup paths may not be reparse points.')
    files = []
    for parent,dirs,names in os.walk(folder,followlinks=False):
        for entry_name in [*dirs,*names]:
            path = Path(parent)/entry_name
            if getattr(path.lstat(),'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise updater.UpdateError('Installer backup paths may not be reparse points.')
            files.append(path)
    total = sum(path.stat().st_size for path in files if path.is_file())
    updater._require_disk_space(folder.parent,total+release.asset_size+updater.PRE_DOWNLOAD_MARGIN_BYTES,
                               disk_usage=disk_usage,phase='installer backup')
    suffix = release.tag+'-'+uuid.uuid4().hex[:10]
    workspace = Path(tempfile.mkdtemp(prefix=f'.{folder.name}-update-{suffix}-',dir=folder.parent)).resolve()
    updates = updater.samsara_home_dir()/'updates'
    prepared = PreparedInstaller(release.version,release.tag,folder,workspace/'payload',
        folder.parent/f'.{folder.name}-rollback-{suffix}',workspace,updates/'last_update.json',
        updates/f'install-{suffix}.ps1',installer_path=workspace/name,components=info.components,registry_view=info.view)
    try:
        checksum = updater._download_bytes(release.checksum_url,tag=release.tag,filename=name+'.sha256',
            opener=opener,timeout=timeout,limit=min(updater.MAX_CHECKSUM_BYTES,release.checksum_size))
        expected = updater._expected_checksum(checksum,name)
        download(release,prepared.installer_path,expected,opener,timeout,progress_callback)
        shutil.copytree(folder,prepared.staged_dir)
        from dataclasses import replace
        return replace(prepared,installer_sha256=expected)
    except Exception:
        shutil.rmtree(workspace)
        raise


def download(release,path,expected,opener,timeout,progress):
    import urllib.request
    name = path.name
    updater._validate_release_asset_url(release.asset_url,release.tag,name)
    if not 0<release.asset_size<=updater.MAX_DOWNLOAD_BYTES:
        raise updater.UpdateIntegrityError('The installer declared size is unsafe.')
    digest = hashlib.sha256()
    count = 0
    request = urllib.request.Request(release.asset_url,headers={'User-Agent':'Samsara updater'})
    with updater._open_response(opener,request,timeout) as response, path.open('xb') as stream:
        updater._validate_download_redirect(response.geturl(),release.asset_url)
        while chunk:=response.read(1024*1024):
            count += len(chunk)
            if count>release.asset_size or count>updater.MAX_DOWNLOAD_BYTES:
                raise updater.UpdateIntegrityError('The installer exceeds its declared size.')
            digest.update(chunk)
            stream.write(chunk)
            if progress:
                progress(count,release.asset_size)
    if count!=release.asset_size or digest.hexdigest()!=expected:
        raise updater.UpdateIntegrityError('The installer failed size or SHA-256 verification.')


def helper(prepared,current_pid):
    """Reuse confined paths/quit wait; installer transaction owns new metadata."""
    info = installation(prepared.install_dir)
    if info.kind!='installed' or info.hive!='HKCU':
        raise updater.UpdateNotSupported(GUARD_MESSAGE)
    path = prepared.installer_path.resolve()
    expected_path = prepared.workspace_dir/f'SamsaraSetup-{prepared.version}.exe'
    if path!=expected_path or path.is_symlink() or not path.is_file():
        raise updater.UpdateError('The prepared installer path is invalid.')
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream,'sha256').hexdigest()
    if actual!=prepared.installer_sha256:
        raise updater.UpdateIntegrityError('The prepared installer checksum changed.')
    base = updater._archive_helper_script(prepared,current_pid)
    prefix = base[:base.index('$oldMoved = $false')]
    literal = updater._ps_literal
    arguments = ' '.join('"'+arg.replace('"','""')+'"' for arg in installer_arguments(prepared.install_dir,prepared.components))
    registry_name = UNINSTALL_ROOT+'\\'+APP_KEY
    prefix += f"\n$installer = {literal(path)}\n$installerHash = {literal(prepared.installer_sha256)}\n$installerArgs = {literal(arguments)}\n$registryName = {literal(registry_name)}\n"
    prefix += '$registryView = [Microsoft.Win32.RegistryView]::'+('Registry32' if prepared.registry_view==32 else 'Registry64')+'\n'
    return prefix+INSTALLER_TRANSACTION


INSTALLER_TRANSACTION = r'''
function Read-InstallRegistry {
    $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser,$registryView)
    $key = $baseKey.OpenSubKey($registryName)
    if ($null -eq $key) { throw 'The installer registration disappeared.' }
    try {
        if (-not [string]::Equals([IO.Path]::GetFullPath([string]$key.GetValue('InstallLocation')), [IO.Path]::GetFullPath($install), [StringComparison]::OrdinalIgnoreCase)) { throw 'The installer registration belongs to another folder.' }
        $saved = @{}
        foreach ($name in $key.GetValueNames()) { $saved[$name] = @{ value=$key.GetValue($name); kind=$key.GetValueKind($name) } }
        return $saved
    } finally { $key.Close(); $baseKey.Close() }
}
function Restore-InstallRegistry($saved) {
    $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser,$registryView)
    $key = $baseKey.CreateSubKey($registryName)
    try {
        foreach ($name in $key.GetValueNames()) { if (-not $saved.ContainsKey($name)) { $key.DeleteValue($name) } }
        foreach ($name in $saved.Keys) { $key.SetValue($name, $saved[$name].value, $saved[$name].kind) }
    } finally { $key.Close(); $baseKey.Close() }
}
function Assert-InstalledMetadata {
    $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser,$registryView)
    $key = $baseKey.OpenSubKey($registryName)
    if ($null -eq $key) { throw 'The installer registration is missing.' }
    try {
        if ([string]$key.GetValue('DisplayVersion') -ne $tag.TrimStart('v')) { throw 'The installer registered the wrong version.' }
        if (-not [string]::Equals([IO.Path]::GetFullPath([string]$key.GetValue('InstallLocation')), [IO.Path]::GetFullPath($install), [StringComparison]::OrdinalIgnoreCase)) { throw 'The installer registered another folder.' }
    } finally { $key.Close(); $baseKey.Close() }
}
$installerProcess = $null
$newProcess = $null
$snapshot = $null
$backupReady = $false
try {
    Assert-NotReparsePoint $installer $installParent
    $stream = [IO.File]::OpenRead($installer)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $actualHash = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','') }
    finally { $sha.Dispose(); $stream.Dispose() }
    if ($actualHash -ine $installerHash) { throw 'The installer checksum changed.' }
    $snapshot = Read-InstallRegistry
    Assert-NotReparsePoint $staged $installParent
    Move-Item -LiteralPath $staged -Destination $rollback
    $backupReady = $true
    $installerProcess = Start-Process -FilePath $installer -ArgumentList $installerArgs -WindowStyle Hidden -PassThru
    if (-not $installerProcess.WaitForExit(300000)) { throw 'The installer did not finish within five minutes.' }
    if ($installerProcess.ExitCode -ne 0) { throw "The installer returned code $($installerProcess.ExitCode)." }
    if (-not (Test-Path -LiteralPath (Join-Path $install 'unins000.exe') -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $install 'unins000.dat') -PathType Leaf)) { throw 'The installer did not preserve its uninstaller.' }
    Assert-InstalledMetadata
    Write-UpdateStatus 'awaiting_confirmation' 'The installer completed; waiting for Samsara to start.'
    $newProcess = Start-Process -FilePath (Join-Path $install $executable) -WorkingDirectory $install -WindowStyle Hidden -PassThru
    $healthDeadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $healthDeadline) {
        $current = Get-Content -LiteralPath $status -Raw | ConvertFrom-Json
        if (($current.state -eq 'installed') -or ($current.state -eq 'reported')) {
            try {
                Assert-NotReparsePoint $rollback $installParent
                Remove-Item -LiteralPath $rollback -Recurse -Force
                Assert-NotReparsePoint $workspace $installParent
                Remove-Item -LiteralPath $workspace -Recurse -Force
            } catch { Write-UpdateStatus 'cleanup_pending' 'The installer update succeeded; old backup cleanup will retry.' }
            exit 0
        }
        if ($newProcess.HasExited) { throw 'Samsara closed before confirming startup.' }
        Start-Sleep -Milliseconds 250
        $newProcess.Refresh()
    }
    throw 'Samsara did not confirm startup.'
} catch {
    $problem = $_.Exception.Message
    if (($null -ne $installerProcess) -and (-not $installerProcess.HasExited)) {
        Write-UpdateStatus 'failed' "Installer still running; the previous folder backup is retained. $problem"
        Show-UpdateFailure 'The installer has not finished. The previous version backup has been kept; nothing was closed by force.'
        exit 3
    }
    if (($null -ne $newProcess) -and (-not $newProcess.HasExited)) {
        $null = $newProcess.CloseMainWindow()
        if (-not $newProcess.WaitForExit(10000)) {
            Write-UpdateStatus 'failed' "Samsara did not close; the previous folder backup is retained. $problem"
            Show-UpdateFailure 'The update could not be verified. Close Samsara normally before restoring the retained previous-version backup.'
            exit 3
        }
    }
    try {
        if ($backupReady) {
            if (-not (Test-Path -LiteralPath $rollback -PathType Container)) { throw 'The previous version backup is missing.' }
            Assert-NotReparsePoint $rollback $installParent
            Assert-NotReparsePoint $install $installParent
            if (Test-Path -LiteralPath $install) { Remove-Item -LiteralPath $install -Recurse -Force }
            Move-Item -LiteralPath $rollback -Destination $install
            Restore-InstallRegistry $snapshot
        }
        Write-UpdateStatus 'rolled_back' "The installer update failed; the previous installation was restored. $problem"
        Start-Process -FilePath (Join-Path $install $executable) -WorkingDirectory $install -WindowStyle Hidden
        Show-UpdateFailure 'The update failed. The previous Samsara installation has been restored. You can use the installer to try again.'
        exit 2
    } catch {
        Write-UpdateStatus 'failed' "The update and restoration failed: $problem / $($_.Exception.Message)"
        Show-UpdateFailure 'The update could not be restored automatically. The retained files must be checked before trying the installer again.'
        exit 3
    }
}
'''
