"""Release-shaped update preparation, derived from the build spec's data rules."""
from __future__ import annotations

import ast
import hashlib
import io
import json
from pathlib import Path
import stat
from types import SimpleNamespace
import zipfile

import pytest

from samsara import update_customizations as migration, updater
from samsara.runtime_manifest import OWW_MODEL_FILENAMES


def _spec_paths():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "scripts/samsara.spec")
                     .read_text(encoding="utf-8"))
    exe = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Name) and node.func.id == "EXE")
    keywords = {kw.arg: kw.value for kw in exe.keywords}
    # PyInstaller 6's onedir default when EXE omits contents_directory.
    contents = ast.literal_eval(keywords["contents_directory"]) if "contents_directory" in keywords else "_internal"
    prefix = Path(contents or ".")
    commands = plugins = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "datas"):
            source, destination = node.args[0].elts
            if (isinstance(source, ast.Call) and isinstance(source.args[0], ast.BinOp)
                    and isinstance(source.args[0].right, ast.Constant)
                    and source.args[0].right.value == "commands.json"):
                commands = prefix / ast.literal_eval(destination) / source.args[0].right.value
        if (isinstance(node.func, ast.Name) and node.func.id == "tracked_tree_datas"
                and ast.literal_eval(node.args[1]) == "plugins"):
            plugins = prefix / ast.literal_eval(node.args[2]) / "commands"
    assert commands is not None and plugins is not None
    return commands, plugins, ast.literal_eval(keywords["name"]) + ".exe"


def _commands(commands):
    return json.dumps({"commands": commands}).encode("utf-8")


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.mark.parametrize("old_layout", ["frozen", "flat"])
def test_prepare_spec_layout_migrates_commands_plugins_and_backup(tmp_path, monkeypatch, old_layout):
    commands_path, plugins_path, executable = _spec_paths()
    install = tmp_path / "Samsara"
    _write(install / executable, b"old executable placeholder")
    old_commands = commands_path if old_layout == "frozen" else Path("commands.json")
    old_plugins = plugins_path if old_layout == "frozen" else Path("plugins/commands")
    old_bytes = _commands({"shared": {"text": "user"}, "custom": {"text": "custom"}})
    _write(install / old_commands, old_bytes)
    _write(install / old_plugins / "custom.py", b"CUSTOM = True\n")
    _write(install / old_plugins / "builtin.py", b"OLD = True\n")
    entries = {
        executable: b"new executable placeholder",
        commands_path.as_posix(): _commands({"shared": {"text": "new"}, "new": {"text": "new"}}),
        (plugins_path / "builtin.py").as_posix(): b"NEW = True\n",
    }
    for filename in OWW_MODEL_FILENAMES:
        entries[f"_internal/openwakeword/resources/models/{filename}"] = b"model placeholder"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, data in entries.items():
            archive.writestr(path, data)
    data = output.getvalue()
    tag = "v0.31.0"
    name = f"Samsara-Windows-{tag}.zip"
    url = f"https://github.com/Morne-Ingstar/Samsara/releases/download/{tag}/{name}"
    release = updater.ReleaseInfo("0.31.0", tag, len(data), url, url + ".sha256", 96)
    responses = {url: data, url + ".sha256": f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()}

    def opener(request, timeout=None):
        response = io.BytesIO(responses[request.full_url])
        response.geturl = lambda: request.full_url
        return response

    home = tmp_path / "profile"
    monkeypatch.setattr(updater, "_require_update_eligible", lambda: None)
    monkeypatch.setattr(updater, "update_unavailable_reason", lambda: None)
    monkeypatch.setattr(updater, "_frozen_install_dir", lambda: install)
    monkeypatch.setattr(updater, "samsara_home_dir", lambda: home)
    prepared = updater.prepare_update(release, install_dir=install, opener=opener)
    merged = json.loads((prepared.staged_dir / commands_path).read_bytes())["commands"]
    assert merged == {"shared": {"text": "user"}, "custom": {"text": "custom"}, "new": {"text": "new"}}
    assert (prepared.staged_dir / plugins_path / "custom.py").read_bytes() == b"CUSTOM = True\n"
    assert (prepared.staged_dir / plugins_path / "builtin.py").read_bytes() == b"NEW = True\n"
    backup, = (home / "updates/backups").glob("customizations-*")
    assert (backup / "commands.json").read_bytes() == old_bytes
    assert (backup / "plugins/commands/custom.py").read_bytes() == b"CUSTOM = True\n"
    assert (backup / "plugins/commands/builtin.py").read_bytes() == b"OLD = True\n"
    assert json.loads((backup / "manifest.json").read_bytes())["packaged_plugins_kept"] == ["builtin.py"]
    assert (install / old_commands).read_bytes() == old_bytes


