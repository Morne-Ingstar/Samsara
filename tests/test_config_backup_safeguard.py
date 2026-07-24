"""Regression tests for the config-backup safeguard (2026-07-2x incident:
config.json was silently wiped to defaults during a boot crash -- most
likely load_config()'s own `else: self.config = default_config; ...;
self.save_config()` fallback, which used to run on ANY read failure, not
just "no file existed at all").

Exercises the REAL bound DictationApp methods via types.MethodType against
a minimal duck-typed `self` (same philosophy as tests/test_wake_word_
config_migration.py and the *_ava_command_session_* suites), with
dictation.samsara_home_dir() monkeypatched to a pytest tmp_path so backups
land in an isolated directory instead of the real ~/.samsara.
"""
import json
import shutil
import sys
import threading
import types
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation

_BOUND_METHODS = (
    'load_config', 'save_config', 'persist_config',
    '_quarantine_corrupt_config', '_config_backups_dir',
    '_rotate_config_backup', '_prune_config_backups',
    '_write_last_known_good', 'list_config_backups',
    '_migrate_wake_word_config', '_wake_word_config_already_migrated',
    '_deep_update', '_migrate_command_matching_enabled_flag',
    '_migrate_ai_command_mode_config',
)


def _make_app(tmp_path, monkeypatch):
    monkeypatch.setattr(dictation, 'samsara_home_dir', lambda: tmp_path)
    app = types.SimpleNamespace()
    app.config_path = tmp_path / "config.json"
    app._config_lock = threading.Lock()
    app._config_last_disk_snapshot = {}
    app.config = {}
    app._config_load_failed = False
    app._config_corrupt_backup_name = None
    app._CONFIG_BACKUP_DIRNAME = dictation.DictationApp._CONFIG_BACKUP_DIRNAME
    app._CONFIG_BACKUP_KEEP = dictation.DictationApp._CONFIG_BACKUP_KEEP
    app._LAST_KNOWN_GOOD_FILENAME = dictation.DictationApp._LAST_KNOWN_GOOD_FILENAME
    for name in _BOUND_METHODS:
        setattr(app, name, types.MethodType(getattr(dictation.DictationApp, name), app))
    return app


def _backups_dir(tmp_path):
    return tmp_path / "config_backups"


