"""Refuse a local release build while source or frozen Samsara is running."""

from __future__ import annotations

import os
import subprocess
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


def main() -> int:
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
    print("[PASS] Samsara is closed and the release checkout is clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
