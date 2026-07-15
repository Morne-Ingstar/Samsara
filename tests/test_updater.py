from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

from samsara import updater


class _Response(io.BytesIO):
    def __init__(self, data: bytes, url: str):
        super().__init__(data)
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _opener_for(mapping):
    def open_request(request, timeout=None):
        del timeout
        url = request.full_url
        return _Response(mapping[url], url)

    return open_request


def _release_payload(tag="v0.22.1", archive_size=123, *, checksum=True):
    zip_name = f"Samsara-Windows-{tag}.zip"
    assets = [
        {
            "name": zip_name,
            "size": archive_size,
            "browser_download_url": f"https://github.com/Morne-Ingstar/Samsara/releases/download/{tag}/{zip_name}",
        }
    ]
    if checksum:
        assets.append(
            {
                "name": f"{zip_name}.sha256",
                "size": 96,
                "browser_download_url": f"https://github.com/Morne-Ingstar/Samsara/releases/download/{tag}/{zip_name}.sha256",
            }
        )
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": assets,
    }


def _archive_bytes(entries=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, contents in (entries or {
            "Samsara.exe": b"new exe",
            "commands.json": b'{"commands": {}}',
            "_internal/ctranslate2/ctranslate2.dll": b"new runtime",
        }).items():
            bundle.writestr(name, contents)
    return output.getvalue()


def _release_for_archive(archive: bytes, tag="v0.22.1"):
    zip_name = f"Samsara-Windows-{tag}.zip"
    base = f"https://github.com/Morne-Ingstar/Samsara/releases/download/{tag}/"
    return updater.ReleaseInfo(
        version=tag.lstrip("v"),
        tag=tag,
        asset_size=len(archive),
        asset_url=base + zip_name,
        checksum_url=base + zip_name + ".sha256",
        checksum_size=96,
    )


def _prepare_inputs(tmp_path, monkeypatch, archive=None):
    archive = archive or _archive_bytes()
    release = _release_for_archive(archive)
    install = tmp_path / "Samsara"
    install.mkdir()
    (install / "Samsara.exe").write_bytes(b"old exe")
    home = tmp_path / "profile"
    monkeypatch.setattr(updater, "_require_update_eligible", lambda: None)
    monkeypatch.setattr(updater, "update_unavailable_reason", lambda: None)
    monkeypatch.setattr(updater, "samsara_home_dir", lambda: home)
    monkeypatch.setattr(updater, "_frozen_install_dir", lambda: install)
    checksum = hashlib.sha256(archive).hexdigest()
    zip_name = f"Samsara-Windows-{release.tag}.zip"
    opener = _opener_for(
        {
            release.asset_url: archive,
            release.checksum_url: f"{checksum}  {zip_name}\n".encode("ascii"),
        }
    )
    return release, install, home, opener


def test_update_eligibility_blocks_source_and_isolated_profiles():
    assert "source" in updater.update_unavailable_reason(
        frozen=False, platform_name="nt", home_override="", disable_override=""
    ).lower()
    assert "isolated" in updater.update_unavailable_reason(
        frozen=True,
        platform_name="nt",
        home_override="X:/isolated",
        disable_override="",
    ).lower()
    assert updater.update_unavailable_reason(
        frozen=True, platform_name="nt", home_override="", disable_override=""
    ) is None
    assert "SAMSARA_DISABLE_UPDATE_CHECK" in updater.update_unavailable_reason(
        frozen=True,
        platform_name="nt",
        home_override="",
        disable_override="yes",
    )


def test_check_does_not_touch_network_when_process_is_ineligible(monkeypatch):
    called = False

    def opener(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network should not be touched")

    monkeypatch.setattr(updater, "update_unavailable_reason", lambda: "source launch")
    with pytest.raises(updater.UpdateNotSupported, match="source launch"):
        updater.check_for_update(opener=opener)
    assert called is False


@pytest.mark.parametrize(
    ("current", "tag", "has_update"),
    [
        ("0.22.0", "v0.22.1", True),
        ("v0.22.1", "v0.22.1", False),
        ("0.22.10", "v0.22.9", False),
        ("0.9.9", "v1.0.0", True),
    ],
)
def test_check_for_update_compares_numeric_versions(
    monkeypatch, current, tag, has_update
):
    monkeypatch.setattr(updater, "_require_update_eligible", lambda: None)
    payload = _release_payload(tag)
    opener = _opener_for(
        {updater.GITHUB_RELEASES_API: json.dumps(payload).encode("utf-8")}
    )
    result = updater.check_for_update(current, opener=opener)
    assert (result is not None) is has_update
    if result:
        assert result.tag == tag
        assert result.version == tag.lstrip("v")
        assert result.asset_size == 123


def test_check_rejects_prerelease_and_missing_checksum(monkeypatch):
    monkeypatch.setattr(updater, "_require_update_eligible", lambda: None)
    prerelease = _release_payload("v0.22.1")
    prerelease["prerelease"] = True
    opener = _opener_for(
        {updater.GITHUB_RELEASES_API: json.dumps(prerelease).encode()}
    )
    with pytest.raises(updater.ReleaseMetadataError, match="not a stable"):
        updater.check_for_update("0.22.0", opener=opener)

    missing = _release_payload("v0.22.1", checksum=False)
    opener = _opener_for(
        {updater.GITHUB_RELEASES_API: json.dumps(missing).encode()}
    )
    with pytest.raises(updater.ReleaseMetadataError, match="unverified update"):
        updater.check_for_update("0.22.0", opener=opener)


def test_check_requires_exact_asset_names_and_github_urls(monkeypatch):
    monkeypatch.setattr(updater, "_require_update_eligible", lambda: None)
    payload = _release_payload()
    payload["assets"][0]["name"] = "Samsara-Windows-latest.zip"
    opener = _opener_for(
        {updater.GITHUB_RELEASES_API: json.dumps(payload).encode()}
    )
    with pytest.raises(updater.ReleaseMetadataError, match="missing"):
        updater.check_for_update("0.22.0", opener=opener)

    payload = _release_payload()
    payload["assets"][0]["browser_download_url"] = "https://example.com/update.zip"
    opener = _opener_for(
        {updater.GITHUB_RELEASES_API: json.dumps(payload).encode()}
    )
    with pytest.raises(updater.ReleaseMetadataError, match="asset URL"):
        updater.check_for_update("0.22.0", opener=opener)


@pytest.mark.parametrize(
    "bad_api",
    [
        "https://api.github.com/repos/SomeoneElse/Samsara/releases/latest",
        updater.GITHUB_RELEASES_API + "?token=bad",
        "https://user@api.github.com/repos/Morne-Ingstar/Samsara/releases/latest",
    ],
)
def test_check_requires_exact_api_url(monkeypatch, bad_api):
    monkeypatch.setattr(updater, "_require_update_eligible", lambda: None)
    with pytest.raises(updater.ReleaseMetadataError, match="Releases API|non-GitHub"):
        updater.check_for_update("0.22.0", api_url=bad_api, opener=lambda *_a: None)


@pytest.mark.parametrize(
    "bad_asset",
    [
        "https://github.com/Other/Samsara/releases/download/v0.22.1/Samsara-Windows-v0.22.1.zip",
        "https://github.com/Morne-Ingstar/Samsara/releases/download/v0.22.1/Samsara-Windows-v0.22.1.zip?raw=1",
        "https://github.com/Morne-Ingstar/Samsara/releases/download/v9.9.9/Samsara-Windows-v0.22.1.zip",
    ],
)
def test_check_requires_exact_repository_tag_and_asset_path(monkeypatch, bad_asset):
    monkeypatch.setattr(updater, "_require_update_eligible", lambda: None)
    payload = _release_payload()
    payload["assets"][0]["browser_download_url"] = bad_asset
    opener = _opener_for(
        {updater.GITHUB_RELEASES_API: json.dumps(payload).encode()}
    )
    with pytest.raises(updater.ReleaseMetadataError, match="asset URL"):
        updater.check_for_update("0.22.0", opener=opener)


def test_prepare_rejects_same_host_redirect_to_a_different_asset(
    tmp_path, monkeypatch
):
    release, install, _home, normal_opener = _prepare_inputs(tmp_path, monkeypatch)

    def opener(request, timeout=None):
        del timeout
        if request.full_url == release.checksum_url:
            return normal_opener(request)
        return _Response(
            b"x" * release.asset_size,
            "https://github.com/attacker/project/releases/download/v1/evil.zip",
        )

    with pytest.raises(updater.ReleaseMetadataError, match="redirected outside"):
        updater.prepare_update(release, install, opener=opener)
    assert not list(tmp_path.glob(".Samsara-update-*"))


def test_prepare_verifies_extracts_beside_install_and_preserves_only_cuda_allowlist(
    tmp_path, monkeypatch
):
    release, install, home, opener = _prepare_inputs(tmp_path, monkeypatch)
    old_cuda = install / "_internal" / "ctranslate2"
    old_cuda.mkdir(parents=True)
    for index, name in enumerate(sorted(updater.CUDA_DLL_ALLOWLIST)):
        (old_cuda / name).write_bytes(f"cuda-{index}".encode())
    (old_cuda / "do-not-preserve.dll").write_bytes(b"private")
    progress = []

    prepared = updater.prepare_update(
        release,
        install,
        lambda done, total: progress.append((done, total)),
        opener=opener,
    )

    assert prepared.staged_dir.parent.parent == install.parent
    assert prepared.install_dir == install
    assert prepared.status_path == home / "updates" / "last_update.json"
    assert (prepared.staged_dir / "Samsara.exe").read_bytes() == b"new exe"
    for index, name in enumerate(sorted(updater.CUDA_DLL_ALLOWLIST)):
        assert (
            prepared.staged_dir / "_internal" / "ctranslate2" / name
        ).read_bytes() == f"cuda-{index}".encode()
    assert not (
        prepared.staged_dir / "_internal" / "ctranslate2" / "do-not-preserve.dll"
    ).exists()
    assert progress[-1] == (release.asset_size, release.asset_size)
    assert not list(prepared.workspace_dir.glob("*.zip"))


def test_prepare_does_not_overwrite_cuda_dll_shipped_by_new_release(
    tmp_path, monkeypatch
):
    cuda_name = sorted(updater.CUDA_DLL_ALLOWLIST)[0]
    archive = _archive_bytes(
        {
            "Samsara.exe": b"new exe",
            "commands.json": b'{"commands": {}}',
            f"_internal/ctranslate2/{cuda_name}": b"new release cuda",
        }
    )
    release, install, _home, opener = _prepare_inputs(
        tmp_path, monkeypatch, archive=archive
    )
    old_cuda = install / "_internal" / "ctranslate2"
    old_cuda.mkdir(parents=True)
    (old_cuda / cuda_name).write_bytes(b"old local cuda")

    prepared = updater.prepare_update(release, install, opener=opener)

    assert (
        prepared.staged_dir / "_internal" / "ctranslate2" / cuda_name
    ).read_bytes() == b"new release cuda"


def test_prepare_preserves_commands_and_custom_plugins_with_profile_backup(
    tmp_path, monkeypatch,
):
    archive = _archive_bytes(
        {
            "Samsara.exe": b"new exe",
            "commands.json": json.dumps(
                {
                    "commands": {
                        "new packaged": {"type": "hotkey", "keys": "ctrl+n"},
                    }
                }
            ).encode(),
            "plugins/commands/packaged.py": b"NEW_PACKAGED = True\n",
        }
    )
    release, install, home, opener = _prepare_inputs(
        tmp_path, monkeypatch, archive=archive,
    )
    (install / "commands.json").write_text(
        json.dumps(
            {
                "commands": {
                    "my command": {"type": "hotkey", "keys": "ctrl+alt+m"},
                }
            }
        ),
        encoding="utf-8",
    )
    plugins = install / "plugins" / "commands"
    plugins.mkdir(parents=True)
    (plugins / "mine.py").write_text("CUSTOM = True\n", encoding="utf-8")
    (plugins / "packaged.py").write_text("OLD_PACKAGED = True\n", encoding="utf-8")

    prepared = updater.prepare_update(release, install, opener=opener)

    merged = json.loads(
        (prepared.staged_dir / "commands.json").read_text(encoding="utf-8")
    )["commands"]
    assert set(merged) == {"new packaged", "my command"}
    assert (prepared.staged_dir / "plugins" / "commands" / "mine.py").is_file()
    assert (
        prepared.staged_dir / "plugins" / "commands" / "packaged.py"
    ).read_text(encoding="utf-8") == "NEW_PACKAGED = True\n"
    backups = list((home / "updates" / "backups").glob("customizations-*"))
    assert len(backups) == 1
    assert (backups[0] / "commands.json").is_file()
    assert (backups[0] / "plugins" / "commands" / "mine.py").is_file()


def test_prepare_rejects_checksum_mismatch_and_cleans_workspace(tmp_path, monkeypatch):
    release, install, _home, opener = _prepare_inputs(tmp_path, monkeypatch)
    bad = _opener_for(
        {
            release.asset_url: b"x" * release.asset_size,
            release.checksum_url: ("0" * 64).encode(),
        }
    )
    with pytest.raises(updater.UpdateIntegrityError):
        updater.prepare_update(release, install, opener=bad)
    assert not list(tmp_path.glob(".Samsara-update-*"))


def test_prepare_checks_free_space_before_network(tmp_path, monkeypatch):
    release, install, _home, _opener = _prepare_inputs(tmp_path, monkeypatch)
    called = False

    def opener(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("download must not start")

    class Usage:
        free = 1

    with pytest.raises(updater.UpdateError, match="Not enough disk space.*required"):
        updater.prepare_update(
            release, install, opener=opener, disk_usage=lambda _path: Usage()
        )
    assert called is False


@pytest.mark.parametrize("unsafe_name", ["../outside.exe", "C:/outside.exe", "/root.exe"])
def test_safe_extraction_rejects_traversal(tmp_path, unsafe_name):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(_archive_bytes({unsafe_name: b"bad"}))
    destination = tmp_path / "output"
    with pytest.raises(updater.UnsafeArchiveError):
        updater._safe_extract_zip(archive, destination)
    assert not (tmp_path / "outside.exe").exists()


def test_safe_extraction_rejects_symlinks(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(info, "target")
    with pytest.raises(updater.UnsafeArchiveError, match="symbolic link"):
        updater._safe_extract_zip(archive, tmp_path / "output")


def test_launch_writes_detached_rollback_helper_and_visible_status(tmp_path, monkeypatch):
    release, install, home, opener = _prepare_inputs(tmp_path, monkeypatch)
    prepared = updater.prepare_update(release, install, opener=opener)
    calls = []

    updater.launch_prepared_update(
        prepared, current_pid=4242, process_runner=lambda *a, **kw: calls.append((a, kw))
    )

    assert len(calls) == 1
    command = calls[0][0][0]
    assert Path(command[0]).is_absolute()
    assert command[0].casefold().endswith(
        r"\system32\windowspowershell\v1.0\powershell.exe"
    )
    assert "-NoProfile" in command and "-NonInteractive" in command
    assert command[-1] == str(prepared.helper_path)
    script = prepared.helper_path.read_text(encoding="utf-8-sig")
    assert "Get-Process -Id 4242" in script
    assert "Move-Item -LiteralPath $install -Destination $rollback" in script
    assert "Move-Item -LiteralPath $rollback -Destination $install" in script
    assert "Write-UpdateStatus 'awaiting_confirmation'" in script
    assert "[System.Windows.MessageBox]::Show" in script
    assert "Start-Process -FilePath (Join-Path $install $executable) -WorkingDirectory $install -PassThru" in script
    assert "$currentStatus.state -eq 'installed'" in script
    assert "$currentStatus.state -eq 'reported'" in script
    assert "The updated Samsara closed before startup completed" in script
    assert "The updated Samsara did not confirm a healthy startup within 180 seconds" in script
    assert "Assert-SafeUpdatePaths" in script
    assert "[IO.FileAttributes]::ReparsePoint" in script
    assert "Complete-PreSwapFailure" in script
    assert "Start-Process" in script
    status = json.loads((home / "updates" / "last_update.json").read_text())
    assert status["state"] == "ready"
    assert status["tag"] == "v0.22.1"


def test_launch_failure_is_written_to_status(tmp_path, monkeypatch):
    release, install, home, opener = _prepare_inputs(tmp_path, monkeypatch)
    prepared = updater.prepare_update(release, install, opener=opener)

    def fail(*_args, **_kwargs):
        raise OSError("blocked")

    with pytest.raises(updater.UpdateError, match="blocked"):
        updater.launch_prepared_update(prepared, process_runner=fail)
    assert not prepared.workspace_dir.exists()
    assert not prepared.helper_path.exists()
    status = json.loads((home / "updates" / "last_update.json").read_text())
    assert status["state"] == "failed"
    assert "blocked" in status["message"]


def test_launch_rejects_forged_staging_path_without_deleting_it(tmp_path, monkeypatch):
    release, install, _home, opener = _prepare_inputs(tmp_path, monkeypatch)
    prepared = updater.prepare_update(release, install, opener=opener)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "Samsara.exe").write_bytes(b"not an update")
    forged = updater.PreparedUpdate(
        version=prepared.version,
        tag=prepared.tag,
        install_dir=prepared.install_dir,
        staged_dir=outside,
        rollback_dir=prepared.rollback_dir,
        workspace_dir=outside,
        status_path=prepared.status_path,
        helper_path=prepared.helper_path,
    )

    with pytest.raises(updater.UpdateError, match="staging paths"):
        updater.launch_prepared_update(
            forged, process_runner=lambda *_a, **_kw: pytest.fail("must not launch")
        )
    assert outside.exists()


def test_launch_validation_failure_cleans_safe_staged_workspace(
    tmp_path, monkeypatch
):
    release, install, home, opener = _prepare_inputs(tmp_path, monkeypatch)
    prepared = updater.prepare_update(release, install, opener=opener)
    (prepared.staged_dir / "Samsara.exe").unlink()

    with pytest.raises(updater.UpdateError, match="staging paths"):
        updater.launch_prepared_update(
            prepared, process_runner=lambda *_a, **_kw: pytest.fail("must not launch")
        )

    assert not prepared.workspace_dir.exists()
    stored = json.loads((home / "updates" / "last_update.json").read_text())
    assert stored["state"] == "failed"


def test_failed_preswap_cleanup_is_retryable_off_startup_thread(
    tmp_path, monkeypatch
):
    release, install, home, opener = _prepare_inputs(tmp_path, monkeypatch)
    prepared = updater.prepare_update(release, install, opener=opener)

    def fail_process(*_args, **_kwargs):
        raise OSError("process blocked")

    real_rmtree = updater.shutil.rmtree
    monkeypatch.setattr(
        updater.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("file still locked")),
    )
    with pytest.raises(updater.UpdateError, match="process blocked"):
        updater.launch_prepared_update(prepared, process_runner=fail_process)
    pending = json.loads((home / "updates" / "last_update.json").read_text())
    assert pending["state"] == "cleanup_pending"
    assert prepared.workspace_dir.exists()

    monkeypatch.setattr(updater.shutil, "rmtree", real_rmtree)
    cleanup_calls = []
    status = updater.reconcile_update_on_startup(
        process_runner=lambda *a, **kw: cleanup_calls.append((a, kw))
    )
    assert status.state == "cleanup_pending"
    assert len(cleanup_calls) == 1
    cleanup_command = cleanup_calls[0][0][0]
    assert Path(cleanup_command[0]).is_absolute()
    cleanup_script = Path(cleanup_command[-1]).read_text(encoding="utf-8-sig")
    assert "foreach ($candidate in @($rollbackFull, $workspaceFull))" in cleanup_script
    assert "Remove-Item -LiteralPath $candidate -Recurse -Force" in cleanup_script
    assert "cleanup_complete" in cleanup_script
    assert "[IO.FileAttributes]::ReparsePoint" in cleanup_script
    assert "$expectedParent" in cleanup_script


def test_cleanup_pending_with_missing_workspace_is_consumed(tmp_path, monkeypatch):
    release, install, home, opener = _prepare_inputs(tmp_path, monkeypatch)
    prepared = updater.prepare_update(release, install, opener=opener)
    updater.shutil.rmtree(prepared.workspace_dir)
    updater._write_status(
        prepared.status_path,
        "cleanup_pending",
        "cleanup needed",
        prepared.tag,
        prepared,
    )

    status = updater.reconcile_update_on_startup()

    assert status.state == "cleanup_complete"
    stored = json.loads((home / "updates" / "last_update.json").read_text())
    assert stored["state"] == "reported"
    assert updater.reconcile_update_on_startup() is None
    assert not (home / "updates" / "last_update.json").exists()


def test_startup_reconciliation_confirms_update_without_blocking_on_backup_cleanup(
    tmp_path, monkeypatch
):
    release, install, home, opener = _prepare_inputs(tmp_path, monkeypatch)
    prepared = updater.prepare_update(release, install, opener=opener)
    prepared.rollback_dir.mkdir()
    (prepared.rollback_dir / "Samsara.exe").write_bytes(b"old")
    prepared.helper_path.write_text("helper", encoding="utf-8")
    updater._write_status(
        prepared.status_path,
        "awaiting_confirmation",
        "waiting",
        prepared.tag,
        prepared,
    )
    monkeypatch.setattr(updater, "update_unavailable_reason", lambda: None)
    monkeypatch.setattr(updater, "_frozen_install_dir", lambda: install)

    status = updater.reconcile_update_on_startup()

    assert status == updater.UpdateStatus(
        "installed", "The update was installed successfully.", "v0.22.1"
    )
    # Reconciliation runs on the Qt startup thread. The detached helper sees
    # the installed handshake and performs these potentially huge deletions.
    assert prepared.rollback_dir.exists()
    assert prepared.workspace_dir.exists()
    assert prepared.helper_path.exists()
    stored = json.loads((home / "updates" / "last_update.json").read_text())
    assert stored["state"] == "installed"

    # A later call (e.g. a subsequent app launch) still finds the rollback
    # and workspace directories on disk -- indistinguishable, from the
    # status string alone, from a helper that died before cleaning up.
    # Reconciliation must not silently trust this forever: it retries
    # cleanup off-thread (a cheap stat check plus a detached relaunch, never
    # a synchronous delete here) instead of just marking it "reported".
    cleanup_calls = []
    status = updater.reconcile_update_on_startup(
        process_runner=lambda *a, **kw: cleanup_calls.append((a, kw))
    )
    assert status.state == "cleanup_pending"
    assert len(cleanup_calls) == 1
    assert prepared.rollback_dir.exists()
    assert prepared.workspace_dir.exists()
    stored = json.loads((home / "updates" / "last_update.json").read_text())
    assert stored["state"] == "cleanup_pending"
    assert stored["rollback_dir"] == str(prepared.rollback_dir)
    assert stored["workspace_dir"] == str(prepared.workspace_dir)

    # Once the (simulated) retry actually removes the leftovers, reconcile
    # consumes the result exactly once, same as the pre-existing
    # cleanup_pending -> cleanup_complete -> reported -> gone path.
    updater.shutil.rmtree(prepared.rollback_dir)
    updater.shutil.rmtree(prepared.workspace_dir)
    status = updater.reconcile_update_on_startup()
    assert status.state == "cleanup_complete"
    stored = json.loads((home / "updates" / "last_update.json").read_text())
    assert stored["state"] == "reported"
    assert updater.reconcile_update_on_startup() is None
    assert not (home / "updates" / "last_update.json").exists()


def test_startup_reconciliation_surfaces_helper_failure(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    status_path = home / "updates" / "last_update.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps(
            {"state": "rolled_back", "message": "disk was full", "tag": "v0.22.1"}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(updater, "samsara_home_dir", lambda: home)
    monkeypatch.setattr(updater, "update_unavailable_reason", lambda: None)

    assert updater.reconcile_update_on_startup() == updater.UpdateStatus(
        "rolled_back", "disk was full", "v0.22.1"
    )
    stored = json.loads(status_path.read_text())
    assert stored["state"] == "reported"
    assert updater.reconcile_update_on_startup() is None


def test_malformed_status_is_quarantined_and_not_repeated(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    updates = home / "updates"
    updates.mkdir(parents=True)
    status_path = updates / "last_update.json"
    status_path.write_text("{ definitely not json", encoding="utf-8")
    monkeypatch.setattr(updater, "samsara_home_dir", lambda: home)
    monkeypatch.setattr(updater, "update_unavailable_reason", lambda: None)

    status = updater.reconcile_update_on_startup()

    assert status.state == "failed"
    assert "Could not read" in status.message
    assert not status_path.exists()
    assert len(list(updates.glob("last_update.invalid-*.json"))) == 1
    assert updater.reconcile_update_on_startup() is None


def test_stale_transient_status_is_quarantined(tmp_path, monkeypatch):
    release, install, home, opener = _prepare_inputs(tmp_path, monkeypatch)
    prepared = updater.prepare_update(release, install, opener=opener)
    updater._write_status(
        prepared.status_path,
        "awaiting_confirmation",
        "waiting",
        prepared.tag,
        prepared,
    )

    status = updater.reconcile_update_on_startup(now=10**10)

    assert status.state == "failed"
    assert "stale" in status.message
    assert not (home / "updates" / "last_update.json").exists()
    assert list((home / "updates").glob("last_update.invalid-*.json"))


def test_release_workflow_publishes_zip_checksum_asset():
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    assert "Get-FileHash -LiteralPath $zipName -Algorithm SHA256" in workflow
    assert "checksum_name=$checksumName" in workflow
    assert "${{ steps.package.outputs.checksum_name }}" in workflow


def test_frozen_build_explicitly_collects_updater_module():
    spec = (
        Path(__file__).parents[1] / "scripts" / "samsara.spec"
    ).read_text(encoding="utf-8")

    assert "'samsara.updater'" in spec
