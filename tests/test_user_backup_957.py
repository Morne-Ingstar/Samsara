"""Restore drills use isolated synthetic profiles; never inspect a live home."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest

from samsara import user_backup as backup


def put(home, name, value):
    path = home / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    return path


def seed(home, marker="original"):
    files = {
        "config.json": {"language": "en", "theme": marker, "max_tokens": 500},
        "training_data.json": {"vocabulary": ["Samsara"], "corrections": {"samsara": "Samsara"}},
        "user_corrections.json": {"example": marker},
        "user_wake_corrections.json": {"jarves": "jarvis"},
        "ava_corrections.json": {"aliases": {}},
        "correction_queue.json": {"items": []},
        "user_aliases.json": {"aliases": {}},
        "commands.json": {"commands": {}},
        "memos/memos.jsonl": b'{"id":"synthetic","text":"example"}\n',
        "memos/memos.md": b"Synthetic memo\r\n",
        "snippets/snippets.jsonl": b'{"name":"example","text":"example"}\n',
        "profiles/commands/custom.json": {"commands": {}},
        "personal_plugins/example.py": b"# synthetic plugin\n",
        "ava_memory.md": b"Synthetic memory\n",
        "ava_memory_archive.md": b"Synthetic archived memory\n",
    }
    return {name: put(home, name, value).read_bytes() for name, value in files.items()}


def rewrite(path, transform):
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    transform(members)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)


def test_clean_home_restore_drill_is_byte_for_byte(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "clean"
    expected = seed(source)
    snapshot = backup.create_snapshot(source)
    manifest, files = backup.verify_snapshot(snapshot)
    assert manifest["version"] == backup.__version__
    assert files == expected
    result = backup.restore_snapshot(snapshot, destination)
    assert result["safety"].exists()
    assert {name: (destination / name).read_bytes() for name in expected} == expected
    assert not list(destination.rglob(".restore-*"))


@pytest.mark.parametrize("damage", ["zip", "manifest", "hash", "extra", "size"])
def test_corruption_is_visible_refused_and_falls_back(tmp_path, damage):
    seed(tmp_path)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    good = backup.create_snapshot(tmp_path, now=start)
    put(tmp_path, "config.json", {"language": "fr"})
    broken = backup.create_snapshot(tmp_path, now=start + timedelta(days=1))
    if damage == "zip":
        broken.write_bytes(b"not a zip")
    else:
        def change(members):
            if damage == "manifest":
                members.pop("manifest.json")
            elif damage == "hash":
                members["data/config.json"] = b'{}'
            elif damage == "extra":
                members["unlisted.txt"] = b"unlisted"
            elif damage == "size":
                manifest = json.loads(members["manifest.json"])
                manifest["files"][0]["size"] += 1
                members["manifest.json"] = json.dumps(manifest).encode()
        rewrite(broken, change)
    before = (tmp_path / "config.json").read_bytes()
    with pytest.raises(backup.BackupError):
        backup.restore_snapshot(broken, tmp_path)
    assert (tmp_path / "config.json").read_bytes() == before
    assert backup.list_snapshots(tmp_path)[0]["damaged"]
    result = backup.restore_snapshot(broken, tmp_path, fallback=True)
    assert result["snapshot"] == good
    assert json.loads((tmp_path / "config.json").read_bytes())["language"] == "en"


def test_credentials_excluded_but_settings_and_current_credentials_preserved(tmp_path):
    # Synthetic values remain in test process only; no credential bytes in evidence.
    secret = "synthetic-credential-value"
    put(tmp_path, "config.json", {"cloud_llm": {"provider": "example", "api_key": secret},
                                  "tts": {"access_token": secret}, "hotkey": "ctrl",
                                  "max_tokens": 500})
    put(tmp_path, "training_data.json", {"vocabulary": ["example"], "password": secret})
    put(tmp_path, "browser_bridge/token.json", {"token": secret})
    put(tmp_path, "agent_server/token.json", {"token": secret})
    put(tmp_path, "personal_plugins/credentials.py", b"sensitive")
    put(tmp_path, "personal_plugins/example.py", ('api_key = "' + secret + '"').encode())
    put(tmp_path, "memos/private.md", ('password: ' + secret).encode())
    put(tmp_path, "unknown.json", {"value": secret})
    for excluded in ("logs/log.txt", "cache/data.json", "models/config.json", "history.db"):
        put(tmp_path, excluded, secret.encode())
    snapshot = backup.create_snapshot(tmp_path)
    _, files = backup.verify_snapshot(snapshot)
    assert set(files) == {"config.json", "training_data.json"}
    assert all(secret.encode() not in raw for raw in files.values())
    config = json.loads(files["config.json"])
    assert config["max_tokens"] == 500 and config["hotkey"] == "ctrl"
    assert config["cloud_llm"] == {"provider": "example"}
    put(tmp_path, "config.json", {"cloud_llm": {"provider": "example", "api_key": secret},
                                  "tts": {"access_token": secret}, "hotkey": "alt"})
    backup.restore_snapshot(snapshot, tmp_path)
    config = json.loads((tmp_path / "config.json").read_bytes())
    assert config["cloud_llm"]["api_key"] == secret
    assert config["tts"]["access_token"] == secret
    assert config["hotkey"] == "ctrl"
    assert (tmp_path / "unknown.json").exists()


def test_provider_change_does_not_reuse_machine_credential(tmp_path):
    assert backup._preserve_secrets(
        {"provider": "new"}, {"provider": "old", "api_key": "synthetic"}) == {"provider": "new"}


def test_retention_seven_daily_four_older_weeks_and_bounded_safety(tmp_path):
    put(tmp_path, "config.json", {})
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for day in range(65):
        backup.create_snapshot(tmp_path, now=start + timedelta(days=day), prune=False)
    for hour in range(3):
        for reason in ("safety", "pre-change"):
            backup.create_snapshot(tmp_path, reason=reason,
                                   now=start + timedelta(days=66, hours=hour, minutes=(reason == "safety")),
                                   prune=False)
    backup.prune_snapshots(tmp_path)
    entries = backup.list_snapshots(tmp_path)
    daily = [e for e in entries if e["manifest"]["reason"] == "daily"]
    assert len(daily) == backup.RETENTION["daily"] + backup.RETENTION["weekly"]
    dates = sorted(datetime.fromisoformat(e["manifest"]["created_at"]).date() for e in daily)
    assert dates[-7:] == [(start + timedelta(days=d)).date() for d in range(58, 65)]
    assert len({d.isocalendar()[:2] for d in dates[:-7]}) == 4
    assert len(entries) == 13


def test_daily_once_per_date_and_rolls_over_without_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    put(tmp_path, "config.json", {})
    day = datetime(2026, 10, 1, tzinfo=timezone.utc)
    first = backup.ensure_daily(now=day)
    assert first.parent == tmp_path / "backups"
    assert backup.ensure_daily(now=day + timedelta(hours=5)) == first
    assert backup.ensure_daily(now=day + timedelta(days=1)) != first


def test_failed_replace_rolls_back_every_written_byte(tmp_path, monkeypatch):
    seed(tmp_path)
    snapshot = backup.create_snapshot(tmp_path)
    current = seed(tmp_path, "current")
    original = backup._replace
    calls = []
    def fail_once(path, raw):
        calls.append(path)
        if len(calls) == 3:
            raise OSError("synthetic disk failure")
        original(path, raw)
    monkeypatch.setattr(backup, "_replace", fail_once)
    with pytest.raises(backup.BackupError, match="previous files were restored"):
        backup.restore_snapshot(snapshot, tmp_path)
    assert {name: (tmp_path / name).read_bytes() for name in current} == current
    assert any(e["manifest"]["reason"] == "safety" for e in backup.list_snapshots(tmp_path))


def test_safety_snapshot_failure_prevents_all_restore_writes(tmp_path, monkeypatch):
    seed(tmp_path)
    snapshot = backup.create_snapshot(tmp_path)
    current = seed(tmp_path, "current")
    monkeypatch.setattr(backup, "create_snapshot", Mock(side_effect=OSError("synthetic")))
    with pytest.raises(OSError):
        backup.restore_snapshot(snapshot, tmp_path)
    assert {name: (tmp_path / name).read_bytes() for name in current} == current


@pytest.mark.parametrize("name", ["../config.json", "memos/../../outside.md",
                                  "memos/../config.json", "memos\\escape.md",
                                  "memos/escape:stream.md", "memos/trailing./x.md", "memos/CON.txt"])
def test_archive_traversal_rejected(tmp_path, name):
    put(tmp_path, "config.json", {})
    snapshot = backup.create_snapshot(tmp_path)
    def change(members):
        raw = members.pop("data/config.json")
        members["data/" + name] = raw
        manifest = json.loads(members["manifest.json"])
        manifest["files"][0]["path"] = name
        members["manifest.json"] = json.dumps(manifest).encode()
    rewrite(snapshot, change)
    with pytest.raises(backup.BackupError):
        backup.verify_snapshot(snapshot)


def test_oversize_archive_refused_before_reading(tmp_path, monkeypatch):
    put(tmp_path, "config.json", {})
    snapshot = backup.create_snapshot(tmp_path)
    monkeypatch.setattr(backup, "MAX_FILE_BYTES", 1)
    with pytest.raises(backup.BackupError, match="size"):
        backup.verify_snapshot(snapshot)


def test_pre_migration_hook_preserves_original_and_fails_closed(tmp_path, monkeypatch):
    put(tmp_path, "config.json", {"legacy": True})
    app = SimpleNamespace(config_path=tmp_path / "config.json")
    backup.before_config_load(app)
    entries = backup.list_snapshots(tmp_path)
    pre = next(e for e in entries if e["manifest"]["reason"] == "pre-change")
    assert json.loads(backup.verify_snapshot(pre["path"])[1]["config.json"]) == {"legacy": True}
    monkeypatch.setattr(backup, "create_snapshot", Mock(side_effect=OSError("synthetic")))
    with pytest.raises(OSError):
        backup.before_config_load(app)


def test_restore_for_app_calls_actual_reload_services(tmp_path, monkeypatch):
    seed(tmp_path)
    snapshot = backup.create_snapshot(tmp_path)
    put(tmp_path, "config.json", {"language": "fr"})
    app = SimpleNamespace(config_path=tmp_path / "config.json", config={"language": "fr"},
                          _config_lock=threading.Lock(), _config_load_failed=True,
                          dictionary_store=SimpleNamespace(load_training_data=Mock()),
                          command_executor=SimpleNamespace(reload_commands=Mock()))
    def reload():
        assert app._config_last_disk_snapshot == app.config
        app.config = json.loads(app.config_path.read_bytes())
    app.reload_config_from_disk = Mock(side_effect=reload)
    from samsara import plugin_commands
    monkeypatch.setattr(plugin_commands, "_shared_matcher", None)
    backup.restore_for_app(app, snapshot)
    assert app.config["language"] == "en"
    assert not app._config_load_failed
    app.reload_config_from_disk.assert_called_once()
    app.dictionary_store.load_training_data.assert_called_once()
    app.command_executor.reload_commands.assert_called_once()


def test_voice_route_opens_picker_on_qt_thread_without_restoring(monkeypatch):
    from plugins.commands import core_utils
    from samsara.ui import qt_runtime
    from samsara import plugin_commands
    plugin_commands._reinstall_module_commands(core_utils)
    monkeypatch.setattr(plugin_commands, "_shared_matcher", None)
    entry, remainder = plugin_commands.find_command("restore my settings")
    assert entry["func"] is core_utils.restore_my_settings and not remainder
    callbacks = []
    monkeypatch.setattr(qt_runtime, "post", callbacks.append)
    picker = Mock()
    monkeypatch.setattr(backup, "show_restore_dialog", picker)
    app = object()
    assert core_utils.restore_my_settings(app)
    picker.assert_not_called()
    callbacks.pop()()
    picker.assert_called_once_with(app)


def test_picker_disables_damaged_and_cancel_does_not_write(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QListWidget
    seed(tmp_path)
    backup.create_snapshot(tmp_path)
    broken = tmp_path / "backups" / "999999.zip"
    broken.write_bytes(b"damaged")
    before = (tmp_path / "config.json").read_bytes()
    def inspect(dialog):
        listing = dialog.findChild(QListWidget)
        assert "Damaged" in listing.item(0).text()
        assert not listing.item(0).flags() & Qt.ItemFlag.ItemIsEnabled
        assert listing.currentRow() == 1
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(QDialog, "exec", inspect)
    backup.show_restore_dialog(SimpleNamespace(config_path=tmp_path / "config.json"))
    assert (tmp_path / "config.json").read_bytes() == before


def test_updater_snapshot_failure_prevents_update_work(monkeypatch):
    from samsara import updater
    monkeypatch.setattr(updater, "_require_update_eligible", lambda: None)
    capture = Mock(side_effect=backup.BackupError("synthetic protection failure"))
    monkeypatch.setattr(backup, "create_snapshot", capture)
    with pytest.raises(backup.BackupError, match="protection failure"):
        updater.prepare_update(SimpleNamespace())
    capture.assert_called_once_with(reason="pre-change")


@pytest.mark.parametrize("snapshot_fails", [False, True])
def test_update_launch_protects_validated_data_before_starting_helper(monkeypatch, snapshot_fails):
    from samsara import updater, update_installer
    events = []
    prepared, runner = object(), Mock()
    monkeypatch.setattr(updater, "_require_update_eligible", lambda: events.append("eligible"))
    monkeypatch.setattr(updater, "_validate_prepared_update", lambda p: events.append("validate"))
    def capture(**kwargs):
        assert kwargs == {"reason": "pre-change"}
        events.append("snapshot")
        if snapshot_fails:
            raise backup.BackupError("protection unavailable")
    monkeypatch.setattr(backup, "create_snapshot", capture)
    launch = Mock(side_effect=lambda *args: events.append("launch"))
    cleanup = Mock(side_effect=lambda *args: events.append("cleanup"))
    monkeypatch.setattr(update_installer, "launch_helper", launch)
    monkeypatch.setattr(updater, "_cleanup_pre_swap_failure", cleanup)
    if snapshot_fails:
        with pytest.raises(updater.UpdateError, match="protection unavailable") as error:
            updater.launch_prepared_update(prepared, current_pid=957, process_runner=runner)
        assert isinstance(error.value.__cause__, backup.BackupError)
        assert events == ["eligible", "validate", "snapshot", "cleanup"]
        launch.assert_not_called()
        cleanup.assert_called_once_with(prepared, "Could not launch the update installer: protection unavailable")
    else:
        updater.launch_prepared_update(prepared, current_pid=957, process_runner=runner)
        assert events == ["eligible", "validate", "snapshot", "launch"]
        launch.assert_called_once_with(prepared, 957, runner)
        cleanup.assert_not_called()


def test_overnight_service_checks_next_day_and_is_registered_once(tmp_path, monkeypatch):
    from samsara.runtime import thread_registry
    seed(tmp_path)
    app = SimpleNamespace(config_path=tmp_path / "config.json", _config_lock=threading.Lock())
    tasks, shutdown = [], []
    def spawn(name, target, **kwargs):
        tasks.append(target)
        return object()
    monkeypatch.setattr(thread_registry, "spawn", spawn)
    monkeypatch.setattr(thread_registry, "on_shutdown", shutdown.append)
    backup.start_daily_service(app)
    backup.start_daily_service(app)
    assert len(tasks) == len(shutdown) == 1
    days = [datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc)]
    actual = backup.ensure_daily
    def daily(home):
        now = days.pop(0)
        actual(home, now=now)
        if not days:
            shutdown[0]()
    monkeypatch.setattr(backup, "ensure_daily", daily)
    monkeypatch.setattr(app._user_backup_stop, "wait", lambda _: None)
    tasks[0]()  # Run the real worker synchronously, with no sleep or app process.
    assert len(backup.list_snapshots(tmp_path)) == 2


def test_reuse_checks_every_file_and_rejects_corrupt_cached_snapshot(tmp_path):
    seed(tmp_path)
    first = backup.create_snapshot(tmp_path, reason="pre-change", reuse=True)
    assert backup.create_snapshot(tmp_path, reason="pre-change", reuse=True) == first
    put(tmp_path, "user_corrections.json", {"changed": "value"})
    second = backup.create_snapshot(tmp_path, reason="pre-change", reuse=True)
    assert second != first
    second.write_bytes(b"damaged")
    assert backup.create_snapshot(tmp_path, reason="pre-change", reuse=True) != second


def test_linked_profile_paths_are_refused_before_writes(tmp_path, monkeypatch):
    seed(tmp_path)
    original = backup._linked
    def linked(path):
        return path == tmp_path / "config.json" or original(path)
    monkeypatch.setattr(backup, "_linked", linked)
    with pytest.raises(backup.BackupError, match="Linked"):
        backup.create_snapshot(tmp_path)
    assert not (tmp_path / "backups").exists()


def test_restore_keeps_explicit_speech_model_choice_after_future_saves(tmp_path, monkeypatch):
    from samsara import model_guard, plugin_commands
    seed(tmp_path)
    put(tmp_path, "config.json", {"model_size": "large-v3"})
    snapshot = backup.create_snapshot(tmp_path)
    put(tmp_path, "config.json", {"model_size": "base"})
    app = SimpleNamespace(config_path=tmp_path / "config.json", config={"model_size": "base"},
                          _config_lock=threading.Lock(),
                          dictionary_store=SimpleNamespace(load_training_data=Mock()),
                          command_executor=SimpleNamespace(reload_commands=Mock()))
    app._model_guard = model_guard.ModelGuard(app)
    app._model_guard.write_checkpoint()
    app.reload_config_from_disk = lambda: setattr(app, "config", json.loads(app.config_path.read_bytes()))
    monkeypatch.setattr(plugin_commands, "_shared_matcher", None)
    backup.restore_for_app(app, snapshot)
    assert model_guard.protect_save(app, dict(app.config))["model_size"] == "large-v3"
    model_guard.loaded(app)
    assert app.config["model_size"] == "large-v3"


def test_settings_button_opens_shared_picker(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QPushButton
    from samsara.ui import settings_qt
    from samsara.ui.first_run_qt import ComponentsPage
    from tests.test_settings_pages import _StubApp, _schema_defaults
    monkeypatch.setattr(ComponentsPage, "refresh_async", lambda self: None)
    picker = Mock()
    monkeypatch.setattr(backup, "show_restore_dialog", picker)
    app = _StubApp(_schema_defaults())
    app.config_path = tmp_path / "config.json"
    window = settings_qt._SettingsWindow(app)
    try:
        window.findChild(QPushButton, "restoreUserSnapshotButton").click()
        picker.assert_called_once_with(app, window)
    finally:
        window.deleteLater()


def test_picker_confirmation_routes_selected_snapshot_and_closes_stale_settings(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog, QMessageBox, QPushButton, QWidget
    seed(tmp_path)
    snapshot = backup.create_snapshot(tmp_path)
    parent = QWidget()
    parent.show()
    app = SimpleNamespace(config_path=tmp_path / "config.json")
    restored = Mock()
    monkeypatch.setattr(backup, "restore_for_app", restored)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    def confirm(dialog):
        button = next(b for b in dialog.findChildren(QPushButton)
                      if b.text() == "Restore selected snapshot…")
        button.click()
        assert dialog.result() == QDialog.DialogCode.Accepted
        return dialog.result()
    monkeypatch.setattr(QDialog, "exec", confirm)
    backup.show_restore_dialog(app, parent)
    restored.assert_called_once_with(app, str(snapshot))
    assert not parent.isVisible()
    parent.deleteLater()
