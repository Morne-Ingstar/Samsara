"""Regression tests for the stage (c) CLOSED-WORLD selection gate.

Defect (found in a G1 replay rerun, 2026-07-23): stage (c)'s LLM fallback
synthesized novel commands from nonsense input -- e.g. "purple thinking
clouds today" -> a fabricated CONFIRM+ACTION for 'set wallpaper to
"purple thinking clouds"'; "asdkfj random gibberish text" -> a fabricated
ACTION for 'insert random gibberish text'. Zero misses on the 5-utterance
nonsense corpus -- in a latched command session, ambient/misheard speech
was turning into text-mutating actions.

RULE CHANGE (spec, enforced in samsara.ava_command_session._closed_world_
selection_ok, not just the prompt): stage (c) may ONLY (1) select one
candidate VERBATIM from the fuzzy shortlist it was given, or (2) emit a
valid ACTION2 phrase whose verb is a real ACTION2 verb, or (3) MISS.

This file exercises the REAL _process_utterance/_stage_c_llm_fallback path
via mocked ask_ollama.ask_ollama (so no live Ollama call is needed) --
same pattern as tests/test_ava_command_session_g2_matrix.py's
TestWaterfallMissSingleFallbackThenMissFeedback -- plus direct unit tests
of the gate function itself.
"""
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import ava_command_session
from plugins.commands import ask_ollama

# Same 5 nonsense utterances from tools/ava_command_replay.py's CORPUS
# (spec-required "nonsense" category), paired with a CONFIRM+ACTION
# response reproducing the actual class of fabrication the G1 rerun found
# -- a command name that is NOT verbatim on the shortlist we gave the
# model, sometimes carrying the whole nonsense utterance as if it were a
# free-text argument to an invented command.
NONSENSE_FABRICATIONS = [
    (
        "banana weather umbrella",
        'CONFIRM Do a banana weather umbrella thing.\nACTION banana weather umbrella',
    ),
    (
        "purple thinking clouds today",
        'CONFIRM Set your wallpaper to purple thinking clouds.\n'
        'ACTION set wallpaper to "purple thinking clouds"',
    ),
    (
        "asdkfj random gibberish text",
        'CONFIRM Insert random gibberish text.\nACTION insert random gibberish text',
    ),
    (
        "the quick brown fox jumps over",
        'CONFIRM Type the quick brown fox jumps over.\n'
        'ACTION type the quick brown fox jumps over',
    ),
    (
        "nothing here matches anything useful",
        'CONFIRM Do nothing here matches anything useful.\n'
        'ACTION nothing here matches anything useful',
    ),
]

# A shortlist of real, unrelated commands -- none of the fabrications above
# are verbatim members of this list, which is the whole point.
SHORTLIST = ["screenshot", "close tab", "scroll up", "mute", "maximize"]


@pytest.fixture(autouse=True)
def _reset_pending_action():
    ask_ollama.clear_pending_action()
    yield
    ask_ollama.clear_pending_action()


def _make_app():
    app = Mock()
    app._ava_cmd_generation = 0
    app._ava_cmd_miss_count = 0
    app.ava_command_session_active = True
    app.command_executor = Mock()
    app.command_executor.process_text = Mock(return_value=(None, False))  # stage (a) miss
    app.audio_coordinator = Mock()
    app.exit_ava_command_session = Mock()
    app.play_sound = Mock()
    app.config = {'ava_command_session': {}}
    return app


def _spoken_texts(app):
    return [c.args[0] for c in app.audio_coordinator.speak.call_args_list]


class TestNonsenseFabricationsAreRejectedAsMiss:
    @pytest.mark.parametrize("utterance,fabricated_response", NONSENSE_FABRICATIONS)
    def test_fabricated_action_never_reaches_handle_response(self, monkeypatch, utterance, fabricated_response):
        app = _make_app()
        monkeypatch.setattr(ava_command_session, '_match_action2_grammar', lambda u: None)
        monkeypatch.setattr(ava_command_session, '_build_shortlist', lambda app, u, cfg: list(SHORTLIST))
        monkeypatch.setattr(ask_ollama, 'ask_ollama', Mock(return_value=fabricated_response))
        handle_response_mock = Mock()
        monkeypatch.setattr(ask_ollama, 'handle_response', handle_response_mock)
        execute_command_mock = Mock()
        app.command_executor.execute_command = execute_command_mock

        ava_command_session._process_utterance(app, 0, utterance)

        handle_response_mock.assert_not_called()
        execute_command_mock.assert_not_called()

    @pytest.mark.parametrize("utterance,fabricated_response", NONSENSE_FABRICATIONS)
    def test_fabricated_action_registers_as_miss_with_spoken_feedback(self, monkeypatch, utterance, fabricated_response):
        app = _make_app()
        monkeypatch.setattr(ava_command_session, '_match_action2_grammar', lambda u: None)
        monkeypatch.setattr(ava_command_session, '_build_shortlist', lambda app, u, cfg: list(SHORTLIST))
        monkeypatch.setattr(ask_ollama, 'ask_ollama', Mock(return_value=fabricated_response))
        monkeypatch.setattr(ask_ollama, 'handle_response', Mock())

        ava_command_session._process_utterance(app, 0, utterance)

        assert app._ava_cmd_miss_count == 1
        assert _spoken_texts(app) == ["I didn't catch a command in that."]


