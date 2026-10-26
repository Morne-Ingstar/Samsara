"""Locked add-on installs, rollback, early restart completion and core-free packs.

All files are synthetic under tmp_path; no app bootstrap, hardware or network.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pytest

from samsara import components as c
from samsara.ui import first_run_qt as fq
from tools import gen_release_manifest as manifest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "scripts" / "samsara.spec"
PIL = "PIL/_imaging.cp311-win_amd64.pyd"
ADDONS = [s for s in manifest.component_specs("v9.9.9") if s["id"] != "core"]


@pytest.fixture(params=ADDONS, ids=lambda spec: spec["id"])
def addon(request):
    # command-model is not offered for installation yet. Its future install_dir
    # is exercised with synthetic bytes, without claiming a released file list.
    spec = request.param
    shared = {"gesture-control": PIL, "cuda-pack": "cudart64_12.dll",
              "wake-word-models": "embedding_model.onnx", "command-model": "weights.bin"}[spec["id"]]
    return spec, shared


def archive_bytes(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


def component(blob, spec=None):
    entry = dict(id="gesture-control", name="Gesture control", description="Webcam hand poses.",
                kind="feature", source="built", available=True, install_dir="_internal",
                requires=[], default=False, filename="gesture.zip", size_bytes=len(blob),
                sha256=hashlib.sha256(blob).hexdigest(), min_disk_bytes=2 * len(blob),
                url="https://example.invalid/gesture.zip")
    if spec is not None:
        entry.update(spec)
        entry.update(available=True, filename="gesture.zip", url="https://example.invalid/gesture.zip")
    return entry


def write(root, relative, data):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def assert_no_install_temp(root):
    assert not list(root.rglob(".samsara-install-*"))
    assert not list(root.rglob(".samsara-stage-*"))


@contextmanager
def locked(path):
    """A real Windows read handle permitting reads but denying replacement."""
    if sys.platform != "win32":
        with path.open("rb"):
            yield
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def restart_hook(tmp_path):
    """Execute the actual spec's hook-generation statements without PyInstaller."""
    selected = []
    for node in ast.parse(SPEC.read_text(encoding="utf-8")).body:
        text = ast.unparse(node)
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_component_restart_hook"
                                                for t in node.targets)
                or isinstance(node, ast.Expr) and text.startswith("_component_restart_hook.")):
            selected.append(node)
    ns = {"Path": Path, "app_dir": ROOT, "_build_work": tmp_path / "build"}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SPEC), "exec"), ns)
    hook = ns["_component_restart_hook"]
    return compile(hook.read_text(encoding="utf-8"), str(hook), "exec")


def run_restart(tmp_path, app, monkeypatch):
    hook = restart_hook(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "frozen", True, raising=False)
        patch.setattr(sys, "executable", str(app / "Samsara.exe"))
        exec(hook, {"__name__": "component_restart_proof"})


def test_identical_real_locked_file_is_skipped_and_install_succeeds(tmp_path, monkeypatch, addon):
    spec, relative = addon
    app, downloads = tmp_path / "app", tmp_path / "downloads"
    target = app / spec["install_dir"]
    shared = write(target, relative, b"same core bytes")
    blob = archive_bytes({relative: shared.read_bytes(), "addon-new.bin": b"addon"})
    comp = component(blob, spec)
    replace = os.replace
    attempted = []

    def deny_shared(source, dest):
        attempted.append(Path(dest))
        if Path(dest) == shared:
            raise PermissionError(5, "Access is denied", str(dest))
        return replace(source, dest)

    monkeypatch.setattr(os, "replace", deny_shared)
    with locked(shared):
        installed = fq.install_component(io.BytesIO(blob), comp, app, downloads)
    assert set(installed) == {relative, "addon-new.bin"}
    assert shared not in attempted
    assert shared.read_bytes() == b"same core bytes"
    assert (target / "addon-new.bin").read_bytes() == b"addon"
    assert fq.component_installed(comp, app, downloads)
    assert not c.pending_component(app, comp).exists()
    assert_no_install_temp(app)


