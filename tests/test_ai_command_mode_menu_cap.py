"""Tests for AI-command-mode's configurable menu cap.

resolve_utterance() used to silently truncate the command menu with a
hardcoded `menu[:200]` -- with the registry holding 288+ phrases plus
runtime plugin registrations, commands past the cut were simply never
resolvable, with no config to raise the limit and no log to explain why.

ai_command_mode.menu_limit now controls this (default 0 = no limit).
Truncation, when it does happen, is logged as a WARNING with exact
counts. See samsara/ai_command_mode.py's _capped_menu.
"""
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from samsara import ai_command_mode


@pytest.fixture(autouse=True)
def _reset_module_state():
    ai_command_mode._pending_plan = None
    ai_command_mode._cancel.clear()
    yield
    ai_command_mode._pending_plan = None
    ai_command_mode._cancel.clear()


def _make_app(menu_limit=0, num_commands=288, backend='ollama'):
    app = Mock()
    app.config = {
        'ai_command_mode': {
            'menu_limit': menu_limit,
            'backend': backend,
            'show_plan_hud': False,
        },
    }
    app._ai_cmd_miss_count = 0
    app.ai_command_mode_active = True
    commands = {f'command {i:03d}': {'ai_visible': True} for i in range(num_commands)}
    app.command_executor = SimpleNamespace(commands=commands, execute_command=Mock())
    app.audio_coordinator = Mock()
    app.exit_ai_command_mode = Mock()
    return app


class TestCappedMenuHelper:
    def test_default_limit_zero_returns_full_menu_untouched(self):
        menu = [f'cmd{i}' for i in range(288)]
        cfg = {'menu_limit': 0}
        assert ai_command_mode._capped_menu(menu, cfg) == menu

    def test_menu_shorter_than_limit_is_unaffected(self):
        menu = ['a', 'b', 'c']
        cfg = {'menu_limit': 200}
        assert ai_command_mode._capped_menu(menu, cfg) == menu

    def test_menu_longer_than_limit_is_truncated(self):
        menu = [f'cmd{i}' for i in range(250)]
        cfg = {'menu_limit': 200}
        result = ai_command_mode._capped_menu(menu, cfg)
        assert result == menu[:200]
        assert len(result) == 200

    def test_default_config_key_used_when_absent(self):
        menu = [f'cmd{i}' for i in range(300)]
        assert ai_command_mode._capped_menu(menu, {}) == menu  # _DEFAULTS["menu_limit"] == 0

    def test_truncation_logs_warning_with_counts(self, caplog):
        menu = [f'cmd{i}' for i in range(250)]
        cfg = {'menu_limit': 200}
        with caplog.at_level(logging.WARNING, logger="Samsara.samsara.ai_command_mode"):
            ai_command_mode._capped_menu(menu, cfg)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].message
        assert "250" in msg
        assert "200" in msg
        assert "50" in msg  # dropped count

    def test_no_truncation_no_warning(self, caplog):
        menu = [f'cmd{i}' for i in range(150)]
        cfg = {'menu_limit': 200}
        with caplog.at_level(logging.WARNING, logger="Samsara.samsara.ai_command_mode"):
            ai_command_mode._capped_menu(menu, cfg)
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_no_limit_no_warning_even_with_huge_menu(self, caplog):
        menu = [f'cmd{i}' for i in range(500)]
        cfg = {'menu_limit': 0}
        with caplog.at_level(logging.WARNING, logger="Samsara.samsara.ai_command_mode"):
            ai_command_mode._capped_menu(menu, cfg)
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


class TestProcessUtteranceMenuPassthrough:
    """Confirms _process_utterance actually wires _capped_menu into the
    resolver call, not just that the helper works in isolation."""

    def test_default_config_passes_untruncated_menu_to_resolver(self, monkeypatch):
        app = _make_app(menu_limit=0, num_commands=288)
        captured = {}

        def _fake_resolve(utterance, menu, model, host):
            captured['menu'] = menu
            return []

        monkeypatch.setattr(ai_command_mode, 'resolve_utterance', _fake_resolve)
        ai_command_mode._process_utterance(app, "do something")
        assert len(captured['menu']) == 288

    def test_low_menu_limit_truncates_menu_passed_to_resolver(self, monkeypatch, caplog):
        app = _make_app(menu_limit=50, num_commands=288)
        captured = {}

        def _fake_resolve(utterance, menu, model, host):
            captured['menu'] = menu
            return []

        monkeypatch.setattr(ai_command_mode, 'resolve_utterance', _fake_resolve)
        with caplog.at_level(logging.WARNING, logger="Samsara.samsara.ai_command_mode"):
            ai_command_mode._process_utterance(app, "do something")
        assert len(captured['menu']) == 50
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "288" in warnings[0].message
        assert "50" in warnings[0].message

    def test_cloud_backend_also_gets_capped_menu(self, monkeypatch):
        app = _make_app(menu_limit=50, num_commands=288, backend='cloud')
        captured = {}

        def _fake_resolve_cloud(utterance, menu, app_):
            captured['menu'] = menu
            return []

        monkeypatch.setattr(ai_command_mode, '_resolve_via_cloud', _fake_resolve_cloud)
        ai_command_mode._process_utterance(app, "do something")
        assert len(captured['menu']) == 50
