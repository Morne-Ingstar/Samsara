"""Generate manifest.json (docs/RELEASE_MANIFEST.md, schema 1) from the built
release artifacts.

    python tools/gen_release_manifest.py --artifacts-dir . --tag v0.23.0-beta.1 \\
        --repo Morne-Ingstar/Samsara --git-sha <40-hex> --output manifest.json

Every `built` component's file must exist in --artifacts-dir; its size and
SHA-256 come from the file, its URL from the tag's Release assets. A missing
file is a hard failure naming it. A `pinned` component (an asset from an
earlier release that this build does not produce) carries a full pin here;
a local copy, when present, must match the pin. Nothing is ever emitted with
an empty or placeholder hash. Output is sorted-keys JSON with the build
date taken from Git, so two runs over one build are byte-identical.

Hashing and the wake-word pins are tools/release_preflight's (_sha256 and
OWW_MODELS): the models archive is opened and every member checked against
the pinned hash before its entry is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from samsara.components import SCHEMA_VERSION, validate_manifest  # noqa: E402
from tools.release_preflight import OWW_MODELS, _sha256  # noqa: E402

DEFAULT_REPO = "Morne-Ingstar/Samsara"
OWW_MODELS_DIR_IN_DIST = Path("_internal") / "openwakeword" / "resources" / "models"

# Assets this workflow does not build, pinned by URL, size and SHA-256 the
# way OWW_MODELS pins the wake-word files. cuda-pack: docs/CUDA.md
# (v0.20.0's Samsara-CUDA-Pack, ten NVIDIA runtime DLLs). A component
# pinned without a hash is refused at generation time.
PINNED_ASSETS = {
    "cuda-pack": {
        "url": "https://github.com/Morne-Ingstar/Samsara/releases/download/v0.20.0/Samsara-CUDA-Pack-v0.20.0.zip",
        "filename": "Samsara-CUDA-Pack-v0.20.0.zip",
        "size_bytes": 1128045243,
        "sha256": "5dc752c89ca4e6ad777b545907a7e654471ce3dfe10a3d96bbfc705386db335d",
    },
}


class ManifestGenerationError(RuntimeError):
    pass


def component_specs(tag: str) -> list:
    """The components of a release for `tag`, before sizes and hashes."""
    return [
        {
            "id": "core",
            "name": "Samsara",
            "description": "The application, CPU build (verified on CI).",
            "kind": "core",
            "source": "built",
            "filename": f"Samsara-Windows-{tag}.zip",
            "install_dir": ".",
            "requires": [],
            "default": True,
            "available": True,
        },
        {
            "id": "cuda-pack",
            "name": "NVIDIA CUDA acceleration",
            "description": "GPU acceleration is unavailable until a pack is verified for this release.",  # PLACEHOLDER COPY (917)
            "kind": "acceleration",
            "source": "built",
            "filename": f"Samsara-CUDA-Pack-{tag}.zip",
            "install_dir": "_internal/ctranslate2",
            "requires": ["nvidia_gpu"],
            "default": False,
            "available": False,
        },
        {
            "id": "wake-word-models",
            "name": "Wake-word models",
            "description": "The nine OpenWakeWord ONNX models (hands-free wake phrases and VAD).",
            "kind": "model",
            "source": "built",
            "filename": f"Samsara-WakeWord-Models-{tag}.zip",
            "install_dir": OWW_MODELS_DIR_IN_DIST.as_posix(),
            "requires": [],
            "default": True,
            "available": True,
        },
        {
            "id": "command-model",
            "name": "Command model",
            "description": "Local model for the hands-free command lane (not yet released).",
            "kind": "model",
            "source": "pinned",
            "filename": "Samsara-Command-Model.zip",
            "install_dir": "_internal/models/command",
            "requires": [],
            "default": False,
            "available": False,
        },
        {
            "id": "gesture-control",
            "name": "Gesture control",
            "description": "Webcam hand poses for hands-free commands.",
            "kind": "feature",
            "source": "built",
            "filename": f"Samsara-GestureControl-{tag}.zip",
            "install_dir": "_internal",
            "requires": [],
            "default": False,
            "available": True,
        },
    ]


def asset_url(repo: str, tag: str, filename: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{filename}"


def verify_cuda_identity(entry: dict, tag: str, app_version: str, repo: str) -> None:
    """An enabled CUDA pack must belong to this exact application release (917)."""
    if entry["id"] != "cuda-pack" or not entry["available"]:
        return
    expected = f"Samsara-CUDA-Pack-{tag}.zip"
    if (tag != f"v{app_version}" or entry["filename"] != expected
            or entry["url"] != asset_url(repo, tag, expected)):
        raise ManifestGenerationError(
            "cuda-pack version does not match app/release version; "
            "build and verify a matching pack before enabling it")


def verify_wake_word_archive(path: Path) -> None:
    """Every OWW_MODELS file must be in the archive with its pinned hash."""
    with zipfile.ZipFile(path) as zf:
        members = {Path(n).name: n for n in zf.namelist() if not n.endswith("/")}
        problems = []
        for name, expected in sorted(OWW_MODELS.items()):
            if name not in members:
                problems.append(f"missing {name}")
                continue
            actual = hashlib.sha256(zf.read(members[name])).hexdigest()
            if actual != expected:
                problems.append(f"sha256 mismatch {name}: {actual} != {expected}")
        extra = sorted(set(members) - set(OWW_MODELS))
        if extra:
            problems.append(f"unexpected members: {', '.join(extra)}")
    if problems:
        raise ManifestGenerationError(f"{path.name}: " + "; ".join(problems))


def read_app_version(project_root: Path = PROJECT_ROOT) -> str:
    text = (project_root / "samsara" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise ManifestGenerationError("samsara/__init__.py has no __version__")
    return match.group(1)


def git_commit_date(git_sha: str, project_root: Path = PROJECT_ROOT) -> str:
    """The committer date of git_sha as ISO 8601 UTC (deterministic per build)."""
    result = subprocess.run(
        ["git", "-C", str(project_root), "show", "-s", "--format=%cI", git_sha],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ManifestGenerationError(f"git show {git_sha} failed: {result.stderr.strip() or 'no output'}")
    stamp = result.stdout.strip()
    from datetime import datetime, timezone  # noqa: PLC0415
    return datetime.fromisoformat(stamp).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_manifest(*, artifacts_dir, tag: str, repo: str, git_sha: str, build_date: str,
                   app_version: str, specs=None, pins=None, hasher=_sha256,
                   verify_models=verify_wake_word_archive) -> dict:
    """The manifest document. Raises ManifestGenerationError rather than
    emit a missing artifact or a placeholder hash."""
    artifacts_dir = Path(artifacts_dir)
    pins = PINNED_ASSETS if pins is None else pins
    components = []
    for spec in (component_specs(tag) if specs is None else specs):
        entry = dict(spec)
        if not entry["available"]:
            entry.update(url=None, size_bytes=None, sha256=None, min_disk_bytes=None)
            components.append(entry)
            continue
        if entry["source"] == "built":
            path = artifacts_dir / entry["filename"]
            if not path.is_file():
                raise ManifestGenerationError(
                    f"built artifact for component {entry['id']!r} is missing: {path}")
            if entry["id"] == "wake-word-models":
                verify_models(path)
            size = path.stat().st_size
            digest = hasher(path)
            entry.update(url=asset_url(repo, tag, entry["filename"]), size_bytes=size, sha256=digest)
        else:
            pin = pins.get(entry["id"])
            if not pin or not re.fullmatch(r"[0-9a-f]{64}", str(pin.get("sha256", ""))):
                raise ManifestGenerationError(
                    f"pinned component {entry['id']!r} has no full sha256 pin; refusing to emit a placeholder")
            path = artifacts_dir / entry["filename"]
            if path.is_file():
                size, digest = path.stat().st_size, hasher(path)
                if (size, digest) != (pin["size_bytes"], pin["sha256"]):
                    raise ManifestGenerationError(
                        f"local {path.name} does not match its pin: size {size} sha256 {digest}")
            entry.update(url=pin["url"], size_bytes=pin["size_bytes"], sha256=pin["sha256"])
        verify_cuda_identity(entry, tag, app_version, repo)
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"])):
            raise ManifestGenerationError(f"component {entry['id']!r}: refusing to emit hash {entry['sha256']!r}")
        # Download plus unpack: the archive and its contents, rounded up.
        entry["min_disk_bytes"] = entry["size_bytes"] * 2
        components.append(entry)
    doc = {
        "schema_version": SCHEMA_VERSION,
        "app_version": app_version,
        "release_tag": tag,
        "build_date": build_date,
        "git_sha": git_sha,
        "components": sorted(components, key=lambda c: c["id"]),
    }
    problems = validate_manifest(doc)
    if problems:
        raise ManifestGenerationError("generated manifest is invalid: " + "; ".join(problems))
    return doc


def dumps(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def audit_addon_overlaps(doc: dict, artifacts_dir) -> dict:
    """Inventory local release ZIPs against the actual core, without downloads.

    Pinned CUDA assets may be absent locally; say so rather than infer their
    contents from a different environment. PYZ omissions are recorded by the
    spec's separate source-module audit, not by this filesystem comparison.
    """
    artifacts_dir = Path(artifacts_dir)
    core = next(c for c in doc["components"] if c["id"] == "core")

    def members(archive):
        result = {}
        for info in archive.infolist():
            if info.is_dir():
                continue
            path = PurePosixPath(info.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or ":" in str(path):
                raise ManifestGenerationError(f"unsafe archive member: {info.filename}")
            key = path.as_posix().casefold()
            if key in result:
                raise ManifestGenerationError(f"duplicate archive member: {info.filename}")
            result[key] = info
        return result

    def digest(archive, info):
        with archive.open(info) as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    rows = []
    try:
        with zipfile.ZipFile(artifacts_dir / core["filename"]) as core_zip:
            core_files, core_hashes = members(core_zip), {}
            for component in doc["components"]:
                if component["id"] == "core":
                    continue
                path = artifacts_dir / component["filename"]
                row = dict(id=component["id"], install_dir=component["install_dir"], files=[])
                rows.append(row)
                if not component["available"]:
                    row["status"] = "unavailable"
                    continue
                if not path.is_file():
                    row["status"] = "artifact-missing"
                    continue
                row["status"] = "audited"
                with zipfile.ZipFile(path) as addon_zip:
                    for _name, info in sorted(members(addon_zip).items()):
                        target = (PurePosixPath(component["install_dir"]) /
                                  info.filename.replace("\\", "/")).as_posix()
                        shared = core_files.get(target.casefold())
                        incoming_hash = digest(addon_zip, info)
                        core_hash = None
                        if shared is not None:
                            if target.casefold() not in core_hashes:
                                core_hashes[target.casefold()] = digest(core_zip, shared)
                            core_hash = core_hashes[target.casefold()]
                        row["files"].append(dict(path=info.filename, target=target,
                            sha256=incoming_hash, core_sha256=core_hash,
                            overlap="absent" if shared is None else
                                    "identical" if core_hash == incoming_hash else "different"))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ManifestGenerationError(f"cannot audit release archives: {exc}") from exc
    return dict(core=core["filename"], components=rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifacts-dir", required=True, help="directory holding the built ZIPs")
    parser.add_argument("--tag", required=True, help="release tag the assets live under, e.g. v0.23.0-beta.1")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub owner/name (default %(default)s)")
    parser.add_argument("--git-sha", required=True, help="full commit sha of the build")
    parser.add_argument("--build-date", default=None,
                        help="ISO 8601 UTC; default: the committer date of --git-sha")
    parser.add_argument("--app-version", default=None, help="default: samsara.__version__")
    parser.add_argument("--output", default="manifest.json")
    parser.add_argument("--overlap-audit", help="write a per-file core/add-on hash audit (local ZIPs only)")
    args = parser.parse_args(argv)
    try:
        doc = build_manifest(
            artifacts_dir=args.artifacts_dir, tag=args.tag, repo=args.repo, git_sha=args.git_sha,
            build_date=args.build_date or git_commit_date(args.git_sha),
            app_version=args.app_version or read_app_version(),
        )
        if args.overlap_audit:
            Path(args.overlap_audit).write_text(
                dumps(audit_addon_overlaps(doc, args.artifacts_dir)), encoding="utf-8")
    except ManifestGenerationError as exc:
        print(f"[FAIL] manifest: {exc}")
        return 1
    Path(args.output).write_text(dumps(doc), encoding="utf-8", newline="\n")
    for component in doc["components"]:
        state = f"{component['size_bytes']} bytes sha256 {component['sha256']}" if component["available"] \
            else "declared, not yet available"
        print(f"[PASS] {component['id']}: {state}")
    print(f"[PASS] wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
