"""Legacy wake control words must obey the shared phrase boundary."""
from samsara.log import describe_text, safe_exception
import ast
import copy
import logging
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import config_defaults
from samsara.stage_timing import StageTiming


@pytest.fixture
def app():
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    app_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                     and node.name == "DictationApp")
    decoder = next(node for node in app_class.body if isinstance(node, ast.FunctionDef)
                   and node.name == "_decode_wake_word_buffer")
    branch = next(node for node in ast.walk(decoder) if isinstance(node, ast.If)
                  and isinstance(node.test, ast.Compare)
                  and isinstance(node.test.left, ast.Attribute)
                  and node.test.left.attr == "app_state"
                  and any(isinstance(value, ast.Tuple) for value in node.test.comparators))
    route = ast.parse("def route(self, text):\n text_lower = text.lower()\n ww_config = self.config.get('wake_word_config', {})\n stage_timing = None\n").body[0]
    route.body.append(copy.deepcopy(branch))
    namespace = {"describe_text": describe_text, "safe_exception": safe_exception,
                 "logger": logging.getLogger(__name__), "config_defaults": config_defaults,
                 "StageTiming": StageTiming,
                 "_WAKE_SESSION_SEND_WORDS": ["over"]}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[route], type_ignores=[])),
                 "<control-words-production-ast>", "exec"), namespace)
    instance = SimpleNamespace(
        config={"wake_word_config": {"wake_abort_phrase": ["cancel", "cancel dictation"],
                                     "end_words": ["over", "done"],
                                     "pause_words": ["pause"], "resume_words": ["resume"]}},
        app_state="long_dictation", _dictation_paused=False, _dictation_require_end=True,
        wake_dictation_buffer=["existing draft"], _emit_wake_trace=Mock(),
        play_sound=Mock(), _end_wake_session=Mock(), _reset_wake_dictation=Mock(),
        _output_dictation=Mock(return_value=True),
    )
    instance.route = MethodType(namespace["route"], instance)
    return instance


@pytest.mark.parametrize("phrase", ["I told him to cancel the meeting", "cancellation policy",
                                    "the project is overdue", "the menu is redone",
                                    "come over here", "the job is done tomorrow"])
def test_embedded_words_are_dictated(app, phrase):
    app.route(phrase)
    assert app.wake_dictation_buffer == ["existing draft", phrase]
    app._reset_wake_dictation.assert_not_called()
    app._output_dictation.assert_not_called()


@pytest.mark.parametrize("phrase", ["CANCEL!", "cancel dictation."])
def test_whole_cancel_with_case_and_punctuation(app, phrase):
    app.route(phrase)
    app._reset_wake_dictation.assert_called_once()
    app._output_dictation.assert_not_called()


@pytest.mark.parametrize("phrase", ["OVER!", "done."])
def test_whole_end_commits_only_buffer(app, phrase):
    app.route(phrase)
    app._output_dictation.assert_called_once_with("existing draft")
    app._reset_wake_dictation.assert_called_once()


def test_paused_end_still_commits_and_embedded_cancel_does_not(app):
    app._dictation_paused = True
    app.route("I told him to cancel the meeting")
    app._reset_wake_dictation.assert_not_called()
    app.route("Over!")
    app._output_dictation.assert_called_once_with("existing draft")


def test_command_collision_does_not_cancel(app):
    app.route("cancel my appointment")
    assert app.wake_dictation_buffer[-1] == "cancel my appointment"
    app._reset_wake_dictation.assert_not_called()
