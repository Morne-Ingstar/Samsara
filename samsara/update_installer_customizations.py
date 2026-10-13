"""Installer maintenance and bounded recovery before normal app startup (560).

Reuse the ZIP migration and its backup-before-write checks. Successful installer
updates retain the newest five valid customization archives in updates/backups,
including the current update's archive; failures never prune their backups.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import stat
import sys
import time
import uuid
from datetime import datetime, timezone

from samsara.update_customizations import (
    CustomizationMigrationError, DEFAULT_MAX_BACKUP_BYTES, MAX_COMMANDS_BYTES,
    _assert_safe_existing_path, _checked_lstat, _enumerate_plugin_files,
    _copy_verified_file, _load_commands, _packaged_data_root, _read_verified_file,
    _replace_file_atomically, _write_new_file, migrate_update_customizations,
)

MAINTENANCE_ARGUMENT = "--migrate-update-customizations"
BACKUPS_TO_KEEP = 5
LEGACY_HELPER_GRACE_SECONDS = 200
logger = logging.getLogger("Samsara")
_invalid_logged = False
# STAND-IN COPY (560)
RECOVERY_NOTICE = (
    "Samsara could not restore your edited commands after the update. "
)


def _safe_absolute(path: Path, root: Path | None = None) -> Path:
    if not path.is_absolute():
        raise CustomizationMigrationError("Maintenance paths must be absolute.")
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root)) if root is not None else path
    if os.path.lexists(root):
        _checked_lstat(root)
    _assert_safe_existing_path(path, root)
    return path


def validate_installer_customizations(install: Path) -> None:
    """Check old data before any installer download, using the ZIP validators."""
    _checked_lstat(install)
    data = _packaged_data_root(install)
    commands = data / "commands.json"
    size = 0
    if os.path.lexists(commands):
        contents, _info = _read_verified_file(commands, MAX_COMMANDS_BYTES)
        _load_commands(contents, commands)
        size = len(contents)
    plugins = data / "plugins" / "commands"
    _assert_safe_existing_path(plugins, install)
    files = _enumerate_plugin_files(plugins)
    size += sum(item.stat_result.st_size for item in files)
    if size > DEFAULT_MAX_BACKUP_BYTES:
        raise CustomizationMigrationError(
            f"Customization backup would use {size:,} bytes, exceeding "
            f"the {DEFAULT_MAX_BACKUP_BYTES:,}-byte safety limit."
        )


def _prune_backups(backups: Path, current: Path) -> None:
    """Prune only confined, complete archives; keep current even if it is older."""
    try:
        archives = []
        for archive in backups.glob("customizations-*"):
            try:
                info = _checked_lstat(archive)
                if not stat.S_ISDIR(info.st_mode):
                    continue
                manifest = _read_object(archive / "manifest.json", archive)
                if manifest.get("schema_version") != 1:
                    continue
                archives.append((info.st_mtime_ns, archive.name, archive))
            except Exception:
                continue
        newest = [item[2] for item in sorted(archives, reverse=True) if item[2] != current]
        for archive in newest[BACKUPS_TO_KEEP - 1:]:
            _checked_lstat(backups)
            _checked_lstat(archive)
            for parent, dirs, names in os.walk(archive, followlinks=False):
                for name in [*dirs, *names]:
                    _checked_lstat(Path(parent) / name)
            if archive.parent != backups or archive == current:
                continue
            shutil.rmtree(archive)
    except Exception:
        logger.warning("[UPDATE] Customization backup cleanup deferred")


def migrate_installer_customizations(snapshot, install, backups, receipt):
    """Run the shared migration on confined roots and record completion."""
    snapshot, install, backups, receipt = map(Path, (snapshot, install, backups, receipt))
    workspace = receipt.parent
    if (
        not all(path.is_absolute() for path in (snapshot, install, backups, receipt))
        or snapshot.parent != install.parent
        or not snapshot.name.startswith(f".{install.name}-rollback-")
        or workspace.parent != install.parent
        or not workspace.name.startswith(f".{install.name}-update-")
        or receipt.name != "customizations-migrated.json"
        or os.path.lexists(receipt)
    ):
        raise CustomizationMigrationError("Invalid installer migration paths.")
    for root in (snapshot, install, workspace):
        if not stat.S_ISDIR(_checked_lstat(root).st_mode):
            raise CustomizationMigrationError("A migration root is not a directory.")
        if backups == root or backups.is_relative_to(root) or root.is_relative_to(backups):
            raise CustomizationMigrationError("Customization backups must be outside update trees.")
    receipt = _safe_absolute(receipt, workspace)
    backups = _safe_absolute(backups, backups.parent.parent)
    summary = migrate_update_customizations(snapshot, install, backups)
    payload = {
        "schema_version": 1, "snapshot_dir": str(snapshot),
        "install_dir": str(install), "backup_dir": str(summary.backup_dir),
    }
    _write_new_file(receipt, (json.dumps(payload) + "\n").encode("utf-8"))
    _prune_backups(backups, summary.backup_dir)
    return summary


def maintenance_main(argv) -> int:
    """Return before normal app boot, audio, windows or the instance lock."""
    try:
        if len(argv) != 4 or argv[0] != MAINTENANCE_ARGUMENT:
            return 2
        from samsara.paths import samsara_home_dir
        backups = _safe_absolute(Path(argv[2]), samsara_home_dir())
        if backups != Path(os.path.abspath(samsara_home_dir() / "updates" / "backups")):
            return 2
        migrate_installer_customizations(argv[1], Path(sys.executable).parent, backups, argv[3])
        return 0
    except Exception:
        return 2


def _read_object(path: Path, root: Path) -> dict:
    _safe_absolute(path, root)
    data, _info = _read_verified_file(path, 64 * 1024)
    payload = json.loads(data.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("Update metadata must be a JSON object")
    return payload


def _ignore_invalid_update() -> bool:
    global _invalid_logged
    if not _invalid_logged:
        _invalid_logged = True
        logger.warning("[UPDATE] Invalid or unreadable update metadata ignored at startup")
    return False


def _process_creation_filetime(pid: int) -> int | None:
    """Read identity/liveness only: no process signalling, including PID reuse."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if kernel.WaitForSingleObject(handle, 0) != 258:
            return None
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def _helper_owns_update(payload: dict) -> bool:
    try:
        if "helper_pid" not in payload and "helper_started_filetime" not in payload:
            # beta.4's helper has no identity fields, but writes an ISO UTC time
            # before its 180-second confirmation wait. Give that owner 200 s.
            if payload.get("state") != "awaiting_confirmation":
                return False
            stamp = datetime.fromisoformat(payload["updated_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return False
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            return 0 <= age <= LEGACY_HELPER_GRACE_SECONDS
        pid = payload.get("helper_pid")
        started = payload.get("helper_started_filetime")
        if type(pid) is not int or pid <= 0 or not isinstance(started, str) or not started.isdecimal():
            return False
        return _process_creation_filetime(pid) == int(started)
    except Exception:
        return False


def _archive_raw_customizations(snapshot: Path, backups: Path) -> Path:
    """Archive even malformed/oversized JSON, without parsing or staged writes.

    Commands plus the entire plugins tree share the 256 MiB archive cap and
    10,000-file plugin cap. Verified copies reject links/reparse/special files.
    The manifest is written last, so incomplete archives are never pruned.
    """
    _checked_lstat(snapshot)
    data = _packaged_data_root(snapshot)
    commands = data / "commands.json"
    command_info = None
    if os.path.lexists(commands):
        _assert_safe_existing_path(commands, snapshot)
        command_info = _checked_lstat(commands)
        if not stat.S_ISREG(command_info.st_mode):
            raise CustomizationMigrationError("Recovery commands are not a regular file.")
    plugins = data / "plugins"
    _assert_safe_existing_path(plugins, snapshot)
    files = _enumerate_plugin_files(plugins)
    manifest = (json.dumps({
        "schema_version": 1, "raw_recovery": True,
        "commands_backed_up": command_info is not None,
        "plugin_files": [item.relative.as_posix() for item in files],
    }) + "\n").encode("utf-8")
    size = len(manifest) + (command_info.st_size if command_info else 0)
    size += sum(item.stat_result.st_size for item in files)
    if size > DEFAULT_MAX_BACKUP_BYTES:
        raise CustomizationMigrationError("Raw customization archive exceeds the safety limit.")
    _safe_absolute(backups, backups.parent.parent)
    backups.mkdir(parents=True, exist_ok=True)
    archive = backups / ("customizations-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                         + "-" + uuid.uuid4().hex[:10])
    archive.mkdir()
    remaining = DEFAULT_MAX_BACKUP_BYTES - len(manifest)
    if command_info is not None:
        remaining -= _copy_verified_file(commands, archive / "commands.json",
                                         command_info, byte_limit=remaining)
    for item in files:
        _assert_safe_existing_path(item.source, snapshot)
        remaining -= _copy_verified_file(item.source, archive / "plugins" / item.relative,
                                         item.stat_result, byte_limit=remaining)
    _write_new_file(archive / "manifest.json", manifest)
    return archive


def _show_recovery_notice(message: str) -> bool:
    """Native early-boot notice; independent of either listening display or Qt."""
    try:
        import ctypes
        return bool(ctypes.windll.user32.MessageBoxW(None, message, "Samsara Update", 0x40))
    except Exception:
        logger.warning("[UPDATE] Could not show customization recovery notice")
        return False


def migrate_pending_installer_update() -> bool:
    """Bridge older helpers; orphan failures record a terminal status and boot normally.

    Invalid status/receipt files mean no pending update. Only a verified live
    helper (PID plus creation time, or a fresh legacy status) may require startup
    to exit for rollback. An old legacy status cannot block later starts.
    """
    try:
        from samsara.paths import samsara_home_dir
        updates = samsara_home_dir() / "updates"
        status = updates / "last_update.json"
        if not os.path.lexists(status):
            return False
        payload = _read_object(status, updates)
        if not isinstance(payload.get("state"), str):
            return _ignore_invalid_update()
        if payload.get("state") != "awaiting_confirmation":
            return False
        # Establish applicability first. Mounted ancestors of portable/foreign
        # copies must not turn their updates into installer failures.
        install = Path(os.path.abspath(Path(sys.executable).parent))
        recorded_install = payload.get("install_dir")
        if not isinstance(recorded_install, str) or not Path(recorded_install).is_absolute():
            return _ignore_invalid_update()
        if recorded_install != str(install):
            return False
        snapshot = Path(payload["rollback_dir"])
        workspace = Path(payload["workspace_dir"])
        if not all(path.is_absolute() for path in (snapshot, workspace)):
            return _ignore_invalid_update()
        if not all(os.path.lexists(snapshot / name) for name in ("unins000.exe", "unins000.dat")):
            return False
        if (
            snapshot.parent != install.parent
            or not snapshot.name.startswith(f".{install.name}-rollback-")
            or workspace.parent != install.parent
            or not workspace.name.startswith(f".{install.name}-update-")
        ):
            return _ignore_invalid_update()
        for root in (install, snapshot, workspace):
            if not stat.S_ISDIR(_checked_lstat(root).st_mode):
                return _ignore_invalid_update()
        for name in ("unins000.exe", "unins000.dat"):
            if not stat.S_ISREG(_checked_lstat(snapshot / name).st_mode):
                return _ignore_invalid_update()
        backups = _safe_absolute(updates / "backups", updates.parent)
        receipt = workspace / "customizations-migrated.json"
        if os.path.lexists(receipt):
            result = _read_object(receipt, workspace)
            backup = Path(result["backup_dir"])
            if (
                type(result.get("schema_version")) is not int
                or result.get("schema_version") != 1 or backup.parent != backups
                or result.get("snapshot_dir") != str(snapshot)
                or result.get("install_dir") != str(install)
            ):
                return _ignore_invalid_update()
            manifest = _read_object(backup / "manifest.json", backups)
            if manifest.get("schema_version") != 1:
                return _ignore_invalid_update()
            return False
    except Exception:
        return _ignore_invalid_update()

    packaged_path = None
    packaged_bytes = None
    try:
        packaged_path = _packaged_data_root(install) / "commands.json"
        packaged_bytes, _info = _read_verified_file(packaged_path, MAX_COMMANDS_BYTES)
        migrate_installer_customizations(snapshot, install, backups, receipt)
        return True
    except Exception as exc:
        if _helper_owns_update(payload):
            raise CustomizationMigrationError("Pending installer customization migration failed") from exc
        # Receipt I/O can fail after the merge. Preserve packaged commands and
        # retain the snapshot and every complete or partial archive on failure.
        if packaged_bytes is not None:
            try:
                _assert_safe_existing_path(packaged_path, install)
                _replace_file_atomically(packaged_path, packaged_bytes)
            except Exception:
                logger.warning("[UPDATE] Packaged command restoration deferred")
        archive = None
        try:
            # BEFORE any terminal status: a legacy helper deletes rollback on
            # "reported", even when commands could not be parsed by migration.
            archive = _archive_raw_customizations(snapshot, backups)
        except Exception:
            logger.warning("[UPDATE] Raw customization archive could not be saved")
        # STAND-IN COPY (560). Keep rollback/workspace for full recovery, rather
        # than deleting the only old executable or any non-customization data.
        message = RECOVERY_NOTICE + (
            f"Your command files and plugins were saved to {archive}. " if archive else
            "No customization archive could be made. "
        ) + f"The previous installation remains at {snapshot}."
        # An unarchived failure is NON-terminal and never a legacy success
        # token; both boot and the normal tray leave it pending without retry.
        payload.update(state="failed" if archive else "customization_recovery_pending", message=message,
                       customization_failure="orphaned_helper", customization_retry=False)
        if archive:
            payload["customization_backup_dir"] = str(archive)
        try:
            _safe_absolute(status, updates)
            _replace_file_atomically(status, (json.dumps(payload) + "\n").encode("utf-8"))
        except Exception:
            logger.warning("[UPDATE] Customization failure status could not be saved")
        if _show_recovery_notice(message) and archive:
            # The ordinary tray reconcile consumes "reported" without another
            # balloon; the failure reason stays recorded until that consumption.
            payload.update(state="reported", customization_state="failed",
                           customization_notice_shown=True)
            try:
                _safe_absolute(status, updates)
                _replace_file_atomically(status, (json.dumps(payload) + "\n").encode("utf-8"))
            except Exception:
                logger.warning("[UPDATE] Customization notice acknowledgement could not be saved")
        return False
