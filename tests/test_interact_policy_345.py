"""345b: bound registry, policy and confirmation never retarget an action."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama, interact
from samsara import execution_policy as ep, plugin_commands
from samsara.command_registry import DispatchState
from samsara.subjects import SubjectRef
from samsara.window_targets import BoundWindowEffects, WindowSnapshot, WindowTargetAdapter


class FakeNative:
    def __init__(self):
        self.window_value = WindowSnapshot(101, 101, 0, True, False, 0,
                                           "Downloads - Explorer", "CabinetWClass", 42,
                                           r"C:\Windows\explorer.exe", 123.5, "ok")
        self.pointer_reads = 0

    def window(self, hwnd):
        return self.window_value if hwnd == 101 else None

    def sample_pointer(self):
        self.pointer_reads += 1
        raise AssertionError("an effect must never sample the pointer")


class FakeActions:
    def __init__(self):
        self.calls = []
        self.succeed = True
        self.state_ok = True

    def focus(self, hwnd, preflight):
        assert preflight()
        self.calls.append(("focus", hwnd))
        return self.succeed

    def show(self, hwnd, how):
        self.calls.append((how, hwnd))

    def state(self, hwnd, how):
        return self.state_ok

    def move(self, hwnd, number, preflight):
        assert preflight()
        self.calls.append(("move", hwnd, number))
        return self.succeed

    def snap(self, hwnd, side, preflight):
        assert preflight()
        self.calls.append(("snap", hwnd, side))
        return self.succeed

    def close(self, hwnd):
        self.calls.append(("close", hwnd))
        if not self.succeed:
            raise OSError("PostMessage refused")


@pytest.fixture
def bound():
    ask_ollama.clear_pending_action()
    for entry in plugin_commands._MODULE_ENTRIES[interact.__name__].values():
        plugin_commands._register(entry)
    native = FakeNative()
    targets = WindowTargetAdapter(native, own_pid=999, aliases={})
    actions = FakeActions()
    effects = BoundWindowEffects(targets, actions, sleep=lambda _: None)
    identity = targets.inspect(101).identity
    ref = SubjectRef("window", identity, "secret-token", 3, 7,
                     "Explorer - Downloads", "old-title", "name", 100)
    app = SimpleNamespace(config={}, _ava_cmd_generation=11, _show_outcome_chip=Mock())
    state = [7, 2, 3]
    selected = {"secret-token": ref}
    results = []
    context = interact.BoundContext(app, ref, "secret-token", 2, 11,
                                    selected.get, lambda: tuple(state), effects,
                                    results.append, confirmation_guard=lambda: True)
    yield SimpleNamespace(app=app, native=native, actions=actions, targets=targets,
                          ref=ref, context=context, state=state, selected=selected,
                          results=results)
    ask_ollama.clear_pending_action()


def test_registry_has_six_hidden_noncomposable_identities_and_direct_calls_fail(bound):
    for verb in ("focus", "minimize", "maximize", "move", "snap", "close"):
        entry = plugin_commands._REGISTRY["interact " + verb]
        assert entry["ai_visible"] is False
        assert entry["ai_composable"] is False
        assert entry["voice_triggerable"] is False
        assert entry["func"](bound.app, "101") is DispatchState.FAILED
    assert bound.actions.calls == []


def test_exact_close_requires_confirmation_and_posts_only_after_one_yes(bound):
    inv = ep.Invocation("interact close", {"subject_token": "secret-token"}, ep.Route.EXACT, 11)
    assert isinstance(ep.authorize(inv, app=bound.app), ep.NeedsConfirmation)
    outcome = interact.invoke_bound(bound.context, "close")
    assert isinstance(outcome.decision, ep.NeedsConfirmation)
    assert outcome.pending is ep.pending_operation()
    assert bound.actions.calls == []
    assert ep.answer_pending(bound.app, "yes") == "approved"
    assert bound.actions.calls == [("close", 101)]
    assert bound.results[0].reason == "close_requested"
    assert ep.answer_pending(bound.app, "yes") is None
    assert not outcome.pending.approve(app=bound.app)
    assert bound.actions.calls == [("close", 101)]


def test_no_and_wait_never_close_or_extend(bound):
    outcome = interact.invoke_bound(bound.context, "close")
    assert ep.answer_pending(bound.app, "wait") == "refused:interact deadline"
    assert not outcome.pending.extended
    assert ep.answer_pending(bound.app, "no") == "rejected"
    assert bound.actions.calls == []


def test_always_approve_is_unavailable_for_interact_close(bound):
    outcome = interact.invoke_bound(bound.context, "close")
    assert not outcome.pending.approve(always=True, app=bound.app)
    assert bound.actions.calls == []
    assert ep.answer_pending(bound.app, "no") == "rejected"


@pytest.mark.parametrize("change", [
    {"pid": 43}, {"process_name": r"C:\Other\explorer.exe"},
    {"process_start_time": 124.5}, {"title": "Another document - Explorer"},
])
def test_close_refuses_target_or_title_change_after_question(bound, change):
    outcome = interact.invoke_bound(bound.context, "close")
    bound.native.window_value = replace(bound.native.window_value, **change)
    assert ep.answer_pending(bound.app, "yes").startswith("refused:target changed")
    assert bound.actions.calls == []
    assert not outcome.pending.approved


def test_pending_argument_mutation_cannot_transfer_approval(bound):
    outcome = interact.invoke_bound(bound.context, "close")
    outcome.pending.invocation.args["subject_token"] = "different-token"
    assert ep.answer_pending(bound.app, "yes") == "refused:arguments changed"
    assert bound.actions.calls == []


def test_generation_and_session_revision_changes_refuse(bound):
    interact.invoke_bound(bound.context, "close")
    bound.app._ava_cmd_generation += 1
    assert ep.answer_pending(bound.app, "yes").startswith("refused:stale")
    assert bound.actions.calls == []
    bound.app._ava_cmd_generation -= 1
    bound.state[2] += 1
    assert isinstance(interact.invoke_bound(bound.context, "focus").decision, ep.Denied)


def test_foreground_and_pointer_drift_do_not_retarget(bound):
    bound.native.pointer_reads = 0
    outcome = interact.invoke_bound(bound.context, "focus")
    assert isinstance(outcome.decision, ep.Allowed)
    assert outcome.effect.ok and outcome.effect.subject_ref == bound.ref
    assert bound.actions.calls == [("focus", 101)]
    assert bound.native.pointer_reads == 0


@pytest.mark.parametrize("verb,kwargs,call", [
    ("minimize", {}, ("minimize", 101)),
    ("maximize", {}, ("maximize", 101)),
    ("move", {"monitor": 2}, ("move", 101, 2)),
    ("snap", {"side": "right"}, ("snap", 101, "right")),
])
def test_bound_ui_verbs_use_exact_hwnd(bound, verb, kwargs, call):
    outcome = interact.invoke_bound(bound.context, verb, **kwargs)
    assert outcome.effect.ok
    assert bound.actions.calls == [call]


@pytest.mark.parametrize("verb,kwargs", [
    ("move", {"monitor": 0}), ("snap", {"side": "north"}),
])
def test_schema_rejects_invalid_arguments_before_native_calls(bound, verb, kwargs):
    assert isinstance(interact.invoke_bound(bound.context, verb, **kwargs).decision, ep.Denied)
    assert bound.actions.calls == []


def test_failed_native_call_never_reports_success(bound):
    bound.actions.succeed = False
    outcome = interact.invoke_bound(bound.context, "focus")
    assert outcome.effect.ok is False and outcome.effect.reason == "native_failed"
    assert bound.results == [outcome.effect]


def test_failed_close_post_is_reported_as_failure_after_approval(bound):
    bound.actions.succeed = False
    interact.invoke_bound(bound.context, "close")
    assert ep.answer_pending(bound.app, "yes") == "approved"
    assert bound.results[-1].ok is False
    assert bound.results[-1].reason == "native_failed"


def test_missing_or_superseded_token_refuses_without_effect(bound):
    bound.selected.clear()
    outcome = interact.invoke_bound(bound.context, "close")
    assert isinstance(outcome.decision, ep.Denied)
    assert bound.actions.calls == []


def test_dead_window_is_refused_before_policy_authorization(bound, monkeypatch):
    bound.native.window_value = None
    authorize = Mock(side_effect=AssertionError("authorization must not see dead target"))
    monkeypatch.setattr(ep, "authorize", authorize)
    outcome = interact.invoke_bound(bound.context, "focus")
    assert isinstance(outcome.decision, ep.Denied)
    assert outcome.decision.reason == "target_changed"
    authorize.assert_not_called()
    assert bound.actions.calls == []


def test_close_requires_controller_reply_guard(bound):
    unsafe = replace(bound.context, confirmation_guard=None)
    outcome = interact.invoke_bound(unsafe, "close")
    assert isinstance(outcome.decision, ep.Denied)
    assert outcome.pending is None and bound.actions.calls == []


def test_close_reply_guard_can_reject_a_pre_question_or_ineligible_yes(bound):
    eligible = [True]
    context = replace(bound.context, confirmation_guard=lambda: eligible[0])
    interact.invoke_bound(context, "close")
    eligible[0] = False
    assert ep.answer_pending(bound.app, "yes") == "refused:bound context changed"
    assert bound.actions.calls == []
