"""Fail unless Samsara's release-version declarations agree."""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _string_assignment(path: Path, variable: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == variable for target in node.targets):
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, str) and value:
            return value
    raise ValueError(f"{variable} is not a non-empty string assignment in {path}")


def normalize_version(value: str) -> str:
    value = value.strip()
    return value[1:] if value.lower().startswith("v") else value


def check_versions(project_root: str | Path, expected: str | None = None) -> str:
    root = Path(project_root)
    package_version = _string_assignment(root / "samsara" / "__init__.py", "__version__")
    bridge_version = _string_assignment(
        root / "samsara" / "smart_actions_bridge.py", "SAMSARA_VERSION"
    )
    if package_version != bridge_version:
        raise ValueError(
            f"version mismatch: samsara.__version__={package_version!r}, "
            f"Smart Actions={bridge_version!r}"
        )
    if expected is not None and normalize_version(expected) != package_version:
        raise ValueError(
            f"release identity mismatch: tag/build={expected!r}, embedded={package_version!r}"
        )
    return package_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", help="Expected version or tag, with optional leading v")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 11):
        print(
            f"[FAIL] release builds require Python 3.11; running "
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )
        return 1
    try:
        version = check_versions(args.project_root, args.expected)
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"[PASS] release version identity: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