def _trees(tmp_path):
    commands, plugins, _exe = _spec_paths()
    install, staged, backups = (tmp_path / name for name in ("install", "staged", "backups"))
    _write(install / commands, _commands({"custom": {"text": "user"}}))
    _write(staged / commands, _commands({"new": {"text": "packaged"}}))
    return install, staged, backups, commands, plugins


def test_runtime_copy_wins_over_root_compatibility_copy(tmp_path):
    install, staged, backups, commands, plugins = _trees(tmp_path)
    root_bytes = _commands({"decoy": {"text": "compatibility"}})
    for root in (install, staged):
        _write(root / "commands.json", root_bytes)
        _write(root / "plugins/commands/decoy.py", b"DECOY = True\n")
    _write(install / plugins / "custom.py", b"CUSTOM = True\n")
    summary = migration.migrate_update_customizations(install, staged, backups)
    assert set(json.loads((staged / commands).read_bytes())["commands"]) == {"custom", "new"}
    assert (staged / "commands.json").read_bytes() == root_bytes
    assert summary.plugins_copied == ("custom.py",)
    assert (staged / plugins / "custom.py").is_file()


def test_frozen_install_can_migrate_to_flat_stage(tmp_path):
    install, staged, backups, commands, plugins = _trees(tmp_path)
    (staged / commands).unlink()
    _write(staged / "commands.json", _commands({"new": {"text": "packaged"}}))
    _write(install / plugins / "custom.py", b"CUSTOM = True\n")
    migration.migrate_update_customizations(install, staged, backups)
    assert set(json.loads((staged / "commands.json").read_bytes())["commands"]) == {"custom", "new"}
    assert (staged / "plugins/commands/custom.py").is_file()


def test_missing_installed_commands_still_preserves_internal_plugins(tmp_path):
    install, staged, backups, commands, plugins = _trees(tmp_path)
    (install / commands).unlink()
    _write(install / plugins / "custom.py", b"CUSTOM = True\n")
    summary = migration.migrate_update_customizations(install, staged, backups)
    assert summary.installed_command_count == 0
    assert summary.plugins_copied == ("custom.py",)
    assert (staged / plugins / "custom.py").is_file()
    assert not (summary.backup_dir / "commands.json").exists()


@pytest.mark.parametrize("side", ["install", "staged"])
@pytest.mark.parametrize("ancestor", ["_internal", "_internal/plugins"])
def test_reparse_parent_is_rejected_before_reading_customizations(tmp_path, monkeypatch, side, ancestor):
    install, staged, backups, commands, plugins = _trees(tmp_path)
    _write(install / plugins / "custom.py", b"CUSTOM = True\n")
    (staged / plugins).mkdir(parents=True)
    target = {"install": install, "staged": staged}[side] / ancestor
    checked_lstat = migration._checked_lstat

    def lstat(path):
        if path == target:
            info = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            assert migration._is_link_or_reparse(info)
            raise migration.UnsafeCustomizationError(f"Refusing to migrate link or reparse point: {path}")
        return checked_lstat(path)

    monkeypatch.setattr(migration, "_checked_lstat", lstat)
    with pytest.raises(migration.UnsafeCustomizationError, match="reparse point"):
        migration.migrate_update_customizations(install, staged, backups)
    assert not backups.exists()


@pytest.mark.parametrize("side", ["install", "staged"])
def test_malformed_internal_commands_do_not_fall_back_to_valid_root(tmp_path, side):
    install, staged, backups, commands, _plugins = _trees(tmp_path)
    root = {"install": install, "staged": staged}[side]
    _write(root / commands, b"{malformed")
    _write(root / "commands.json", _commands({}))
    before = (staged / commands).read_bytes()
    with pytest.raises(migration.CustomizationMigrationError, match="Invalid commands file"):
        migration.migrate_update_customizations(install, staged, backups)
    assert (staged / commands).read_bytes() == before
    assert not backups.exists()


@pytest.mark.parametrize("limit", ["MAX_COMMANDS_BYTES", "MAX_MERGED_COMMANDS_BYTES", "MAX_PLUGIN_FILES", "backup"])
def test_frozen_layout_keeps_safety_limits(tmp_path, monkeypatch, limit):
    install, staged, backups, commands, plugins = _trees(tmp_path)
    _write(install / plugins / "custom.py", b"CUSTOM = True\n")
    before = (staged / commands).read_bytes()
    if limit != "backup":
        monkeypatch.setattr(migration, limit, 1 if limit != "MAX_PLUGIN_FILES" else 0)
    kwargs = {"max_backup_bytes": 1} if limit == "backup" else {}
    with pytest.raises(migration.CustomizationMigrationError, match="limit|exceeding"):
        migration.migrate_update_customizations(install, staged, backups, **kwargs)
    assert (staged / commands).read_bytes() == before
    assert not backups.exists()
