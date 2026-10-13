"""Real early boot/migration on disposable profiles, without live app/registry."""
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from samsara import boot, update_installer as ins, updater as up
from samsara import update_installer_customizations as migration
from samsara import update_customizations as shared


@pytest.fixture
def pending(tmp_path, monkeypatch):
    install = tmp_path / "Samsara"
    snapshot = tmp_path / ".Samsara-rollback-v1-test"
    workspace = tmp_path / ".Samsara-update-v1-test"
    profile = tmp_path / "profile"
    for root in (install, snapshot):
        (root / "_internal").mkdir(parents=True)
        (root / "_internal/commands.json").write_text(json.dumps({
            "commands": {"packaged" if root == install else "edited": {"action": "test"}},
        }))
    for name in ("unins000.exe", "unins000.dat"):
        (snapshot / name).write_bytes(b"snapshot marker")
    workspace.mkdir()
    updates = profile / "updates"
    updates.mkdir(parents=True)
    status = updates / "last_update.json"
    status.write_text(json.dumps({
        "state": "awaiting_confirmation", "install_dir": str(install),
        "rollback_dir": str(snapshot), "workspace_dir": str(workspace),
    }))
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(profile))
    monkeypatch.setattr(sys, "executable", str(install / "Samsara.exe"))
    monkeypatch.setattr(sys, "argv", [str(install / "Samsara.exe")])
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(migration, "_invalid_logged", False)
    notice = Mock(return_value=True)
    monkeypatch.setattr(migration, "_show_recovery_notice", notice)
    return install, snapshot, workspace, updates, status, notice


@pytest.mark.parametrize("target", ["status", "receipt"])
@pytest.mark.parametrize("bad", ["[]", "null", "123", '"text"', "{broken", "oversized", "unreadable", "directory", "bad-fields"])
def test_bad_metadata_never_escapes_boot_and_logs_once(pending, monkeypatch, caplog, target, bad):
    install, snapshot, workspace, updates, status, notice = pending
    path = status if target == "status" else workspace / "customizations-migrated.json"
    if bad == "directory":
        if path.exists():
            path.unlink()
        path.mkdir()
    else:
        contents = b" " * (64 * 1024 + 1) if bad == "oversized" else (
            b'{"state":"awaiting_confirmation","rollback_dir":[]}' if bad == "bad-fields" else bad.encode()
        )
        path.write_bytes(contents)
    if bad == "unreadable":
        original = Path.open
        def denied(self, *args, **kwargs):
            if self == path:
                raise PermissionError("synthetic unreadable metadata")
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, "open", denied)
    packaged = (install / "_internal/commands.json").read_bytes()
    for _ in range(2):
        boot.migrate_before_boot()
    assert (install / "_internal/commands.json").read_bytes() == packaged
    assert not (updates / "backups").exists()
    notice.assert_not_called()
    assert sum("Invalid or unreadable update metadata ignored" in rec.message for rec in caplog.records) == 1


@pytest.mark.parametrize("failure", ["malformed", "oversized", "receipt-write"])
def test_orphan_failure_keeps_packaged_data_and_backups_then_never_retries(pending, monkeypatch, failure):
    install, snapshot, workspace, updates, status, notice = pending
    packaged = (install / "_internal/commands.json").read_bytes()
    old = snapshot / "_internal/commands.json"
    if failure == "malformed":
        old.write_bytes(b"{broken")
    elif failure == "oversized":
        with old.open("wb") as stream:
            stream.truncate(shared.MAX_COMMANDS_BYTES + 1)
    else:
        original = migration._write_new_file
        def write(path, data):
            if path == workspace / "customizations-migrated.json":
                raise PermissionError("synthetic receipt failure after merge")
            return original(path, data)
        monkeypatch.setattr(migration, "_write_new_file", write)
    boot.migrate_before_boot()
    assert (install / "_internal/commands.json").read_bytes() == packaged
    assert old.is_file() and snapshot.is_dir()
    payload = json.loads(status.read_text())
    assert payload["state"] == "reported" and payload["customization_state"] == "failed"
    assert payload["customization_retry"] is False
    notice.assert_called_once_with(payload["message"])
    archive = Path(payload["customization_backup_dir"])
    assert (archive / "commands.json").read_bytes() == old.read_bytes()
    assert str(archive) in payload["message"] and str(snapshot) in payload["message"]
    if failure == "receipt-write":
        assert len(list((updates / "backups").glob("customizations-*"))) == 2
    refused = Mock(side_effect=AssertionError("terminal failure must never retry"))
    monkeypatch.setattr(migration, "migrate_installer_customizations", refused)
    boot.migrate_before_boot()
    boot.migrate_before_boot()
    refused.assert_not_called()
    notice.assert_called_once_with(payload["message"])
    monkeypatch.setattr(up, "update_unavailable_reason", lambda: None)
    monkeypatch.setattr(ins, "repair_notice", lambda: None)
    assert up.reconcile_update_on_startup() is None  # No repeated tray notice.
    boot.migrate_before_boot()
    refused.assert_not_called()


