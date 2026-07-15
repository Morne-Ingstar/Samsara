import json
import os

from samsara.paths import (
    migrate_legacy_source_config,
    samsara_config_path,
    samsara_home_dir,
)


def test_config_uses_per_user_profile_without_override(monkeypatch, tmp_path):
    monkeypatch.delenv("SAMSARA_HOME_DIR", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert samsara_home_dir() == tmp_path / ".samsara"
    assert samsara_config_path() == tmp_path / ".samsara" / "config.json"


def test_config_honors_isolated_home_override(monkeypatch, tmp_path):
    isolated = tmp_path / "preview-profile"
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(isolated))

    assert samsara_config_path() == isolated / "config.json"


def test_newer_legacy_source_config_is_migrated_once(monkeypatch, tmp_path):
    monkeypatch.delenv("SAMSARA_HOME_DIR", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    target = samsara_config_path()
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"profile": "old-home"}), encoding="utf-8")
    legacy = tmp_path / "repo" / "config.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"profile": "current-source"}), encoding="utf-8")
    newer = target.stat().st_mtime + 10
    os.utime(legacy, (newer, newer))

    assert migrate_legacy_source_config(legacy) is True
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "profile": "current-source"
    }

    # The marker protects subsequent per-user changes from the old source file.
    target.write_text(json.dumps({"profile": "later-home"}), encoding="utf-8")
    assert migrate_legacy_source_config(legacy) is False
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "profile": "later-home"
    }


def test_source_migration_never_touches_explicit_isolated_profile(monkeypatch, tmp_path):
    isolated = tmp_path / "isolated"
    legacy = tmp_path / "repo-config.json"
    legacy.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(isolated))

    assert migrate_legacy_source_config(legacy) is False
    assert not isolated.exists()
