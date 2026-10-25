"""Generate Windows version resources from the package's literal version."""

from __future__ import annotations

import ast
from pathlib import Path
import re


PRODUCT_NAME = "Samsara"
FILE_DESCRIPTION = "Samsara voice control and dictation"
COMPANY_NAME = "Samsara"
COPYRIGHT = "Copyright Samsara contributors. Licensed under AGPL-3.0."


def release_version(root: Path) -> str:
    tree = ast.parse((root / "samsara" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                numeric_version(value)
                return value
    raise ValueError("missing literal samsara.__version__")


def numeric_version(version: str) -> tuple[int, int, int, int]:
    """The fourth field is the prerelease counter (zero for a stable release)."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-[A-Za-z]+[.-]?(\d+))?", version)
    if match is None:
        raise ValueError(f"unsupported release version: {version!r}")
    fields = tuple(int(field or 0) for field in match.groups())
    if any(field > 65535 for field in fields):
        raise ValueError("Windows version fields must fit unsigned 16-bit integers")
    return fields


def write_version_resource(root: Path, output: Path, filename: str) -> Path:
    version = release_version(root)
    numeric = numeric_version(version)
    strings = {
        "CompanyName": COMPANY_NAME,
        "FileDescription": FILE_DESCRIPTION,
        "FileVersion": ".".join(map(str, numeric)),
        "InternalName": Path(filename).stem,
        "LegalCopyright": COPYRIGHT,
        "OriginalFilename": filename,
        "ProductName": PRODUCT_NAME,
        "ProductVersion": version,
    }
    entries = ",\n".join(f"            StringStruct({key!r}, {value!r})" for key, value in strings.items())
    text = f"""# Generated from samsara/__init__.py; do not edit.
VSVersionInfo(
    ffi=FixedFileInfo(filevers={numeric!r}, prodvers={numeric!r},
                      mask=0x3f, flags={2 if '-' in version else 0}, OS=0x40004,
                      fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[StringFileInfo([StringTable('040904B0', [
{entries}
    ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return output
