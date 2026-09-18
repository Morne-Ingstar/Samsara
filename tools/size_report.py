"""Report the size of a PyInstaller onedir tree or ZIP by runtime group."""

from __future__ import annotations

import argparse
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path


MIB = 1024 * 1024


def group_for(relative: Path) -> str:
    parts = relative.parts
    if not parts:
        return "[root]"
    if parts[0] == "_internal" and len(parts) > 1:
        return f"_internal/{parts[1]}"
    return parts[0]


def _zip_root(names: list[str]) -> str:
    first = {name.split("/", 1)[0] for name in names if "/" in name}
    return next(iter(first)) if len(first) == 1 else ""


def report(path: Path) -> dict:
    """Return unpacked/compressed totals, groups, and largest files for *path*."""
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            root = _zip_root([info.filename for info in infos])
            rows = []
            for info in infos:
                name = info.filename.removeprefix(f"{root}/") if root else info.filename
                rows.append((Path(name), info.file_size, info.compress_size))
        zip_size = path.stat().st_size
    elif path.is_dir():
        files = [file for file in path.rglob("*") if file.is_file()]
        temporary = tempfile.NamedTemporaryFile(prefix="samsara_size_", suffix=".zip", delete=False)
        temporary.close()
        try:
            with zipfile.ZipFile(temporary.name, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for file in files:
                    archive.write(file, file.relative_to(path).as_posix())
            with zipfile.ZipFile(temporary.name) as archive:
                compressed = {Path(info.filename): info.compress_size
                              for info in archive.infolist() if not info.is_dir()}
            rows = [(file.relative_to(path), file.stat().st_size,
                     compressed[file.relative_to(path)]) for file in files]
            zip_size = Path(temporary.name).stat().st_size
        finally:
            Path(temporary.name).unlink(missing_ok=True)
    else:
        raise ValueError(f"not a directory or ZIP: {path}")

    groups = defaultdict(lambda: [0, 0])
    for relative, unpacked, compressed in rows:
        bucket = groups[group_for(relative)]
        bucket[0] += unpacked
        bucket[1] += unpacked if compressed is None else compressed
    return {
        "path": str(path),
        "unpacked": sum(unpacked for _name, unpacked, _compressed in rows),
        "compressed": sum(compressed if compressed is not None else unpacked
                          for _name, unpacked, compressed in rows),
        "zip_size": zip_size,
        "groups": dict(groups),
        "largest": sorted(rows, key=lambda row: row[1], reverse=True)[:30],
    }


def _mb(size: int | None) -> str:
    return "n/a" if size is None else f"{size / MIB:,.1f}"


def print_report(result: dict) -> None:
    print(f"Size report: {result['path']}")
    print(f"Unpacked: {_mb(result['unpacked'])} MiB")
    print(f"Compressed contents: {_mb(result['compressed'])} MiB")
    print(f"ZIP size: {_mb(result['zip_size'])} MiB")
    print("\nTop-level runtime groups (MiB):")
    print(f"{'group':<36} {'unpacked':>12} {'compressed':>12}")
    for name, (unpacked, compressed) in sorted(result["groups"].items(), key=lambda item: item[1][0], reverse=True):
        print(f"{name:<36} {_mb(unpacked):>12} {_mb(compressed):>12}")
    print("\n30 largest files (unpacked MiB):")
    for relative, unpacked, _compressed in result["largest"]:
        print(f"{_mb(unpacked):>10}  {relative.as_posix()}")


def print_compare(before: dict, after: dict) -> None:
    print(f"Compare: {before['path']} -> {after['path']}")
    print(f"{'group':<36} {'before':>10} {'after':>10} {'delta':>10}")
    names = set(before["groups"]) | set(after["groups"])
    for name in sorted(names, key=lambda key: after["groups"].get(key, [0])[0] - before["groups"].get(key, [0])[0]):
        old = before["groups"].get(name, [0, 0])[0]
        new = after["groups"].get(name, [0, 0])[0]
        print(f"{name:<36} {_mb(old):>10} {_mb(new):>10} {_mb(new - old):>10}")
    print(f"{'TOTAL unpacked':<36} {_mb(before['unpacked']):>10} {_mb(after['unpacked']):>10} {_mb(after['unpacked'] - before['unpacked']):>10}")
    print(f"{'TOTAL compressed':<36} {_mb(before['compressed']):>10} {_mb(after['compressed']):>10} {_mb(after['compressed'] - before['compressed']):>10}")
    print(f"{'ZIP size':<36} {_mb(before['zip_size']):>10} {_mb(after['zip_size']):>10} {_mb((after['zip_size'] or 0) - (before['zip_size'] or 0)):>10}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, help="dist directory or ZIP")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"), type=Path)
    args = parser.parse_args(argv)
    if args.compare:
        print_compare(report(args.compare[0]), report(args.compare[1]))
        return 0
    if args.path is None:
        parser.error("path or --compare is required")
    print_report(report(args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
