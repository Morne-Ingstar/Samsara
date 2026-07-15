"""Focused regressions for deterministic and fail-closed release tooling."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools import release_manifest
from tools.check_release_version import check_versions
from tools.release_manifest import tracked_tree_datas
from tools.release_preflight import git_release_blockers, samsara_process_reason


def _write_version_files(root: Path, package: str, bridge: str) -> None:
    package_dir = root / "samsara"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text(
        f'__version__ = "{package}"\n', encoding="utf-8"
    )
    (package_dir / "smart_actions_bridge.py").write_text(
        f'SAMSARA_VERSION = "{bridge}"\n', encoding="utf-8"
    )


def test_release_version_accepts_matching_tag_and_embedded_versions(tmp_path):
    _write_version_files(tmp_path, "0.22.0", "0.22.0")
    assert check_versions(tmp_path, "v0.22.0") == "0.22.0"


@pytest.mark.parametrize(
    ("package", "bridge", "expected"),
    [("0.22.0", "0.21.1", None), ("0.22.0", "0.22.0", "v0.23.0")],
)
def test_release_version_rejects_any_identity_mismatch(
    tmp_path, package, bridge, expected
):
    _write_version_files(tmp_path, package, bridge)
    with pytest.raises(ValueError, match="mismatch"):
        check_versions(tmp_path, expected)


def test_release_manifest_uses_git_index_not_untracked_files(tmp_path):
    plugin_dir = tmp_path / "plugins" / "commands"
    plugin_dir.mkdir(parents=True)
    tracked = plugin_dir / "tracked.py"
    ignored = plugin_dir / "local_demo.py"
    tracked.write_text("TRACKED = True\n", encoding="utf-8")
    ignored.write_text("SHOULD_NOT_SHIP = True\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "plugins/commands/tracked.py"],
        check=True,
    )

    entries = tracked_tree_datas(tmp_path, "plugins", "plugins")
    sources = {Path(source).name for source, _destination in entries}
    assert sources == {"tracked.py"}
    assert entries[0][1].replace("\\", "/") == "plugins/commands"


def test_release_manifest_fails_closed_outside_git_checkout(tmp_path):
    plugin_dir = tmp_path / "plugins" / "commands"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "command.py").write_text("OK = True\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="requires a Git checkout"):
        tracked_tree_datas(tmp_path, "plugins", "plugins")


def test_release_manifest_fails_closed_when_git_cannot_run(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "plugins").mkdir()

    def _missing_git(*_args, **_kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(release_manifest.subprocess, "run", _missing_git)
    with pytest.raises(RuntimeError, match="could not execute Git"):
        tracked_tree_datas(tmp_path, "plugins", "plugins")


def test_release_preflight_detects_frozen_and_this_checkout_source(tmp_path):
    root = tmp_path / "Samsara"
    root.mkdir()
    source_info = {
        "pid": 100,
        "name": "python.exe",
        "cwd": str(root),
        "cmdline": ["python.exe", "dictation.py"],
    }
    frozen_info = {
        "pid": 101,
        "name": "Samsara.exe",
        "cwd": None,
        "cmdline": [],
    }
    assert samsara_process_reason(source_info, root, current_pid=999) == "source dictation.py"
    assert samsara_process_reason(frozen_info, root, current_pid=999) == "frozen Samsara.exe"


def test_release_preflight_ignores_unrelated_python(tmp_path):
    info = {
        "pid": 100,
        "name": "python.exe",
        "cwd": str(tmp_path),
        "cmdline": ["python.exe", "other_script.py"],
    }
    assert samsara_process_reason(info, tmp_path, current_pid=999) is None


def test_release_preflight_blocks_tracked_changes_and_required_untracked_only(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    tracked.write_text("changed\n", encoding="utf-8")
    (tmp_path / "required.py").write_text("needed\n", encoding="utf-8")
    (tmp_path / "unrelated-artifact.zip").write_text("preserve me\n", encoding="utf-8")

    blockers = git_release_blockers(tmp_path, required_paths=("required.py",))
    assert any("tracked.txt" in line for line in blockers)
    assert any("required.py" in line for line in blockers)
    assert not any("unrelated-artifact.zip" in line for line in blockers)


def test_release_scripts_are_fail_closed_and_manifest_based():
    root = Path(__file__).resolve().parents[1]
    spec = (root / "scripts" / "samsara.spec").read_text(encoding="utf-8")
    batch = (root / "build_release.bat").read_text(encoding="utf-8")

    for directory in ("sounds", "profiles", "plugins", "browser_extension"):
        assert f"tracked_tree_datas(app_dir, '{directory}', '{directory}')" in spec
    assert "taskkill" not in batch.casefold()
    assert "tools\\release_preflight.py" in batch
    assert "tools\\check_release_version.py" in batch
    assert "set INCLUDE_CUDA=0" in batch
    assert "SAMSARA_PYTHON" in batch
    assert "set PYTHON=F:\\envs\\sami\\python.exe" not in batch
