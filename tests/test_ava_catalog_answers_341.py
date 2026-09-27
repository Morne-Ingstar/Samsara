"""Queue 341: local catalog guidance and speech-only recovery after refused ACTION."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama as ava
from samsara.intent.grammar import catalog_question_goal


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    from samsara.ui import ava_captions_qt
    monkeypatch.setattr(ava_captions_qt, "show_caption", Mock())


def _app():
    executor = SimpleNamespace(_matcher=None, execute_canonical=Mock())
    return SimpleNamespace(config={"ava": {"catalog_answers": True}},
                           command_executor=executor)


@pytest.mark.parametrize("words,goal", [
    ("Do I have any commands for splitting apps on a monitor?", "splitting apps on a monitor"),
    ("What commands do I have to split windows?", "split windows"),
    ("How do I split windows?", "split windows"),
    ("Is there a command for splitting windows?", "splitting windows"),
    ("What can I say to split windows?", "split windows"),
])
def test_question_grammar(words, goal):
    assert catalog_question_goal(words) == goal
    assert catalog_question_goal("Please split windows") is None


def test_catalog_split_answer_skips_model_and_pages(monkeypatch):
    app = _app()
    spoken = []
    outcomes = []
    from samsara.ui import ava_captions_qt
    monkeypatch.setattr(ava_captions_qt, "show_caption", Mock())
    monkeypatch.setattr(ava, "_ai_state", lambda _app: SimpleNamespace(allowed=True))
    monkeypatch.setattr(ava, "speak", lambda _app, text, **kw: spoken.append(text) or True)
    monkeypatch.setattr(ava, "ask_ollama", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(ava, "_log_turn", lambda *_a, **kw: outcomes.append(kw["outcome"]))
    monkeypatch.setattr(ava.thread_registry, "spawn", lambda _name, fn, **_kw: fn())
    from plugins.commands import comfy
    monkeypatch.setattr(comfy, "try_voice_command", lambda *_a: None)

    ava.handle_ask_ava(app, "Do I have any commands for splitting apps on a monitor?")
    assert outcomes == ["catalog_answered"]
    assert spoken[0].startswith("You can say: ")
    assert "snap left" in spoken[0] and "snap right" in spoken[0]
    assert "window tile" in spoken[0]
    assert app._ava_catalog_answer["index"] == 3
    assert "snap left" in app._ava_catalog_answer["full"]
    assert ava.catalog_more(app)
    assert len(spoken) == 2
    app.command_executor.execute_canonical.assert_not_called()


def test_catalog_respects_current_command_availability():
    app = _app()
    app.command_executor.command_availability = (
        lambda phrase: ("out_of_scope", "test") if phrase == "snap left" else None)
    names = [ava.command_catalog.canonical_phrase(record) for record in ava._catalog_records(app)]
    assert "snap left" not in names
    assert "snap right" in names


def _refusal_setup(monkeypatch, second):
    app = _app()
    spoken = []
    model_calls = []
    monkeypatch.setattr(ava, "speak", lambda _app, text, **kw: spoken.append(text) or True)
    monkeypatch.setattr(ava, "_ai_state", lambda _app: SimpleNamespace(allowed=True))
    monkeypatch.setattr(ava.execution_policy, "command_exists", lambda *_a, **_kw: False)
    def reply(prompt, _app, **kwargs):
        model_calls.append((prompt, kwargs))
        return ava.AvaReply(second, "ollama", 1)
    monkeypatch.setattr(ava, "ask_model", reply)
    return app, spoken, model_calls


def test_unknown_action_gets_plain_conversation_without_execution(monkeypatch):
    app, spoken, calls = _refusal_setup(
        monkeypatch, "You can put one window on each side by saying snap left and snap right.")
    result = ava.handle_response(app, "CONFIRM You can split a window.\nACTION window split",
                                 original_text="How do I split apps?")
    assert result.reason == "action_refused_conversational"
    assert result.state == "spoken"
    assert spoken == ["You can put one window on each side by saying snap left and snap right."]
    assert len(calls) == 1 and calls[0][1]["allow_search"] is False
    assert "snap left" in calls[0][0]
    app.command_executor.execute_canonical.assert_not_called()


def test_second_unknown_action_falls_back_to_catalog(monkeypatch):
    app, spoken, calls = _refusal_setup(
        monkeypatch, "CONFIRM Do it.\nACTION window arrange all")
    result = ava.handle_response(app, "CONFIRM Split it.\nACTION window split",
                                 original_text="I wonder how to split windows")
    assert result.reason == "action_refused_conversational"
    assert len(calls) == 1
    assert spoken and spoken[0].startswith(("You can say:", "Nothing for"))
    assert all("window arrange all" not in line for line in spoken)
    app.command_executor.execute_canonical.assert_not_called()


def test_direct_run_request_two_failures_uses_stock_help(monkeypatch):
    app, spoken, _calls = _refusal_setup(monkeypatch, "CONFIRM Yes.\nACTION window arrange all")
    ava.handle_response(app, "CONFIRM Split it.\nACTION window split",
                        original_text="Please split the window")
    assert spoken == ["I couldn't find a command for that. Say 'what can I say to "
                      "window split' and I'll list what's close."]
    app.command_executor.execute_canonical.assert_not_called()


def test_registered_but_not_offered_still_refused(monkeypatch):
    app, spoken, _calls = _refusal_setup(monkeypatch, "Here are the nearby commands.")
    monkeypatch.setattr(ava.execution_policy, "command_exists", lambda *_a, **_kw: True)
    monkeypatch.setattr(ava, "offered_menu", lambda *_a: ["snap left"])
    monkeypatch.setattr(ava.execution_policy, "_emit", Mock())
    result = ava.handle_response(app, "CONFIRM Open it.\nACTION open chrome",
                                 original_text="How do I arrange windows?")
    assert result.reason == "action_refused_conversational"
    assert spoken == ["Here are the nearby commands."]
    ava.execution_policy._emit.assert_called_once()
    app.command_executor.execute_canonical.assert_not_called()


def test_refusal_retry_dropped_after_cancel(monkeypatch):
    app, spoken, _calls = _refusal_setup(monkeypatch, "That should not be spoken.")
    monkeypatch.setattr(ava, "ask_model", lambda *_a, **_kw: (
        setattr(app, "_ava_cmd_generation", 1) or ava.AvaReply("Too late.", "ollama", 1)))
    result = ava.handle_response(app, "CONFIRM Split.\nACTION window split",
                                 original_text="How do I split windows?", generation=0)
    assert result.state == "stale"
    assert spoken == []
    app.command_executor.execute_canonical.assert_not_called()


def test_refusal_outcome_logged_by_ava_turn(monkeypatch):
    app, spoken, _calls = _refusal_setup(monkeypatch, "You can say snap left or snap right.")
    outcomes = []
    monkeypatch.setattr(ava, "ask_ollama", lambda *_a: "CONFIRM Split it.\nACTION window split")
    monkeypatch.setattr(ava, "_log_turn", lambda *_a, **kw: outcomes.append(kw["outcome"]))
    monkeypatch.setattr(ava.thread_registry, "spawn", lambda _name, fn, **_kw: fn())
    from plugins.commands import comfy
    monkeypatch.setattr(comfy, "try_voice_command", lambda *_a: None)
    ava.handle_ask_ava(app, "Tell me how to split windows")
    assert outcomes == ["action_refused_conversational"]
    assert spoken == ["You can say snap left or snap right."]
    app.command_executor.execute_canonical.assert_not_called()
