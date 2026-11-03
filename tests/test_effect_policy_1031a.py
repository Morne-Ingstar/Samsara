"""Local-effect modes and real submit seams, with inert adapters only."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import ast
import json
import sys

import pytest

from samsara import execution_policy as ep, effect_policy as effects, plugin_commands
from samsara.command_registry import DispatchState
from samsara.commands import CommandExecutor
from plugins.commands import ask_ollama, message_claude, message_chatgpt, ask_agent
from tests.test_cancel_window_69 import FakeTimer


@pytest.fixture(autouse=True)
def clean():
    registry = dict(plugin_commands._REGISTRY)
    modules = {k: dict(v) for k, v in plugin_commands._MODULE_ENTRIES.items()}
    ask_ollama.clear_pending_action()
    ep._speech.last_onset = ep._speech.last_dispatch_start = 0
    yield
    ask_ollama.clear_pending_action()
    plugin_commands._REGISTRY.clear()
    plugin_commands._REGISTRY.update(registry)
    plugin_commands._MODULE_ENTRIES.clear()
    plugin_commands._MODULE_ENTRIES.update(modules)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    path = tmp_path / "commands.json"
    path.write_text(json.dumps({"commands": {
        "submit": {"type": "press", "key": "enter"},
        "renamed send key": {"type": "hotkey", "keys": ["ctrl", "enter"]},
        "snap left": {"type": "hotkey", "keys": ["win", "left"]},
        "close window": {"type": "hotkey", "keys": ["alt", "f4"]},
        "save": {"type": "hotkey", "keys": ["ctrl", "s"]},
    }}), encoding="utf-8")
    app = SimpleNamespace(config={}, _ava_cmd_generation=7, audio_coordinator=Mock(),
                          _show_outcome_chip=Mock(), play_sound=Mock())
    executor = CommandExecutor(path, plugins_dir=tmp_path / "no-plugins", app=app)
    app.command_executor = executor
    target = [effects.SubmitTarget("editor", 42, "notepad.exe", "edit", "body-v1")]
    monkeypatch.setattr(effects, "resolve_submit_target", lambda app=None: target[0])
    # 1031a: foreground and composer probes must share the inert destination.
    monkeypatch.setattr(ep, "foreground_window_identity", lambda: {
        "hwnd": target[0].hwnd, "process": target[0].process, "pid": 7,
        "title_hash": target[0].conversation})
    calls = []
    from samsara import commands
    monkeypatch.setattr(commands, "get_handler", lambda kind: SimpleNamespace(
        execute=lambda command, context: calls.append(command) or True))
    monkeypatch.setattr(ep, "listen_for_confirmation", lambda *args: None)
    return app, executor, target, calls


@pytest.mark.parametrize("kind,tier,mode,wait", [
    ("ai", "T0", "immediate", 0), ("editor", "T1", "undoable", 0),
    ("known_person", "T2", "short_send_grace", 1),
    ("email", "T3", "explicit_confirm", 0), ("unknown", "T3", "explicit_confirm", 0),
    ("public", "T3", "explicit_confirm", 0), ("shell", "T3", "explicit_confirm", 0),
])
def test_resolved_destination_selects_handling(kind, tier, mode, wait):
    target = effects.SubmitTarget(kind, 42, "app.exe", "composer", "revision",
                                  "account-id", "conversation-id", "person-id")
    assert effects.send_handling(target) == effects.Handling(tier, mode, wait)


@pytest.mark.parametrize("field,value", [("account", ""), ("conversation", ""),
                                         ("recipient", ""), ("user_authored", False)])
def test_dm_requires_stable_identity_and_user_authorship(field, value):
    target = effects.SubmitTarget("known_person", 42, "chat.exe", "edit", "revision",
                                  "account", "conversation", "person")
    assert effects.send_handling(replace(target, **{field: value})) == effects.CONFIRM
    assert effects.send_handling(target, route=ep.Route.MODEL) == effects.CONFIRM
    assert effects.send_handling(target, route=ep.Route.SCHEDULE) == effects.CONFIRM


@pytest.mark.parametrize("phrase", ["submit", "renamed send key", "key:return", "key:ctrl+enter", "key:space"])
@pytest.mark.parametrize("kind,expected", [("ai", DispatchState.COMPLETED),
                                            ("editor", DispatchState.COMPLETED),
                                            ("email", DispatchState.QUEUED),
                                            ("unknown", DispatchState.QUEUED)])
def test_raw_and_registered_keys_inherit_destination(harness, phrase, kind, expected):
    app, executor, target, calls = harness
    target[0] = replace(target[0], kind=kind)
    result = executor.execute_canonical(phrase, app)
    assert result.state is expected
    assert len(calls) == int(expected is DispatchState.COMPLETED)
    assert ep.cancel_window_for(phrase, app) == 0
    if expected is DispatchState.QUEUED:
        assert result.detail["awaiting_confirmation"]
        assert not isinstance(ep.pending_operation(), ep.TimedOperation)
        assert ep.answer_pending(app, "yes") == "approved"
        assert len(calls) == 1


@pytest.mark.parametrize("change", ["revision", "conversation", "recipient", "hwnd", "control"])
def test_confirmation_refuses_changed_composer_and_destination(harness, change):
    app, executor, target, calls = harness
    target[0] = replace(target[0], kind="email")
    assert executor.execute_canonical("submit", app).state is DispatchState.QUEUED
    target[0] = replace(target[0], **{change: 43 if change == "hwnd" else "changed"})
    assert ep.answer_pending(app, "yes").startswith("refused")
    assert calls == []


def test_close_never_gets_timer_even_with_undoable_metadata(harness):
    app, executor, target, calls = harness
    assert executor.execute_canonical("close window", app).state is DispatchState.QUEUED
    assert ep.pending_operation().handling == effects.CONFIRM
    assert calls == [] and ep.cancel_window_for("close window", app) == 0


def test_ordinary_save_has_no_delay_or_question(harness):
    app, executor, target, calls = harness
    assert executor.execute_canonical("save", app).state is DispatchState.COMPLETED
    assert len(calls) == 1 and ep.pending_operation() is None


@pytest.mark.parametrize("keys,expected", [
    ("ctrl+delete", DispatchState.COMPLETED), ("ctrl+del", DispatchState.COMPLETED),
    ("shift+delete", DispatchState.QUEUED), ("ctrl+alt+delete", DispatchState.QUEUED),
])
def test_word_edit_does_not_exempt_destructive_key_combinations(harness, keys, expected):
    app, executor, target, calls = harness
    result = executor.execute_canonical("key:" + keys, app)
    assert result.state is expected
    assert len(calls) == int(expected is DispatchState.COMPLETED)
    assert ep.cancel_window_for("key:" + keys, app) == 0


@pytest.mark.parametrize("module,phrase,risk", [
    ("flashforge_printer", "start print", "destructive"),
    ("flashforge_printer", "resume print", "ui"),
    ("workflow_capture", "start capture", "destructive"),
    ("voice_memo", "cancel memo", "destructive"),
])
def test_owner_immediate_exemptions(harness, module, phrase, risk):
    app, executor, target, calls = harness
    handler = Mock(return_value=True)
    handler.__module__ = f"plugins.commands.{module}"
    handler.__name__ = "effect"
    plugin_commands.command(phrase, risk_class=risk)(handler)
    assert isinstance(ep.authorize(ep.Invocation(phrase, generation=7), app=app), ep.Allowed)
    assert ep.cancel_window_for(phrase, app) == 0


@pytest.mark.parametrize("gate", ["stale", "invalid_args", "pack_disabled", "model_allowlist"])
def test_immediacy_cannot_waive_authorization(harness, gate, monkeypatch):
    app, executor, target, calls = harness
    target[0] = replace(target[0], kind="ai")
    route = ep.Route.MODEL if gate == "model_allowlist" else ep.Route.EXACT
    generation = 6 if gate == "stale" else 7
    args = {"extra": "bad"} if gate == "invalid_args" else {}
    if gate == "pack_disabled":
        monkeypatch.setattr(executor, "command_availability", lambda cid: ("pack_disabled", "disabled"))
    if gate == "model_allowlist":
        app.config["execution_policy"] = {"model_tool_allowlist": []}
    result = executor.execute_canonical("submit", app, route=route, generation=generation, args=args)
    assert result.state is DispatchState.REJECTED and calls == []


def private_send(harness, monkeypatch):
    app, executor, target, calls = harness
    target[0] = replace(target[0], kind="known_person", account="a", conversation="c",
                         recipient="p", label="Test person", preview="A synthetic message for this test")
    timers = []
    from samsara.runtime import thread_registry
    monkeypatch.setattr(thread_registry, "timer", lambda name, seconds, fn, **kw: FakeTimer(seconds, fn, timers))
    result = executor.execute_canonical("submit", app)
    assert result.state is DispatchState.QUEUED and result.detail["send_grace_s"] == 1
    assert len(timers) == 1 and timers[0].seconds == 1
    assert calls == [] and ep.pending_operation().handling == effects.PRIVATE_SEND
    return timers


@pytest.mark.parametrize("action", ["elapsed", "send now", "cancel", "onset cancel", "stop", "changed", "next command"])
def test_private_message_has_one_grace_and_lifecycle_guards(harness, monkeypatch, action):
    app, executor, target, calls = harness
    timers = private_send(harness, monkeypatch)
    if action == "send now":
        assert ep.answer_pending(app, "send now") == "approved"
    elif action in {"cancel", "onset cancel"}:
        if action == "onset cancel":
            ep.note_speech_onset(app)
            timers[0].fire()
            assert ep.pending_operation().held and calls == []
        assert ep.answer_pending(app, "cancel") == "rejected"
    elif action == "stop":
        app._ava_cmd_generation += 1
    elif action == "changed":
        target[0] = replace(target[0], revision="changed")
    elif action == "next command":
        ep.flush_cancel_window()
    timers[0].fire()
    assert len(calls) == int(action in {"elapsed", "send now"})
    timers[0].fire()
    assert len(calls) <= 1


def wake_app(harness, monkeypatch, kind="ai"):
    app, executor, target, calls = harness
    target[0] = replace(target[0], kind=kind)
    app.config["auto_paste"] = True
    app._wake_session_mode = "focus_dictate"
    app._wake_session_target_hwnd = target[0].hwnd
    app._wake_session_target_process = target[0].process
    app._wake_session_delivered_text = "Synthetic message"
    app.wake_dictation_buffer = []
    app._wake_session_id = "test-session"
    app._emit_wake_trace = Mock()
    app._end_wake_session = Mock()
    press = Mock()
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(press=press))
    return app, target, press


def test_wake_ai_immediate_and_once_only(harness, monkeypatch):
    app, target, press = wake_app(harness, monkeypatch)
    assert effects.submit_wake_profile(app) is True
    assert effects.submit_wake_profile(app) is False
    press.assert_called_once_with("return")
    assert ep.pending_operation() is None


def test_wake_email_return_asks_once_and_binds_body(harness, monkeypatch):
    app, target, press = wake_app(harness, monkeypatch, "email")
    assert effects.submit_wake_profile(app) is False
    op = ep.pending_operation()
    assert op.handling == effects.CONFIRM and not isinstance(op, ep.TimedOperation)
    press.assert_not_called()
    app._wake_session_delivered_text += " changed"
    assert not op.approve(app=app)
    press.assert_not_called()


def test_wake_email_approval_submits_once(harness, monkeypatch):
    app, target, press = wake_app(harness, monkeypatch, "email")
    assert not effects.submit_wake_profile(app)
    assert ep.answer_pending(app, "yes") == "approved"
    press.assert_called_once_with("return")
    app._end_wake_session.assert_called_once()


def test_stage_send_never_submits(harness, monkeypatch):
    app, target, press = wake_app(harness, monkeypatch)
    app._wake_session_mode = "stage_send"
    assert not effects.submit_wake_profile(app)
    press.assert_not_called()
    assert ep.pending_operation() is None
    source = (Path(__file__).resolve().parents[1] / "dictation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    branch = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                  and ast.unparse(node.test) == "_mode == 'focus_dictate'")
    assert any(isinstance(node, ast.Call) and ast.unparse(node.func) == "submit_wake_profile"
               for statement in branch.body for node in ast.walk(statement))
    assert not any(isinstance(node, ast.Call) and ast.unparse(node.func) == "submit_wake_profile"
                   for statement in branch.orelse for node in ast.walk(statement))


def test_wake_unknown_outcome_is_never_retried(harness, monkeypatch):
    app, target, press = wake_app(harness, monkeypatch)
    press.side_effect = RuntimeError("unknown outcome")
    with pytest.raises(RuntimeError):
        effects.submit_wake_profile(app)
    assert effects.submit_wake_profile(app) is False
    press.assert_called_once()


def test_dynamic_cli_alias_remains_immediate(harness):
    app, executor, target, calls = harness
    plugin_commands._reinstall_module_commands(message_claude)
    ask_agent.start_services(app)
    assert "ask claude" not in plugin_commands._MODULE_ENTRIES[
        "plugins.commands.message_claude"]["claude message prepare"]["aliases"]
    assert isinstance(ep.authorize(ep.Invocation("ask claude", {"remainder": "Synthetic question"},
                                               generation=7), app=app), ep.Allowed)
