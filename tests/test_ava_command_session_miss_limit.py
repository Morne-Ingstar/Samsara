"""Tests for the Ava command session's miss-count/auto-exit state machine.

2026-07-19 nag incident: every unmatched utterance spoke "I didn't catch
a command in that" via TTS, indefinitely, with no miss-limit or auto-exit
-- unlike the hands-free command_mode's miss_limit (dictation.py), which
silently counts and exits. This mirrors that pattern but adds a graduated
escalation: spoken notice on the first miss, soft chime on the rest, and
a single spoken "Ava command session off" when the session auto-exits.

Ava Front Door P1: migrated from tests/test_ai_command_mode_miss_limit.py
(deleted) -- ai_command_mode.py was replaced by
samsara/ava_command_session.py, whose _register_miss/_register_hit are a
verbatim carry-over of this escalation (module docstring: "Miss handling
(PRESERVED from ai_command_mode, per spec's behavior inventory)").

Two tests from the old suite are NOT migrated:
"pending_plan confirm/deny counts as hit not miss" -- the old module had
its own separate _pending_plan confirmation state checked inside
_process_utterance. The new architecture has no _pending_plan: "yes"/
"confirm it" is a REGULAR REGISTERED COMMAND (plugins/commands/ask_ollama.py's
handle_ava_confirm, @command("yes", ...)) that stage (a)'s existing
CommandExecutor.process_text() already matches like any other command --
so a confirm utterance resolving as a stage (a) hit (and resetting the
miss counter via the generic _register_hit path) is already covered by
test_resolved_utterance_resets_miss_count below; there is no
confirmation-specific branch left inside _process_utterance to test
separately. "no"/deny has no equivalent registered command at all -- a
denial is intercepted by dictation.py's unified scratch-that check
BEFORE the utterance ever reaches enqueue_utterance/_process_utterance
(see _handle_ava_command_utterance), so that behavior is out of this
module's scope; it belongs to (and is covered by) the confirmation-binding
G2 test matrix row instead.

See samsara/ava_command_session.py's module docstring and
_register_miss / _register_hit / _process_utterance.
"""
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from samsara import ava_command_session


@pytest.fixture(autouse=True)
def _reset_module_state():
    """ava_command_session keeps queue/cancel state at module scope --
    reset it around every test so tests can't leak into each other."""
    ava_command_session._cancel.clear()
    yield
    ava_command_session._cancel.clear()


def _make_app(miss_limit=3, backend='ollama'):
    app = Mock()
    app.config = {
        'ava_command_session': {
            'miss_limit': miss_limit,
            'backend': backend,
        },
    }
    app._ava_cmd_miss_count = 0
    app._ava_cmd_generation = 0
    app.ava_command_session_active = True
    app.command_executor = Mock()
    app.command_executor.process_text = Mock(return_value=(None, False))  # default: stage (a) miss
    app.audio_coordinator = Mock()

    def _exit():
        app.ava_command_session_active = False

    app.exit_ava_command_session = Mock(side_effect=_exit)
    return app


def _spoken_texts(app):
    return [c.args[0] for c in app.audio_coordinator.speak.call_args_list]


class TestRegisterMiss:
    """Unit-level coverage of the escalation helper directly."""

    def test_first_miss_speaks_notice_only(self):
        app = _make_app(miss_limit=3)
        cfg = app.config['ava_command_session']
        ava_command_session._register_miss(app, cfg, 0)
        assert app._ava_cmd_miss_count == 1
        assert _spoken_texts(app) == ["I didn't catch a command in that."]
        app.play_sound.assert_not_called()

    def test_second_miss_chimes_not_speaks(self):
        app = _make_app(miss_limit=3)
        cfg = app.config['ava_command_session']
        ava_command_session._register_miss(app, cfg, 0)
        app.audio_coordinator.speak.reset_mock()
        ava_command_session._register_miss(app, cfg, 0)
        assert app._ava_cmd_miss_count == 2
        assert _spoken_texts(app) == []
        app.play_sound.assert_called_once_with('scratch_refuse')

    def test_miss_limit_reached_speaks_off_and_exits(self):
        app = _make_app(miss_limit=3)
        cfg = app.config['ava_command_session']
        ava_command_session._register_miss(app, cfg, 0)
        ava_command_session._register_miss(app, cfg, 0)
        app.audio_coordinator.speak.reset_mock()
        app.play_sound.reset_mock()
        ava_command_session._register_miss(app, cfg, 0)
        assert app._ava_cmd_miss_count == 3
        assert _spoken_texts(app) == ["Ava command session off."]
        app.play_sound.assert_not_called()
        app.exit_ava_command_session.assert_called_once()
        assert app.ava_command_session_active is False

    def test_miss_limit_is_configurable(self):
        app = _make_app(miss_limit=1)
        cfg = app.config['ava_command_session']
        ava_command_session._register_miss(app, cfg, 0)
        assert app.exit_ava_command_session.call_count == 1
        assert app.ava_command_session_active is False


class TestRegisterHit:
    def test_hit_resets_counter(self):
        app = _make_app(miss_limit=3)
        app._ava_cmd_miss_count = 2
        ava_command_session._register_hit(app)
        assert app._ava_cmd_miss_count == 0


class TestProcessUtteranceIntegration:
    """Exercises _register_miss/_register_hit through the real
    _process_utterance waterfall dispatch (stages a/b/c), not just the
    helpers directly. Stage (b)/(c) are neutralized (no ACTION2 match, no
    LLM hit) so stage (a)'s process_text() return value is the single
    hit/miss signal, mirroring the old suite's single resolver-mock
    control point."""

    def _neutralize_b_and_c(self, monkeypatch):
        monkeypatch.setattr(ava_command_session, '_match_action2_grammar', lambda u: None)
        monkeypatch.setattr(ava_command_session, '_build_shortlist', lambda app, u, cfg: [])
        monkeypatch.setattr(ava_command_session, '_stage_c_llm_fallback', lambda *a, **k: False)

    def test_unresolved_utterance_counts_as_miss(self, monkeypatch):
        app = _make_app(miss_limit=3)
        self._neutralize_b_and_c(monkeypatch)
        ava_command_session._process_utterance(app, 0, "complete gibberish")
        assert app._ava_cmd_miss_count == 1
        assert _spoken_texts(app) == ["I didn't catch a command in that."]

    def test_three_consecutive_misses_autoexit(self, monkeypatch):
        app = _make_app(miss_limit=3)
        self._neutralize_b_and_c(monkeypatch)
        for _ in range(3):
            ava_command_session._process_utterance(app, 0, "still gibberish")
        assert app.ava_command_session_active is False
        assert _spoken_texts(app)[-1] == "Ava command session off."

    def test_resolved_utterance_resets_miss_count(self, monkeypatch):
        app = _make_app(miss_limit=3)
        self._neutralize_b_and_c(monkeypatch)

        app.command_executor.process_text = Mock(return_value=(None, False))
        ava_command_session._process_utterance(app, 0, "gibberish")
        assert app._ava_cmd_miss_count == 1

        app.command_executor.process_text = Mock(return_value=("screenshot", True))
        ava_command_session._process_utterance(app, 0, "take a screenshot")
        assert app._ava_cmd_miss_count == 0

        # Miss counting starts over -- next miss speaks the notice again,
        # not the chime, proving the escalation state actually reset.
        app.audio_coordinator.speak.reset_mock()
        app.command_executor.process_text = Mock(return_value=(None, False))
        ava_command_session._process_utterance(app, 0, "gibberish again")
        assert _spoken_texts(app) == ["I didn't catch a command in that."]
