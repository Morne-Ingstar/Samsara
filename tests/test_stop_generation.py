"""Freshness and the stop path (02_conversational_architecture.md sections
1, 5 and 7 step 1).

* Every request carries the generation captured when it was CREATED; a
  missing or older generation is Denied(stale) -- a request made under N and
  evaluated under N+1 does nothing.
* "stop" (whole utterance) bumps the generation FIRST, cancels model
  requests, drains queued effects, keeps the draft, keeps the microphone
  armed, chip "stopped". "go to sleep" = the same stop, then disarm.
* "scratch that" cancels an unexecuted proposal before touching the undo
  stack.
With a stalled fake model and a stalled fake effect: after stop, zero effects
from earlier requests.
"""
import collections
import json
import sys
import threading
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.commands import ask_ollama  # noqa: E402
from samsara import commands as commands_mod, execution_policy as ep  # noqa: E402
from samsara import plugin_commands  # noqa: E402
from samsara.command_registry import DispatchResult, DispatchState  # noqa: E402
from samsara.commands import CommandExecutor  # noqa: E402
from samsara.execution_policy import Allowed, Denied, Invocation, Route  # noqa: E402
from samsara import session_modes as sm  # noqa: E402

COMMANDS = {
    "switch window": {"type": "hotkey", "keys": ["alt", "tab"]},
    "enter": {"type": "press", "key": "enter"},
    "permanent delete": {"type": "hotkey", "keys": ["shift", "delete"]},
}
SIGNALS = sm.UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))


class _FakeHandler:
    def __init__(self, effects):
        self.effects = effects

    def execute(self, cmd, ctx):
        self.effects.append(cmd)
        return True


@pytest.fixture
def effects():
    return []


@pytest.fixture
def executor(tmp_path, effects, monkeypatch):
    path = tmp_path / "commands.json"
    path.write_text(json.dumps({"commands": COMMANDS}), encoding="utf-8")
    ex = CommandExecutor(path, plugins_dir=tmp_path / "no_plugins")
    monkeypatch.setattr(commands_mod, "get_handler", lambda _type: _FakeHandler(effects))
    return ex


@pytest.fixture
def app(executor):
    a = types.SimpleNamespace()
    a.config = {"ollama": {"enabled": True}}
    a.audio_coordinator = Mock()
    a.play_sound = Mock()
    a.command_executor = executor
    a._ava_cmd_generation = 3
    a._ava_cmd_mode_lock = threading.Lock()
    a._ava_session_dispatch_queue = collections.deque(maxlen=3)
    a._ava_session_dispatch_lock = threading.Lock()
    a._ava_session_request_in_flight = False
    a._show_outcome_chip = Mock()
    a.command_matching_enabled = True
    a.command_mode_active = True
    return a


@pytest.fixture(autouse=True)
def _clean_pending():
    ask_ollama.clear_pending_action()
    ask_ollama._stop_schedule()
    yield
    ask_ollama.clear_pending_action()
    ask_ollama._stop_schedule()


def _manager(app, order=None, **kw):
    order = order if order is not None else []

    def stop_fn(reason):
        order.append(("stop", reason, ep.current_generation(app)))
        return ep.stop_all(app, reason)

    def on_abort():
        order.append(("disarm", ep.current_generation(app)))

    manager = sm.SessionModeManager(
        abort_phrases=["cancel", *sm.GLOBAL_SESSION_EXIT_PHRASES],
        foreground_exe_resolver=lambda: "obsidian.exe",
        inject_fn=lambda *a, **k: True,
        remove_chars_fn=kw.pop("remove_chars_fn", lambda n: None),
        command_dispatch_fn=lambda text: sm.CommandDispatchResult(matched=False),
        agent_dispatch_fn=lambda *a: None,
        buffer_dictate_until_commit=True,
        stop_fn=stop_fn,
        on_abort=on_abort,
        **kw,
    )
    manager.reset(initial_mode=sm.SessionMode.DICTATE)
    return manager, order


# ---------------------------------------------------------------------------
# 1. Freshness: capture-time generation, never evaluation-time
# ---------------------------------------------------------------------------

