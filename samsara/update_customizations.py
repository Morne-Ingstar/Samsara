"""Preserve installation-local customizations while staging an update.

The updater replaces the packaged application directory.  Older Samsara
builds also stored editable ``commands.json`` and user-supplied command
plugins in that directory, so those files need an explicit migration before
the directory swap.

This module has no network, process, Qt, or import-time filesystem activity.
The caller is responsible for validating the install, staging, and backup
roots before calling :func:`migrate_update_customizations`.
"""
from __future__ import annotations

import json
import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


DEFAULT_MAX_BACKUP_BYTES = 256 * 1024 * 1024
MAX_COMMANDS_BYTES = 16 * 1024 * 1024
MAX_MERGED_COMMANDS_BYTES = 32 * 1024 * 1024
MAX_PLUGIN_FILES = 10_000
_COPY_CHUNK_BYTES = 1024 * 1024
_REPARSE_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


class CustomizationMigrationError(RuntimeError):
    """A customization could not be backed up or migrated safely."""


class UnsafeCustomizationError(CustomizationMigrationError):
    """A customization tree contains a link, reparse point, or special file."""


@dataclass(frozen=True)
class CustomizationMigrationSummary:
    """Auditable result of one customization migration."""

    backup_dir: Path
    packaged_command_count: int
    installed_command_count: int
    merged_command_count: int
    overridden_command_count: int
    packaged_only_command_count: int
    plugins_copied: tuple[str, ...]
    packaged_plugins_kept: tuple[str, ...]
    backed_up_file_count: int
    backup_bytes: int


@dataclass(frozen=True)
class _PluginFile:
    source: Path
    relative: Path
    stat_result: os.stat_result


def _is_link_or_reparse(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & _REPARSE_ATTRIBUTE)


