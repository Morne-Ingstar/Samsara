"""Tests for the 2026-07-16 correction-store hardening: success logging,
empty-overwrite guards, corrupt-file quarantine, and lazy path resolution.

Root cause fixed (2026-07-09 correction-store loss): all four writers
logged only on FAILURE, so a successful-looking save that actually lost
data left no trail; voice_training_qt.load_training_data caught a JSON
parse error, fell back to empty in-memory state, and the next
save_training_data() wrote that empty state straight over the file --
a read-fail -> silent-empty -> overwrite loop. Every test here targets
one link in that chain.

Every test that touches a real store sets SAMSARA_HOME_DIR via
monkeypatch BEFORE calling into the module, so writes land in tmp_path,
never ~/.samsara -- this is what the lazy-path-resolution fix (see
TestLazyPathResolution) makes possible: phonetic_wash/wake_corrections/
ava_corrections used to resolve their store path at IMPORT time via a
module-level constant, so setting the env var after import (the only way
a shared test-session import can isolate a per-test tmp_path) did nothing.
"""
import json
import logging
from pathlib import Path

import pytest


# ============================================================================
# Lazy path resolution -- the test-isolation hole itself. phonetic_wash,
# wake_corrections, and ava_corrections all import cleanly at module-
# collection time (before any test's monkeypatch.setenv runs) -- if their
# store path were still a module-level constant, every test below would
# silently touch ~/.samsara instead of tmp_path.
# ============================================================================

class TestLazyPathResolution:
    def test_phonetic_wash_path_honors_env_var_set_after_import(self, monkeypatch, tmp_path):
        import samsara.phonetic_wash as pw  # already imported by earlier tests/collection
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        assert pw._user_corrections_path() == tmp_path / "user_corrections.json"

    def test_wake_corrections_path_honors_env_var_set_after_import(self, monkeypatch, tmp_path):
        import samsara.wake_corrections as wc
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        assert wc._user_corrections_path() == tmp_path / "user_wake_corrections.json"

    def test_ava_corrections_path_honors_env_var_set_after_import(self, monkeypatch, tmp_path):
        import samsara.ava_corrections as ac
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        assert ac._corrections_path() == tmp_path / "ava_corrections.json"


# ============================================================================
# Empty-overwrite guard -- stores 1-3 (phonetic_wash, wake_corrections,
# ava_corrections). A write that would replace a non-empty on-disk store
# with an empty one is refused unless allow_empty=True.
# ============================================================================