class TestFreshness:
    @pytest.mark.parametrize("route", list(Route))
    def test_created_under_n_evaluated_under_n_plus_1_is_denied(self, app, executor, route):
        inv = Invocation("switch window", route=route, generation=ep.capture_generation(app))
        assert isinstance(ep.authorize(inv, app=app, executor=executor), Allowed)
        ep.bump_generation(app, "stop")
        d = ep.authorize(inv, app=app, executor=executor)
        assert isinstance(d, Denied) and d.reason == "stale"

    @pytest.mark.parametrize("route", list(Route))
    @pytest.mark.parametrize("generation", [None, "3", True, 99, -1])
    def test_missing_or_unknown_generation_is_denied(self, app, executor, route, generation):
        d = ep.authorize(Invocation("switch window", route=route, generation=generation),
                         app=app, executor=executor)
        assert isinstance(d, Denied) and d.reason == "stale"

    def test_model_route_without_a_captured_generation_runs_nothing(self, app, executor, effects):
        assert executor.execute_command("switch window", app, route=Route.MODEL) is False
        assert effects == []

    def test_exact_call_captures_its_generation_at_the_call(self, app, executor, effects):
        assert executor.execute_command("switch window", app) is True
        assert tuple(executor.process_text("switch window", app, force_commands=True)) == ("switch window", True)
        assert len(effects) == 2

    def test_utterance_captured_before_a_stop_runs_nothing_after_it(self, app, executor, effects):
        captured = ep.capture_generation(app)       # the wake FIFO stamped this utterance
        ep.stop_all(app, "stop")
        result = executor.process_text("switch window", app, force_commands=True, generation=captured)
        assert result.claimed and result.state is not DispatchState.COMPLETED
        assert effects == []

    def test_stale_plugin_utterance_never_reaches_its_handler(self, app, executor):
        saved = dict(plugin_commands._REGISTRY)
        plugin_commands._REGISTRY.clear()
        calls = []
        try:
            plugin_commands.command("tidy desk", risk_class="safe")(lambda a, r: calls.append(r) or True)
            executor.rebuild_matcher()
            captured = ep.capture_generation(app)
            ep.bump_generation(app, "stop")
            result = executor.process_text("tidy desk", app, force_commands=True, generation=captured)
            assert result.state is DispatchState.REJECTED and calls == []
        finally:
            plugin_commands._REGISTRY.clear()
            plugin_commands._REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# 2. Stop
# ---------------------------------------------------------------------------