@pytest.mark.parametrize("hold_lock", [False, True], ids=["different-unlocked", "different-locked"])
def test_differing_core_is_staged_untouched_then_completed_by_actual_hook(tmp_path, monkeypatch, hold_lock, addon):
    spec, relative = addon
    app, downloads = tmp_path / "app", tmp_path / "downloads"
    target = app / spec["install_dir"]
    shared = write(target, relative, b"old core")
    before = snapshot(app / "_internal")
    blob = archive_bytes({"addon-new.bin": b"addon", relative: b"updated core"})
    comp = component(blob, spec)

    def install():
        with pytest.raises(c.ComponentRestartRequired, match="Restart Samsara to finish installing"):
            fq.install_component(io.BytesIO(blob), comp, app, downloads)

    if hold_lock:
        with locked(shared):
            install()
    else:
        install()
    assert snapshot(app / "_internal") == before
    pending = c.pending_component(app, comp)
    assert (pending / "payload" / relative).read_bytes() == b"updated core"
    assert not fq.component_installed(comp, app, downloads)
    assert not (downloads / (comp["id"] + ".installed")).exists()
    assert_no_install_temp(app)
    run_restart(tmp_path, app, monkeypatch)
    assert shared.read_bytes() == b"updated core"
    assert (target / "addon-new.bin").read_bytes() == b"addon"
    assert fq.component_installed(comp, app, downloads)
    assert not pending.exists()
    assert_no_install_temp(app)


@pytest.mark.parametrize("permission", [False, True], ids=["disk-error", "access-denied"])
@pytest.mark.parametrize("where", ["payload", "marker"])
def test_mid_commit_failure_rolls_back_all_new_files_and_preserves_core(tmp_path, monkeypatch, permission, where):
    app, downloads = tmp_path / "app", tmp_path / "downloads"
    write(app / "_internal", PIL, b"unchanged core")
    before = snapshot(app / "_internal")
    blob = archive_bytes({"mediapipe/nested/first.py": b"one", "cv2/cv2.pyd": b"two"})
    comp = component(blob)
    replace = os.replace

    def fail_second(source, dest):
        failing = app / "_internal/cv2/cv2.pyd" if where == "payload" else downloads / "gesture-control.installed"
        if Path(dest) == failing:
            if permission:
                raise PermissionError(5, "Access is denied", str(dest))
            raise OSError(28, "No space left", str(dest))
        return replace(source, dest)

    monkeypatch.setattr(os, "replace", fail_second)
    error = c.ComponentRestartRequired if permission else c.ComponentInstallError
    with pytest.raises(error) as caught:
        fq.install_component(io.BytesIO(blob), comp, app, downloads)
    assert "WinError" not in str(caught.value) and str(app) not in str(caught.value)
    assert snapshot(app / "_internal") == before
    assert not (app / "_internal/mediapipe").exists()
    assert not (app / "_internal/cv2").exists()
    assert c.pending_component(app, comp).exists() == permission
    assert not (downloads / "gesture-control.installed").exists()
    assert_no_install_temp(app)
    if permission:
        monkeypatch.setattr(os, "replace", replace)
        run_restart(tmp_path, app, monkeypatch)
        assert fq.component_installed(comp, app, downloads)


