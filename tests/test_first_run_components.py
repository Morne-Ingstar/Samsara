"""samsara/ui/first_run_qt.py: the headless --fetch-components mode and the
wizard's Optional Components page. No network: the manifest is a local
file and the archives are served by an in-memory opener."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import components
from samsara.ui import first_run_qt as fq

GIT_SHA = "0" * 40
TAG = "v9.9.9"
URL = f"https://example.invalid/releases/download/{TAG}/"


def _zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _component(cid, name, data: bytes | None, *, kind="model", install_dir="parts", requires=(), available=True):
    entry = {
        "id": cid, "name": name, "description": f"{name} description", "kind": kind,
        "available": available, "filename": f"{cid}.zip", "install_dir": install_dir,
        "requires": list(requires), "default": False, "source": "built",
        "url": None, "size_bytes": None, "sha256": None, "min_disk_bytes": None,
    }
    if available:
        entry.update(url=URL + f"{cid}.zip", size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                     min_disk_bytes=2 * len(data))
    return entry


@pytest.fixture
def world(tmp_path):
    """A manifest on disk, the archives it describes in memory, and an
    opener serving them (with Range support)."""
    core = b"core"
    wake = _zip_bytes({"a.onnx": b"a" * 3000, "b.onnx": b"b" * 3000})
    cuda = _zip_bytes({"cudart64_12.dll": b"c" * 5000})
    manifest = {
        "schema_version": 1, "app_version": "9.9.9", "release_tag": TAG,
        "build_date": "2026-09-13T00:00:00Z", "git_sha": GIT_SHA,
        "components": [
            _component("core", "Samsara", core, kind="core", install_dir="."),
            _component("wake-word-models", "Wake-word models", wake, install_dir="models"),
            _component("cuda-pack", "NVIDIA CUDA acceleration", cuda, kind="acceleration",
                       install_dir="_internal/ctranslate2", requires=("nvidia_gpu",)),
            _component("command-model", "Command model", None, available=False),
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    blobs = {"core.zip": core, "wake-word-models.zip": wake, "cuda-pack.zip": cuda}
    requests = []

    class _Resp:
        def __init__(self, body, status):
            self._body, self._pos, self.status = body, 0, status
            self.headers = {"Content-Length": str(len(body))}

        def read(self, n):
            chunk = self._body[self._pos:self._pos + n]
            self._pos += len(chunk)
            return chunk

        def close(self):
            pass

    def opener(url, headers):
        requests.append((url, dict(headers)))
        name = url.rsplit("/", 1)[1]
        data = blobs[name]
        rng = headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            return _Resp(data[start:], 206)
        return _Resp(data, 200)

    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "Samsara.exe").write_bytes(b"exe")
    downloads = tmp_path / "downloads"
    return {"manifest": manifest, "path": manifest_path, "opener": opener, "requests": requests,
            "app_root": app_root, "downloads": downloads, "blobs": blobs}


def _snapshot(root: Path) -> set:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")} if root.exists() else set()


# ---------------------------------------------------------------------------
# Headless mode
# ---------------------------------------------------------------------------

class TestFetchComponentsMain:
    def _run(self, world, ids, **kw):
        out = io.StringIO()
        rc = fq.fetch_components_main(
            ["--fetch-components", ids, "--manifest", str(world["path"]),
             "--app-root", str(world["app_root"]), "--downloads-dir", str(world["downloads"])],
            opener=world["opener"], out=out, **kw)
        return rc, out.getvalue()

    def test_bad_id_exits_non_zero_without_touching_disk(self, world):
        before = _snapshot(world["app_root"])
        rc, out = self._run(world, "wake-word-models,typo-id")
        assert rc == fq.EXIT_BAD_ARGS and "unknown component id(s): typo-id" in out
        assert _snapshot(world["app_root"]) == before
        assert not world["downloads"].exists()
        assert world["requests"] == []

    def test_unavailable_id_exits_without_touching_disk(self, world):
        rc, out = self._run(world, "command-model")
        assert rc == fq.EXIT_UNAVAILABLE and "not available yet: command-model" in out
        assert not world["downloads"].exists() and world["requests"] == []

    def test_fetches_verifies_installs_and_reports_progress(self, world):
        rc, out = self._run(world, "wake-word-models,cuda-pack")
        assert rc == fq.EXIT_OK, out
        assert (world["app_root"] / "models" / "a.onnx").read_bytes() == b"a" * 3000
        assert (world["app_root"] / "_internal" / "ctranslate2" / "cudart64_12.dll").exists()
        assert "PROGRESS wake-word-models" in out and "[PASS] cuda-pack: installed" in out
        assert (world["downloads"] / "wake-word-models.installed").read_text().split()[0] == \
            world["manifest"]["components"][1]["sha256"]
        assert not any(p.suffix == ".part" for p in world["downloads"].iterdir())
        # a second run finds them installed and does nothing
        rc, out = self._run(world, "wake-word-models")
        assert rc == fq.EXIT_OK and "[SKIP] wake-word-models: already installed" in out

    def test_cancel_mid_fetch_leaves_core_intact(self, world):
        before = _snapshot(world["app_root"])
        seen = []

        def cancel():
            return len(seen) >= 1

        # wrap the opener so we can count chunks: cancel is polled after each
        opener = world["opener"]

        def counting_opener(url, headers):
            resp = opener(url, headers)
            real_read = resp.read

            def read(n):
                chunk = real_read(n)
                if chunk:
                    seen.append(len(chunk))
                return chunk
            resp.read = read
            return resp

        out = io.StringIO()
        rc = fq.fetch_components_main(
            ["--fetch-components", "wake-word-models", "--manifest", str(world["path"]),
             "--app-root", str(world["app_root"]), "--downloads-dir", str(world["downloads"])],
            opener=counting_opener, cancel=cancel, out=out)
        assert rc == fq.EXIT_CANCELLED and "[CANCELLED] wake-word-models" in out.getvalue()
        assert _snapshot(world["app_root"]) == before, "nothing under the app folder changed"
        files = {p.name for p in world["downloads"].iterdir()}
        assert "wake-word-models.zip" not in files, "no final archive"
        assert "wake-word-models.zip.part" in files, "the partial download is kept for resume"

    def test_no_network_exits_4_and_says_core_is_complete(self, world):
        def dead(url, headers):
            raise components.DownloadError("name resolution failed")

        out = io.StringIO()
        rc = fq.fetch_components_main(
            ["--fetch-components", "wake-word-models", "--manifest", str(world["path"]),
             "--app-root", str(world["app_root"]), "--downloads-dir", str(world["downloads"])],
            opener=dead, out=out)
        assert rc == fq.EXIT_NETWORK and "core install is complete" in out.getvalue()
        assert not (world["app_root"] / "models").exists()

        # the manifest itself unreachable is the same outcome
        out = io.StringIO()
        rc = fq.fetch_components_main(
            ["--fetch-components", "wake-word-models", "--manifest", "https://example.invalid/manifest.json",
             "--app-root", str(world["app_root"]), "--downloads-dir", str(world["downloads"])],
            fetch=lambda url: (_ for _ in ()).throw(OSError("offline")), out=out)
        assert rc == fq.EXIT_NETWORK and "could not reach the manifest" in out.getvalue()

    def test_hash_mismatch_deletes_partial_and_reports(self, world):
        data = world["blobs"]["wake-word-models.zip"]
        world["blobs"]["wake-word-models.zip"] = data[:-1] + bytes([data[-1] ^ 0x01])   # same length, wrong hash
        rc, out = self._run(world, "wake-word-models")
        assert rc == fq.EXIT_FAILED and "sha256" in out
        assert not (world["app_root"] / "models").exists()
        assert not any(p.suffix in (".part", ".zip") for p in world["downloads"].iterdir())

    def test_components_dir_serves_local_archives(self, world, tmp_path):
        folder = tmp_path / "carried-over"
        folder.mkdir()
        for name, data in world["blobs"].items():
            (folder / name).write_bytes(data)
        out = io.StringIO()
        rc = fq.fetch_components_main(
            ["--fetch-components", "wake-word-models", "--manifest", str(world["path"]),
             "--components-dir", str(folder),
             "--app-root", str(world["app_root"]), "--downloads-dir", str(world["downloads"])], out=out)
        assert rc == fq.EXIT_OK, out.getvalue()
        assert (world["app_root"] / "models" / "b.onnx").exists()
        assert world["requests"] == [], "nothing went to the network opener"

    def test_unsafe_archive_members_are_refused(self, world):
        evil = _zip_bytes({"../escape.txt": b"x"})
        world["blobs"]["wake-word-models.zip"] = evil
        world["manifest"]["components"][1].update(size_bytes=len(evil), sha256=hashlib.sha256(evil).hexdigest(),
                                                  min_disk_bytes=2 * len(evil))
        world["path"].write_text(json.dumps(world["manifest"]), encoding="utf-8")
        rc, out = self._run(world, "wake-word-models")
        assert rc == fq.EXIT_NETWORK and "refused" in out
        assert not (world["app_root"].parent / "escape.txt").exists()
        assert not (world["app_root"] / "escape.txt").exists()

    def test_no_ids_or_no_args_is_usage(self, world):
        out = io.StringIO()
        assert fq.fetch_components_main(["--fetch-components", "", "--manifest", str(world["path"])], out=out) == fq.EXIT_BAD_ARGS


class TestNotes:
    def test_component_note_states_the_reason(self):
        cuda = {"id": "cuda-pack", "available": True, "requires": ["nvidia_gpu"]}
        assert fq.component_note(cuda, False) == fq.NO_NVIDIA
        assert fq.component_note(cuda, True) is None
        assert "Could not detect" in fq.component_note(cuda, None)
        assert fq.component_note({"id": "command-model", "available": False, "requires": []}, True) == fq.COMING_SOON
        assert fq.component_note({"id": "wake-word-models", "available": True, "requires": []}, False) is None

    def test_format_size(self):
        assert fq.format_size(None) == "size unknown"
        assert fq.format_size(500) == "500 bytes"
        assert fq.format_size(8966335) == "8.6 MB"
        assert fq.format_size(1128045243) == "1.1 GB"


# ---------------------------------------------------------------------------
# The wizard page
# ---------------------------------------------------------------------------

@pytest.fixture
def page(qapp, world):
    from PySide6.QtWidgets import QWidget

    host = QWidget()
    later = []
    p = fq.ComponentsPage(host, on_later=lambda: later.append(True),
                          manifest_loader=lambda: world["manifest"],
                          app_root=world["app_root"], downloads_dir=world["downloads"],
                          opener=world["opener"], gpu_probe=lambda: False, run_in_thread=False)
    host.resize(760, 700)
    host.show()
    qapp.processEvents()
    yield p, later, host
    host.close()


class TestComponentsPage:
    def test_async_refresh_does_not_run_loader_on_qt_thread(self, page, monkeypatch):
        p, _, _ = page
        workers = []
        monkeypatch.setattr(fq.thread_registry, "spawn", lambda _name, target, **_kw: workers.append(target))
        monkeypatch.setattr(p, "_post", lambda fn: fn())
        calls = []
        p._threaded = True
        p._loader = lambda: calls.append("loaded") or {"components": []}

        assert p.refresh_async() == []
        assert calls == []
        assert p._status.text() == "Loading component list…"
        assert len(workers) == 1
        workers[0]()
        assert calls == ["loaded"]
        assert p._status.text() == "Everything is installed."

    def test_lists_exactly_the_missing_components(self, page, world):
        p, _, _ = page
        listed = p.refresh()
        assert [c["id"] for c in listed] == ["cuda-pack", "wake-word-models", "command-model"]
        # install one by hand (marker + files) and it drops off the list
        fq.install_component(io.BytesIO(world["blobs"]["wake-word-models.zip"]),
                             world["manifest"]["components"][1], world["app_root"], world["downloads"])
        assert [c["id"] for c in p.refresh()] == ["cuda-pack", "command-model"]

    def test_rows_show_size_benefit_and_reasons(self, page):
        from PySide6.QtWidgets import QLabel

        p, _, host = page
        p.refresh()
        texts = [w.text() for w in host.findChildren(QLabel)]
        assert any("Wake-word models" in t and "KB" in t for t in texts)
        assert fq.COMPONENT_BENEFITS["wake-word-models"] in texts
        assert p._rows["cuda-pack"]["note"].text() == fq.NO_NVIDIA and not p._rows["cuda-pack"]["get"].isEnabled()
        assert p._rows["command-model"]["note"].text() == fq.COMING_SOON
        assert not p._rows["command-model"]["get"].isEnabled()
        assert p._rows["wake-word-models"]["get"].isEnabled()

    def test_get_now_fetches_and_installs_and_later_advances(self, page, world, qapp):
        p, later, _ = page
        p.refresh()
        p._rows["wake-word-models"]["get"].click()
        qapp.processEvents()
        assert (world["app_root"] / "models" / "a.onnx").exists()
        assert p._rows["wake-word-models"]["result"].text() == "Installed."
        assert p._rows["wake-word-models"]["get"].text() == "Installed Wake-word models"
        p._later_btn.click()
        assert later == [True]

    def test_cancel_button_stops_the_fetch_and_keeps_core(self, page, world, qapp):
        p, _, _ = page
        p.refresh()
        row = p._rows["wake-word-models"]
        # cancel as soon as the first progress report arrives
        original = fq.fetch_and_install

        def fetch_then_cancel(component, app_root, downloads_dir, *, progress=None, cancel=None, opener=None):
            def _progress(done, total):
                p.cancel_fetch("wake-word-models")
                if progress:
                    progress(done, total)
            return original(component, app_root, downloads_dir, progress=_progress, cancel=cancel, opener=opener)

        fq.fetch_and_install = fetch_then_cancel
        try:
            row["get"].click()
            qapp.processEvents()
        finally:
            fq.fetch_and_install = original
        assert row["result"].text().startswith("Cancelled")
        assert row["get"].isEnabled(), "can try again"
        assert not (world["app_root"] / "models").exists()

    def test_accessibility_targets_and_names(self, page):
        from PySide6.QtWidgets import QAbstractButton

        p, _, host = page
        p.refresh()
        buttons = host.findChildren(QAbstractButton)
        assert buttons
        for b in buttons:
            assert b.accessibleName() == b.text(), b.text()
            assert b.minimumHeight() >= fq.MIN_TARGET and b.minimumWidth() >= fq.MIN_TARGET, b.text()
        visible = [b for b in buttons if b.isVisibleTo(host)]
        ys = [b.mapTo(host, b.rect().topLeft()).y() for b in visible]
        assert ys == sorted(ys), "top-to-bottom order"
        assert p._later_btn.text() == "Later"

    def test_manifest_failure_is_a_visible_message_not_an_exception(self, qapp, world):
        from PySide6.QtWidgets import QWidget

        host = QWidget()
        p = fq.ComponentsPage(host, on_later=lambda: None,
                              manifest_loader=lambda: (_ for _ in ()).throw(OSError("offline")),
                              app_root=world["app_root"], downloads_dir=world["downloads"], run_in_thread=False)
        assert p.refresh() == []
        assert "unavailable" in p._status.text() and "Settings" in p._status.text()


class TestWizardHosting:
    def test_one_page_after_the_microphone_step(self):
        from samsara.ui import first_run_wizard_qt as wz

        names = [s[0] for s in wz._STEPS]
        assert names.index("Components") == names.index("Microphone") + 1
        assert names.count("Components") == 1
        assert len(wz._STEPS) == 7
