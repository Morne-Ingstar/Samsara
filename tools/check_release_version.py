"""Fail unless Samsara's release-version declarations agree."""

from __future__ import annotations

import argparse
import ast
import ctypes
from ctypes import wintypes
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


def _derived_version_assignment(path: Path, variable: str) -> None:
    """Require a bridge version assignment derived from package __version__."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == variable for target in node.targets):
            continue
        value = node.value
        if isinstance(value, ast.Name) and value.id == "__version__":
            return
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "__version__"
            and isinstance(value.value, ast.Name)
            and value.value.id == "samsara"
        ):
            return
        raise ValueError(
            f"{variable} must derive from samsara.__version__ in {path}"
        )
    raise ValueError(
        f"{variable} must derive from samsara.__version__ in {path}"
    )


def normalize_version(value: str) -> str:
    value = value.strip()
    return value[1:] if value.lower().startswith("v") else value


def check_versions(project_root: str | Path, expected: str | None = None) -> str:
    root = Path(project_root)
    package_version = _string_assignment(root / "samsara" / "__init__.py", "__version__")
    _derived_version_assignment(
        root / "samsara" / "smart_actions_bridge.py", "SAMSARA_VERSION"
    )
    if expected is not None and normalize_version(expected) != package_version:
        raise ValueError(
            f"release identity mismatch: tag/build={expected!r}, embedded={package_version!r}"
        )
    return package_version


def read_pe_version(path: str | Path) -> dict[str, str]:
    """Read PE resources without loading or executing the application."""
    api = ctypes.WinDLL("version", use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    api.GetFileVersionInfoW.restype = wintypes.BOOL
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    api.VerQueryValueW.restype = wintypes.BOOL
    filename = str(Path(path).resolve())
    ignored = wintypes.DWORD()
    size = api.GetFileVersionInfoSizeW(filename, ctypes.byref(ignored))
    if not size:
        raise ValueError(f"missing PE version resource: {path}")
    data = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(filename, 0, size, data):
        raise ValueError(f"unreadable PE version resource: {path}")

    def query(key):
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not api.VerQueryValueW(data, key, ctypes.byref(pointer), ctypes.byref(length)):
            raise ValueError(f"missing PE version field {key}: {path}")
        return pointer, length.value

    pointer, length = query(r"\VarFileInfo\Translation")
    if length < 4 or length % 4:
        raise ValueError(f"invalid PE version translation table: {path}")
    translations = ctypes.cast(pointer, ctypes.POINTER(wintypes.WORD))
    result = {}
    for index in range(0, length // 2, 2):
        prefix = f"\\StringFileInfo\\{translations[index]:04x}{translations[index + 1]:04x}\\"
        for field in ("ProductName", "ProductVersion", "FileVersion", "CompanyName"):
            pointer, count = query(prefix + field)
            value = ctypes.wstring_at(pointer, count).rstrip("\0")
            if field in result and result[field] != value:
                raise ValueError(f"conflicting PE {field} translations: {path}")
            result[field] = value
    pointer, length = query("\\")
    if length < 52:
        raise ValueError(f"invalid fixed PE version info: {path}")
    fixed = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    if fixed[0] != 0xFEEF04BD:
        raise ValueError(f"invalid fixed PE version signature: {path}")
    for field, offset in (("FixedFileVersion", 2), ("FixedProductVersion", 4)):
        result[field] = ".".join(map(str, (fixed[offset] >> 16, fixed[offset] & 65535,
                                         fixed[offset + 1] >> 16, fixed[offset + 1] & 65535)))
    return result


def check_pe_version(path: str | Path, version: str) -> None:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from scripts.version_info import numeric_version

    numeric = ".".join(map(str, numeric_version(version)))
    expected = {"ProductName": "Samsara", "ProductVersion": version,
                "FileVersion": numeric, "CompanyName": "Samsara",
                "FixedFileVersion": numeric, "FixedProductVersion": numeric}
    actual = read_pe_version(path)
    for field, value in expected.items():
        if actual.get(field) != value:
            raise ValueError(f"PE {field} mismatch in {path}: {actual.get(field)!r} != {value!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", help="Expected version or tag, with optional leading v")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--exe", action="append", default=[], help="Built PE to verify (repeatable)")
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 11):
        print(
            f"[FAIL] release builds require Python 3.11; running "
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )
        return 1
    try:
        version = check_versions(args.project_root, args.expected)
        for executable in args.exe:
            check_pe_version(executable, version)
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"[PASS] release version identity: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
