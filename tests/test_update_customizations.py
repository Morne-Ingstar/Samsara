from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara import update_customizations as customizations


def _write_commands(path: Path, commands: dict, **extra) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**extra, "commands": commands}
    data = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    path.write_bytes(data)
    return data


def _roots(tmp_path: Path):
    install = tmp_path / "Samsara-old"
    staged = tmp_path / "staging" / "payload"
    backups = tmp_path / "profile" / "updates" / "backups"
    install.mkdir()
    staged.mkdir(parents=True)
    return install, staged, backups


def test_merge_preserves_new_commands_and_old_overrides_with_persistent_backup(
    tmp_path,
):
    install, staged, backups = _roots(tmp_path)
    old_bytes = _write_commands(
        install / "commands.json",
        {
            "shared": {"type": "text", "text": "user override"},
            "user only": {"type": "hotkey", "keys": "ctrl+alt+u"},
        },
        old_metadata="not migrated",
    )
    _write_commands(
        staged / "commands.json",
        {
            "shared": {"type": "text", "text": "new packaged value"},
            "new only": {"type": "hotkey", "keys": "ctrl+alt+n"},
        },
        packaged_metadata="retained",
    )

    old_plugins = install / "plugins" / "commands"
    new_plugins = staged / "plugins" / "commands"
    (old_plugins / "nested").mkdir(parents=True)
    new_plugins.mkdir(parents=True)
    (old_plugins / "custom.py").write_text("CUSTOM = True\n", encoding="utf-8")
    (old_plugins / "nested" / "extra.txt").write_text("extra", encoding="utf-8")
    (old_plugins / "builtin.py").write_text("OLD = True\n", encoding="utf-8")
    (new_plugins / "builtin.py").write_text("NEW = True\n", encoding="utf-8")

    summary = customizations.migrate_update_customizations(
        install, staged, backups
    )

    merged = json.loads((staged / "commands.json").read_text(encoding="utf-8"))
    assert merged["packaged_metadata"] == "retained"
    assert "old_metadata" not in merged
    assert merged["commands"] == {
        "shared": {"type": "text", "text": "user override"},
        "new only": {"type": "hotkey", "keys": "ctrl+alt+n"},
        "user only": {"type": "hotkey", "keys": "ctrl+alt+u"},
    }
    assert (new_plugins / "custom.py").read_text() == "CUSTOM = True\n"
    assert (new_plugins / "nested" / "extra.txt").read_text() == "extra"
    assert (new_plugins / "builtin.py").read_text() == "NEW = True\n"

    assert summary.packaged_command_count == 2
    assert summary.installed_command_count == 2
    assert summary.merged_command_count == 3
    assert summary.overridden_command_count == 1
    assert summary.packaged_only_command_count == 1
    assert summary.plugins_copied == ("custom.py", "nested/extra.txt")
    assert summary.packaged_plugins_kept == ("builtin.py",)
    assert summary.backed_up_file_count == 5
    assert summary.backup_bytes <= customizations.DEFAULT_MAX_BACKUP_BYTES
    assert summary.backup_dir.parent == backups
    assert (summary.backup_dir / "commands.json").read_bytes() == old_bytes
    assert (
        summary.backup_dir / "plugins" / "commands" / "builtin.py"
    ).read_text() == "OLD = True\n"
    manifest = json.loads((summary.backup_dir / "manifest.json").read_text())
    assert manifest["commands_backed_up"] is True
    assert manifest["packaged_plugins_kept"] == ["builtin.py"]


def test_missing_installed_customizations_leave_packaged_commands_unchanged(tmp_path):
    install, staged, backups = _roots(tmp_path)
    packaged = _write_commands(
        staged / "commands.json",
        {"new command": {"type": "hotkey", "keys": "ctrl+n"}},
    )

    summary = customizations.migrate_update_customizations(
        install, staged, backups
    )

    assert (staged / "commands.json").read_bytes() == packaged
    assert summary.installed_command_count == 0
    assert summary.plugins_copied == ()
    assert summary.backed_up_file_count == 1
    assert (summary.backup_dir / "manifest.json").is_file()


def test_backup_limit_fails_before_creating_backup_or_changing_stage(tmp_path):
    install, staged, backups = _roots(tmp_path)
    _write_commands(
        install / "commands.json",
        {"custom": {"type": "text", "text": "x" * 200}},
    )
    staged_bytes = _write_commands(
        staged / "commands.json",
        {"packaged": {"type": "text", "text": "keep"}},
    )

    with pytest.raises(
        customizations.CustomizationMigrationError,
        match="backup would use.*exceeding",
    ):
        customizations.migrate_update_customizations(
            install, staged, backups, max_backup_bytes=10
        )

    assert (staged / "commands.json").read_bytes() == staged_bytes
    assert not backups.exists()


def test_malformed_installed_commands_fail_explicitly_without_touching_stage(tmp_path):
    install, staged, backups = _roots(tmp_path)
    (install / "commands.json").write_text("{not json", encoding="utf-8")
    staged_bytes = _write_commands(
        staged / "commands.json",
        {"packaged": {"type": "text", "text": "keep"}},
    )

    with pytest.raises(
        customizations.CustomizationMigrationError,
        match="Invalid commands file",
    ):
        customizations.migrate_update_customizations(install, staged, backups)

    assert (staged / "commands.json").read_bytes() == staged_bytes
    assert not backups.exists()


def test_reparse_attribute_is_classified_as_unsafe():
    info = SimpleNamespace(
        st_mode=stat.S_IFREG,
        st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
    )
    assert customizations._is_link_or_reparse(info) is True


def test_plugin_symlink_is_rejected_when_platform_allows_creation(tmp_path):
    install, staged, backups = _roots(tmp_path)
    _write_commands(staged / "commands.json", {})
    plugins = install / "plugins" / "commands"
    plugins.mkdir(parents=True)
    target = tmp_path / "outside.py"
    target.write_text("OUTSIDE = True\n", encoding="utf-8")
    link = plugins / "linked.py"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"This Windows configuration cannot create symlinks: {exc}")

    with pytest.raises(
        customizations.UnsafeCustomizationError,
        match="link or reparse point",
    ):
        customizations.migrate_update_customizations(install, staged, backups)

    assert not backups.exists()


def test_packaged_plugin_symlink_is_rejected_instead_of_followed(tmp_path):
    install, staged, backups = _roots(tmp_path)
    _write_commands(staged / "commands.json", {})
    old_plugins = install / "plugins" / "commands"
    old_plugins.mkdir(parents=True)
    (old_plugins / "custom.py").write_text("CUSTOM = True\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    staged_plugins = staged / "plugins" / "commands"
    staged_plugins.parent.mkdir(parents=True)
    try:
        staged_plugins.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"This Windows configuration cannot create symlinks: {exc}")

    with pytest.raises(customizations.UnsafeCustomizationError):
        customizations.migrate_update_customizations(install, staged, backups)
    assert not (outside / "custom.py").exists()