def test_still_locked_restart_rolls_back_new_files_and_retries_safely(tmp_path, monkeypatch):
    app, downloads = tmp_path / "app", tmp_path / "downloads"
    shared = write(app / "_internal", PIL, b"old")
    write(app / "_internal", "PIL/helper.pyd", b"old helper")
    before = snapshot(app / "_internal")
    blob = archive_bytes({"PIL/helper.pyd": b"new helper", "mediapipe/new.py": b"addon", PIL: b"new"})
    comp = component(blob)
    with pytest.raises(c.ComponentRestartRequired):
        fq.install_component(io.BytesIO(blob), comp, app, downloads)
    replace = os.replace

    def deny(source, dest):
        if Path(dest) == shared:
            raise PermissionError(5, "Access is denied", str(dest))
        return replace(source, dest)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", deny)
        run_restart(tmp_path, app, patch)
    assert snapshot(app / "_internal") == before
    assert not fq.component_installed(comp, app, downloads)
    assert c.pending_install_message(app, comp) == c.PENDING_FAILED_MESSAGE
    assert_no_install_temp(app)
    run_restart(tmp_path, app, monkeypatch)
    assert fq.component_installed(comp, app, downloads)


@pytest.mark.parametrize("change", ["payload", "core", "traversal"])
def test_staged_tampering_or_newer_core_is_refused_before_any_writes(tmp_path, monkeypatch, change):
    app, downloads = tmp_path / "app", tmp_path / "downloads"
    shared = write(app / "_internal", PIL, b"old")
    blob = archive_bytes({PIL: b"new", "mediapipe/new.py": b"addon"})
    comp = component(blob)
    with pytest.raises(c.ComponentRestartRequired):
        fq.install_component(io.BytesIO(blob), comp, app, downloads)
    pending = c.pending_component(app, comp)
    if change == "payload":
        (pending / "payload" / PIL).write_bytes(b"tampered")
    elif change == "core":
        shared.write_bytes(b"newer core from another install")
    else:
        receipt = json.loads((pending / "receipt.json").read_text())
        receipt["files"][0]["path"] = "../../outside"
        (pending / "receipt.json").write_text(json.dumps(receipt))
    before = snapshot(app / "_internal")
    run_restart(tmp_path, app, monkeypatch)
    assert snapshot(app / "_internal") == before
    assert (pending / "failed").read_text() == c.PENDING_FAILED_MESSAGE
    assert not fq.component_installed(comp, app, downloads)
    assert_no_install_temp(app)


def test_cancel_during_extraction_removes_temp_and_preserves_core(tmp_path):
    app = tmp_path / "app"
    write(app / "_internal", PIL, b"core")
    before = snapshot(app / "_internal")
    blob = archive_bytes({PIL: b"different", "mediapipe/new.py": b"addon"})
    with pytest.raises(fq.FetchCancelled):
        fq.install_component(io.BytesIO(blob), component(blob), app, tmp_path / "downloads", cancel=lambda: True)
    assert snapshot(app / "_internal") == before
    assert_no_install_temp(app)


def test_restart_hook_precedes_app_import_hook():
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    analysis = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "a" for t in node.targets))
    hooks = next(k.value for k in analysis.keywords if k.arg == "runtime_hooks")
    assert "_component_restart_hook" in ast.unparse(hooks.elts[0])
    assert "_runtime_import_hook" in ast.unparse(hooks.elts[1])


@pytest.mark.parametrize("cannot_stage", [False, True], ids=["restart", "stage-write-denied"])
def test_headless_restart_is_a_clear_result_without_raw_exception(tmp_path, monkeypatch, cannot_stage, addon):
    spec, relative = addon
    app, downloads = tmp_path / "app", tmp_path / "downloads"
    write(app / spec["install_dir"], relative, b"old")
    blob = archive_bytes({relative: b"new", "addon-new.bin": b"addon"})
    comp = component(blob, spec)
    if cannot_stage:
        replace = os.replace

        def deny_stage(source, dest):
            if Path(dest) == c.pending_component(app, comp):
                raise PermissionError(5, "Access is denied", str(dest))
            return replace(source, dest)

        monkeypatch.setattr(os, "replace", deny_stage)
    catalog = dict(schema_version=1, app_version="9.9.9", release_tag="v9.9.9", build_date="2026-10-16",
                   git_sha="0" * 40, components=[comp])
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(catalog))
    write(tmp_path / "archives", "gesture.zip", blob)
    out = io.StringIO()
    rc = fq.fetch_components_main(
        ["--fetch-components", comp["id"], "--manifest", str(path), "--app-root", str(app),
         "--downloads-dir", str(downloads), "--components-dir", str(tmp_path / "archives")], out=out)
    assert rc == fq.EXIT_FAILED
    expected = ("[FAIL] " + comp["id"] + ": " + c.INSTALL_FAILED_MESSAGE if cannot_stage
                else "[RESTART] " + comp["id"] + ": " + c.RESTART_INSTALL_MESSAGE)
    assert expected in out.getvalue()
    assert "Traceback" not in out.getvalue() and "WinError" not in out.getvalue()
    assert (downloads / "gesture.zip").read_bytes() == blob
    assert_no_install_temp(app)


