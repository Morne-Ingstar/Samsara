"""Installed onedir identity and icon paths; never bootstrap dictation.py."""

import ast
import os
from pathlib import Path
import re
import shutil
import sys

import pytest
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMainWindow

from samsara.ui import tray_qt


REPO = Path(__file__).resolve().parents[1]


def _tree(path):
    return ast.parse((REPO / path).read_text(encoding="utf-8"))


@pytest.fixture
def frozen_root(tmp_path, monkeypatch):
    """Copy the spec's actual icon datas into PyInstaller 6's _internal."""
    internal = tmp_path / "Samsara" / "_internal"
    for node in ast.walk(_tree("scripts/samsara.spec")):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and ast.unparse(node.func) == "datas.append"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Tuple):
            continue
        source, dest = node.args[0].elts
        if not isinstance(dest, ast.Constant) or dest.value not in ("assets/brand", "assets/icon"):
            continue
        source = Path(eval(compile(ast.Expression(source), "spec-icon-data", "eval"),
                           {"app_dir": REPO, "str": str}))
        target = internal / dest.value / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(internal.parent / "Samsara.exe"))
    return internal


def test_every_runtime_icon_path_exists_in_installed_onedir(qapp, frozen_root):
    # Execute only the two path assignments from startup, without importing it.
    assignments = []
    for node in ast.walk(_tree("dictation.py")):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in ("_icon_base", "_icon_ico_path")
                for t in node.targets):
            assignments.append(node)
    assert len(assignments) == 2
    namespace = {"sys": sys, "os": os, "__file__": str(frozen_root / "dictation.py")}
    exec(compile(ast.Module(body=sorted(assignments, key=lambda n: n.lineno),
                            type_ignores=[]), "startup-icon-path", "exec"), namespace)
    paths = (Path(namespace["_icon_ico_path"]), tray_qt.idle_brand_icon_path(),
             tray_qt.mark_svg_path())
    for path in paths:
        assert path.is_relative_to(frozen_root)
        assert path.is_file(), path
        assert not QIcon(str(path)).isNull(), path
    assert paths[0] == paths[1]


def test_hub_sets_its_own_nonblank_icon_on_qt_thread(qapp, frozen_root):
    # Exercise the real constructor's icon statement on a Qt window, avoiding
    # unrelated hub panels and any DictationApp construction.
    cls = next(n for n in _tree("samsara/ui/main_window_qt.py").body
               if isinstance(n, ast.ClassDef) and n.name == "_MainWindow")
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    icon_calls = [n for n in init.body if isinstance(n, ast.Expr)
                  and isinstance(n.value, ast.Call)
                  and isinstance(n.value.func, ast.Attribute)
                  and n.value.func.attr == "setWindowIcon"]
    assert len(icon_calls) == 1
    window = QMainWindow()
    window.setWindowIcon(QIcon())
    try:
        exec(compile(ast.Module(body=icon_calls, type_ignores=[]), "hub-icon", "exec"),
             {"self": window, "QIcon": QIcon,
              "idle_brand_icon_path": tray_qt.idle_brand_icon_path})
        for size in (16, 24, 32, 48, 256):
            assert not window.windowIcon().pixmap(size, size).isNull()
    finally:
        window.close()


def test_shortcuts_match_process_identity_and_use_embedded_exe_icon():
    calls = [n for n in ast.walk(_tree("dictation.py")) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "SetCurrentProcessExplicitAppUserModelID"]
    assert len(calls) == 1
    app_id = ast.literal_eval(calls[0].args[0])
    installer = (REPO / "installer/samsara.iss").read_text(encoding="utf-8")
    shortcuts = installer.split("[Icons]", 1)[1].split("[Tasks]", 1)[0]
    entries = [line for line in shortcuts.splitlines() if line.startswith("Name:")]
    assert len(entries) == 2
    for entry in entries:
        assert re.search(r'AppUserModelID:\s*"([^"]+)"', entry).group(1) == app_id
        assert 'IconFilename: "{app}\\{#AppExeName}"' in entry
        assert 'Filename: "{app}\\{#AppExeName}"' in entry
    assert 'icon=str(app_dir / \'assets\' / \'brand\' / \'samsara.ico\')' in (
        REPO / "scripts/samsara.spec").read_text(encoding="utf-8")


def test_source_icon_paths_remain_valid(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert tray_qt.idle_brand_icon_path() == REPO / "assets/brand/samsara.ico"
    assert tray_qt.mark_svg_path() == REPO / "assets/icon/samsara.svg"
    assert tray_qt.idle_brand_icon_path().is_file()
    assert tray_qt.mark_svg_path().is_file()
