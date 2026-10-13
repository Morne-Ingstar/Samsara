"""Installer ownership detection and installed-copy update policy (554)."""
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sys
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