class TestLegitimateSelectionsStillResolve:
    def test_verbatim_shortlist_selection_still_resolves(self, monkeypatch):
        app = _make_app()
        monkeypatch.setattr(ava_command_session, '_match_action2_grammar', lambda u: None)
        monkeypatch.setattr(ava_command_session, '_build_shortlist', lambda app, u, cfg: list(SHORTLIST))
        monkeypatch.setattr(
            ask_ollama, 'ask_ollama',
            Mock(return_value='CONFIRM Take a screenshot.\nACTION screenshot'),
        )
        handle_response_mock = Mock()
        monkeypatch.setattr(ask_ollama, 'handle_response', handle_response_mock)

        ava_command_session._process_utterance(app, 0, "grab the screen")

        handle_response_mock.assert_called_once()
        assert app._ava_cmd_miss_count == 0
        assert _spoken_texts(app) == []  # no miss feedback -- this was a hit

    def test_valid_action2_fallback_still_resolves(self, monkeypatch):
        app = _make_app()
        monkeypatch.setattr(ava_command_session, '_match_action2_grammar', lambda u: None)
        monkeypatch.setattr(ava_command_session, '_build_shortlist', lambda app, u, cfg: list(SHORTLIST))
        monkeypatch.setattr(
            ask_ollama, 'ask_ollama',
            Mock(return_value='CONFIRM Open Notepad.\nACTION2 open | notepad'),
        )
        handle_response_mock = Mock()
        monkeypatch.setattr(ask_ollama, 'handle_response', handle_response_mock)

        ava_command_session._process_utterance(app, 0, "please open notepad")

        handle_response_mock.assert_called_once()
        assert app._ava_cmd_miss_count == 0
        assert _spoken_texts(app) == []


class TestClosedWorldSelectionOkUnit:
    """Direct unit coverage of the gate function itself, independent of the
    worker plumbing above."""

    def test_action_verbatim_in_shortlist_passes(self):
        parsed = {"type": "action", "command": "screenshot"}
        assert ava_command_session._closed_world_selection_ok(parsed, SHORTLIST) is True

    def test_action_not_in_shortlist_fails(self):
        parsed = {"type": "action", "command": "set wallpaper to purple thinking clouds"}
        assert ava_command_session._closed_world_selection_ok(parsed, SHORTLIST) is False

    def test_action_trailing_punctuation_is_tolerated(self):
        # _parse_structured_response already lowercases parsed["command"];
        # only punctuation/whitespace normalization is this gate's job.
        parsed = {"type": "action", "command": "screenshot."}
        assert ava_command_session._closed_world_selection_ok(parsed, SHORTLIST) is True

    def test_action_with_trailing_extra_words_fails(self):
        # The whole point: appending free text to a real command name must
        # not sneak through as a "close enough" verbatim match.
        parsed = {"type": "action", "command": "screenshot of my desktop"}
        assert ava_command_session._closed_world_selection_ok(parsed, SHORTLIST) is False

    def test_action_empty_command_fails(self):
        parsed = {"type": "action", "command": ""}
        assert ava_command_session._closed_world_selection_ok(parsed, SHORTLIST) is False

    @pytest.mark.parametrize("verb", ["focus", "open", "close"])
    def test_action2_valid_verb_passes(self, verb):
        parsed = {"type": "action2", "verb": verb, "argument": "notepad"}
        assert ava_command_session._closed_world_selection_ok(parsed, SHORTLIST) is True

    def test_action2_invalid_verb_fails(self):
        parsed = {"type": "action2", "verb": "delete", "argument": "notepad"}
        assert ava_command_session._closed_world_selection_ok(parsed, SHORTLIST) is False

    def test_schedule_type_fails(self):
        parsed = {"type": "schedule", "command": "screenshot", "interval_seconds": 60, "key": None}
        assert ava_command_session._closed_world_selection_ok(parsed, SHORTLIST) is False

    def test_conversation_type_fails(self):
        parsed = {"type": "conversation"}
        assert ava_command_session._closed_world_selection_ok(parsed, SHORTLIST) is False


class TestNormalizeCommandName:
    def test_strips_surrounding_quotes(self):
        assert ava_command_session._normalize_command_name('"screenshot"') == "screenshot"

    def test_strips_trailing_punctuation(self):
        assert ava_command_session._normalize_command_name("screenshot.") == "screenshot"

    def test_collapses_internal_whitespace(self):
        assert ava_command_session._normalize_command_name("close   tab") == "close tab"

    def test_none_input_returns_empty_string(self):
        assert ava_command_session._normalize_command_name(None) == ""
