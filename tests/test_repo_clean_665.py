"""Public-tree privacy regression checks, usable in a plain source export.

The cleanup's tracked-file inventory is taken with git ls-files outside pytest.
This guard scans the source tree directly so exports need neither Git nor a
user profile. It also catches new source files before they are tracked.
"""
from __future__ import annotations

import os
import hashlib
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
# 881: filled with one-way fingerprints of retired personal command phrases.
# The private phrases themselves must never become test fixtures or public text.
PERSONAL_PHRASES = {
    2: frozenset({
        "14171ef6f2aa501c5867e80c98b03ccf515325eaa968a8f39142472c112f9913",
        "4bf56c06b192bf101d1aa0c7398a0a8c8bf314966eeec2f8a0e88d7f040ecd0c",
        "5d9312f1f3b77168a0689721fa2b5a39f5ea328573764162d63ebd38a90ef8bf",
        "6b53baca66d11d2ef621c64c62edf75cbf8501cabaaaa060d9f4441f6f0eda4d",
        "88148b5549f1d016aedfe3c6ec37e1157346772cd0468d603aa93b34ceaa1f53",
        "a43eea4ca256f4f15a7008f28238e16c81c9bfc392d22c8ce5084c176339acff",
        "b46194ea272eaa5bac03752f677894fc6d103964fed18150ba727b00d2af51ea",
        "c77f64bc9928c9d945b68f490a035f1bfec88d53396782c9eaacb38e54579f33",
        "ce20b114480908fe7ae472fc02b47493109829336a4dbc46fcec15693bd9b5a2",
        "dc63c8663546ca3671536c7d44495c3df5fbbf9a753926b6addc1597fc5dd98e",
        "fb91bfb1dfcd61a454942be24522a7dc85adf2006241f09ecb3810555d31141b",
    }),
    3: frozenset({
        "055c8d859be435859407994c37fb1aa0496cb19533bc61036f36af9c75c3c32f",
        "430cff51878f0b6c28e740a956046022dd42bbcf4f10dcbd924ee98db4438119",
        "a301567691c986379139fae81d6fe8452537dd9dbe7808fd9003741116ba0b19",
        "a4f1c653f9815f945efa9bef934e36fb8a41288402eb92b37c3e9e097f10caf7",
        "e1256d8dae4e33603841c89c256b2c497b7f6fb39388e2f29ef019ecb42ed75f",
    }),
    4: frozenset({
        "2ac02b7ec9a5e31539a46b97c25fd02666794dc5f8f9d1a1e048194f2bfd2a38",
        "4c8d06b7fd86cb3f4ee58af5dabe0032b187c52c13b5fe541fdbfef16de0e018",
        "54f81f9669136a0c23c02fc68d85ac649a4cec21600856b80821a9efe0c0ff1a",
    }),
    5: frozenset({
        "03603896f1aefc3ca30a6ab13adb8b03e379cbc052d9b57282154cda57a06aab",
    }),
}
# No path-wide source exemptions: provider/model names, public contacts,
# accessibility descriptions, tabletop terms, and functional review markers
# remain allowed because they do not match these private signatures.


def source_files(root):
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs
                         if name not in LOCAL_DIRS
                         and not name.startswith("ci-")
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
        relative = path.relative_to(root).as_posix()
        if ((path.name.startswith('local_') and path.suffix == '.py')
                or 'personal_plugins' in path.relative_to(root).parts):
            result.append((relative, 0, "personal plugin file"))
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError:
            continue  # Binary assets; this guard makes no content claim for them.
        for number, line in enumerate(text.splitlines(), 1):
            words = re.findall(r'[a-z0-9]+', line.lower())
            for start, word in enumerate(words):
                if word not in {'3d', 'launch', 'print', 'restart', 'show', 'start', 'stop', 'three'}:
                    continue
                for size, fingerprints in PERSONAL_PHRASES.items():
                    phrase = ' '.join(words[start:start + size])
                    if hashlib.sha256(phrase.encode()).hexdigest() in fingerprints:
                        result.append((relative, number, "personal command phrase"))
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
    private = "C:" + "/Users/" + OWNER + "/file.txt"
    for directory in ("build", "ci-hang-report", "ci-scratch"):
        local = tmp_path / directory
        local.mkdir()
        (local / "output.txt").write_text(private, encoding="utf-8")
    (tmp_path / "binary.png").write_bytes(b"\0" + private.encode())
    assert findings(tmp_path) == []
    (tmp_path / "new_source.txt").write_text(private, encoding="utf-8")
    assert findings(tmp_path) == [("new_source.txt", 1, "private workstation path")]


@pytest.mark.parametrize('relative', [
    'plugins/commands/local_example.py',
    'another/local_example.py',
    'personal_plugins/example.py',
    'nested/personal_plugins/data.bin',
])
def test_guard_rejects_personal_plugin_files_even_without_private_text(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b'\0synthetic data')
    assert findings(tmp_path) == [(relative, 0, 'personal plugin file')]


def test_guard_detects_phrase_fingerprints_without_embedding_private_text(tmp_path, monkeypatch):
    # Exercise the detector using synthetic content, never an owner's command.
    phrase = 'launch synthetic fixture'
    monkeypatch.setitem(PERSONAL_PHRASES, 3, {hashlib.sha256(phrase.encode()).hexdigest()})
    (tmp_path / 'new_source.md').write_text('Example: LAUNCH synthetic fixture.\n', encoding='utf-8')
    assert findings(tmp_path) == [('new_source.md', 1, 'personal command phrase')]


def test_retired_phrase_fingerprints_are_present():
    assert sum(map(len, PERSONAL_PHRASES.values())) >= 20