class TestStop:
    def test_stop_with_a_stalled_fake_model_leaves_zero_effects(self, app, executor, effects, monkeypatch):
        release, started = threading.Event(), threading.Event()

        def stalled_model(prompt, app_):
            started.set()
            release.wait(5)
            return "CONFIRM Switching.\nACTION switch window"

        threads = []

        def spawn(name, target, *a, **k):
            t = threading.Thread(target=target, daemon=True)
            threads.append(t)
            t.start()
            return t

        monkeypatch.setattr(ask_ollama, "ask_ollama", stalled_model)
        monkeypatch.setattr(ask_ollama, "_check_ollama_available", lambda host, timeout=3: True)
        monkeypatch.setattr(ask_ollama, "_check_teaching_intent", lambda app_, text: False)
        monkeypatch.setattr(ask_ollama.thread_registry, "spawn", spawn)

        manager, order = _manager(app)
        manager.dispatch_utterance("a staged thought", SIGNALS)
        ask_ollama.handle_ask_ava(app, "switch to the other window",
                                  generation=ep.capture_generation(app))
        assert started.wait(2)
        gen_before = ep.current_generation(app)

        outcome = manager.dispatch_utterance("Stop.", SIGNALS)

        assert outcome.kind == "stopped"
        assert order == [("stop", "stop", gen_before)]            # stop ran; no disarm
        assert ep.current_generation(app) == gen_before + 1         # generation first
        assert manager.dictate_pending_buffer.strip() == "a staged thought"   # draft kept
        assert manager.mode is sm.SessionMode.DICTATE               # mic/session still armed
        assert outcome.detail["draft_kept_chars"] == len(manager.dictate_pending_buffer)
        assert sm.outcome_chip(outcome.kind, outcome.detail) == ("stopped", "warning")
        assert ("stopped", "warning") in [c.args for c in app._show_outcome_chip.call_args_list]

        release.set()
        for t in threads:
            t.join(3)
        assert effects == [], "the stalled model's late answer must not act"

    def test_stop_with_a_stalled_fake_effect_leaves_zero_effects(self, app, executor, effects):
        """An approved effect whose delivery stalls before the effect boundary
        (e.g. marshalled to another thread) re-checks the generation there."""
        at_boundary, release = threading.Event(), threading.Event()
        inv = Invocation("permanent delete", {}, Route.MODEL, ep.capture_generation(app))

        def stalled_delivery(op):
            at_boundary.set()
            release.wait(5)
            executor.execute_command(inv.command_id, app, route=inv.route,
                                     generation=inv.generation, confirmed=True)

        ep.stage_pending(app, inv, ep.confirmation_prompt(inv.command_id), on_approve=stalled_delivery)
        worker = threading.Thread(target=lambda: ep.answer_pending(app, "yes"), daemon=True)
        worker.start()
        assert at_boundary.wait(2)

        manager, _order = _manager(app)
        assert manager.dispatch_utterance("stop", SIGNALS).kind == "stopped"
        release.set()
        worker.join(3)
        assert effects == []

    def test_stop_drains_queued_requests_and_cancels_the_pending_question(self, app, executor, effects):
        app._ava_session_dispatch_queue.append((3, "close the window"))
        op = ep.stage_pending(app, Invocation("permanent delete", {}, Route.MODEL, 3), "Permanent delete?")
        manager, _ = _manager(app)
        outcome = manager.dispatch_utterance("stop", SIGNALS)
        assert outcome.detail["cleared"]["queued"] >= 1 and outcome.detail["cleared"]["pending"] is True
        assert len(app._ava_session_dispatch_queue) == 0
        assert op.approved is False and ep.pending_operation() is None
        assert ep.answer_pending(app, "yes") is None and effects == []

    @pytest.mark.parametrize("prose", ["stop the music", "please don't stop", "stop sign ahead"])
    def test_stop_is_whole_utterance_only(self, app, executor, prose):
        manager, order = _manager(app)
        assert manager.dispatch_utterance(prose, SIGNALS).kind == "dictate_staged"
        assert order == [] and ep.current_generation(app) == 3

    def test_without_a_stop_hook_stop_stays_ordinary_text(self, app):
        manager = sm.SessionModeManager(
            abort_phrases=["cancel"], foreground_exe_resolver=lambda: "x.exe",
            inject_fn=lambda *a, **k: True, remove_chars_fn=lambda n: None,
            command_dispatch_fn=lambda text: sm.CommandDispatchResult(matched=False),
            agent_dispatch_fn=lambda *a: None, buffer_dictate_until_commit=True)
        manager.reset(initial_mode=sm.SessionMode.DICTATE)
        assert manager.dispatch_utterance("stop", SIGNALS).kind == "dictate_staged"

    def test_go_to_sleep_is_stop_then_disarm(self, app, executor):
        manager, order = _manager(app)
        manager.dispatch_utterance("unsent words", SIGNALS)
        outcome = manager.dispatch_utterance("go to sleep", SIGNALS)
        assert outcome.kind == "mode_switch" and outcome.detail["sleep"] is True
        assert outcome.detail["stopped"] is True
        assert [step[0] for step in order] == ["stop", "disarm"]
        assert order[1][1] == order[0][2] + 1, "disarm observes the already-bumped generation"
        assert manager.retained_draft.strip() == "unsent words"


# ---------------------------------------------------------------------------
# 3. Scratch that: cancel an unexecuted proposal before any undo
# ---------------------------------------------------------------------------

class TestScratchOrdering:
    def test_pending_proposal_is_cancelled_first_and_nothing_is_deleted(self, app, executor):
        removed = []
        op = ep.stage_pending(app, Invocation("permanent delete", {}, Route.MODEL, 3), "Permanent delete?")

        def cancel_pending_first():
            live = ep.pending_operation()
            if live is None:
                return None
            live.cancel(app, "scratched")
            ask_ollama.clear_pending_action()
            return True

        manager, _ = _manager(app, remove_chars_fn=removed.append,
                              pending_action_scratch_fn=cancel_pending_first)
        manager._stack.push(sm.StackItem(kind="dictation_chunk", payload="hello", mode=sm.SessionMode.DICTATE,
                                         timestamp=0, extra={"target_process": "obsidian.exe", "hwnd": None}))
        assert manager.dispatch_utterance("scratch that", SIGNALS).kind == "scratch_success"
        assert op.approved is False and op.cancel_reason == "scratched"
        assert removed == [] and manager.stack_depth == 1, "the undo stack is untouched"

    def test_without_a_proposal_scratch_falls_through_to_the_undo_stack(self, app):
        manager, _ = _manager(app, pending_action_scratch_fn=lambda: None)
        manager.dispatch_utterance("staged words", SIGNALS)
        assert manager.dispatch_utterance("scratch that", SIGNALS).kind == "scratch_success"
        assert manager.dictate_pending_buffer == ""


