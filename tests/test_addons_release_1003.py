"""1003: app-only releases with legacy sidecars, shared packs and manifest hashes.

All transports are injected; no live profiles, processes, Git or network.
"""
import hashlib
import io
import json
import traceback
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
import zipfile

import pytest

from samsara import components, update_installer, updater
from samsara.ui import first_run_qt as fq
from tools import gen_release_manifest as gen


TAG = "v0.40.4"
VERSION = "0.40.4"
REPO = "Morne-Ingstar/Samsara"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def bundle(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return out.getvalue()


def build(tmp_path, *, tag=TAG, reuse=None):
    files = {"Samsara.exe": b"new executable", "commands.json": b'{"commands": {}}'}
    files.update({f"_internal/openwakeword/resources/models/{n}": b"model"
                  for n in updater.OWW_MODEL_FILENAMES})
    blobs = {f"Samsara-Windows-{tag}.zip": bundle(files),
             f"SamsaraSetup-{tag[1:]}.exe": b"signed installer fixture"}
    if reuse is None:
        blobs.update({f"Samsara-WakeWord-Models-{tag}.zip": bundle({"model.onnx": b"model"}),
                      f"Samsara-GestureControl-{tag}.zip": bundle({"cv2/runtime.dll": b"runtime"}),
                      f"Samsara-CUDA-Pack-{tag}.zip": bundle({"cublas64_12.dll": b"cuda"})})
    for name, data in blobs.items():
        (tmp_path / name).write_bytes(data)
    doc = gen.build_manifest(artifacts_dir=tmp_path, tag=tag, repo=REPO,
                             git_sha="a" * 40, build_date="2026-11-01T00:00:00Z",
                             app_version=tag[1:], reuse_manifest=reuse, verify_models=lambda _: None)
    return doc, blobs


class Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


def transport(mapping, calls):
    def open_request(request, timeout=None):
        url = request.full_url
        calls.append(url)
        value = mapping[url]
        if isinstance(value, Exception):
            raise value
        return Response(value, url)
    return open_request


def offer(doc, blobs, monkeypatch, *, installer=False, mutate=None, fallback=False):
    name = gen.version_release_filenames(TAG)[0 if installer else 1]
    records = [*doc["components"], *doc["release_assets"]]
    asset = next(r for r in records if r["filename"] == name)
    payload = {"tag_name": TAG, "draft": False, "prerelease": False,
               "assets": [{"name": r["filename"], "size": r["size_bytes"],
                           "browser_download_url": r["url"]}
                          for r in records if r["filename"] in gen.version_release_filenames(TAG)]}
    if mutate:
        mutate(doc)
    url = components.release_manifest_url(VERSION)
    mapping = {updater.GITHUB_RELEASES_API: json.dumps(payload).encode(),
               url: json.dumps(doc).encode(), asset["url"]: blobs[name]}
    if fallback:
        mapping[url] = HTTPError(url, 404, "missing", {}, None)
        mapping[url.rsplit("/", 1)[0] + "/manifest-latest.json"] = json.dumps(doc).encode()
    calls = []
    opener = transport(mapping, calls)
    monkeypatch.setattr(updater, "_require_update_eligible", lambda: None)
    monkeypatch.setattr(update_installer, "asset_name", lambda _: name)
    return opener, calls, mapping, asset


@pytest.mark.parametrize("version", [VERSION, TAG, "0.40.4-beta.1"])
def test_manifest_urls_are_under_addons(version):
    assert fq.default_manifest_url(version) == gen.asset_url(REPO, "addons", f"manifest-{version.removeprefix('v')}.json")


def test_installer_default_uses_addons():
    script = (Path(__file__).resolve().parents[1] / "installer/samsara.iss").read_text(encoding="utf-8")
    assert '#define ManifestUrl "https://github.com/Morne-Ingstar/Samsara/releases/download/addons/manifest-" + AppVersion + ".json"' in script


def test_generator_separates_app_and_optional_assets(tmp_path):
    doc, blobs = build(tmp_path)
    assert components.validate_manifest(doc) == []
    for record in [*doc["components"], *doc["release_assets"]]:
        if not record.get("available", True):
            continue
        tag = TAG if record.get("id") == "core" or record["filename"].endswith(".exe") else "addons"
        assert record["url"] == gen.asset_url(REPO, tag, record["filename"])
        assert record["sha256"] == sha(blobs[record["filename"]])


@pytest.fixture
def published_plan(tmp_path, monkeypatch):
    doc, _ = build(tmp_path)
    monkeypatch.setattr(gen, "verify_wake_word_archive", lambda _: None)
    # Inject fixture model validation at the public builder's default seam.
    real_build = gen.build_manifest
    monkeypatch.setattr(gen, "build_manifest", lambda **kw: real_build(**kw, verify_models=lambda _: None))
    output = tmp_path / "manifest.json"
    plan_path = tmp_path / "plan.json"
    assert gen.main(["--artifacts-dir", str(tmp_path), "--tag", TAG, "--app-version", VERSION,
                     "--git-sha", "a" * 40, "--build-date", "2026-11-01T00:00:00Z",
                     "--output", str(output), "--publish-plan", str(plan_path)]) == 0
    plan = json.loads(plan_path.read_text())
    return doc, plan, output


def test_publish_plan_contains_only_installer_and_portable_zip(tmp_path, published_plan):
    doc, plan, output = published_plan
    # 1003 FINISH IT 2: preserve legacy auto-update with only the two app sidecars.
    assert plan["version_release"] == {"tag": TAG, "files": [
        "SamsaraSetup-0.40.4.exe", "Samsara-Windows-v0.40.4.zip",
        "SamsaraSetup-0.40.4.exe.sha256", "Samsara-Windows-v0.40.4.zip.sha256"]}
    assert plan["addons_release"]["tag"] == "addons"
    assert "manifest-0.40.4.json" in plan["addons_release"]["files"]
    assert "manifest-latest.json" in plan["addons_release"]["files"]
    assert json.loads((tmp_path / "manifest-0.40.4.json").read_text()) == doc
    assert (tmp_path / "manifest-latest.json").read_bytes() == output.read_bytes()
    for component in doc["components"]:
        if component["available"] and component["id"] != "core":
            assert (tmp_path / (component["filename"] + ".sha256")).read_text().startswith(component["sha256"])


@pytest.mark.parametrize("filename", ["SamsaraSetup-0.40.4.exe", "Samsara-Windows-v0.40.4.zip"])
def test_v0403_expected_sidecars_are_in_version_release_and_verify_final_bytes(tmp_path, published_plan, filename):
    # Frozen contract from v0.40.3 updater.check_for_update / update_installer.asset_name:
    # installed selects SamsaraSetup-{tag.removeprefix('v')}.exe; portable selects
    # Samsara-Windows-{tag}.zip; both require checksum_name = f"{zip_name}.sha256".
    doc, plan, _ = published_plan
    checksum_name = filename + ".sha256"
    assert filename in plan["version_release"]["files"]
    assert checksum_name in plan["version_release"]["files"]
    assert checksum_name not in plan["addons_release"]["files"]
    expected = sha((tmp_path / filename).read_bytes())
    sidecar = (tmp_path / checksum_name).read_bytes()
    assert sidecar == f"{expected}  {filename}\n".encode("ascii")
    assert updater._expected_checksum(sidecar, filename) == expected
    record = next(c for c in [*doc["components"], *doc["release_assets"]] if c["filename"] == filename)
    assert record["sha256"] == expected


def test_reuse_packs_without_local_copy_and_without_renaming(tmp_path):
    previous = tmp_path / "previous"
    previous.mkdir()
    doc, _ = build(previous)
    current = tmp_path / "current"
    current.mkdir()
    reused, _ = build(current, tag="v0.40.5", reuse=doc)
    old = {c["id"]: c for c in doc["components"]}
    for component in reused["components"]:
        if component["available"] and component["id"] != "core":
            for key in ("filename", "sha256", "url", "size_bytes"):
                assert component[key] == old[component["id"]][key]
            assert component["source"] == "pinned"
    assert reused["release_assets"][0]["filename"] == "SamsaraSetup-0.40.5.exe"


def test_new_local_pack_overrides_approved_old_pack(tmp_path):
    doc, _ = build(tmp_path)
    new_name = "Samsara-GestureControl-v0.40.5.zip"
    (tmp_path / new_name).write_bytes(b"updated runtime")
    reused, _ = build(tmp_path, tag="v0.40.5", reuse=doc)
    gesture = next(c for c in reused["components"] if c["id"] == "gesture-control")
    assert gesture["filename"] == new_name and gesture["source"] == "built"
    assert gesture["sha256"] == sha(b"updated runtime")


def test_publish_plan_requires_final_installer(tmp_path, monkeypatch):
    build(tmp_path)
    (tmp_path / "SamsaraSetup-0.40.4.exe").unlink()
    real_build = gen.build_manifest
    monkeypatch.setattr(gen, "build_manifest", lambda **kw: real_build(**kw, verify_models=lambda _: None))
    assert gen.main(["--artifacts-dir", str(tmp_path), "--tag", TAG, "--app-version", VERSION,
                     "--git-sha", "a" * 40, "--build-date", "2026-11-01T00:00:00Z",
                     "--output", str(tmp_path / "out.json"),
                     "--publish-plan", str(tmp_path / "plan.json")]) == 1
    assert not (tmp_path / "plan.json").exists()


def test_reuse_migrates_legacy_pack_url_once(tmp_path):
    doc, _ = build(tmp_path)
    for record in doc["components"]:
        if record["available"] and record["id"] != "core":
            record["url"] = gen.asset_url(REPO, TAG, record["filename"])
    reused, _ = build(tmp_path, tag="v0.40.5", reuse=doc)
    assert all("/addons/" in c["url"] for c in reused["components"] if c["available"] and c["id"] != "core")


@pytest.mark.parametrize("tamper", ["hash", "install_dir", "schema"])
def test_reuse_rejects_tampered_local_copy_or_metadata(tmp_path, tamper):
    doc, _ = build(tmp_path)
    if tamper == "hash":
        (tmp_path / "Samsara-CUDA-Pack-v0.40.4.zip").write_bytes(b"tampered")
    elif tamper == "schema":
        doc["schema_version"] = 99
    else:
        next(c for c in doc["components"] if c["id"] == "cuda-pack")["install_dir"] = "wrong"
    with pytest.raises(gen.ManifestGenerationError):
        build(tmp_path, tag="v0.40.5", reuse=doc)


@pytest.mark.parametrize("code", [404, 403, 500])
def test_components_fallback_only_when_version_asset_missing(tmp_path, code):
    doc, _ = build(tmp_path)
    calls = []
    def fetch(url):
        calls.append(url)
        if url.endswith("manifest-0.40.4.json"):
            raise HTTPError(url, code, "fixture", {}, None)
        return json.dumps(doc).encode()
    if code == 404:
        assert components.load_release_manifest(VERSION, fetch=fetch) == doc
        assert calls[-1].endswith("/addons/manifest-latest.json")
    else:
        with pytest.raises(HTTPError):
            components.load_release_manifest(VERSION, fetch=fetch)
        assert len(calls) == 1


def test_corrupt_manifest_does_not_fall_back():
    calls = []
    with pytest.raises(components.ManifestError):
        components.load_release_manifest(VERSION, fetch=lambda u: calls.append(u) or b"{}")
    assert len(calls) == 1


def test_installer_fetch_mode_falls_back_and_installs_from_manifest(tmp_path):
    doc, blobs = build(tmp_path)
    calls = []
    def fetch(url):
        calls.append(url)
        if url.endswith("manifest-0.40.4.json"):
            raise HTTPError(url, 404, "missing", {}, None)
        return json.dumps(doc).encode()
    pack = next(c for c in doc["components"] if c["id"] == "gesture-control")
    downloads = tmp_path / "downloads"
    out = io.StringIO()
    rc = fq.fetch_components_main(["--fetch-components", "gesture-control", "--manifest", fq.default_manifest_url(VERSION),
                                    "--app-root", str(tmp_path / "app"), "--downloads-dir", str(downloads)],
                                   fetch=fetch, opener=lambda url, _: Response(blobs[pack["filename"]], url), out=out)
    assert rc == fq.EXIT_OK, out.getvalue()
    assert calls == [fq.default_manifest_url(VERSION), gen.asset_url(REPO, "addons", "manifest-latest.json")]
    assert json.loads((downloads / "manifest.json").read_text()) == doc


@pytest.mark.parametrize("current_cache", [False, True])
def test_default_components_catalog_uses_only_own_version_cache(tmp_path, monkeypatch, current_cache):
    import samsara
    doc, _ = build(tmp_path)
    monkeypatch.setattr(samsara, "__version__", VERSION)
    if not current_cache:
        doc["app_version"] = "0.40.3"
    (tmp_path / "manifest.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(fq, "default_downloads_dir", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(components, "load_release_manifest", lambda version: calls.append(version) or {"network": version})
    result = fq._load_manifest_default()
    if current_cache:
        assert result == doc and not calls
    else:
        assert result == {"network": VERSION} and calls == [VERSION]


def test_components_downloads_manifest_pack_url_and_verifies_hash(tmp_path):
    doc, blobs = build(tmp_path)
    pack = next(c for c in doc["components"] if c["id"] == "gesture-control")
    calls = []
    def opener(url, headers):
        calls.append(url)
        return Response(blobs[pack["filename"]], url)
    installed = fq.fetch_and_install(pack, tmp_path / "app", tmp_path / "downloads", opener=opener)
    assert calls == [pack["url"]]
    assert sha(installed.read_bytes()) == pack["sha256"]
    assert (tmp_path / "app/_internal/cv2/runtime.dll").read_bytes() == b"runtime"


@pytest.mark.parametrize("installer", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
def test_update_accepts_two_file_release_and_bound_manifest(tmp_path, monkeypatch, installer, fallback):
    doc, blobs = build(tmp_path)
    opener, calls, _, asset = offer(doc, blobs, monkeypatch, installer=installer, fallback=fallback)
    release = updater.check_for_update("0.40.3", opener=opener)
    assert release.sha256 == asset["sha256"]
    assert release.checksum_url == "" and release.checksum_size == 0
    assert updater._release_checksum(release, asset["filename"], opener, 1) == asset["sha256"]
    assert not any(url.endswith(".sha256") for url in calls)


@pytest.mark.parametrize("field,value", [("sha256", "broken"), ("url", "https://evil.invalid/payload"),
                                          ("size_bytes", 987), ("app_version", "0.40.5"),
                                          ("release_tag", "v0.40.5")])
def test_updater_rejects_mismatched_manifest(tmp_path, monkeypatch, field, value):
    doc, blobs = build(tmp_path)
    def mutate(doc):
        if field in ("app_version", "release_tag"):
            doc[field] = value
        else:
            next(c for c in doc["components"] if c["id"] == "core")[field] = value
    opener, _, _, _ = offer(doc, blobs, monkeypatch, mutate=mutate, fallback=True)
    with pytest.raises(updater.ReleaseMetadataError):
        updater.check_for_update("0.40.3", opener=opener)


@pytest.mark.parametrize("kind", ["transport", "http", "json", "schema"])
def test_manifest_failure_does_not_echo_untrusted_details(tmp_path, monkeypatch, kind):
    doc, blobs = build(tmp_path)
    opener, _, mapping, _ = offer(doc, blobs, monkeypatch)
    url = components.release_manifest_url(VERSION)
    detail = "synthetic private directory and manifest value"
    if kind == "transport":
        mapping[url] = OSError(detail)
    elif kind == "http":
        mapping[url] = HTTPError(url + "?" + detail, 500, detail, {}, None)
    elif kind == "json":
        mapping[url] = detail.encode()
    else:
        doc["components"][0]["id"] = detail
        doc["components"][0]["filename"] = "../invalid.zip"
        mapping[url] = json.dumps(doc).encode()
    with pytest.raises(updater.ReleaseMetadataError) as failure:
        updater.check_for_update("0.40.3", opener=opener)
    assert str(failure.value) == "Could not read the update manifest. Try again later."
    rendered = "".join(traceback.format_exception(failure.value))
    assert detail not in rendered
    assert failure.value.__suppress_context__


@pytest.mark.parametrize("installer", [False, True])
@pytest.mark.parametrize("tamper", [False, True])
def test_prepare_verifies_manifest_hash_without_sidecar(tmp_path, monkeypatch, installer, tamper):
    doc, blobs = build(tmp_path)
    opener, calls, mapping, asset = offer(doc, blobs, monkeypatch, installer=installer)
    release = updater.check_for_update("0.40.3", opener=opener)
    if tamper:
        mapping[asset["url"]] = b"x" * asset["size_bytes"]
    install = tmp_path / "installed"
    install.mkdir()
    (install / "Samsara.exe").write_bytes(b"old executable")
    monkeypatch.setattr(updater, "samsara_home_dir", lambda: tmp_path / "profile")
    monkeypatch.setattr(update_installer, "installation", lambda *_: update_installer.Installation(
        install, update_installer.APP_KEY if installer else None, kind="installed" if installer else "portable"))
    from samsara import user_backup
    monkeypatch.setattr(user_backup, "create_snapshot", lambda **_: None)
    kwargs = dict(opener=opener, disk_usage=lambda _: SimpleNamespace(free=100 * 1024**3))
    if tamper:
        with pytest.raises(updater.UpdateIntegrityError):
            updater.prepare_update(release, install, **kwargs)
    else:
        prepared = updater.prepare_update(release, install, **kwargs)
        if installer:
            assert prepared.installer_sha256 == asset["sha256"]
        else:
            assert (prepared.staged_dir / "Samsara.exe").read_bytes() == b"new executable"
    assert not any(url.endswith(".sha256") for url in calls)
    assert (install / "Samsara.exe").read_bytes() == b"old executable"


def test_legacy_target_still_fetches_named_checksum():
    name = "Samsara-Windows-v0.40.3.zip"
    url = gen.asset_url(REPO, "v0.40.3", name + ".sha256")
    release = updater.ReleaseInfo("0.40.3", "v0.40.3", 123, url[:-7], url, 200)
    calls = []
    opener = transport({url: ("a" * 64 + "  " + name + "\n").encode()}, calls)
    assert updater._release_checksum(release, name, opener, 1) == "a" * 64
    assert calls == [url]


@pytest.mark.parametrize("bad", ["not-an-array", [{}], [{"filename": "../bad"}]])
def test_release_assets_schema_is_validated(tmp_path, bad):
    doc, _ = build(tmp_path)
    doc["release_assets"] = bad
    assert any("release_assets" in p for p in components.validate_manifest(doc))


@pytest.mark.parametrize("include_installed", [False, True])
def test_components_render_only_optional_packs(tmp_path, qapp, include_installed):
    from PySide6.QtWidgets import QLabel, QWidget

    doc, _ = build(tmp_path)
    prototype = next(c for c in doc["components"] if c["id"] == "core")
    excluded = [dict(prototype, id="application-" + kind, name="Excluded " + kind, kind=kind)
                for kind in ("core", "app", "installer", "zip")]
    # Even a mistyped optional kind cannot turn the core app into a pack.
    prototype["kind"] = "feature"
    doc["components"].extend(excluded)
    optional = next(c for c in doc["components"] if c["id"] == "gesture-control")
    installed = components.local_component_marker(tmp_path / "app", optional)
    installed.parent.mkdir(parents=True)
    installed.write_text(optional["sha256"], encoding="ascii")
    host = QWidget()
    page = fq.ComponentsPage(host, on_later=lambda: None, manifest_loader=lambda: doc,
                             app_root=tmp_path / "app", downloads_dir=tmp_path / "downloads",
                             gpu_probe=lambda: True, run_in_thread=False,
                             include_installed=include_installed)
    listed = page.refresh()
    expected = {"cuda-pack", "wake-word-models"}
    if include_installed:
        expected.add("gesture-control")
        assert page._rows["gesture-control"]["get"].text() == "Installed"
    assert {c["id"] for c in listed} == expected
    assert set(page._rows) == expected
    labels = [label.text() for label in page.widget.findChildren(QLabel)]
    assert not any("Excluded" in text or "The application itself" in text for text in labels)
    assert not page._status.isVisibleTo(page.widget), "Empty status must not reserve a blank row"
    # A second refresh must keep the same optional-only list.
    assert {c["id"] for c in page.refresh()} == expected
    host.deleteLater()
