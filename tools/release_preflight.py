"""Refuse a local release build while source or frozen Samsara is running,
the checkout is dirty, or the nine bundled OpenWakeWord models are missing.

Wake-word models (10a, 2026-09-13): the ``openwakeword`` wheel ships NO model
files -- ``openwakeword.utils.download_models()`` fetches them into
``site-packages/openwakeword/resources/models`` the first time the app runs.
A developer machine has them because Samsara ran once; a fresh CI runner
never has, and ``scripts/samsara.spec``'s ``collect_data_files`` silently
collected nothing (v0.23.0-beta.1's CI ZIP shipped 0 of 9). ``OWW_MODELS``
pins the nine files to the upstream v0.5.1 release assets by SHA-256;
``--fetch-oww-models`` downloads any missing/mismatched file and re-verifies,
``--oww-models`` only verifies, and the default run verifies as part of the
local preflight. A wrong hash is always a hard failure.

    python tools/release_preflight.py                      # local pre-build gate
    python tools/release_preflight.py --oww-models         # models present + hashes (no network)
    python tools/release_preflight.py --fetch-oww-models   # CI: download + verify (never a silent skip)
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import urllib.request
from pathlib import Path

import psutil


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PYTHON_NAMES = {"python", "python.exe", "pythonw.exe"}
REQUIRED_TRACKED_PATHS = (
    "samsara/config_transfer.py",
    "samsara/output_devices.py",
    "samsara/single_instance.py",
    "samsara/ui_scale.py",
    "tools/check_release_version.py",
    "tools/release_manifest.py",
    "tools/release_preflight.py",
)

# The nine ONNX files scripts/samsara.spec bundles (oww_model_filenames) and
# tools/build_and_smoke.cmd check [12] / release-build.yml assert in the
# frozen output. Upstream: github.com/dscripka/openWakeWord release v0.5.1
# (openwakeword.MODELS/FEATURE_MODELS/VAD_MODELS point at the .tflite URLs;
# download_models() derives the .onnx URL by replacing the suffix). Hashes
# verified 2026-09-13 against both the released assets and the local
# openwakeword 0.6.0 install.
OWW_MODELS_RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
OWW_MODELS = {
    "alexa_v0.1.onnx": "6ff566a01d12670e8d9e3c59da32651db1575d17272a601b7f8a39283dfbae3e",
    "embedding_model.onnx": "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
    "hey_jarvis_v0.1.onnx": "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb",
    "hey_mycroft_v0.1.onnx": "c2a311e8fa1338de89c31b3b46dc4dffd4af2f9a8d6ddead48893c2d301b1f18",
    "hey_rhasspy_v0.1.onnx": "5a9b3ed3be2910e35780e097905aa9f35a9c10038df47914cf2b3ec4d670f6ea",
    "melspectrogram.onnx": "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
    "silero_vad.onnx": "a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28",
    "timer_v0.1.onnx": "371e44535470a29248b3b8f1bbbbaf2525c86417fd8f75c67fcf02ae0b9626df",
    "weather_v0.1.onnx": "8441da8e746899e8d969528d5bad5651cdd563079c05962788f77753041f60e7",
}


def samsara_process_reason(
    info: dict,
    project_root: str | Path,
    *,
    current_pid: int | None = None,
) -> str | None:
    """Describe a frozen or this-checkout source process, otherwise ``None``."""

    pid = info.get("pid")
    if pid == (os.getpid() if current_pid is None else current_pid):
        return None
    name = str(info.get("name") or "").casefold()
    if name == "samsara.exe":
        return "frozen Samsara.exe"
    if name not in _PYTHON_NAMES:
        return None

    root = Path(project_root).resolve()
    expected_script = (root / "dictation.py").resolve()
    cwd_raw = info.get("cwd")
    cwd = Path(cwd_raw).resolve() if cwd_raw else None
    for raw_arg in info.get("cmdline") or ():
        arg = str(raw_arg).strip().strip('"')
        if Path(arg).name.casefold() != "dictation.py":
            continue
        candidate = Path(arg)
        if not candidate.is_absolute():
            if cwd is None:
                continue
            candidate = cwd / candidate
        try:
            if candidate.resolve() == expected_script:
                return "source dictation.py"
        except OSError:
            continue
    return None


def running_samsara_processes(project_root: str | Path = PROJECT_ROOT) -> list[tuple[int, str]]:
    found = []
    for process in psutil.process_iter(["pid", "name", "cmdline", "cwd"]):
        try:
            reason = samsara_process_reason(process.info, project_root)
        except (psutil.Error, OSError):
            continue
        if reason:
            found.append((process.pid, reason))
    return found


def git_release_blockers(
    project_root: str | Path = PROJECT_ROOT,
    required_paths: tuple[str, ...] = REQUIRED_TRACKED_PATHS,
) -> list[str]:
    """Return changes that make the release source differ from its commit.

    Unrelated untracked user artifacts are intentionally tolerated: the spec's
    Git-index manifest excludes them. Required runtime/release files are a
    separate allowlist and must themselves be tracked.
    """

    root = Path(project_root)
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain",
            "--untracked-files=no",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "git status failed"
        raise RuntimeError(detail)
    blockers = [line for line in result.stdout.splitlines() if line]

    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", *required_paths],
        capture_output=True,
        check=False,
    )
    if tracked.returncode != 0:
        detail = tracked.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "git ls-files failed")
    tracked_paths = {
        raw.decode("utf-8").replace("\\", "/")
        for raw in tracked.stdout.split(b"\0")
        if raw
    }
    for required in required_paths:
        if required.replace("\\", "/") not in tracked_paths:
            blockers.append(f"?? {required} (required release file is not tracked)")
    return blockers


# ---------------------------------------------------------------------------
# OpenWakeWord models
# ---------------------------------------------------------------------------

def oww_models_dir() -> Path:
    """The directory scripts/samsara.spec collects from:
    site-packages/openwakeword/resources/models of the CURRENT interpreter."""
    import openwakeword  # noqa: PLC0415  (a requirements.txt dependency)

    return Path(openwakeword.__file__).resolve().parent / "resources" / "models"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def oww_model_problems(models_dir: str | Path | None = None) -> list[str]:
    """Missing or hash-mismatched files among OWW_MODELS, as messages."""
    directory = Path(models_dir) if models_dir is not None else oww_models_dir()
    problems = []
    for name, expected in OWW_MODELS.items():
        path = directory / name
        if not path.is_file():
            problems.append(f"missing {name}")
            continue
        actual = _sha256(path)
        if actual != expected:
            problems.append(f"sha256 mismatch {name}: {actual} != {expected}")
    return problems


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 (pinned https URL)
        return resp.read()


def fetch_oww_models(
    models_dir: str | Path | None = None,
    *,
    release_url: str = OWW_MODELS_RELEASE,
    download=_download,
    log=print,
) -> list[str]:
    """Download every missing or mismatched model from the pinned release,
    verify SHA-256 before writing, then re-check the whole set. Returns the
    remaining problems (empty on success). A bad download is never kept."""
    directory = Path(models_dir) if models_dir is not None else oww_models_dir()
    directory.mkdir(parents=True, exist_ok=True)
    problems = []
    for name, expected in OWW_MODELS.items():
        path = directory / name
        if path.is_file() and _sha256(path) == expected:
            log(f"[OWW] present  {name}")
            continue
        url = release_url + name
        try:
            data = download(url)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            problems.append(f"download failed {name}: {exc}")
            log(f"[OWW] FAILED   {name}: {exc}")
            continue
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            problems.append(f"sha256 mismatch for downloaded {name}: {actual} != {expected}")
            log(f"[OWW] REJECTED {name}: sha256 {actual}")
            continue
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        log(f"[OWW] fetched  {name} ({len(data)} bytes, sha256 ok)")
    return problems + [p for p in oww_model_problems(directory) if p not in problems]


def _report_models(problems: list[str], directory: Path) -> int:
    if problems:
        print(f"[FAIL] OpenWakeWord models in {directory}: " + "; ".join(problems))
        print("       run: python tools/release_preflight.py --fetch-oww-models")
        return 1
    print(f"[PASS] all {len(OWW_MODELS)} OpenWakeWord model files present with pinned SHA-256 in {directory}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--oww-models", action="store_true",
                        help="only verify the nine OpenWakeWord model files (presence + SHA-256)")
    parser.add_argument("--fetch-oww-models", action="store_true",
                        help="download missing/mismatched OpenWakeWord models from the pinned release, then verify")
    parser.add_argument("--models-dir", default=None,
                        help="override the models directory (default: the installed openwakeword package)")
    args = parser.parse_args(argv)

    if args.fetch_oww_models or args.oww_models:
        directory = Path(args.models_dir) if args.models_dir else oww_models_dir()
        if args.fetch_oww_models:
            problems = fetch_oww_models(directory)
        else:
            problems = oww_model_problems(directory)
        return _report_models(problems, directory)

    running = running_samsara_processes()
    if running:
        details = ", ".join(f"PID {pid} ({reason})" for pid, reason in running)
        print(f"[FAIL] Close Samsara before building: {details}")
        return 1
    try:
        changes = git_release_blockers()
    except RuntimeError as exc:
        print(f"[FAIL] Could not verify a clean release checkout: {exc}")
        return 1
    if changes:
        preview = "; ".join(changes[:8])
        remainder = len(changes) - 8
        if remainder > 0:
            preview += f"; ... and {remainder} more"
        print(f"[FAIL] Commit the release-source changes before building: {preview}")
        return 1
    directory = Path(args.models_dir) if args.models_dir else oww_models_dir()
    if _report_models(oww_model_problems(directory), directory):
        return 1
    print("[PASS] Samsara is closed and the release checkout is clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