# ---------------------------------------------------------------------------
# 4. Argument schemas (strict where declared)
# ---------------------------------------------------------------------------

class TestArgumentSchemas:
    SCHEMA = {"level": {"type": "int", "min": 0, "max": 100, "required": True},
              "label": {"type": "str", "max_len": 10}}

    @pytest.mark.parametrize("args,reason", [
        ({"level": 40}, None),
        ({"level": 40, "label": "kitchen"}, None),
        ({}, "invalid_args"),                                  # missing required
        ({"level": 40, "extra": 1}, "invalid_args"),           # extra key
        ({"level": "40"}, "invalid_args"),                     # wrong type, no coercion
        ({"level": True}, "invalid_args"),                     # bool is not int
        ({"level": 400}, "invalid_args"),
        ({"level": 40, "label": "x" * 11}, "invalid_args"),    # bounded string
    ])
    @pytest.mark.parametrize("route", [Route.EXACT, Route.MODEL])
    def test_declared_schema_is_strict(self, args, reason, route):
        err = ep.validate_args(self.SCHEMA, args, route=route)
        assert (err[0] if err else None) == reason

    def test_default_string_bound_applies(self):
        schema = {"text": {"type": "str"}}
        assert ep.validate_args(schema, {"text": "a" * ep.DEFAULT_MAX_STR_LEN}, route=Route.MODEL) is None
        assert ep.validate_args(schema, {"text": "a" * (ep.DEFAULT_MAX_STR_LEN + 1)},
                                route=Route.MODEL)[0] == "invalid_args"

    @pytest.mark.parametrize("schema", [None, {}])
    def test_undeclared_or_empty_schema_is_not_no_arguments(self, schema):
        assert ep.validate_args(schema, {}, route=Route.MODEL)[0] == "unvalidated"
        assert ep.validate_args(schema, {}, route=Route.SMART_ACTION)[0] == "unvalidated"
        # user routes: only the user's own spoken remainder may ride along
        assert ep.validate_args(schema, {"remainder": "obsidian"}, route=Route.EXACT) is None
        assert ep.validate_args(schema, {}, route=Route.EXACT) is None
        assert ep.validate_args(schema, {"level": 3}, route=Route.EXACT)[0] == "invalid_args"

    def test_no_args_schema_rejects_any_argument(self):
        assert ep.validate_args(ep.NO_ARGS_SCHEMA, {}, route=Route.MODEL) is None
        assert ep.validate_args(ep.NO_ARGS_SCHEMA, {"x": 1}, route=Route.MODEL)[0] == "invalid_args"
        assert ep.validate_args(ep.NO_ARGS_SCHEMA, {"remainder": "now"}, route=Route.EXACT)[0] == "invalid_args"

    def test_a_schema_that_declares_remainder_validates_it(self):
        schema = {"remainder": {"type": "str", "required": True, "max_len": 20}}
        assert ep.validate_args(schema, {"remainder": "hello"}, route=Route.EXACT) is None
        assert ep.validate_args(schema, {}, route=Route.EXACT)[0] == "invalid_args"
        assert ep.validate_args(schema, {"remainder": "x" * 21}, route=Route.EXACT)[0] == "invalid_args"

    def test_action2_target_is_bounded(self, app):
        ok = ep.authorize(Invocation("action2:focus", {"target": "notepad"}, Route.MODEL, 3), app=app)
        assert isinstance(ok, Allowed)
        d = ep.authorize(Invocation("action2:focus", {"target": "n" * 121}, Route.MODEL, 3), app=app)
        assert isinstance(d, Denied) and d.reason == "invalid_args"


# ---------------------------------------------------------------------------
# 5. DispatchResult.succeeded is COMPLETED only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", list(DispatchState))
def test_succeeded_means_completed_only(state):
    assert DispatchResult(state, "x", "x").succeeded is (state is DispatchState.COMPLETED)
