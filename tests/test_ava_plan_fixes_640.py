"""633 regressions: real policy/dispatch, synthetic requests and fake effects."""
import json
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama as ava, app_verbs
from samsara import ava_pending, ava_plan, commands, execution_policy as policy
from samsara.commands import CommandExecutor
from tests.test_ava_plan_and_ask_624 import rig


@pytest.fixture
def real_rig(rig, monkeypatch):
    r = rig
    r.menu.extend(["open", "focus"])
    r.executor.execute_canonical = MethodType(CommandExecutor.execute_canonical, r.executor)
    r.executor.command_availability = lambda _cid: None
    r.executor._build_context = lambda _app: None
    r.executor._app = r.app

    def execute(command, _ctx):
        cid = next(k for k, v in r.executor.commands.items() if v is command)
        r.effects.append(cid)
        return True

    monkeypatch.setattr(commands, "get_handler", lambda _typ: SimpleNamespace(execute=execute))
    monkeypatch.setattr(ava, "_track_alias_uses", lambda _text: None)
    return r


def deliver(r, plan, utterance="open chrome and scroll down"):
    return ava_plan.handle_json(r.app, json.dumps({"type": "plan", "plan": plan}), utterance, 0)


@pytest.mark.parametrize("name,utterance", [
    ("chrome invented_target", "open chrome"),
    ("chrome", "open chromebook"),
    ("visual code", "open visual studio code"),
    ("code visual", "open visual code"),
    ("the chrome", "open chrome"),
    ("x chrome", "open chrome"),
    ("!!!", "open chrome"),
])
def test_app_target_requires_whole_contiguous_tokens(real_rig, monkeypatch, name, utterance):
    opened = Mock(return_value=app_verbs.ActionResult.DONE)
    monkeypatch.setattr(app_verbs, "do_open", opened)
    turn = deliver(real_rig, [{"id": "open", "args": {"app_name": name}}], utterance)
    assert turn.state == "refused"
    opened.assert_not_called()
    assert real_rig.effects == []


@pytest.mark.parametrize("name,utterance", [
    ("CHROME", "please open Chrome!"),
    ("Visual-Studio Code", "open VISUAL studio, code."),
    ("x", "open X"),
    ("the app", "open the app"),
])
def test_app_target_preserves_case_and_punctuation_normalization(real_rig, monkeypatch, name, utterance):
    opened = Mock(return_value=app_verbs.ActionResult.DONE)
    monkeypatch.setattr(app_verbs, "do_open", opened)
    assert deliver(real_rig, [{"id": "open", "args": {"app_name": name}}], utterance).state == "completed"
    opened.assert_called_once_with(name)


@pytest.mark.parametrize("path", ["consume", "tick"])
def test_expired_policy_question_releases_its_operation_before_late_registered_yes(real_rig, path):
    r = real_rig
    assert deliver(r, ["close window", "scroll down"]).state == "queued"
    q, op = r.app._ava_question, policy.pending_operation()
    before = list(r.spoken)
    r.now[0] = q.deadline + 0.01  # No expiry timer has fired yet.
    if path == "consume":
        assert ava_pending.consume(r.app, "yes")
    else:
        ava_pending._tick(r.app, q)
    assert r.app._ava_question is None and q.timer.cancelled
    assert policy.pending_operation() is None
    assert op.approved is False
    r.app._release_wake_consumer.assert_called_once_with("ava_question")
    assert r.spoken == before
    assert not ava_pending.consume(r.app, "yes")
    ava.handle_ava_confirm(r.app)  # Actual registered yes handler, as in 633.
    assert r.effects == []


@pytest.mark.parametrize("path", ["consume", "tick"])
def test_expired_old_question_preserves_replacement_operation(real_rig, path):
    r = real_rig
    deliver(r, ["close window"])
    q, old = r.app._ava_question, policy.pending_operation()
    replacement = policy.stage_pending(r.app, policy.Invocation(
        "close window", route=policy.Route.MODEL, generation=0), "Replacement?")
    r.now[0] = q.deadline + 0.01
    if path == "consume":
        assert ava_pending.consume(r.app, "yes")
    else:
        ava_pending._tick(r.app, q)
    assert r.app._ava_question is None
    assert old.approved is False and replacement.approved is None
    assert policy.pending_operation() is replacement
    assert r.effects == []