@pytest.mark.parametrize("restart", [True, False], ids=["restart", "generic-failure"])
def test_component_page_shows_clear_status_and_survives_refresh(qapp, tmp_path, monkeypatch, restart, addon):
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    from samsara.ui import theme

    spec, relative = addon
    app, downloads = tmp_path / "app", tmp_path / "downloads"
    write(app / spec["install_dir"], relative, b"old")
    blob = archive_bytes({relative: b"new", "addon-new.bin": b"addon"})
    comp = component(blob, spec)
    host = QWidget()
    host.setStyleSheet(theme.build_stylesheet())
    page = fq.ComponentsPage(host, on_later=lambda: None, manifest_loader=lambda: {"components": [comp]},
                             app_root=app, downloads_dir=downloads, run_in_thread=False, gpu_probe=lambda: True)
    QVBoxLayout(host).addWidget(page.widget)
    if restart:
        monkeypatch.setattr(fq, "fetch_and_install", lambda *_a, **_kw: fq.install_component(io.BytesIO(blob), comp, app, downloads))
    else:
        def fail(*_a, **_kw):
            raise PermissionError(5, "Access is denied: synthetic/PIL/_imaging.pyd")
        monkeypatch.setattr(fq, "fetch_and_install", fail)
    page.refresh()
    page.start_fetch(comp["id"])
    row = page._rows[comp["id"]]
    assert row["result"].text() == (c.RESTART_INSTALL_MESSAGE if restart else c.INSTALL_FAILED_MESSAGE)
    assert row["get"].isEnabled() != restart
    assert not row["cancel"].isVisible() and not row["bar"].isVisible()
    if restart:
        assert row["get"].text() == "Restart needed"
        page.refresh()
        row = page._rows[comp["id"]]
        assert row["result"].text() == c.RESTART_INSTALL_MESSAGE
        assert not row["get"].isEnabled()
        host.resize(780, 560)
        host.show()
        qapp.processEvents()
        assert host.grab().save(str(tmp_path / "gesture_restart.png"))
    host.close()
    assert_no_install_temp(app)


def builder(site_packages, distributions):
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if (
        isinstance(node, ast.FunctionDef) and node.name in {"_exclude_gesture_member", "_write_gesture_component"}
        or isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_COMPONENT_EXCLUDED_DIRS"
                                                for t in node.targets))]
    namespace = dict(Path=Path, zipfile=zipfile, os=os, site_packages=site_packages,
                     metadata=SimpleNamespace(distribution=lambda name: distributions[name]),
                     _GESTURE_DISTRIBUTIONS=tuple(distributions))
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SPEC), "exec"), namespace)
    return namespace["_write_gesture_component"]