def test_unavailable_native_notice_leaves_failure_for_the_tray_once(pending, monkeypatch):
    install, snapshot, workspace, updates, status, notice = pending
    (snapshot / "_internal/commands.json").write_bytes(b"{broken")
    notice.return_value = False
    boot.migrate_before_boot()
    assert json.loads(status.read_text())["state"] == "failed"
    monkeypatch.setattr(up, "update_unavailable_reason", lambda: None)
    monkeypatch.setattr(ins, "repair_notice", lambda: None)
    result = up.reconcile_update_on_startup()
    assert result.state == "failed" and result.message.startswith(migration.RECOVERY_NOTICE)
    assert up.reconcile_update_on_startup() is None


def test_live_helper_failure_exits_for_rollback_and_reused_pid_does_not(pending, monkeypatch):
    install, snapshot, workspace, updates, status, notice = pending
    (snapshot / "_internal/commands.json").write_bytes(b"{broken")
    payload = json.loads(status.read_text())
    payload.update(helper_pid=12345, helper_started_filetime="100")
    status.write_text(json.dumps(payload))
    monkeypatch.setattr(migration, "_process_creation_filetime", lambda pid: 100)
    with pytest.raises(SystemExit) as error:
        boot.migrate_before_boot()
    assert error.value.code == 2
    assert json.loads(status.read_text())["state"] == "awaiting_confirmation"
    notice.assert_not_called()
    # PID reused after helper died: same PID, different creation time.
    monkeypatch.setattr(migration, "_process_creation_filetime", lambda pid: 101)
    boot.migrate_before_boot()
    assert json.loads(status.read_text())["state"] == "reported"
    assert notice.call_count == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows process identity")
def test_windows_helper_identity_reads_current_process_without_signalling():
    identity = migration._process_creation_filetime(os.getpid())
    assert isinstance(identity, int) and identity > 0
    assert migration._helper_owns_update({"helper_pid": os.getpid(), "helper_started_filetime": str(identity)})
    assert not migration._helper_owns_update({"helper_pid": os.getpid(), "helper_started_filetime": str(identity + 1)})
    assert migration._process_creation_filetime(2_000_000_000) is None


@pytest.mark.parametrize("copy", ["portable", "foreign", "installed"])
def test_mounted_ancestor_does_not_block_start_or_valid_migration(pending, monkeypatch, copy):
    install, snapshot, workspace, updates, status, notice = pending
    checked = migration._checked_lstat
    shared_checked = shared._checked_lstat
    def inspect(path):
        if path == install.parent or path in install.parent.parents:
            raise shared.UnsafeCustomizationError("mounted ancestor above install")
        return checked(path)
    def inspect_shared(path):
        if path == install.parent or path in install.parent.parents:
            raise shared.UnsafeCustomizationError("mounted ancestor above install")
        return shared_checked(path)
    monkeypatch.setattr(migration, "_checked_lstat", inspect)
    monkeypatch.setattr(shared, "_checked_lstat", inspect_shared)
    if copy == "portable":
        (snapshot / "unins000.exe").unlink()
    elif copy == "foreign":
        monkeypatch.setattr(sys, "executable", str(install.parent / "Other/Samsara.exe"))
    assert migration.migrate_pending_installer_update() == (copy == "installed")
    notice.assert_not_called()


