"""Resolve account-relative launch targets without starting a process."""
from __future__ import annotations
import ntpath
import os
import shutil


def _app_path(name: str) -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}",
                                    0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                value = os.path.expandvars(value).strip('"')
                if os.path.isfile(value):
                    return value
            except OSError:
                continue
    return None


def resolve_target(target: str) -> str | None:
    """Keep legacy targets; preflight portable %ENV% targets, then App Paths/PATH."""
    if '%' not in target:
        return target
    expanded = os.path.expandvars(target)
    if os.path.isfile(expanded):
        return expanded
    name = ntpath.basename(target)
    # Discord's Start Menu shortcut can fall back to its registered executable.
    if name.lower().endswith('.lnk'):
        name = name[:-4] + '.exe'
    return (_app_path(name) or shutil.which(name)
            or shutil.which(ntpath.splitext(name)[0]))
