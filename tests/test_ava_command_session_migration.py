"""Tests for the Ava Front Door P1 one-time migration from the deleted
'ai_command_mode' config block to 'ava_command_session', and the
one-time first-run-after-update notice (spec v2 "Migration" section).

G2 acceptance-gate coverage: "migration (keys, notice, tutorial refs)".
Tutorial references: samsara/ui/tutorial_qt.py has no ai_command_mode/
command-mode-related strings at all (verified by inspection), so there is
nothing to migrate there -- this file covers the two things that DO exist:
config-key carryover and the first-run notice.

Exercises the REAL bound DictationApp methods
(_migrate_ai_command_mode_config / _maybe_announce_ava_command_session_migration)
via types.MethodType against a minimal duck-typed `self`, same philosophy
as the other migrated suites in this pass.
"""
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation


def _make_app(config=None):
    app = types.SimpleNamespace()
    app.config = config if config is not None else {}
    app.save_config = Mock()
    app._migrate_ai_command_mode_config = types.MethodType(
        dictation.DictationApp._migrate_ai_command_mode_config, app,
    )
    return app


class TestConfigKeyMigration:
    def test_no_op_when_old_key_absent(self):
        app = _make_app(config={'ava_command_session': {'enabled': True}})
        app._migrate_ai_command_mode_config()
        assert app.config == {'ava_command_session': {'enabled': True}}
        app.save_config.assert_not_called()

    def test_no_op_on_a_fresh_install_with_neither_key(self):
        app = _make_app(config={})
        app._migrate_ai_command_mode_config()
        assert app.config == {}
        app.save_config.assert_not_called()

    def test_meaningful_keys_carried_over_verbatim(self):
        old = {
            'enabled': True, 'key': 'right_ctrl', 'backend': 'ollama',
            'model': 'llama3.2:3b', 'queue_depth_cap': 5, 'keep_warm': False,
            'miss_limit': 4, 'ready_cue_enabled': False,
            'ready_cue_dir': 'custom/cues',
        }
        app = _make_app(config={'ai_command_mode': dict(old)})
        app._migrate_ai_command_mode_config()
        assert app.config['ava_command_session'] == old
        assert 'ai_command_mode' not in app.config
        app.save_config.assert_called_once()

    def test_dropped_keys_are_not_carried_over(self):
        old = {
            'enabled': True, 'key': 'right_ctrl',
            'wake_phrase': 'command mode',
            'show_plan_hud': True,
            'step_settle_seconds': 0.4,
            'menu_limit': 200,
        }
        app = _make_app(config={'ai_command_mode': dict(old)})
        app._migrate_ai_command_mode_config()
        migrated = app.config['ava_command_session']
        assert migrated == {'enabled': True, 'key': 'right_ctrl'}
        for dropped_key in ('wake_phrase', 'show_plan_hud', 'step_settle_seconds', 'menu_limit'):
            assert dropped_key not in migrated

    def test_old_key_removed_from_config_regardless(self):
        app = _make_app(config={'ai_command_mode': {'enabled': False}})
        app._migrate_ai_command_mode_config()
        assert 'ai_command_mode' not in app.config

    def test_existing_ava_command_session_block_wins_over_carried_values(self):
        """An already-migrated (or freshly user-configured) new block must
        not be clobbered by stale carried-over values -- 'existing' wins
        in the merge."""
        app = _make_app(config={
            'ai_command_mode': {'enabled': False, 'key': 'right_ctrl'},
            'ava_command_session': {'enabled': True},
        })
        app._migrate_ai_command_mode_config()
        assert app.config['ava_command_session']['enabled'] is True  # existing wins
        assert app.config['ava_command_session']['key'] == 'right_ctrl'  # carried fills the gap

    def test_migration_persists_via_save_config(self):
        app = _make_app(config={'ai_command_mode': {'enabled': True}})
        app._migrate_ai_command_mode_config()
        app.save_config.assert_called_once_with()


class _FakeHints:
    def __init__(self, shown=None):
        self._shown = set(shown or ())
        self.maybe_show_calls = []

    def maybe_show(self, hint_id, message):
        self.maybe_show_calls.append((hint_id, message))
        self._shown.add(hint_id)


def _make_notice_app(hints=None, audio_coordinator=None):
    app = types.SimpleNamespace()
    if hints is not None:
        app.hints = hints
    if audio_coordinator is not None:
        app.audio_coordinator = audio_coordinator
    app._maybe_announce_ava_command_session_migration = types.MethodType(
        dictation.DictationApp._maybe_announce_ava_command_session_migration, app,
    )
    return app


class TestFirstRunMigrationNotice:
    def test_fresh_notice_shows_toast_and_speaks_once(self):
        hints = _FakeHints()
        speak = Mock()
        app = _make_notice_app(hints=hints, audio_coordinator=types.SimpleNamespace(speak=speak))
        app._maybe_announce_ava_command_session_migration()
        assert hints.maybe_show_calls == [
            ("ava_command_session_migration_v1", "Left-Alt is now an Ava command session.")
        ]
        speak.assert_called_once_with(
            "Left-Alt is now an Ava command session.", category="system_notice",
        )

    def test_already_shown_hint_does_not_speak_again(self):
        hints = _FakeHints(shown={"ava_command_session_migration_v1"})
        speak = Mock()
        app = _make_notice_app(hints=hints, audio_coordinator=types.SimpleNamespace(speak=speak))
        app._maybe_announce_ava_command_session_migration()
        # maybe_show is still called (HintManager's own idempotency owns
        # the toast-suppression logic) -- but the TTS half must not repeat.
        assert hints.maybe_show_calls == [
            ("ava_command_session_migration_v1", "Left-Alt is now an Ava command session.")
        ]
        speak.assert_not_called()

    def test_no_hints_manager_is_a_safe_noop(self):
        app = _make_notice_app(hints=None)
        app._maybe_announce_ava_command_session_migration()  # must not raise

    def test_no_audio_coordinator_is_a_safe_noop(self):
        hints = _FakeHints()
        app = _make_notice_app(hints=hints, audio_coordinator=None)
        app._maybe_announce_ava_command_session_migration()  # must not raise
        assert hints.maybe_show_calls == [
            ("ava_command_session_migration_v1", "Left-Alt is now an Ava command session.")
        ]

    def test_tts_exception_is_swallowed(self):
        hints = _FakeHints()
        speak = Mock(side_effect=RuntimeError("tts backend down"))
        app = _make_notice_app(hints=hints, audio_coordinator=types.SimpleNamespace(speak=speak))
        app._maybe_announce_ava_command_session_migration()  # must not raise