@pytest.mark.parametrize("bad", ["malformed", "oversized", "wrong-object", "plugins-reparse", "too-many-plugins"])
def test_prepare_refuses_old_customizations_with_zip_reason_before_download(pending, monkeypatch, bad):
    install, snapshot, workspace, updates, status, notice = pending
    path = install / "_internal/commands.json"
    if bad == "malformed":
        path.write_bytes(b"{broken")
    elif bad == "oversized":
        with path.open("wb") as stream:
            stream.truncate(shared.MAX_COMMANDS_BYTES + 1)
    elif bad == "wrong-object":
        path.write_bytes(b"[]")
    else:
        plugins = install / "_internal/plugins/commands"
        plugins.mkdir(parents=True)
        (plugins / "dropin.py").write_bytes(b"edited")
        if bad == "too-many-plugins":
            monkeypatch.setattr(shared, "MAX_PLUGIN_FILES", 0)
        else:
            checked = shared._checked_lstat
            def inspect(item):
                if item == plugins:
                    raise shared.UnsafeCustomizationError(f"Refusing to migrate link or reparse point: {item}")
                return checked(item)
            monkeypatch.setattr(shared, "_checked_lstat", inspect)
    with pytest.raises(shared.CustomizationMigrationError) as zipped:
        shared.migrate_update_customizations(install, snapshot, updates / "zip-backups")
    info = ins.Installation(install, ins.APP_KEY, kind="installed")
    monkeypatch.setattr(ins, "installation", lambda folder: info)
    name = "SamsaraSetup-0.31.0.exe"
    base = "https://github.com/Morne-Ingstar/Samsara/releases/download/v0.31.0/"
    release = up.ReleaseInfo("0.31.0", "v0.31.0", 10, base + name, base + name + ".sha256", 100)
    network = Mock(side_effect=AssertionError("invalid old data must refuse before download"))
    before = set(install.parent.glob(".Samsara-update-*"))
    with pytest.raises(shared.CustomizationMigrationError) as installed:
        ins.prepare(release, install, None, opener=network, timeout=1,
                    disk_usage=Mock(), allow_prerelease=False)
    assert str(installed.value) == str(zipped.value)
    network.assert_not_called()
    assert set(install.parent.glob(".Samsara-update-*")) == before


def test_retention_keeps_five_including_current_and_leaves_partial_archives(pending):
    install, snapshot, workspace, updates, status, notice = pending
    backups = updates / "backups"
    backups.mkdir()
    archives = []
    for index in range(8):
        archive = backups / f"customizations-{index}"
        archive.mkdir()
        (archive / "manifest.json").write_text('{"schema_version":1}')
        (archive / "commands.json").write_bytes(b"retained")
        os.utime(archive, ns=(index * 1_000_000_000, index * 1_000_000_000))
        archives.append(archive)
    partial = backups / "customizations-partial"
    partial.mkdir()
    migration._prune_backups(backups, archives[0])
    assert {item.name for item in backups.iterdir()} == {
        archives[index].name for index in (0, 4, 5, 6, 7)
    } | {partial.name}


@pytest.mark.parametrize("age,live", [(0, True), (199, True), (201, False), (1000, False)])
def test_legacy_without_identity_is_live_only_during_confirmation_wait(pending, age, live):
    install, snapshot, workspace, updates, status, notice = pending
    (snapshot / "_internal/commands.json").write_bytes(b"{broken")
    payload = json.loads(status.read_text())
    payload["updated_at"] = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
    status.write_text(json.dumps(payload))
    if live:
        with pytest.raises(SystemExit) as error:
            boot.migrate_before_boot()
        assert error.value.code == 2
        assert json.loads(status.read_text())["state"] == "awaiting_confirmation"
        notice.assert_not_called()
    else:
        boot.migrate_before_boot()
        assert json.loads(status.read_text())["state"] == "reported"
        assert notice.call_count == 1


@pytest.mark.parametrize("problem", ["io", "over-cap", "reparse"])
def test_unarchivable_orphan_never_claims_backup_or_acknowledges_legacy_success(pending, monkeypatch, problem):
    install, snapshot, workspace, updates, status, notice = pending
    old = snapshot / "_internal/commands.json"
    old.write_bytes(b"{broken")
    if problem == "io":
        (updates / "backups").write_bytes(b"not a directory")
    elif problem == "over-cap":
        monkeypatch.setattr(migration, "DEFAULT_MAX_BACKUP_BYTES", 2)
    else:
        checked = migration._checked_lstat
        def inspect(path):
            if path == old:
                raise shared.UnsafeCustomizationError("synthetic reparse point")
            return checked(path)
        monkeypatch.setattr(migration, "_checked_lstat", inspect)
    boot.migrate_before_boot()
    payload = json.loads(status.read_text())
    assert payload["state"] == "customization_recovery_pending"
    assert payload["customization_retry"] is False
    assert "No customization archive could be made." in payload["message"]
    assert str(snapshot) in payload["message"]
    assert "customization_backup_dir" not in payload
    notice.assert_called_once_with(payload["message"])
    # Normal reconcile must never turn an unarchived failure into reported.
    monkeypatch.setattr(up, "update_unavailable_reason", lambda: None)
    monkeypatch.setattr(ins, "repair_notice", lambda: None)
    up.reconcile_update_on_startup()
    assert json.loads(status.read_text())["state"] == "customization_recovery_pending"
    boot.migrate_before_boot()
    assert notice.call_count == 1


