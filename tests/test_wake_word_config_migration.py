"""Tests for _migrate_wake_word_config's idempotency fast path and the
stale wake_targets cleanup (boot-fix pass, 2026-07-23).

Diagnosed from a live boot log: this migration re-ran its full rename/
deep-merge logic on every boot even when the config was already fully
migrated, and an orphaned 'wake_targets' block survived forever once
'wake_profiles' also existed (the rename only fires when wake_profiles is
absent). Exercises the REAL bound DictationApp method via types.MethodType
against a minimal duck-typed `self`, same pattern as
test_ava_command_session_migration.py.
"""
import copy
import sys
import types
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation


DEFAULT_WAKE_WORD_CONFIG = {
    "enabled": True,
    "phrase": "jarvis",
    "phrase_options": ["jarvis", "hey jarvis"],
    "quick_silence_timeout": 1.0,
    "end_words": ["over", "done", "end dictation"],
    "wake_abort_phrase": ["cancel", "cancel dictation", "abort"],
    "pause_words": ["pause", "hold on", "wait"],
    "resume_words": ["resume", "continue", "go on"],
    "audio": {
        "speech_threshold": 0.5,
        "min_speech_duration": 0.3,
        "wake_detection_silence": 1.0,
        "wake_command_timeout": 8.0,
    },
    "feedback": {
        "play_sound_on_wake": True,
        "play_sound_on_end": True,
    },
}

DEFAULT_WAKE_PROFILES = [
    {
        "id": "claude",
        "phrase": "activate claude",
        "target_process": "claude.exe",
        "enabled": True,
        "mode": "focus_dictate",
        "send_word": "over",
    },
]

DEFAULT_CONFIG = {
    "mode": "hold",
    "wake_word_config": DEFAULT_WAKE_WORD_CONFIG,
    "wake_profiles": DEFAULT_WAKE_PROFILES,
}


def _make_app(config):
    app = types.SimpleNamespace()
    app.config = config
    app.save_config = Mock()
    app._deep_update = types.MethodType(dictation.DictationApp._deep_update, app)
    app._wake_word_config_already_migrated = types.MethodType(
        dictation.DictationApp._wake_word_config_already_migrated, app,
    )
    app._migrate_wake_word_config = types.MethodType(
        dictation.DictationApp._migrate_wake_word_config, app,
    )
    return app


def _already_migrated_config():
    return {
        "mode": "hold",
        "wake_word_config": copy.deepcopy(DEFAULT_WAKE_WORD_CONFIG),
        "wake_profiles": copy.deepcopy(DEFAULT_WAKE_PROFILES),
    }


class TestNoOpFastPath:
    def test_already_migrated_config_does_not_save(self):
        app = _make_app(_already_migrated_config())
        app._migrate_wake_word_config(DEFAULT_CONFIG)
        app.save_config.assert_not_called()

    def test_already_migrated_config_is_detected_as_no_op(self):
        app = _make_app(_already_migrated_config())
        assert app._wake_word_config_already_migrated() is True

    def test_already_migrated_config_is_unchanged(self):
        config = _already_migrated_config()
        before = copy.deepcopy(config)
        app = _make_app(config)
        app._migrate_wake_word_config(DEFAULT_CONFIG)
        assert app.config == before

    def test_no_op_path_still_validates_profiles(self):
        # A hand-edited, too-short phrase must still be caught even on the
        # no-op fast path -- validate_wake_profiles is cheap and kept as a
        # safety net, unlike the deep-merge/save this path skips.
        config = _already_migrated_config()
        config["wake_profiles"][0]["phrase"] = "hi"  # 1 syllable, below floor
        app = _make_app(config)
        app._migrate_wake_word_config(DEFAULT_CONFIG)
        assert app.config["wake_profiles"][0]["enabled"] is False
        app.save_config.assert_not_called()


class TestLegacyKeysStillMigrateFully:
    def test_wake_word_mode_alias_still_migrates(self):
        config = _already_migrated_config()
        config["mode"] = "wake_word"
        app = _make_app(config)
        assert app._wake_word_config_already_migrated() is False
        app._migrate_wake_word_config(DEFAULT_CONFIG)
        assert app.config["mode"] == "hold"
        assert app.config["wake_word_enabled"] is True

    def test_missing_wake_word_config_still_migrates(self):
        config = {"mode": "hold", "wake_profiles": copy.deepcopy(DEFAULT_WAKE_PROFILES)}
        app = _make_app(config)
        assert app._wake_word_config_already_migrated() is False
        app._migrate_wake_word_config(DEFAULT_CONFIG)
        assert app.config["wake_word_config"] == DEFAULT_WAKE_WORD_CONFIG
        app.save_config.assert_called_once()

    def test_profile_with_legacy_send_policy_still_migrates(self):
        config = _already_migrated_config()
        del config["wake_profiles"][0]["mode"]
        del config["wake_profiles"][0]["send_word"]
        config["wake_profiles"][0]["send_policy"] = "stage_only"
        app = _make_app(config)
        assert app._wake_word_config_already_migrated() is False
        app._migrate_wake_word_config(DEFAULT_CONFIG)
        assert app.config["wake_profiles"][0]["mode"] == "stage_send"
        assert "send_policy" not in app.config["wake_profiles"][0]


class TestStaleWakeTargetsCleanup:
    def test_wake_targets_removed_when_wake_profiles_also_present(self):
        config = _already_migrated_config()
        config["wake_targets"] = [{"id": "stale", "phrase": "old stale entry"}]
        app = _make_app(config)
        app._migrate_wake_word_config(DEFAULT_CONFIG)
        assert "wake_targets" not in app.config
        # wake_profiles keeps its own (authoritative) contents, not merged
        # with the stale block.
        assert app.config["wake_profiles"] == DEFAULT_WAKE_PROFILES
        app.save_config.assert_called_once()

    def test_wake_targets_renamed_when_wake_profiles_absent(self):
        stale_targets = [{"id": "renamed", "phrase": "activate renamed", "mode": "focus_dictate", "send_word": "over"}]
        config = {
            "mode": "hold",
            "wake_word_config": copy.deepcopy(DEFAULT_WAKE_WORD_CONFIG),
            "wake_targets": copy.deepcopy(stale_targets),
        }
        app = _make_app(config)
        app._migrate_wake_word_config(DEFAULT_CONFIG)
        assert "wake_targets" not in app.config
        assert app.config["wake_profiles"][0]["id"] == "renamed"
