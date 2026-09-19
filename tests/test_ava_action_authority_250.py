"""Queue 250: advice cannot act, and confirmation binds its shown window.

All target identities and effects are synthetic.  These tests deliberately
never ask Windows for the actual foreground window or send input to it.
"""
import sys
import threading
import types
from pathlib import Path
from unittest.mock import Mock

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.commands import ask_ollama  # noqa: E402
from samsara import execution_policy as ep  # noqa: E402
from samsara.commands import CommandExecutor  # noqa: E402
from samsara.execution_policy import Invocation, NeedsConfirmation, Route  # noqa: E402


class _Speech:
    def __init__(self):
        self.calls = []
        self.is_speaking = False

    def speak(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return types.SimpleNamespace(utterance_id="spoken")

    def cancel_speech(self):
        return None


def _app(executor=None):
    return types.SimpleNamespace(
        config={"ollama": {"enabled": True}},
        audio_coordinator=_Speech(),
        command_executor=executor,
        _ava_cmd_generation=41,
        _ava_cmd_mode_lock=threading.Lock(),
        _show_outcome_chip=Mock(),
    )


def _foreground(hwnd=100, pid=200, process="writer.exe", title="a"):
    import hashlib
    return {
        "hwnd": hwnd,
        "pid": pid,
        "process": process,
        "title_hash": hashlib.sha256(title.encode("utf-8")).hexdigest(),
    }


def _executor(effects):
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.commands = {"close window": {"type": "hotkey", "keys": ["alt", "f4"]}}

    def execute(*args, **kwargs):
        effects.append((args, kwargs))

    executor.execute_canonical = execute
    return executor


@pytest.fixture(autouse=True)
def _clear_pending():
    ask_ollama.clear_pending_action()
    yield
    ask_ollama.clear_pending_action()


def _stage_close(app, executor):
    invocation = Invocation("close window", route=Route.MODEL, generation=41)
    executor._stage_confirmation(app, invocation, NeedsConfirmation("Close this window?"))


def test_advice_response_with_action_syntax_is_text_only(monkeypatch):
    app = _app()
    malicious_reply = "CONFIRM Close the window.\nACTION close window"
    parsed = Mock()
    monkeypatch.setattr(ask_ollama, "handle_response", parsed)
    monkeypatch.setattr(ask_ollama, "ask_ollama", lambda _prompt, _app: malicious_reply)
    monkeypatch.setattr(ask_ollama, "_check_teaching_intent", lambda _app, _text: False)
    monkeypatch.setattr(ask_ollama.cloud_llm, "is_enabled", lambda _app: True)
    monkeypatch.setattr(ask_ollama.thread_registry, "spawn",
                        lambda _name, worker, **_kwargs: worker())

    ask_ollama.handle_is_it_safe(app, "close the window")

    parsed.assert_not_called()
    assert ep.pending_operation() is None
    assert app.audio_coordinator.calls


def test_unchanged_foreground_target_executes_once(monkeypatch):
    effects = []
    executor = _executor(effects)
    app = _app(executor)
    target = _foreground()
    monkeypatch.setattr(ep, "foreground_window_identity", lambda: dict(target))

    _stage_close(app, executor)

    assert ep.answer_pending(app, "yes") == "approved"
    assert len(effects) == 1


def test_changed_foreground_refuses_and_speaks_the_local_reason(monkeypatch):
    effects = []
    executor = _executor(effects)
    app = _app(executor)
    targets = iter([_foreground(), _foreground(hwnd=101, title="b")])
    monkeypatch.setattr(ep, "foreground_window_identity", lambda: next(targets))

    _stage_close(app, executor)

    assert ep.answer_pending(app, "yes") == "refused:target changed: a different window is in front now"
    assert effects == []
    assert ("That window changed, so I didn't do it", {"category": "confirmation"}) in app.audio_coordinator.calls
    assert ep.pending_operation() is None


def test_closed_foreground_refuses_and_clears_pending_item(monkeypatch):
    effects = []
    executor = _executor(effects)
    app = _app(executor)
    targets = iter([_foreground(), None])
    monkeypatch.setattr(ep, "foreground_window_identity", lambda: next(targets))

    _stage_close(app, executor)

    assert ep.answer_pending(app, "yes") == "refused:target changed: the window is not open any more"
    assert effects == []
    assert ("That window changed, so I didn't do it", {"category": "confirmation"}) in app.audio_coordinator.calls
    assert ep.pending_operation() is None


def test_stop_and_expiry_still_invalidate_the_confirmation(monkeypatch):
    effects = []
    executor = _executor(effects)
    app = _app(executor)
    monkeypatch.setattr(ep, "foreground_window_identity", _foreground)

    _stage_close(app, executor)
    ep.stop_all(app, "test stop")
    assert ep.answer_pending(app, "yes") is None
    assert effects == []

    app._ava_cmd_generation = 41
    _stage_close(app, executor)
    pending = ep.pending_operation()
    pending.deadline = 0
    assert ep.answer_pending(app, "yes") == "refused:expired"
    assert effects == []


def test_named_target_binding_remains_the_existing_path():
    invocation = Invocation("action2:close", {"target": "notes"}, Route.MODEL, 41)
    target = {"hwnd": 77, "process": "notepad.exe"}

    targets, probe, foreground_bound = ep.bind_staged_target(
        invocation, object(), resolver=lambda name: dict(target),
        foreground_resolver=lambda: pytest.fail("named target must not probe foreground"))

    assert foreground_bound is False
    assert targets == target
    assert probe() == target
