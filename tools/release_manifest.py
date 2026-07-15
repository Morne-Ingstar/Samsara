"""Deterministic project-data manifests for PyInstaller release builds."""

from __future__ import annotations

import subprocess
from pathlib import Path


def tracked_tree_datas(
    project_root: str | Path,
    source_dir: str,
    destination_dir: str,
) -> list[tuple[str, str]]:
    """Return PyInstaller ``datas`` entries for one project directory.

    The Git index is the release manifest, so ignored and untracked workstation
    files can never leak into a local package. This deliberately fails closed
    outside a Git checkout: a release must be built from traceable source, not
    from an arbitrary directory whose provenance cannot be verified.
    """

    root = Path(project_root).resolve()
    source_root = root / source_dir
    if not source_root.is_dir():
        raise FileNotFoundError(f"release data directory does not exist: {source_root}")

    if not (root / ".git").exists():
        raise RuntimeError(f"release build requires a Git checkout: {root}")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", source_dir],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"could not execute Git for release manifest: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"could not read Git release manifest: {detail}")
    relative_files = [
        Path(raw.decode("utf-8"))
        for raw in result.stdout.split(b"\0")
        if raw
    ]
    if not relative_files:
        raise RuntimeError(f"Git release manifest is empty for {source_dir!r}")

    entries = []
    source_prefix = Path(source_dir)
    destination_prefix = Path(destination_dir)
    for relative in sorted(relative_files, key=lambda value: value.as_posix()):
        absolute = root / relative
        if not absolute.is_file():
            raise FileNotFoundError(f"tracked release file is missing: {absolute}")
        nested_parent = relative.relative_to(source_prefix).parent
        destination = destination_prefix / nested_parent
        entries.append((str(absolute), str(destination)))
    return entries


__all__ = ["tracked_tree_datas"]
