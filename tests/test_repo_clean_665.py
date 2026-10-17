"""Public-tree privacy regression checks, usable in a plain source export.

The cleanup's tracked-file inventory is taken with git ls-files outside pytest.
This guard scans the source tree directly so exports need neither Git nor a
user profile. It also catches new source files before they are tracked.
"""
from __future__ import annotations

import os
import ipaddress
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REMOVED = (
    "AGENTS.md", "CLAUDE.md", "Docs/HANDS_FREE_GATES_FINDINGS.md",
    "tools/ai_config_probe_findings.md", "tools/archive/gen_review.py",
    "tools/archive/home_bottom_renders.py", "tools/archive/auto_accept_claude.ahk",
    "tools/archive/linecount.py", "tools/archive/patch_aec4.py",
    "ui_proof/372/audio_brief.png",
)
# These contain generated/local artifacts rather than public source. The exact
# removed paths above are checked separately, including ignored directories.
LOCAL_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "build", "dist", "release_staging", "_internal", "ui_proof",
    ".idea", ".vscode", ".claude", ".release_tmp",
}
SEPARATOR = r"[\\/]+"
OWNER = "Mor" + "ne"
PATTERNS = {
    "private workstation path": re.compile(
        rf"C:{SEPARATOR}Users{SEPARATOR}{OWNER}(?=[\\/\s\"']|$)"
        rf"|F:{SEPARATOR}envs\b|D:{SEPARATOR}samsara_", re.I),
    "private deployment coordinates": re.compile(
        "the-" + "arcana|/home/" + OWNER.lower() + "|" + OWNER.lower() + ":" + OWNER.lower(), re.I),
    "personal workflow or circumstances": re.compile(
        "chronic " + "finger|finger[- ]joint " + "pain|Fable-tier " + "model", re.I),
}
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
# An exact dependency pin has four version components; it is not a host.
IP_VERSION_ALLOWLIST = {("requirements.txt", "opencv-contrib-python==" + "4.13." + "0.92")}
# No path-wide source exemptions: provider/model names, public contacts,
# accessibility descriptions, tabletop terms, and functional review markers
# remain allowed because they do not match these private signatures.


def source_files(root):
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs
                         if name not in LOCAL_DIRS
                         and not name.startswith(".pytest_tmp")
                         and not (Path(parent) / name).is_symlink()
                         and (Path(parent) / name).relative_to(root).as_posix()
                         != "tests/fixtures/audio")
        for name in sorted(files):
            path = Path(parent) / name
            if path.is_symlink() or name == ".git" or path.suffix in {".pyc", ".pyo", ".log"}:
                continue
            yield path


def findings(root):
    result = []
    for path in source_files(root):
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError:
            continue  # Binary assets; this guard makes no content claim for them.
        for number, line in enumerate(text.splitlines(), 1):
            for reason, pattern in PATTERNS.items():
                if pattern.search(line):
                    result.append((path.relative_to(root).as_posix(), number, reason))
            relative = path.relative_to(root).as_posix()
            if (relative, line.strip()) in IP_VERSION_ALLOWLIST:
                continue
            for match in IPV4.finditer(line):
                try:
                    address = ipaddress.ip_address(match.group())
                except ValueError:
                    continue
                if address.is_global:
                    result.append((relative, number, "public server IP literal"))
    return result


def test_internal_files_are_absent():
    assert not [name for name in REMOVED if (ROOT / name).exists()]


def test_public_source_has_no_private_signatures():
    # Report locations/categories only, never the matched private content.
    assert not findings(ROOT)


@pytest.mark.parametrize("value", [
    "C:" + "\\Users\\" + OWNER + "\\file.txt",
    "C:" + "\\\\Users\\\\" + OWNER + "\\\\file.txt",
    "c:/users/" + OWNER.lower() + "/file.txt",
    "F:" + "\\envs\\example\\python.exe",
    "D:" + "/samsara_" + "worktree/file.py",
    "the-" + "arcana",
    "/home/" + OWNER.lower() + "/website",
    "finger-joint " + "pain",
    "8." + "8.8.8",
])
def test_guard_detects_private_signatures_in_new_source(tmp_path, value):
    (tmp_path / "new_source.py").write_text(value, encoding="utf-8")
    assert findings(tmp_path)


def test_guard_retains_public_and_synthetic_terms(tmp_path):
    (tmp_path / "example.md").write_text(
        "C:/TestUsers/Example/file.txt\n"
        "Claude model identifiers; chronic pain accessibility; tabletop Sigil\n"
        "Wording is a stand-in.\nhttps://morneis.com/samsara/\n",
        encoding="utf-8")
    assert findings(tmp_path) == []


def test_guard_skips_local_artifacts_without_leaving_source_exempt(tmp_path):
    local = tmp_path / "build"
    local.mkdir()
    private = "C:" + "/Users/" + OWNER + "/file.txt"
    (local / "output.txt").write_text(private, encoding="utf-8")
    (tmp_path / "binary.png").write_bytes(b"\0" + private.encode())
    assert findings(tmp_path) == []
    (tmp_path / "new_source.txt").write_text(private, encoding="utf-8")
    assert findings(tmp_path) == [("new_source.txt", 1, "private workstation path")]
