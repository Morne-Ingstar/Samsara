"""Tests for AI-command-mode's miss-count/auto-exit state machine.

2026-07-19 nag incident: every unmatched utterance spoke "I didn't catch
a command in that" via TTS, indefinitely, with no miss-limit or auto-exit
-- unlike the hands-free command_mode's miss_limit (dictation.py), which
silently counts and exits. This mirrors that pattern but adds a graduated
escalation: spoken notice on the first miss, soft chime on the rest, and
a single spoken "AI command mode off" when the mode auto-exits.

See samsara/ai_command_mode.py's module docstring and
_register_miss / _register_hit / _process_utterance.
"""
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
    """ai_command_mode keeps queue/pending-plan state at module scope --
    reset it around every test so tests can't leak into each other."""
    ai_command_mode._pending_plan = None
    ai_command_mode._cancel.clear()
    yield
    ai_command_mode._pending_plan = None
    ai_command_mode._cancel.clear()


def _make_app(miss_limit=3, backend='ollama', commands=None):
    app = Mock()
    app.config = {
        'ai_command_mode': {
            'miss_limit': miss_limit,
            'backend': backend,
            'show_plan_hud': False,  # skip the 1.5s HUD-hide sleep in _execute_plan
        },
    }
    app._ai_cmd_miss_count = 0
    app._ai_cmd_generation = 0
    app.ai_command_mode_active = True
    app.command_executor = SimpleNamespace(
        commands=commands or {'screenshot': {'ai_visible': True}},
        execute_command=Mock(),
    )
    app.audio_coordinator = Mock()

    def _exit():
        app.ai_command_mode_active = False

    app.exit_ai_command_mode = Mock(side_effect=_exit)
    return app


def _spoken_texts(app):
    return [c.args[0] for c in app.audio_coordinator.speak.call_args_list]


class TestRegisterMiss:
    """Unit-level coverage of the escalation helper directly."""

    def test_first_miss_speaks_notice_only(self):
        app = _make_app(miss_limit=3)
        cfg = app.config['ai_command_mode']
        ai_command_mode._register_miss(app, cfg, 0)
        assert app._ai_cmd_miss_count == 1
        assert _spoken_texts(app) == ["I didn't catch a command in that."]
        app.play_sound.assert_not_called()

    def test_second_miss_chimes_not_speaks(self):
        app = _make_app(miss_limit=3)
        cfg = app.config['ai_command_mode']
        ai_command_mode._register_miss(app, cfg, 0)
        app.audio_coordinator.speak.reset_mock()
        ai_command_mode._register_miss(app, cfg, 0)
        assert app._ai_cmd_miss_count == 2
        assert _spoken_texts(app) == []
        app.play_sound.assert_called_once_with('scratch_refuse')

    def test_miss_limit_reached_speaks_off_and_exits(self):
        app = _make_app(miss_limit=3)
        cfg = app.config['ai_command_mode']
        ai_command_mode._register_miss(app, cfg, 0)
        ai_command_mode._register_miss(app, cfg, 0)
        app.audio_coordinator.speak.reset_mock()
        app.play_sound.reset_mock()
        ai_command_mode._register_miss(app, cfg, 0)
        assert app._ai_cmd_miss_count == 3
        assert _spoken_texts(app) == ["AI command mode off."]
        app.play_sound.assert_not_called()
        app.exit_ai_command_mode.assert_called_once()
        assert app.ai_command_mode_active is False

    def test_miss_limit_is_configurable(self):
        app = _make_app(miss_limit=1)
        cfg = app.config['ai_command_mode']
        ai_command_mode._register_miss(app, cfg, 0)
        assert app.exit_ai_command_mode.call_count == 1
        assert app.ai_command_mode_active is False


class TestRegisterHit:
    def test_hit_resets_counter(self):
        app = _make_app(miss_limit=3)
        app._ai_cmd_miss_count = 2
        ai_command_mode._register_hit(app)
        assert app._ai_cmd_miss_count == 0


class TestProcessUtteranceIntegration:
    """Exercises _register_miss/_register_hit through the real
    _process_utterance dispatch, not just the helpers directly."""

    def test_unresolved_utterance_counts_as_miss(self, monkeypatch):
        app = _make_app(miss_limit=3)
        monkeypatch.setattr(ai_command_mode, 'resolve_utterance', Mock(return_value=[]))
        ai_command_mode._process_utterance(app, 0, "complete gibberish")
        assert app._ai_cmd_miss_count == 1
        assert _spoken_texts(app) == ["I didn't catch a command in that."]

    def test_three_consecutive_misses_autoexit(self, monkeypatch):
        app = _make_app(miss_limit=3)
        monkeypatch.setattr(ai_command_mode, 'resolve_utterance', Mock(return_value=[]))
        for _ in range(3):
            ai_command_mode._process_utterance(app, 0, "still gibberish")
        assert app.ai_command_mode_active is False
        assert _spoken_texts(app)[-1] == "AI command mode off."

    def test_resolved_utterance_resets_miss_count(self, monkeypatch):
        app = _make_app(miss_limit=3)
        resolver = Mock(side_effect=[[], ['screenshot']])
        monkeypatch.setattr(ai_command_mode, 'resolve_utterance', resolver)
        ai_command_mode._process_utterance(app, 0, "gibberish")
        assert app._ai_cmd_miss_count == 1
        ai_command_mode._process_utterance(app, 0, "take a screenshot")
        assert app._ai_cmd_miss_count == 0
        # Miss counting starts over -- next miss speaks the notice again,
        # not the chime, proving the escalation state actually reset.
        app.audio_coordinator.speak.reset_mock()
        monkeypatch.setattr(ai_command_mode, 'resolve_utterance', Mock(return_value=[]))
        ai_command_mode._process_utterance(app, 0, "gibberish again")
        assert _spoken_texts(app) == ["I didn't catch a command in that."]

    def test_pending_plan_confirm_counts_as_hit_not_miss(self):
        app = _make_app(miss_limit=3)
        app._ai_cmd_miss_count = 2
        ai_command_mode._pending_plan = ['screenshot']
        ai_command_mode._process_utterance(app, 0, "yes")
        assert app._ai_cmd_miss_count == 0
        app.exit_ai_command_mode.assert_not_called()

    def test_pending_plan_deny_counts_as_hit_not_miss(self):
        app = _make_app(miss_limit=3)
        app._ai_cmd_miss_count = 2
        ai_command_mode._pending_plan = ['screenshot']
        ai_command_mode._process_utterance(app, 0, "no thanks")
        assert app._ai_cmd_miss_count == 0
        app.exit_ai_command_mode.assert_not_called()
