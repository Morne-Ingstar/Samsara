"""manifest.json schema (samsara.components.validate_manifest) and the
generator (tools/gen_release_manifest.py): a good manifest validates, every
malformed shape is named, the generator fails on a missing artifact, never
emits a placeholder hash, and is byte-identical across runs."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import components
from tools import gen_release_manifest as gen
from tools.release_preflight import OWW_MODELS

GIT_SHA = "0123456789abcdef0123456789abcdef01234567"
TAG = "v9.9.9"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def good_manifest() -> dict:
    core = b"core-zip-bytes"
    return {
        "schema_version": 1,
        "app_version": "9.9.9",
        "release_tag": TAG,
        "build_date": "2026-09-13T11:20:00Z",
        "git_sha": GIT_SHA,
        "components": [
            {
                "id": "core", "name": "Samsara", "description": "The app.", "kind": "core",
                "available": True, "filename": f"Samsara-Windows-{TAG}.zip",
                "url": f"https://github.com/o/r/releases/download/{TAG}/Samsara-Windows-{TAG}.zip",
                "size_bytes": len(core), "sha256": _sha(core), "min_disk_bytes": 2 * len(core),
                "install_dir": ".", "requires": [], "default": True, "source": "built",
            },
            {
                "id": "command-model", "name": "Command model", "description": "Later.", "kind": "model",
                "available": False, "filename": "Samsara-Command-Model.zip", "url": None,
                "size_bytes": None, "sha256": None, "min_disk_bytes": None,
                "install_dir": "_internal/models/command", "requires": [], "default": False,
                "source": "pinned",
            },
        ],
    }


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_good_manifest_validates(self):
        assert components.validate_manifest(good_manifest()) == []

    def test_every_field_is_present_on_every_component(self):
        for c in good_manifest()["components"]:
            assert set(c) == set(components.COMPONENT_FIELDS)

    @pytest.mark.parametrize("mutate,needle", [
        (lambda m: m["components"][0].pop("sha256"), "missing field 'sha256'"),
        (lambda m: m["components"][0].__setitem__("sha256", None), "sha256 must be 64"),
        (lambda m: m["components"][0].__setitem__("sha256", "deadbeef"), "sha256 must be 64"),
        (lambda m: m["components"][0].__setitem__("sha256", "A" * 64), "sha256 must be 64"),
        (lambda m: m["components"][0].__setitem__("size_bytes", -1), "size_bytes must be a positive"),
        (lambda m: m["components"][0].__setitem__("size_bytes", 0), "size_bytes must be a positive"),
        (lambda m: m["components"][0].__setitem__("size_bytes", True), "size_bytes must be a positive"),
        (lambda m: m["components"][0].__setitem__("kind", "plugin"), "unknown kind 'plugin'"),
        (lambda m: m["components"][0].__setitem__("url", "http://github.com/x.zip"), "url must be an https"),
        (lambda m: m["components"][0].__setitem__("url", "ftp://github.com/x.zip"), "url must be an https"),
        (lambda m: m["components"][0].__setitem__("url", "file:///C:/x.zip"), "url must be an https"),
        (lambda m: m["components"][0].__setitem__("min_disk_bytes", 1), "min_disk_bytes (1) is smaller"),
        (lambda m: m["components"][0].__setitem__("id", "Core App"), "id must match"),
        (lambda m: m["components"][0].__setitem__("filename", "../x.zip"), "filename must be a plain"),
        (lambda m: m["components"][0].__setitem__("requires", "nvidia_gpu"), "requires must be a list"),
        (lambda m: m["components"][0].__setitem__("source", "guess"), "unknown source"),
        (lambda m: m["components"][1].__setitem__("sha256", "0" * 64), "must be null while available is false"),
        (lambda m: m.__setitem__("schema_version", 2), "schema_version 2 is not 1"),
        (lambda m: m.__setitem__("git_sha", "abc"), "git_sha must be a 40-hex"),
        (lambda m: m.__setitem__("components", []), "components must be a non-empty"),
        (lambda m: m["components"].append(copy.deepcopy(m["components"][0])), "duplicate id"),
        (lambda m: m.pop("build_date"), "missing field 'build_date'"),
    ])
    def test_each_malformed_shape_is_rejected_by_name(self, mutate, needle):
        doc = good_manifest()
        mutate(doc)
        problems = components.validate_manifest(doc)
        assert any(needle in p for p in problems), problems

    def test_load_manifest_from_path_and_https_only(self, tmp_path):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(good_manifest()), encoding="utf-8")
        assert components.load_manifest(path)["release_tag"] == TAG

        fetched = []
        doc = components.load_manifest(
            "https://example.invalid/manifest.json",
            fetch=lambda url: (fetched.append(url), json.dumps(good_manifest()).encode())[1])
        assert doc["app_version"] == "9.9.9" and fetched == ["https://example.invalid/manifest.json"]

        with pytest.raises(components.ManifestError, match="https"):
            components.load_manifest("http://example.invalid/manifest.json", fetch=lambda u: b"{}")
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(components.ManifestError, match="not valid JSON"):
            components.load_manifest(path)
        bad = good_manifest()
        bad["components"][0]["kind"] = "nope"
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(components.ManifestError) as info:
            components.load_manifest(path)
        assert any("unknown kind" in p for p in info.value.problems)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def _write_models_zip(path: Path, *, drop=None, corrupt=None) -> dict:
    """A wake-word archive whose members hash to the real pins requires the
    real files; tests substitute a pin table via `pins` below instead, so
    here the archive carries fake bytes and the test patches OWW_MODELS."""
    payload = {name: f"model {name}".encode() for name in OWW_MODELS}
    if drop:
        payload.pop(drop)
    if corrupt:
        payload[corrupt] = b"garbage"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in payload.items():
            zf.writestr(name, data)
    return {name: _sha(f"model {name}".encode()) for name in OWW_MODELS}


@pytest.fixture
def build(tmp_path, monkeypatch):
    """A fake artifacts directory with the built core, model, and feature assets."""
    core = tmp_path / f"Samsara-Windows-{TAG}.zip"
    core.write_bytes(b"core zip " * 1000)
    fake_pins = _write_models_zip(tmp_path / f"Samsara-WakeWord-Models-{TAG}.zip")
    (tmp_path / f"Samsara-GestureControl-{TAG}.zip").write_bytes(b"gesture component" * 100)
    monkeypatch.setattr(gen, "OWW_MODELS", fake_pins)
    return tmp_path


def _generate(artifacts_dir, **over):
    kwargs = dict(artifacts_dir=artifacts_dir, tag=TAG, repo="o/r", git_sha=GIT_SHA,
                  build_date="2026-09-13T11:20:00Z", app_version="9.9.9")
    kwargs.update(over)
    return gen.build_manifest(**kwargs)


class TestGenerator:
    def test_sizes_hashes_and_urls_come_from_the_files(self, build):
        doc = _generate(build)
        assert components.validate_manifest(doc) == []
        by_id = {c["id"]: c for c in doc["components"]}
        core = build / f"Samsara-Windows-{TAG}.zip"
        assert by_id["core"]["size_bytes"] == core.stat().st_size
        assert by_id["core"]["sha256"] == _sha(core.read_bytes())
        assert by_id["core"]["url"] == f"https://github.com/o/r/releases/download/{TAG}/Samsara-Windows-{TAG}.zip"
        assert by_id["core"]["min_disk_bytes"] >= by_id["core"]["size_bytes"]
        models = build / f"Samsara-WakeWord-Models-{TAG}.zip"
        assert by_id["wake-word-models"]["sha256"] == _sha(models.read_bytes())
        assert by_id["cuda-pack"]["sha256"] == gen.PINNED_ASSETS["cuda-pack"]["sha256"]
        assert by_id["cuda-pack"]["requires"] == ["nvidia_gpu"] and by_id["cuda-pack"]["default"] is False
        assert by_id["command-model"]["available"] is False
        assert by_id["command-model"]["sha256"] is None and by_id["command-model"]["url"] is None
        assert by_id["gesture-control"]["kind"] == "feature"
        assert by_id["gesture-control"]["default"] is False
        assert [c["id"] for c in doc["components"]] == sorted(c["id"] for c in doc["components"])

    def test_missing_built_artifact_fails_loudly(self, build):
        (build / f"Samsara-Windows-{TAG}.zip").unlink()
        with pytest.raises(gen.ManifestGenerationError, match=r"'core' is missing: .*Samsara-Windows"):
            _generate(build)

    @pytest.mark.parametrize("which", ["drop", "corrupt"])
    def test_wake_word_archive_is_checked_against_the_pins(self, build, monkeypatch, which):
        models = build / f"Samsara-WakeWord-Models-{TAG}.zip"
        name = sorted(OWW_MODELS)[0]
        pins = _write_models_zip(models, **{which: name})
        monkeypatch.setattr(gen, "OWW_MODELS", pins)
        with pytest.raises(gen.ManifestGenerationError, match=name):
            _generate(build)

    def test_never_emits_a_placeholder_hash(self, build):
        pins = {"cuda-pack": dict(gen.PINNED_ASSETS["cuda-pack"], sha256="TBD")}
        with pytest.raises(gen.ManifestGenerationError, match="refusing to emit a placeholder"):
            _generate(build, pins=pins)
        pins = {"cuda-pack": dict(gen.PINNED_ASSETS["cuda-pack"], sha256="")}
        with pytest.raises(gen.ManifestGenerationError, match="refusing"):
            _generate(build, pins=pins)
        with pytest.raises(gen.ManifestGenerationError, match="refusing"):
            _generate(build, pins={})

    def test_local_copy_of_a_pinned_asset_must_match_its_pin(self, build):
        (build / gen.PINNED_ASSETS["cuda-pack"]["filename"]).write_bytes(b"not the real pack")
        with pytest.raises(gen.ManifestGenerationError, match="does not match its pin"):
            _generate(build)

    def test_two_runs_are_byte_identical(self, build, tmp_path):
        first = gen.dumps(_generate(build))
        second = gen.dumps(_generate(build))
        assert first == second
        assert first.endswith("\n") and json.loads(first)["schema_version"] == 1
        # and the key order is sorted at every level
        assert list(json.loads(first)) == sorted(json.loads(first))

    def test_cli_writes_the_file_and_reports(self, build, tmp_path, capsys):
        out = tmp_path / "manifest.json"
        rc = gen.main(["--artifacts-dir", str(build), "--tag", TAG, "--repo", "o/r",
                       "--git-sha", GIT_SHA, "--build-date", "2026-09-13T11:20:00Z",
                       "--app-version", "9.9.9", "--output", str(out)])
        assert rc == 0
        assert components.validate_manifest(json.loads(out.read_text(encoding="utf-8"))) == []
        assert "[PASS] core:" in capsys.readouterr().out

    def test_cli_fails_loudly_on_a_missing_artifact(self, build, tmp_path, capsys):
        (build / f"Samsara-WakeWord-Models-{TAG}.zip").unlink()
        out = tmp_path / "manifest.json"
        rc = gen.main(["--artifacts-dir", str(build), "--tag", TAG, "--git-sha", GIT_SHA,
                       "--build-date", "x", "--app-version", "9.9.9", "--output", str(out)])
        assert rc == 1 and not out.exists()
        assert "[FAIL] manifest: built artifact for component 'wake-word-models' is missing" in capsys.readouterr().out

    def test_real_pins_are_full_hashes(self):
        for cid, pin in gen.PINNED_ASSETS.items():
            assert len(pin["sha256"]) == 64 and pin["url"].startswith("https://"), cid
            assert pin["size_bytes"] > 0