def test_raw_archive_copies_whole_plugins_tree_before_any_terminal_status(pending, monkeypatch):
    install, snapshot, workspace, updates, status, notice = pending
    (snapshot / "_internal/commands.json").write_bytes(b"{broken")
    plugin = snapshot / "_internal/plugins/extra/assets/custom.bin"
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(b"raw plugin asset")
    original = migration._replace_file_atomically
    def replace(path, data):
        if path == status and json.loads(data)["state"] in ("failed", "reported"):
            archive = Path(json.loads(data)["customization_backup_dir"])
            assert (archive / "commands.json").read_bytes() == b"{broken"
            assert (archive / "plugins/extra/assets/custom.bin").read_bytes() == b"raw plugin asset"
            assert (archive / "manifest.json").is_file()
        return original(path, data)
    monkeypatch.setattr(migration, "_replace_file_atomically", replace)
    boot.migrate_before_boot()
    assert json.loads(status.read_text())["state"] == "reported"


def test_rollback_reconcile_preserves_retry_then_installed_copy_migrates(pending, monkeypatch):
    install, snapshot, workspace, updates, status, notice = pending
    payload = json.loads(status.read_text())
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    status.write_text(json.dumps(payload))
    original = status.read_bytes()
    monkeypatch.setattr(up, "update_unavailable_reason", lambda: None)
    monkeypatch.setattr(ins, "repair_notice", lambda: None)
    monkeypatch.setattr(sys, "executable", str(snapshot / "Samsara.exe"))
    boot.migrate_before_boot()
    assert up.reconcile_update_on_startup() is None
    assert status.read_bytes() == original
    assert not list(updates.glob("last_update.invalid-*"))
    monkeypatch.setattr(sys, "executable", str(install / "Samsara.exe"))
    boot.migrate_before_boot()
    assert "edited" in json.loads((install / "_internal/commands.json").read_text())["commands"]
    assert up.reconcile_update_on_startup().state == "installed"


@pytest.mark.parametrize("owner", ["identity", "lock"])
def test_unstopped_migration_defers_boot_and_confirmation_then_retries(pending, monkeypatch, owner):
    install, snapshot, workspace, updates, status, notice = pending
    payload = json.loads(status.read_text())
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    status.write_text(json.dumps(payload))
    if owner == "identity":
        payload.update(migration_pid=12345, migration_started_filetime="100")
        status.write_text(json.dumps(payload))
        monkeypatch.setattr(migration, "_process_creation_filetime", lambda pid: 100)
    monkeypatch.setattr(up, "update_unavailable_reason", lambda: None)
    monkeypatch.setattr(ins, "repair_notice", lambda: None)
    original = status.read_bytes()
    packaged = (install / "_internal/commands.json").read_bytes()
    def assert_deferred():
        boot.migrate_before_boot()
        assert up.reconcile_update_on_startup() is None
        assert status.read_bytes() == original
        assert (install / "_internal/commands.json").read_bytes() == packaged
        assert not (workspace / "customizations-migrated.json").exists()
        notice.assert_not_called()
    if owner == "lock":
        with migration._migration_lock(updates / "backups"):
            assert_deferred()
            with pytest.raises(migration.MigrationBusy):
                migration.migrate_installer_customizations(snapshot, install, updates / "backups", workspace / "customizations-migrated.json")
    else:
        assert_deferred()
        monkeypatch.setattr(migration, "_process_creation_filetime", lambda pid: 101)
    boot.migrate_before_boot()
    assert "edited" in json.loads((install / "_internal/commands.json").read_text())["commands"]
    assert up.reconcile_update_on_startup().state == "installed"


