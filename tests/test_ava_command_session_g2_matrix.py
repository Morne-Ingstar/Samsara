"""Closed-world resolver safety matrix after retirement of the Ava session."""
from types import SimpleNamespace

from samsara import ava_command_session as resolver


class _Executor:
    def ava_menu(self, utterance, **_kwargs):
        return ["increase font size", "close", "open"]


def _app():
    return SimpleNamespace(
        config={"ava_command_session": {"interpret_misses": True, "model": "test"}},
        command_executor=_Executor(),
    )


def _ready(monkeypatch):
    from samsara import ai_preferences, ava_readiness
    monkeypatch.setattr(ai_preferences, "runtime_state", lambda *_args: SimpleNamespace(
        allowed=True, provider=SimpleNamespace(identity="ollama")))
    monkeypatch.setattr(ava_readiness, "readiness_for", lambda _app: SimpleNamespace(ready=True))


def test_fabricated_action_is_not_a_proposal(monkeypatch):
    _ready(monkeypatch)
    from plugins.commands import ask_ollama
    monkeypatch.setattr(ask_ollama, "get_system_prompt", lambda _app: "{COMMAND_LIST}")
    monkeypatch.setattr(ask_ollama, "ask_ollama", lambda *_args, **_kwargs: "CONFIRM no.\nACTION erase the disk")
    assert resolver.resolve(_app(), "erase everything", "fabricated", 10**12) is None


def test_model_action_returns_catalogued_proposal_only(monkeypatch):
    _ready(monkeypatch)
    from plugins.commands import ask_ollama
    monkeypatch.setattr(ask_ollama, "get_system_prompt", lambda _app: "{COMMAND_LIST}")
    monkeypatch.setattr(ask_ollama, "ask_ollama", lambda *_args, **_kwargs: "CONFIRM done.\nACTION increase font size")
    proposal = resolver.resolve(_app(), "make the text bigger", "one", 10**12)
    assert proposal is not None
    assert proposal.command_id == "increase font size"
    assert proposal.origin == "model"


def test_cancelled_response_is_discarded(monkeypatch):
    _ready(monkeypatch)
    from plugins.commands import ask_ollama
    monkeypatch.setattr(ask_ollama, "get_system_prompt", lambda _app: "{COMMAND_LIST}")

    def _late(*_args, **_kwargs):
        resolver.cancel("cancelled")
        return "CONFIRM done.\nACTION increase font size"

    monkeypatch.setattr(ask_ollama, "ask_ollama", _late)
    assert resolver.resolve(_app(), "make the text bigger", "cancelled", 10**12) is None
    resolver.reset_cancellations()


def test_deterministic_app_grammar_is_a_catalogued_model_origin_proposal(monkeypatch):
    _ready(monkeypatch)
    proposal = resolver.resolve(_app(), "open notepad", "grammar", 10**12)
    assert proposal is not None
    assert proposal.command_id == "open"
    assert proposal.args == {"remainder": "notepad"}
