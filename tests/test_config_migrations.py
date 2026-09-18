"""Config migrations must finish: once a legacy key is migrated, it stays gone.

perf_artifacts/boot_profile.md section 4: the three [MIGRATE] steps
(wake_targets, command_mode_enabled, ai_command_mode) re-ran on EVERY boot.
Each migration popped its legacy key and called save_config(), whose
three-way merge takes any key missing from memory but present on disk back
from disk -- so the save wrote the deleted key straight back, and every
config backup filled up with identical copies.

Exercises the real DictationApp.load_config/save_config against a temp
profile (never the live ~/.samsara).
"""
import json
import logging
import threading
from pathlib import Path

import pytest

import dictation

LEGACY_KEYS = ("wake_targets", "command_mode_enabled", "ai_command_mode")


def _legacy_config():
    """A settled current config plus the three orphaned legacy keys the live
    profile still carries."""
    app = _new_app(Path("unused-missing.json"))
    app.save_config = lambda: None
    app.load_config()
    cfg = json.loads(json.dumps(app.config))
    cfg["first_run_complete"] = True
    cfg["microphone"] = 1
    cfg["wake_word_config"]["enabled"] = True
    cfg["wake_targets"] = [{"id": "stale", "phrase": "old"}]
    cfg["command_mode_enabled"] = False
    cfg["ai_command_mode"] = {"enabled": True, "key": "left_alt", "menu_limit": 9}
    cfg.pop("ava_command_session", None)
    return cfg


def _new_app(config_path):
    app = dictation.DictationApp.__new__(dictation.DictationApp)
    app.config_path = Path(config_path)
    app._config_lock = threading.Lock()
    app._config_last_disk_snapshot = {}
    app._config_load_failed = False
    app._config_corrupt_backup_name = None
    return app


def _boot_load(config_path):
    app = _new_app(config_path)
    with app._config_lock:
        app.load_config()
    return app


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(dictation, "samsara_home_dir", lambda: tmp_path)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_legacy_config(), indent=2), encoding="utf-8")
    return path


def _migrate_lines(caplog):
    return [r.getMessage() for r in caplog.records if "[MIGRATE]" in r.getMessage()]


def test_first_load_migrates_and_the_keys_are_gone_on_disk(home, caplog):
    caplog.set_level(logging.INFO, logger=dictation.logger.name)

    app = _boot_load(home)

    assert len(_migrate_lines(caplog)) == 3
    on_disk = json.loads(home.read_text(encoding="utf-8"))
    for key in LEGACY_KEYS:
        assert key not in app.config
        assert key not in on_disk, f"{key} was written back by the migration's own save"
    assert on_disk["command_mode"]["command_matching_enabled"] is False
    assert on_disk["ava_command_session"]["key"] == "left_alt"
    assert "enabled" not in on_disk["wake_word_config"]


def test_second_load_runs_no_migrations(home, caplog):
    _boot_load(home)
    caplog.clear()
    caplog.set_level(logging.INFO, logger=dictation.logger.name)
    backups = home.parent / "config_backups"
    before = sorted(p.name for p in backups.glob("config-*.json"))
    before_bytes = home.read_bytes()
    before_mtime = home.stat().st_mtime_ns

    app = _boot_load(home)

    assert _migrate_lines(caplog) == []
    assert home.read_bytes() == before_bytes
    assert home.stat().st_mtime_ns == before_mtime
    assert sorted(p.name for p in backups.glob("config-*.json")) == before, (
        "a settled config still rotated a backup on load"
    )
    for key in LEGACY_KEYS:
        assert key not in app.config


def test_merge_bypass_is_scoped_to_the_migrations(home):
    """After load, a normal save must still honour an external edit made on
    disk -- the bypass may not leak out of load_config."""
    app = _boot_load(home)
    assert app._config_migration_save is False

    on_disk = json.loads(home.read_text(encoding="utf-8"))
    on_disk["external_edit_marker"] = "kept"
    home.write_text(json.dumps(on_disk), encoding="utf-8")
    app.config["runtime_marker"] = 1
    with app._config_lock:
        app.save_config()

    written = json.loads(home.read_text(encoding="utf-8"))
    assert written["external_edit_marker"] == "kept"
    assert written["runtime_marker"] == 1


def test_bypass_flag_is_cleared_when_a_migration_raises(home, monkeypatch):
    def boom(self):
        raise RuntimeError("migration blew up")

    monkeypatch.setattr(dictation.DictationApp, "_migrate_ai_command_mode_config", boom)
    app = _new_app(home)
    with app._config_lock, pytest.raises(RuntimeError):
        app.load_config()
    assert app._config_migration_save is False
