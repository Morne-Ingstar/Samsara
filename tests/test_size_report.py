"""Queue 211: the release-size report groups a PyInstaller runtime correctly."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("size_report", ROOT / "tools" / "size_report.py")
size_report = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(size_report)


def test_runtime_groups_split_internal_packages_and_root_files(tmp_path):
    (tmp_path / "Samsara.exe").write_bytes(b"x" * 3)
    package = tmp_path / "_internal" / "PySide6"
    package.mkdir(parents=True)
    (package / "Qt6Core.dll").write_bytes(b"x" * 5)

    result = size_report.report(tmp_path)

    assert result["unpacked"] == 8
    assert result["groups"]["Samsara.exe"][0] == 3
    assert result["groups"]["_internal/PySide6"][0] == 5
    assert result["zip_size"] is not None


def test_spec_keeps_only_the_four_imported_qt_bindings():
    source = (ROOT / "scripts" / "samsara.spec").read_text(encoding="utf-8")

    assert "pyside6_datas, pyside6_binaries, pyside6_hiddenimports = collect_all('PySide6')" not in source
    for module in ("QtCore", "QtGui", "QtWidgets", "QtSvg"):
        assert f"'PySide6.{module}'" in source
    assert "SAMSARA_FROZEN_IMPORT_CHECK" in source
