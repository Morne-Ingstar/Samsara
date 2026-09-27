"""Focused checks for the optional gesture archive and isolated build paths."""

import ast
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "scripts" / "samsara.spec"


def _component_builder(site_packages, distributions):
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_exclude_gesture_member", "_write_gesture_component"}
        or isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_COMPONENT_EXCLUDED_DIRS"
                for target in node.targets)
    ]
    namespace = {
        "Path": Path,
        "zipfile": zipfile,
        "os": os,
        "site_packages": site_packages,
        "metadata": SimpleNamespace(distribution=lambda name: distributions[name]),
        "_GESTURE_DISTRIBUTIONS": tuple(distributions),
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SPEC), "exec"), namespace)
    return namespace["_write_gesture_component"]


def test_component_archive_keeps_runtime_files_and_drops_development_files(tmp_path):
    site_packages = tmp_path / "site-packages"
    files = {
        "mediapipe": {
            "mediapipe/__init__.py": b"# runtime",
            "mediapipe/face.tflite": b"model",
            "mediapipe/__pycache__/__init__.cpython-311.pyc": b"cache",
            "mediapipe/tests/test_face.py": b"test",
            "mediapipe/examples/example.py": b"example",
            "mediapipe/testing/helper.py": b"test helper",
            "mediapipe-1.0.dist-info/METADATA": b"metadata",
            "mediapipe-1.0.dist-info/RECORD": b"record",
        },
        "opencv": {
            "cv2/__init__.py": b"# runtime",
            "cv2/cv2.pyd": b"binary",
            "cv2/docs/README.md": b"docs",
            "cv2/__init__.pyi": b"type stub",
        },
        "certifi": {
            "certifi/cacert.pem": b"CA bundle",
            "certifi/core.py": b"# source",
            "certifi/core.pyc": b"redundant bytecode",
            "certifi/generated.pyc": b"bytecode-only module",
        },
    }
    distributions = {}
    for name, members in files.items():
        for relative, contents in members.items():
            path = site_packages / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        distributions[name] = SimpleNamespace(
            files=[Path(relative) for relative in members],
            locate_file=lambda relative, root=site_packages: root / relative,
        )

    output = tmp_path / "gesture.zip"
    _component_builder(site_packages, distributions)(output)
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
    assert names == {
        "mediapipe/__init__.py", "mediapipe/face.tflite",
        "mediapipe-1.0.dist-info/METADATA", "cv2/__init__.py", "cv2/cv2.pyd",
        "certifi/cacert.pem", "certifi/core.py", "certifi/generated.pyc",
    }


def test_spec_places_pyav_in_core_and_build_script_accepts_temp_root():
    spec = SPEC.read_text(encoding="utf-8")
    tree = ast.parse(spec)
    optional = next(node for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "_GESTURE_DISTRIBUTIONS"
                            for target in node.targets))
    assert "'av'" not in ast.get_source_segment(spec, optional)
    assert "from samsara.components import ComponentNotInstalled" in spec
    assert "import av  # noqa: F401" in spec
    script = (ROOT / "tools" / "build_and_smoke.cmd").read_text(encoding="utf-8")
    assert "SAMSARA_TEMP_BUILD_ROOT" in script
    assert 'set "SAMSARA_BUILD_DIST=%DIST_DIR%"' in script
    assert '--distpath "%DIST_DIR%" --workpath "%WORK_DIR%"' in script
    assert 'tools\\frozen_smoke.py "%DIST_DIR%\\Samsara"' in script