@pytest.mark.parametrize("problem", ["reparse", "over-cap", "too-many", "copy-io"])
def test_refused_plugins_still_archive_commands_without_acknowledging_success(pending, monkeypatch, problem):
    install, snapshot, workspace, updates, status, notice = pending
    old = snapshot / "_internal/commands.json"
    old.write_bytes(b"{broken")
    plugin = snapshot / "_internal/plugins/commands/unsafe.py"
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(b"plugin")
    if problem == "reparse":
        checked = migration._checked_lstat
        def inspect(path):
            if path == plugin:
                raise shared.UnsafeCustomizationError("synthetic plugin reparse point")
            return checked(path)
        monkeypatch.setattr(migration, "_checked_lstat", inspect)
        monkeypatch.setattr(shared, "_checked_lstat", inspect)
    elif problem == "over-cap":
        with plugin.open("wb") as stream:
            stream.truncate(migration.DEFAULT_MAX_BACKUP_BYTES + 1)
    elif problem == "too-many":
        monkeypatch.setattr(shared, "MAX_PLUGIN_FILES", 0)
    else:
        copy = migration._copy_verified_file
        def copy_file(source, *args, **kwargs):
            if source == plugin:
                raise PermissionError("synthetic plugin copy error")
            return copy(source, *args, **kwargs)
        monkeypatch.setattr(migration, "_copy_verified_file", copy_file)
    boot.migrate_before_boot()
    payload = json.loads(status.read_text())
    archive = Path(payload["customization_backup_dir"])
    assert (archive / "commands.json").read_bytes() == b"{broken"
    manifest = json.loads((archive / "manifest.json").read_text())
    assert manifest["commands_backed_up"] is True
    assert manifest["plugins_backed_up"] is False
    assert payload["state"] == "customization_recovery_pending"
    assert "Your command file was saved" in payload["message"]
    assert "plugins could not all be archived" in payload["message"]
    assert plugin.exists() and snapshot.is_dir()
    migration._prune_backups(updates / "backups", updates / "backups/customizations-current")
    assert archive.is_dir()


@pytest.mark.parametrize("with_plugin", [False, True])
def test_notice_does_not_claim_absent_command_file(pending, monkeypatch, with_plugin):
    install, snapshot, workspace, updates, status, notice = pending
    (snapshot / "_internal/commands.json").unlink()
    if with_plugin:
        plugin = snapshot / "_internal/plugins/commands/old.py"
        plugin.parent.mkdir(parents=True)
        plugin.write_bytes(b"edited")
    monkeypatch.setattr(migration, "migrate_installer_customizations", Mock(side_effect=PermissionError("synthetic migration failure")))
    boot.migrate_before_boot()
    payload = json.loads(status.read_text())
    assert "Your command" not in payload["message"]
    assert ("Your plugins were saved" if with_plugin else "An empty recovery archive was saved") in payload["message"]


def test_retention_preserves_every_notice_named_archive(pending):
    install, snapshot, workspace, updates, status, notice = pending
    backups = updates / "backups"
    protected = migration._archive_raw_customizations(snapshot, backups)
    os.utime(protected, ns=(0, 0))
    ordinary = []
    for index in range(8):
        archive = backups / f"customizations-new-{index}"
        archive.mkdir()
        (archive / "manifest.json").write_text('{"schema_version":1}')
        os.utime(archive, ns=((index+1)*1_000_000_000, (index+1)*1_000_000_000))
        ordinary.append(archive)
        migration._prune_backups(backups, archive)
    assert protected.is_dir()
    assert {p.name for p in backups.iterdir()} == {protected.name, *(p.name for p in ordinary[-5:])}


@pytest.mark.skipif(os.name != "nt", reason="Windows interprocess maintenance lock")
def test_writer_lock_blocks_retry_even_without_process_identity_or_saved_status(pending, monkeypatch):
    import subprocess
    import time
    install, snapshot, workspace, updates, status, notice = pending
    ready, release = workspace / "writer-ready", workspace / "writer-release"
    driver = workspace / "writer.py"
    driver.write_text(
        "import sys,time\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from samsara.update_installer_customizations import _migration_lock\n"
        f"with _migration_lock(Path({str(updates / 'backups')!r})):\n"
        f"    Path({str(ready)!r}).write_text('ready')\n"
        "    deadline = time.monotonic() + 15\n"
        f"    while not Path({str(release)!r}).exists() and time.monotonic() < deadline: time.sleep(0.02)\n"
    )
    writer = subprocess.Popen([sys._base_executable, "-B", str(driver)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and writer.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        original = status.read_bytes()
        monkeypatch.setattr(up, "update_unavailable_reason", lambda: None)
        monkeypatch.setattr(ins, "repair_notice", lambda: None)
        payload = json.loads(status.read_text())
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        status.write_text(json.dumps(payload))
        original = status.read_bytes()
        boot.migrate_before_boot()
        assert up.reconcile_update_on_startup() is None
        assert status.read_bytes() == original
        assert not (workspace / "customizations-migrated.json").exists()
    finally:
        release.write_text("release")
        output, _ = writer.communicate(timeout=20)
    assert writer.returncode == 0, output
    boot.migrate_before_boot()
    assert "edited" in json.loads((install / "_internal/commands.json").read_text())["commands"]