@pytest.mark.parametrize("path", ["consume", "tick"])
def test_replacement_installed_during_expiry_cleanup_survives(real_rig, path):
    r = real_rig
    deliver(r, ["close window"])
    q, old, replacements = r.app._ava_question, policy.pending_operation(), []

    def replace(_owner):
        replacements.append(policy.stage_pending(r.app, policy.Invocation(
            "close window", route=policy.Route.MODEL, generation=0), "Replacement?"))

    r.app._release_wake_consumer.side_effect = replace
    r.now[0] = q.deadline + 0.01
    if path == "consume":
        assert ava_pending.consume(r.app, "yes")
    else:
        ava_pending._tick(r.app, q)
    assert old.approved is False and replacements[0].approved is None
    assert policy.pending_operation() is replacements[0]
    assert r.effects == []


@pytest.mark.parametrize("first", ["open chrome", "close window"])
def test_known_later_denial_prevents_every_effect_and_confirmation(real_rig, monkeypatch, first):
    r = real_rig
    r.executor.command_availability = lambda cid: ("pack_disabled", "synthetic") if cid == "scroll down" else None
    real_authorize, checks = policy.authorize, []

    def authorize(inv, **kwargs):
        checks.append((inv.command_id, kwargs.get("quiet"), kwargs.get("confirmed", False)))
        return real_authorize(inv, **kwargs)

    monkeypatch.setattr(policy, "authorize", authorize)
    turn = deliver(r, [first, "scroll down", "open chrome"])
    assert turn.state == "failed" and turn.name == "2" and turn.reason == "pack_disabled"
    assert r.effects == [] and policy.pending_operation() is None
    assert getattr(r.app, "_ava_question", None) is None
    assert checks == [(first, True, False), ("scroll down", True, False)]
    assert "Step 2 failed: scroll down" in r.spoken[-1]
    assert "pack disabled" in r.spoken[-1] and "Nothing was run" in r.spoken[-1]


def test_named_app_preflight_uses_same_invocation_as_execution(real_rig, monkeypatch):
    r = real_rig
    r.executor.command_availability = lambda cid: ("pack_disabled", "synthetic") if cid == "open" else None
    opened = Mock(return_value=app_verbs.ActionResult.DONE)
    monkeypatch.setattr(app_verbs, "do_open", opened)
    turn = deliver(r, ["scroll down", {"id": "open", "args": {"app_name": "chrome"}}])
    assert turn.reason == "pack_disabled" and turn.name == "2"
    assert r.effects == []
    opened.assert_not_called()


def test_denial_while_waiting_for_first_step_approval_still_runs_nothing(real_rig):
    r = real_rig
    assert deliver(r, ["close window", "scroll down"]).state == "queued"
    r.executor.command_availability = lambda cid: ("pack_disabled", "synthetic") if cid == "scroll down" else None
    assert ava_pending.consume(r.app, "yes")
    assert r.effects == [] and policy.pending_operation() is None
    assert "Step 2 failed: scroll down" in r.spoken[-1]
    assert "Nothing was run" in r.spoken[-1]


@pytest.mark.parametrize("failure", ["denied", "exception", "failed"])
def test_runtime_failure_reports_completed_steps_without_undo(real_rig, failure):
    r = real_rig
    execute = r.executor.execute_canonical

    def changed(cid, app, **kw):
        if cid == "open chrome":
            result = execute(cid, app, **kw)
            if failure == "denied":
                r.executor.command_availability = lambda name: ("pack_disabled", "synthetic") if name == "scroll down" else None
            return result
        if failure == "exception":
            raise RuntimeError("synthetic")
        return commands.DispatchResult(commands.DispatchState.FAILED, cid, cid)

    r.executor.execute_canonical = changed
    turn = deliver(r, ["open chrome", "scroll down", "open chrome"])
    assert turn.state == "failed" and turn.name == "2"
    assert r.effects == ["open chrome"]
    assert "Step 2 failed: scroll down" in r.spoken[-1]
    assert "Already ran: step 1: open chrome" in r.spoken[-1]


def test_model_question_and_each_destructive_step_keep_separate_approval(real_rig):
    r = real_rig
    plan = ["scroll down", "close window", "close window", "scroll down"]
    data = {"type": "ask", "kind": "yes_no", "question": "Run these steps?", "plan": plan}
    ava_plan.handle_json(r.app, json.dumps(data), "scroll down and close this window", 0)
    assert ava_pending.consume(r.app, "yes") and r.effects == ["scroll down"]
    first = policy.pending_operation()
    assert ava_pending.consume(r.app, "yes") and r.effects == plan[:2]
    assert policy.pending_operation() is not first
    assert ava_pending.consume(r.app, "yes") and r.effects == plan
    assert policy.pending_operation() is None
    assert not ava_pending.consume(r.app, "yes")