class TestRollingBackupsOnSave:
    def test_no_backup_on_the_very_first_save(self, tmp_path, monkeypatch):
        """Nothing exists on disk yet to back up."""
        app = _make_app(tmp_path, monkeypatch)
        app.config = {"hello": "world"}
        with app._config_lock:
            app.save_config()
        backups_dir = _backups_dir(tmp_path)
        assert not backups_dir.exists() or not any(backups_dir.glob("config-*.json"))

    def test_backup_created_on_second_save(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.config = {"hello": "world"}
        with app._config_lock:
            app.save_config()
        app.config = {"hello": "world2"}
        with app._config_lock:
            app.save_config()

        backups = list(_backups_dir(tmp_path).glob("config-*.json"))
        assert len(backups) == 1
        # It's the PRE-write state that got backed up, not the new one.
        assert json.loads(backups[0].read_text()) == {"hello": "world"}

    def test_pruning_keeps_newest_15(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        for i in range(20):
            app.config = {"n": i}
            with app._config_lock:
                app.save_config()

        files = list(_backups_dir(tmp_path).glob("config-*.json"))
        assert len(files) == 15

    def test_pruning_keeps_the_newest_ones_by_mtime_not_oldest(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        for i in range(20):
            app.config = {"n": i}
            with app._config_lock:
                app.save_config()

        files = list(_backups_dir(tmp_path).glob("config-*.json"))
        contents = {json.loads(f.read_text())["n"] for f in files}
        # Backups 0..18 are possible snapshot values (save #20 has no
        # backup of itself yet); the newest 15 surviving must be the
        # highest-numbered ones.
        assert contents == set(range(4, 19))


class TestLastKnownGood:
    def test_not_written_by_ordinary_saves(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        for i in range(5):
            app.config = {"n": i}
            with app._config_lock:
                app.save_config()
        assert not (_backups_dir(tmp_path) / "last_known_good.json").exists()

    def test_written_only_when_explicitly_called(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.config = {"x": 1}
        with app._config_lock:
            app.save_config()

        app._write_last_known_good()

        lkg = _backups_dir(tmp_path) / "last_known_good.json"
        assert lkg.exists()
        assert json.loads(lkg.read_text()) == {"x": 1}

    def test_reflects_the_latest_saved_state(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.config = {"x": 1}
        with app._config_lock:
            app.save_config()
        app.config = {"x": 2}
        with app._config_lock:
            app.save_config()
        app._write_last_known_good()

        lkg = _backups_dir(tmp_path) / "last_known_good.json"
        assert json.loads(lkg.read_text()) == {"x": 2}


class TestCorruptConfigQuarantined:
    def test_unreadable_config_is_renamed_not_overwritten(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.config_path.write_text("{not valid json", encoding="utf-8")
        original_bytes = app.config_path.read_bytes()

        with app._config_lock:
            app.load_config()

        assert not app.config_path.exists(), "corrupt file must be moved aside, not left in place"
        quarantined = list(tmp_path.glob("config.corrupt-*.json"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == original_bytes, "bytes must be preserved verbatim"

    def test_quarantine_never_deletes_bak_recovery_succeeds_instead(self, tmp_path, monkeypatch):
        """When config.json.bak parses fine, that's a SUCCESSFUL load
        (not the failure this safeguard targets) -- no quarantine, no
        latch, and the corrupt primary file is left alone (still evidence,
        just not renamed)."""
        app = _make_app(tmp_path, monkeypatch)
        app.config_path.write_text("{not valid json", encoding="utf-8")
        bak_path = app.config_path.with_suffix('.json.bak')
        bak_path.write_text(json.dumps({"wake_profiles": [], "wake_word_config": {
            "enabled": True, "phrase": "jarvis",
        }, "recovered": True}), encoding="utf-8")

        with app._config_lock:
            app.load_config()

        assert app._config_load_failed is False
        assert app.config.get("recovered") is True
        assert not list(tmp_path.glob("config.corrupt-*.json"))

    def test_flags_and_backup_name_set_for_ui_warning(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.config_path.write_text("not json at all", encoding="utf-8")

        with app._config_lock:
            app.load_config()

        assert app._config_load_failed is True
        quarantined = list(tmp_path.glob("config.corrupt-*.json"))
        assert app._config_corrupt_backup_name == quarantined[0].name


class TestStructuralConfigValidation:
    @pytest.mark.parametrize("payload", [[], None, "not-a-dict"])
    def test_non_dict_root_is_quarantined(self, payload, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.config_path.write_text(json.dumps(payload), encoding="utf-8")

        with app._config_lock:
            app.load_config()

        assert app._config_load_failed is True
        assert app.config.get("mode") == "hold"
        assert not app.config_path.exists()
        quarantined = list(tmp_path.glob("config.corrupt-*.json"))
        assert len(quarantined) == 1
        assert app._config_corrupt_backup_name == quarantined[0].name

    def test_mis_typed_wake_word_config_is_quarantined(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.config_path.write_text(json.dumps({
            "wake_word_config": [],
            "wake_profiles": [],
            "mode": "hold",
        }), encoding="utf-8")

        with app._config_lock:
            app.load_config()

        assert app._config_load_failed is True
        assert app.config.get("mode") == "hold"
        assert not app.config_path.exists()
        assert list(tmp_path.glob("config.corrupt-*.json"))

    def test_mis_typed_wake_profiles_is_quarantined(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.config_path.write_text(json.dumps({
            "wake_profiles": {},
            "wake_word_config": {
                "enabled": True,
                "phrase": "jarvis",
                "phrase_options": ["jarvis"],
                "quick_silence_timeout": 1.0,
                "end_words": ["over"],
                "wake_abort_phrase": ["cancel"],
                "pause_words": ["pause"],
                "resume_words": ["resume"],
                "audio": {"speech_threshold": 0.5, "min_speech_duration": 0.2},
            },
        }), encoding="utf-8")

        with app._config_lock:
            app.load_config()

        assert app._config_load_failed is True
        assert app.config.get("mode") == "hold"
        assert app.config.get("wake_word_config", {}).get("phrase") == "jarvis"
        assert not app.config_path.exists()
        assert list(tmp_path.glob("config.corrupt-*.json"))

    def test_unknown_valid_keys_are_preserved(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.config_path.write_text(json.dumps({
            "unknown_root_key": {"keep": True},
            "wake_profiles": [],
            "wake_word_config": {
                "enabled": True,
                "phrase": "jarvis",
                "phrase_options": ["jarvis"],
                "quick_silence_timeout": 1.0,
                "end_words": ["over"],
                "wake_abort_phrase": ["cancel"],
                "pause_words": ["pause"],
                "resume_words": ["resume"],
                "audio": {"speech_threshold": 0.5, "min_speech_duration": 0.2},
            },
        }), encoding="utf-8")

        with app._config_lock:
            app.load_config()

        assert app._config_load_failed is False
        assert app.config["unknown_root_key"] == {"keep": True}
        assert app.config.get("mode") == "hold"


class TestSaveLatchAfterFailedLoad:
    def test_save_is_blocked_after_a_failed_load(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.config_path.write_text("{not valid json", encoding="utf-8")
        with app._config_lock:
            app.load_config()
        assert app._config_load_failed is True

        app.config["new_key"] = "should never reach disk"
        with app._config_lock:
            app.save_config()

        # The blocked save must not have recreated config.json.
        assert not app.config_path.exists()

    def test_persist_config_also_respects_the_latch(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.config_path.write_text("{not valid json", encoding="utf-8")
        with app._config_lock:
            app.load_config()

        app.persist_config()  # acquires the lock itself, delegates to save_config

        assert not app.config_path.exists()

    def test_save_works_normally_when_latch_is_clear(self, tmp_path, monkeypatch):
        """Sanity check the latch itself, not just its absence of effect:
        a fresh app (no failed load) saves normally."""
        app = _make_app(tmp_path, monkeypatch)
        app.config = {"fine": True}
        with app._config_lock:
            app.save_config()
        assert app.config_path.exists()
        assert json.loads(app.config_path.read_text()) == {"fine": True}


class TestRestorePathRoundTrips:
    def test_backup_content_matches_prior_save(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.config = {"round": "trip", "value": 42}
        with app._config_lock:
            app.save_config()
        app.config = {"round": "changed"}
        with app._config_lock:
            app.save_config()  # backs up the {"round": "trip", "value": 42} state

        entries = app.list_config_backups()
        assert len(entries) == 1
        _label, path = entries[0]
        assert json.loads(path.read_text()) == {"round": "trip", "value": 42}

    def test_copying_a_backup_into_place_restores_it(self, tmp_path, monkeypatch):
        """Core action of Settings' "Restore from backup" control
        (samsara/ui/settings_qt.py's _restore_from_backup): a plain file
        copy of the chosen backup over config.json."""
        app = _make_app(tmp_path, monkeypatch)
        app.config = {"round": "trip", "value": 42}
        with app._config_lock:
            app.save_config()
        app.config = {"round": "changed"}
        with app._config_lock:
            app.save_config()

        _label, backup_path = app.list_config_backups()[0]
        shutil.copy2(backup_path, app.config_path)

        assert json.loads(app.config_path.read_text()) == {"round": "trip", "value": 42}

    def test_list_includes_last_known_good_newest_first(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.config = {"v": 1}
        with app._config_lock:
            app.save_config()
        app._write_last_known_good()
        app.config = {"v": 2}
        with app._config_lock:
            app.save_config()  # rolling backup of {"v": 1}

        entries = app.list_config_backups()
        paths = [p for _label, p in entries]
        assert any(p.name == "last_known_good.json" for p in paths)
        assert any(p.name.startswith("config-") for p in paths)
        mtimes = [p.stat().st_mtime for _label, p in entries]
        assert mtimes == sorted(mtimes, reverse=True)

    def test_empty_when_no_backups_exist_yet(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        assert app.list_config_backups() == []


class TestTrueFirstRunStillWritesDefaults:
    def test_no_config_file_at_all_writes_defaults(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        assert not app.config_path.exists()

        with app._config_lock:
            app.load_config()

        assert app.config_path.exists()
        assert app._config_load_failed is False
        on_disk = json.loads(app.config_path.read_text())
        assert on_disk.get('mode') == 'hold'
        assert 'wake_profiles' in on_disk

    def test_true_first_run_does_not_quarantine_anything(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        with app._config_lock:
            app.load_config()
        assert not list(tmp_path.glob("config.corrupt-*.json"))