def wheels(site):
    files = {"mediapipe": {"mediapipe/__init__.py": b"gesture"},
             "opencv": {"cv2/cv2.pyd": b"camera"},
             "pillow": {PIL: b"imaging", "PIL/Image.py": b"image", "PIL/GestureOnly.py": b"extra"},
             "six": {"six.py": b"six"},
             "protobuf": {"google/protobuf/message.py": b"protobuf"},
             "attrs": {"attrs/__init__.py": b"attrs"}}
    distributions = {}
    for name, members in files.items():
        for relative, contents in members.items():
            write(site, relative, contents)
        distributions[name] = SimpleNamespace(files=[Path(p) for p in members], locate_file=lambda p: site / p)
    return distributions


def test_pack_excludes_all_identical_core_and_pyz_members_and_audits_hashes(tmp_path):
    site, core = tmp_path / "site", tmp_path / "core"
    distributions = wheels(site)
    write(core, PIL, b"imaging")
    write(core, "google/protobuf/message.py", b"protobuf")
    pure = [("PIL.Image", str(site / "PIL/Image.py"), "PYMODULE"),
            ("six", str(site / "six.py"), "PYMODULE"),
            ("attrs", str(site / "attrs/__init__.py"), "PYMODULE"),
            ("google", None, "PYMODULE"), ("namespace", "-", "PYMODULE"), ("empty", "", "PYMODULE")]
    output = tmp_path / "gesture.zip"
    builder(site, distributions)(output, core, pure)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"mediapipe/__init__.py", "cv2/cv2.pyd", "PIL/GestureOnly.py"}
    audit = json.loads(output.with_suffix(".overlap.json").read_text())
    assert {r["path"] for r in audit} == {PIL, "PIL/Image.py", "six.py", "google/protobuf/message.py", "attrs/__init__.py"}
    assert all(r["sha256"] == r["core_sha256"] == hashlib.sha256((site / r["path"]).read_bytes()).hexdigest() for r in audit)
    assert {r["core_storage"] for r in audit} == {"_internal", "PYZ"}
    inventory = json.loads(output.with_suffix(".inventory.json").read_text())
    assert {r["path"] for r in inventory} == {str(p).replace("\\", "/")
                                             for dist in distributions.values() for p in dist.files}
    assert {r["path"] for r in inventory if r["disposition"] == "omitted-identical"} == {r["path"] for r in audit}
    assert {r["path"] for r in inventory if r["disposition"] == "shipped"} == {
        "mediapipe/__init__.py", "cv2/cv2.pyd", "PIL/GestureOnly.py"}
    gesture = next(s for s in manifest.component_specs("v9.9.9") if s["id"] == "gesture-control")
    gesture["filename"] = output.name
    doc = manifest.build_manifest(artifacts_dir=tmp_path, tag="v9.9.9", repo="a/b", git_sha="0" * 40,
                                  build_date="2026-10-16", app_version="9.9.9", specs=[gesture])
    assert doc["components"][0]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert doc["components"][0]["size_bytes"] == output.stat().st_size


@pytest.mark.parametrize("storage", ["_internal", "PYZ", "missing-core"])
def test_pack_refuses_differing_core_bytes_or_missing_core_and_keeps_old_zip(tmp_path, storage):
    site, core = tmp_path / "site", tmp_path / "core"
    distributions = wheels(site)
    pure = []
    if storage == "_internal":
        write(core, PIL, b"different")
    elif storage == "PYZ":
        core.mkdir()
        source = write(tmp_path / "other", "Image.py", b"different")
        pure = [("PIL.Image", str(source), "PYMODULE")]
    output = write(tmp_path, "gesture.zip", b"previous complete archive")
    with pytest.raises(RuntimeError, match="differ|missing"):
        builder(site, distributions)(output, core, pure)
    assert output.read_bytes() == b"previous complete archive"
    assert not output.with_suffix(".zip.tmp").exists()


