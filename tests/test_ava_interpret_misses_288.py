"""Focused contract tests for optional Ava interpretation of command misses."""
from types import SimpleNamespace

from samsara import ava_command_session as resolver
from samsara.session_modes import outcome_chip


class _Executor:
    def ava_menu(self, *_args, **_kwargs):
        return ["increase font size"]


def _app():
    return SimpleNamespace(
        config={"ava_command_session": {"interpret_misses": True}},
        command_executor=_Executor(),
    )


def _allowed(monkeypatch, *, ready=True):
    from samsara import ai_preferences, ava_readiness
    monkeypatch.setattr(ai_preferences, "runtime_state", lambda *_args: SimpleNamespace(
        allowed=True, provider=SimpleNamespace(identity="ollama")))
    monkeypatch.setattr(ava_readiness, "readiness_for", lambda _app: SimpleNamespace(ready=ready))


def test_offline_and_long_misses_do_not_call_the_model(monkeypatch):
    _allowed(monkeypatch, ready=False)
    from plugins.commands import ask_ollama
    calls = []
    monkeypatch.setattr(ask_ollama, "ask_ollama", lambda *_args, **_kwargs: calls.append(True))
    assert resolver.resolve(_app(), "make text bigger", "offline", 10**12) is None
    _allowed(monkeypatch, ready=True)
    assert resolver.resolve(_app(), "one two three four five six seven eight nine", "long", 10**12) is None
    assert calls == []


def test_expired_request_never_becomes_a_proposal(monkeypatch):
    _allowed(monkeypatch)
    assert resolver.resolve(_app(), "make text bigger", "expired", 0.0) is None


def test_ava_success_chip_is_emitted_only_for_completed_execution():
    completed = outcome_chip("command_executed", {
        "phrase": "increase font size", "state": "completed", "origin": "model",
    })
    queued = outcome_chip("command_executed", {
        "phrase": "increase font size", "state": "queued", "origin": "model",
    })
    assert completed == ("✓ increase font (Ava)", "success")
    assert queued != completed
