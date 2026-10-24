"""Tests for the Ava Front Door P1 config migration from the deleted
'ai_command_mode' config block to 'ava_command_session'.

G2 acceptance-gate coverage: "migration (keys, tutorial refs)".
Tutorial references: samsara/ui/tutorial_qt.py has no ai_command_mode/
command-mode-related strings at all (verified by inspection), so there is
there is nothing to migrate there -- this file covers config-key carryover.

Exercises the real bound DictationApp config migration via types.MethodType
against a minimal duck-typed `self`, same philosophy as the other migrated suites.
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