def test_release_inventory_covers_every_manifest_addon_and_compares_actual_target_paths(tmp_path):
    specs = manifest.component_specs("v9.9.9")
    core_spec = next(s for s in specs if s["id"] == "core")
    core, expected = {}, {}
    for spec in ADDONS:
        if not spec["available"]:
            continue
        names = {"wake-word-models": sorted(manifest.OWW_MODELS),
                 "cuda-pack": ["cudart64_12.dll", "cublas64_12.dll"],
                 "gesture-control": [PIL, "mediapipe/__init__.py", "cv2/cv2.pyd"]}[spec["id"]]
        payload = {name: (spec["id"] + name).encode() for name in names}
        write(tmp_path, spec["filename"], archive_bytes(payload))
        expected[spec["id"]] = set(payload)
        for i, (name, data) in enumerate(payload.items()):
            if i < 2:
                core[str(Path(spec["install_dir"]) / name).replace("\\", "/")] = data if i == 0 else b"older core"
    write(tmp_path, core_spec["filename"], archive_bytes(core))
    audit = manifest.audit_addon_overlaps({"components": specs}, tmp_path)
    rows = {r["id"]: r for r in audit["components"]}
    assert set(rows) == {s["id"] for s in ADDONS}
    assert rows["command-model"] == dict(id="command-model", install_dir="_internal/models/command",
                                          status="unavailable", files=[])
    # 917: the legacy CUDA pack is withheld, so it has no offered file inventory.
    assert rows["cuda-pack"] == dict(id="cuda-pack", install_dir="_internal/ctranslate2",
                                      status="unavailable", files=[])
    for spec in ADDONS:
        if not spec["available"]:
            continue
        files = rows[spec["id"]]["files"]
        assert {r["path"] for r in files} == expected[spec["id"]]
        assert sum(r["overlap"] == "identical" for r in files) == 1
        assert sum(r["overlap"] == "different" for r in files) == 1
        for record in files:
            assert record["target"] == (Path(spec["install_dir"]) / record["path"]).as_posix()
            if record["overlap"] == "identical":
                assert record["sha256"] == record["core_sha256"]
            elif record["overlap"] == "different":
                assert record["sha256"] != record["core_sha256"]
            else:
                assert record["core_sha256"] is None
    # 917: missing OFFERED artifacts remain unknown, never reported as core-free.
    for spec in specs:
        if spec["id"] == "core" or not spec["available"]:
            continue
        path = tmp_path / spec["filename"]
        payload = path.read_bytes()
        path.unlink()
        again = manifest.audit_addon_overlaps({"components": specs}, tmp_path)
        missing = next(r for r in again["components"] if r["id"] == spec["id"])
        assert missing["status"] == "artifact-missing" and missing["files"] == []
        path.write_bytes(payload)
    (tmp_path / "release_overlap.synthetic.json").write_text(manifest.dumps(audit))


def test_overlap_audit_cli_and_missing_core_failure(tmp_path, monkeypatch, capsys):
    specs = manifest.component_specs("v9.9.9")
    doc = {"components": specs}
    core = next(s for s in specs if s["id"] == "core")
    write(tmp_path, core["filename"], archive_bytes({"Samsara.exe": b"app"}))
    monkeypatch.setattr(manifest, "build_manifest", lambda **_kw: doc)
    output, audit = tmp_path / "manifest.json", tmp_path / "overlaps.json"
    args = ["--artifacts-dir", str(tmp_path), "--tag", "v9.9.9", "--git-sha", "0" * 40,
            "--build-date", "2026-10-16", "--app-version", "9.9.9", "--output", str(output),
            "--overlap-audit", str(audit)]
    # Fill fields used by the CLI's summary; these are synthetic, not release pins.
    for spec in specs:
        spec.update(size_bytes=1, sha256="0" * 64)
    assert manifest.main(args) == 0
    assert {r["id"] for r in json.loads(audit.read_text())["components"]} == {s["id"] for s in ADDONS}
    (tmp_path / core["filename"]).unlink()
    output.unlink()
    assert manifest.main(args) == 1
    assert not output.exists()
    assert "cannot audit release archives" in capsys.readouterr().out
