"""Keep large Python files from growing; works without a Git checkout.

Physical lines are LF terminators plus one for a final unterminated line.
--init is an explicit baseline reset for the owner's commit gate.
--lower only reduces existing ceilings; it never adds or raises one.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath


SOURCE_FOLDERS = ("samsara", "plugins", "tools", "scripts", "perf_artifacts")
FIXTURE = Path("tests/fixtures/size_ceilings.json")
GROWTH_GUIDANCE = (
    "Put new behaviour in a new module, or move at least as many lines out "
    "in the same change. Raising a ceiling is the owner's decision, not yours."
)


def line_count(path: Path) -> int:
    data = path.read_bytes()
    return data.count(b"\n") + int(bool(data) and not data.endswith(b"\n"))


def source_counts(root: Path) -> dict[str, int]:
    paths = list(root.glob("*.py"))
    for folder in SOURCE_FOLDERS:
        paths.extend((root / folder).rglob("*.py"))
    return {
        path.relative_to(root).as_posix(): line_count(path)
        for path in sorted(paths) if path.is_file()
    }


def read_baseline(root: Path) -> dict:
    baseline = json.loads((root / FIXTURE).read_text(encoding="utf-8"))
    for key in ("threshold", "slack"):
        value = baseline[key]
        if type(value) is not int or value < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    if not isinstance(baseline["ceilings"], dict):
        raise ValueError("ceilings must be a mapping")
    for name, ceiling in baseline["ceilings"].items():
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or "\\" in name
                or ":" in name or path.suffix != ".py"
                or (len(path.parts) > 1 and path.parts[0] not in SOURCE_FOLDERS)):
            raise ValueError(f"Invalid source path: {name}")
        if type(ceiling) is not int or ceiling <= baseline["threshold"]:
            raise ValueError(f"{name}: ceiling must exceed the threshold")
    return baseline


def violations(root: Path, baseline: dict) -> list[str]:
    counts = source_counts(root)
    ceilings = baseline["ceilings"]
    errors = []
    for name, ceiling in sorted(ceilings.items()):
        if name not in counts:
            errors.append(f"{name}: listed source file is missing (ceiling {ceiling}).")
            continue
        count = counts[name]
        if count > ceiling:
            errors.append(f"{name}: {count} lines, ceiling {ceiling}. {GROWTH_GUIDANCE}")
        elif ceiling - count > baseline["slack"]:
            errors.append(
                f"{name}: {count} lines, ceiling {ceiling}. "
                "This file shrank: lower its ceiling with tools/size_ratchet.py --lower"
            )
    for name, count in sorted(counts.items()):
        if name not in ceilings and count > baseline["threshold"]:
            errors.append(
                f"{name}: {count} lines, threshold {baseline['threshold']}; "
                "this new big file must be listed on purpose. " + GROWTH_GUIDANCE
            )
    return errors


def lower_baseline(root: Path, baseline: dict) -> dict:
    counts = source_counts(root)
    missing = sorted(set(baseline["ceilings"]) - counts.keys())
    if missing:
        raise ValueError("Listed source files are missing: " + ", ".join(missing))
    return {
        **baseline,
        "ceilings": {
            name: min(ceiling, counts[name])
            for name, ceiling in baseline["ceilings"].items()
            if counts[name] > baseline["threshold"]
        },
    }


def write_baseline(root: Path, baseline: dict) -> None:
    path = root / FIXTURE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report violations; never write")
    mode.add_argument("--lower", action="store_true", help="reduce existing ceilings only")
    mode.add_argument("--init", action="store_true", help="explicitly reset ceilings to this tree")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.init:
            baseline = {
                "threshold": 1500, "slack": 25,
                "ceilings": {name: count for name, count in source_counts(root).items() if count > 1500},
            }
        else:
            baseline = read_baseline(root)
        if args.check:
            errors = violations(root, baseline)
            for error in errors:
                print(error)
            if not errors:
                print(f"Size ratchet passed: {len(baseline['ceilings'])} ceilings checked.")
            return int(bool(errors))
        if args.lower:
            baseline = lower_baseline(root, baseline)
        write_baseline(root, baseline)
        print(f"{'Lowered' if args.lower else 'Initialized'} {len(baseline['ceilings'])} ceilings in {FIXTURE.as_posix()}.")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Size ratchet could not run: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