def _checked_lstat(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CustomizationMigrationError(f"Could not inspect {path}: {exc}") from exc
    if _is_link_or_reparse(info):
        raise UnsafeCustomizationError(
            f"Refusing to migrate link or reparse point: {path}"
        )
    return info


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare identities where the platform supplies useful inode values."""
    if left.st_ino and right.st_ino:
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    return True


def _open_verified_source(
    path: Path,
    expected: os.stat_result,
) -> tuple[BinaryIO, os.stat_result]:
    current = _checked_lstat(path)
    if not stat.S_ISREG(current.st_mode):
        raise UnsafeCustomizationError(f"Refusing to copy non-regular file: {path}")
    if not _same_file_identity(current, expected) or current.st_size != expected.st_size:
        raise CustomizationMigrationError(f"Customization changed during migration: {path}")
    try:
        source = path.open("rb")
    except OSError as exc:
        raise CustomizationMigrationError(f"Could not open {path}: {exc}") from exc
    try:
        opened = os.fstat(source.fileno())
        if (
            _is_link_or_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or not _same_file_identity(current, opened)
            or opened.st_size != expected.st_size
        ):
            raise UnsafeCustomizationError(
                f"Customization was replaced while being opened: {path}"
            )
    except Exception:
        source.close()
        raise
    return source, opened


def _read_verified_file(path: Path, limit: int) -> tuple[bytes, os.stat_result]:
    info = _checked_lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeCustomizationError(f"Refusing to read non-regular file: {path}")
    if info.st_size > limit:
        raise CustomizationMigrationError(
            f"Customization file exceeds the {limit:,}-byte safety limit: {path}"
        )
    source, _opened = _open_verified_source(path, info)
    try:
        data = source.read(limit + 1)
    except OSError as exc:
        raise CustomizationMigrationError(f"Could not read {path}: {exc}") from exc
    finally:
        source.close()
    if len(data) > limit or len(data) != info.st_size:
        raise CustomizationMigrationError(f"Customization changed while reading: {path}")
    return data, info


def _copy_verified_file(
    source_path: Path,
    destination: Path,
    expected: os.stat_result,
    *,
    byte_limit: int,
) -> int:
    source, _opened = _open_verified_source(source_path, expected)
    copied = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as output:
            while True:
                chunk = source.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > byte_limit or copied > expected.st_size:
                    raise CustomizationMigrationError(
                        f"Customization grew while being copied: {source_path}"
                    )
                output.write(chunk)
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    finally:
        source.close()
    if copied != expected.st_size:
        try:
            destination.unlink()
        except OSError:
            pass
        raise CustomizationMigrationError(
            f"Customization changed while being copied: {source_path}"
        )
    return copied


def _load_commands(data: bytes, path: Path) -> tuple[dict, dict]:
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustomizationMigrationError(f"Invalid commands file {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("commands"), dict):
        raise CustomizationMigrationError(
            f"Commands file must contain an object named 'commands': {path}"
        )
    commands = payload["commands"]
    for phrase, definition in commands.items():
        if not isinstance(phrase, str) or not isinstance(definition, dict):
            raise CustomizationMigrationError(
                f"Commands file contains an invalid command entry: {path}"
            )
    return payload, commands


def _enumerate_plugin_files(root: Path) -> list[_PluginFile]:
    if not os.path.lexists(root):
        return []
    root_info = _checked_lstat(root)
    if not stat.S_ISDIR(root_info.st_mode):
        raise UnsafeCustomizationError(f"Plugin path is not a directory: {root}")

    files: list[_PluginFile] = []
    casefolded: set[str] = set()

    def walk(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise CustomizationMigrationError(
                f"Could not enumerate plugin directory {directory}: {exc}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            info = _checked_lstat(path)
            relative = path.relative_to(root)
            key = relative.as_posix().casefold()
            if key in casefolded:
                raise UnsafeCustomizationError(
                    f"Plugin tree contains case-colliding paths: {relative.as_posix()}"
                )
            casefolded.add(key)
            if stat.S_ISDIR(info.st_mode):
                walk(path)
            elif stat.S_ISREG(info.st_mode):
                files.append(_PluginFile(path, relative, info))
                if len(files) > MAX_PLUGIN_FILES:
                    raise CustomizationMigrationError(
                        f"Plugin tree exceeds the {MAX_PLUGIN_FILES:,}-file safety limit."
                    )
            else:
                raise UnsafeCustomizationError(
                    f"Refusing to migrate special plugin file: {path}"
                )

    walk(root)
    return files


def _assert_safe_existing_path(path: Path, root: Path) -> None:
    """Reject links/reparse points on an existing path below ``root``."""
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(current):
            _checked_lstat(current)


def _write_new_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as output:
            output.write(data)
    except OSError as exc:
        raise CustomizationMigrationError(f"Could not create {path}: {exc}") from exc


def _replace_file_atomically(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.migration-{uuid.uuid4().hex}.tmp")
    try:
        _write_new_file(temporary, data)
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def migrate_update_customizations(
    install_dir: str | Path,
    staged_dir: str | Path,
    backup_root: str | Path,
    *,
    max_backup_bytes: int = DEFAULT_MAX_BACKUP_BYTES,
) -> CustomizationMigrationSummary:
    """Back up and migrate install-local commands/plugins into a staged build.

    Packaged commands form the base and installed command entries override
    matching phrases.  Packaged plugin paths always win; old plugin files are
    copied into the staged tree only when that relative path is absent.

    A unique persistent backup is created below ``backup_root`` before the
    staged payload is changed.  Any unsafe path, malformed command file,
    exceeded limit, or I/O failure raises :class:`CustomizationMigrationError`.
    """
    install = Path(install_dir)
    staged = Path(staged_dir)
    backups = Path(backup_root)
    if max_backup_bytes <= 0:
        raise ValueError("max_backup_bytes must be positive")

    installed_commands_path = install / "commands.json"
    staged_commands_path = staged / "commands.json"
    if not os.path.lexists(staged_commands_path):
        raise CustomizationMigrationError(
            "The staged update is missing its packaged commands.json."
        )
    _assert_safe_existing_path(staged_commands_path, staged)
    staged_bytes, _staged_info = _read_verified_file(
        staged_commands_path, MAX_COMMANDS_BYTES
    )
    staged_payload, packaged_commands = _load_commands(
        staged_bytes, staged_commands_path
    )

    installed_bytes: bytes | None = None
    installed_commands: dict = {}
    if os.path.lexists(installed_commands_path):
        _assert_safe_existing_path(installed_commands_path, install)
        installed_bytes, _installed_info = _read_verified_file(
            installed_commands_path, MAX_COMMANDS_BYTES
        )
        _old_payload, installed_commands = _load_commands(
            installed_bytes, installed_commands_path
        )

    old_plugins_root = install / "plugins" / "commands"
    staged_plugins_root = staged / "plugins" / "commands"
    plugin_files = _enumerate_plugin_files(old_plugins_root)
    if os.path.lexists(staged_plugins_root):
        _assert_safe_existing_path(staged_plugins_root, staged)
        staged_plugins_info = _checked_lstat(staged_plugins_root)
        if not stat.S_ISDIR(staged_plugins_info.st_mode):
            raise UnsafeCustomizationError(
                f"Staged plugin path is not a directory: {staged_plugins_root}"
            )

    plugins_copied: list[str] = []
    packaged_plugins_kept: list[str] = []
    for plugin in plugin_files:
        destination = staged_plugins_root / plugin.relative
        _assert_safe_existing_path(destination, staged)
        if os.path.lexists(destination):
            packaged_plugins_kept.append(plugin.relative.as_posix())
        else:
            plugins_copied.append(plugin.relative.as_posix())

    merged_commands = dict(packaged_commands)
    merged_commands.update(installed_commands)
    merged_payload = dict(staged_payload)
    merged_payload["commands"] = merged_commands
    merged_bytes = (
        json.dumps(merged_payload, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if len(merged_bytes) > MAX_MERGED_COMMANDS_BYTES:
        raise CustomizationMigrationError(
            "The merged commands.json exceeds the safe output size limit."
        )

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_dir = backups / f"customizations-{timestamp}-{uuid.uuid4().hex[:10]}"
    manifest = {
        "schema_version": 1,
        "created_at": timestamp,
        "commands_backed_up": installed_bytes is not None,
        "plugin_files": [plugin.relative.as_posix() for plugin in plugin_files],
        "plugins_copied": plugins_copied,
        "packaged_plugins_kept": packaged_plugins_kept,
    }
    manifest_bytes = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    planned_backup_bytes = (
        len(manifest_bytes)
        + (len(installed_bytes) if installed_bytes is not None else 0)
        + sum(plugin.stat_result.st_size for plugin in plugin_files)
    )
    if planned_backup_bytes > max_backup_bytes:
        raise CustomizationMigrationError(
            f"Customization backup would use {planned_backup_bytes:,} bytes, exceeding "
            f"the {max_backup_bytes:,}-byte safety limit."
        )

    try:
        backups.mkdir(parents=True, exist_ok=True)
        backup_dir.mkdir(exist_ok=False)
        _write_new_file(backup_dir / "manifest.json", manifest_bytes)
        backed_up_files = 1
        backup_bytes = len(manifest_bytes)
        if installed_bytes is not None:
            _write_new_file(backup_dir / "commands.json", installed_bytes)
            backed_up_files += 1
            backup_bytes += len(installed_bytes)
        for plugin in plugin_files:
            backup_bytes += _copy_verified_file(
                plugin.source,
                backup_dir / "plugins" / "commands" / plugin.relative,
                plugin.stat_result,
                byte_limit=max_backup_bytes - backup_bytes,
            )
            backed_up_files += 1

        # The backup is complete before any staged file is changed.
        for plugin in plugin_files:
            relative_text = plugin.relative.as_posix()
            if relative_text not in plugins_copied:
                continue
            destination = staged_plugins_root / plugin.relative
            _assert_safe_existing_path(destination.parent, staged)
            _copy_verified_file(
                plugin.source,
                destination,
                plugin.stat_result,
                byte_limit=plugin.stat_result.st_size,
            )
        if installed_bytes is not None:
            _replace_file_atomically(staged_commands_path, merged_bytes)
    except CustomizationMigrationError:
        raise
    except (OSError, ValueError) as exc:
        raise CustomizationMigrationError(
            f"Could not migrate update customizations; backup path: {backup_dir}: {exc}"
        ) from exc

    overridden = len(set(packaged_commands).intersection(installed_commands))
    summary = CustomizationMigrationSummary(
        backup_dir=backup_dir,
        packaged_command_count=len(packaged_commands),
        installed_command_count=len(installed_commands),
        merged_command_count=len(merged_commands),
        overridden_command_count=overridden,
        packaged_only_command_count=len(set(packaged_commands) - set(installed_commands)),
        plugins_copied=tuple(plugins_copied),
        packaged_plugins_kept=tuple(packaged_plugins_kept),
        backed_up_file_count=backed_up_files,
        backup_bytes=backup_bytes,
    )
    return summary


__all__ = [
    "CustomizationMigrationError",
    "CustomizationMigrationSummary",
    "DEFAULT_MAX_BACKUP_BYTES",
    "UnsafeCustomizationError",
    "migrate_update_customizations",
]