class TestEmptyOverwriteGuardPhoneticWash:
    def test_refuses_empty_overwrite_of_nonempty_store(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.phonetic_wash as pw
        assert pw.set_user_corrections({"foo": "bar"}) is True

        with caplog.at_level(logging.ERROR):
            result = pw.set_user_corrections({})

        assert result is False
        assert pw.get_user_corrections() == {"foo": "bar"}
        assert any("refused to overwrite" in r.message for r in caplog.records)

    def test_allow_empty_bypasses_guard(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.phonetic_wash as pw
        pw.set_user_corrections({"foo": "bar"})

        assert pw.set_user_corrections({}, allow_empty=True) is True
        assert pw.get_user_corrections() == {}

    def test_empty_to_empty_is_not_refused(self, monkeypatch, tmp_path):
        """No prior non-empty file exists -- nothing to protect, so an
        empty write must succeed even with the default allow_empty=False."""
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.phonetic_wash as pw
        assert pw.set_user_corrections({}) is True


class TestEmptyOverwriteGuardWakeCorrections:
    def test_refuses_empty_overwrite_of_nonempty_store(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.wake_corrections as wc
        assert wc.set_user_corrections({"charviss": "jarvis"}) is True

        with caplog.at_level(logging.ERROR):
            result = wc.set_user_corrections({})

        assert result is False
        assert wc.get_user_corrections() == {"charviss": "jarvis"}
        assert any("refused to overwrite" in r.message for r in caplog.records)

    def test_allow_empty_bypasses_guard(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.wake_corrections as wc
        wc.set_user_corrections({"charviss": "jarvis"})

        assert wc.set_user_corrections({}, allow_empty=True) is True
        assert wc.get_user_corrections() == {}


class TestEmptyOverwriteGuardAvaCorrections:
    """ava_corrections' public API is add()/remove(), not a bulk
    set_user_corrections -- exercised at that level, plus a direct _save()
    check for the guard itself. _aliases is a module-level global shared
    across the test session, so every test clears it first."""

    def test_remove_last_alias_is_a_deliberate_clear_and_succeeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.ava_corrections as ac
        ac._aliases.clear()
        ac.add("db", "database")
        assert ac.total_count() == 1

        assert ac.remove("db") is True
        assert ac.total_count() == 0

        # Persisted, not just in-memory -- reload from disk and confirm.
        ac._load()
        assert ac._aliases == {}
        ac._aliases.clear()

    def test_save_refuses_to_blank_nonempty_store_without_allow_empty(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.ava_corrections as ac
        ac._aliases.clear()
        ac.add("db", "database")

        # An empty in-memory _aliases saved WITHOUT going through remove()'s
        # deliberate allow_empty=True -- must be refused.
        ac._aliases.clear()
        with caplog.at_level(logging.ERROR):
            result = ac._save()

        assert result is False
        assert any("refused to overwrite" in r.message for r in caplog.records)
        # On-disk data survived the refused write.
        ac._load()
        assert ac._aliases == {"db": {"expansion": "database", "created": ac._aliases["db"]["created"], "use_count": 0}}
        ac._aliases.clear()


# ============================================================================
# Corrupt-file quarantine -- store 4 (the actual 07-09 bug) and one of
# 1-3 (phonetic_wash). A file that exists but fails to parse is renamed
# aside (preserving the bytes) instead of silently treated as empty.
# ============================================================================

class TestCorruptFileQuarantinePhoneticWash:
    def test_corrupt_file_is_quarantined_not_silently_emptied(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.phonetic_wash as pw

        store_path = tmp_path / "user_corrections.json"
        store_path.write_text("{not valid json", encoding="utf-8")

        with caplog.at_level(logging.ERROR):
            result = pw.get_user_corrections()

        assert result == {}
        assert not store_path.exists()  # renamed aside, not left as-is
        quarantined = list(tmp_path.glob("user_corrections.json.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text(encoding="utf-8") == "{not valid json"
        assert any("quarantined to" in r.message for r in caplog.records)

    def test_save_after_quarantine_does_not_refuse_as_empty_overwrite(self, monkeypatch, tmp_path):
        """The guard in TestEmptyOverwriteGuard must not misread a
        just-quarantined corrupt file as 'non-empty data to protect' --
        the corrupt bytes are already safe in the quarantine file, so a
        fresh (even empty) write to the original path must proceed."""
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.phonetic_wash as pw

        store_path = tmp_path / "user_corrections.json"
        store_path.write_text("{not valid json", encoding="utf-8")

        assert pw.set_user_corrections({}) is True


class TestCorruptFileQuarantineVoiceTraining:
    def test_corrupt_training_data_is_quarantined_and_flagged(self, monkeypatch, tmp_path, caplog):
        from unittest.mock import Mock
        from samsara.ui.voice_training_qt import VoiceTrainingQt

        config_path = tmp_path / "config.json"
        training_path = tmp_path / "training_data.json"
        training_path.write_text('{"vocabulary": ["one"]}trailing garbage', encoding="utf-8")

        app = Mock()
        app.config_path = str(config_path)

        with caplog.at_level(logging.ERROR):
            vt = VoiceTrainingQt(app)

        assert vt.custom_vocab == []
        assert vt.corrections_dict == {}
        assert vt._load_failed is True
        assert not training_path.exists()
        quarantined = list(tmp_path.glob("training_data.json.corrupt-*"))
        assert len(quarantined) == 1
        assert vt._quarantine_path == quarantined[0]
        assert any("quarantined to" in r.message for r in caplog.records)

    def test_first_save_after_quarantine_logs_warning_once(self, monkeypatch, tmp_path, caplog):
        from unittest.mock import Mock
        from samsara.ui.voice_training_qt import VoiceTrainingQt

        config_path = tmp_path / "config.json"
        training_path = tmp_path / "training_data.json"
        training_path.write_text("not json", encoding="utf-8")

        app = Mock()
        app.config_path = str(config_path)
        vt = VoiceTrainingQt(app)
        assert vt._load_failed is True

        with caplog.at_level(logging.WARNING):
            assert vt.save_training_data() is True
        assert any("writing after a previous load failure" in r.message for r in caplog.records)
        assert vt._load_failed is False

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert vt.save_training_data() is True
        assert not any("writing after a previous load failure" in r.message for r in caplog.records)


# ============================================================================
# Success-log emission -- every writer logs at INFO on a successful save,
# not just on failure (the core 07-09 diagnosability gap).
# ============================================================================

class TestSuccessLogEmission:
    def test_phonetic_wash_logs_delta_on_save(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.phonetic_wash as pw
        pw.set_user_corrections({"a": "1", "b": "2"})

        caplog.clear()
        with caplog.at_level(logging.INFO):
            pw.set_user_corrections({"a": "1", "c": "3"})

        msg = next(r.message for r in caplog.records if "[STORE]" in r.message)
        assert "user_corrections.json saved: 2 entries" in msg
        assert "+1 added" in msg
        assert "-1 removed" in msg

    def test_wake_corrections_logs_delta_on_save(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.wake_corrections as wc

        with caplog.at_level(logging.INFO):
            wc.set_user_corrections({"charviss": "jarvis"})

        msg = next(r.message for r in caplog.records if "[STORE]" in r.message)
        assert "user_wake_corrections.json saved: 1 entries" in msg
        assert "+1 added" in msg

    def test_ava_corrections_logs_alias_count_on_save(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.ava_corrections as ac
        ac._aliases.clear()

        with caplog.at_level(logging.INFO):
            ac.add("db", "database")

        msg = next(r.message for r in caplog.records if "[STORE]" in r.message)
        assert "ava_corrections.json saved: 1 aliases" in msg
        ac._aliases.clear()

    def test_voice_training_logs_vocab_and_correction_counts_on_save(self, monkeypatch, tmp_path, caplog):
        from unittest.mock import Mock
        from samsara.ui.voice_training_qt import VoiceTrainingQt

        app = Mock()
        app.config_path = str(tmp_path / "config.json")
        vt = VoiceTrainingQt(app)
        vt.custom_vocab = ["alpha", "beta"]
        vt.corrections_dict = {"flat": "hat"}

        with caplog.at_level(logging.INFO):
            assert vt.save_training_data() is True

        msg = next(r.message for r in caplog.records if "[STORE]" in r.message)
        assert "training_data.json saved: 2 vocab, 1 corrections" in msg


# ============================================================================
# Pre-write backup (.bak) -- all four stores, single rolling backup.
# ============================================================================

class TestPreWriteBackup:
    def test_phonetic_wash_backs_up_before_overwrite(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.phonetic_wash as pw
        pw.set_user_corrections({"a": "1"})
        pw.set_user_corrections({"a": "1", "b": "2"})

        bak = tmp_path / "user_corrections.json.bak"
        assert bak.exists()
        assert json.loads(bak.read_text(encoding="utf-8")) == {"a": "1"}

    def test_no_backup_attempted_when_no_prior_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        import samsara.wake_corrections as wc
        wc.set_user_corrections({"a": "1"})

        assert not (tmp_path / "user_wake_corrections.json.bak").exists()
