"""624: fake providers, real policy, no app bootstrap, microphone or OS effects."""
import ast
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama as ava
from samsara import ai_preferences, ai_transport, ava_pending, ava_plan, execution_policy as policy
from samsara.command_registry import DispatchResult, DispatchState
from samsara.commands import CommandExecutor
from samsara.ava_memory import AvaMemory


class Timer:
    def __init__(self, delay, fn):
        self.delay, self.fn, self.cancelled = delay, fn, False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn()


@pytest.fixture
def rig(monkeypatch, tmp_path):
    from tests.test_ava_one_answer_536 import config
    now, timers, spoken, effects, payloads = [100.0], [], [], [], []
    executor = CommandExecutor.__new__(CommandExecutor)
    executor.commands = {
        "open chrome": {"type": "launch", "path": "synthetic.exe"},
        "close window": {"type": "hotkey", "keys": ["alt", "f4"]},
        "scroll down": {"type": "hotkey", "keys": ["pagedown"]},
    }
    menu = ["open chrome", "close window", "scroll down", "close"]
    executor.ava_menu = lambda *_a, **_kw: list(menu)
    executor._registry_entry = lambda _cid: None
    executor.execute_canonical = lambda cid, app, **kw: (
        effects.append((cid, kw)) or DispatchResult(DispatchState.COMPLETED, cid, cid))
    app = SimpleNamespace(config=config(), command_executor=executor, _ava_memory=AvaMemory(),
        _ava_cmd_generation=0, _ava_cmd_mode_lock=threading.Lock(),
        app_state="asleep", wake_word_triggered=False, _ensure_wake_consumer=Mock(),
        _release_wake_consumer=Mock(), listening_indicator=Mock(),
        _schedule_ui=lambda fn, *a, **kw: fn(*a, **kw),
        _show_outcome_chip=Mock())

    def speak(text, **kw):
        spoken.append(text)
        if kw.get("on_done"):
            kw["on_done"]()
        return SimpleNamespace(utterance_id="fake")

    app.audio_coordinator = SimpleNamespace(speak=speak, is_speaking=False)
    app.config.update(ava_memory={"mode": "last"})
    app.config["ava"]["catalog_answers"] = False
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    monkeypatch.setattr(ava, "_turn_local", threading.local())
    monkeypatch.setattr(ava_pending, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(ava, "_check_ollama_available", lambda *_a, **_kw: True)
    monkeypatch.setattr(ava, "get_system_prompt", lambda _app: "Instructions. {COMMAND_LIST}")
    monkeypatch.setattr(ava.ava_profile, "build_context_section", lambda *_a: "")
    monkeypatch.setattr(ava.ava_corrections, "build_context_section", lambda **_kw: "")
    monkeypatch.setattr(ava, "_assistant_recall_allowed", lambda *_a: False)
    monkeypatch.setattr(policy, "foreground_window_identity", lambda: {"hwnd": 42, "pid": 100})
    monkeypatch.setattr(policy, "resolve_target", lambda _name: {"hwnd": 43, "pid": 101})

    def timer(_name, delay, fn, **_kw):
        t = Timer(delay, fn)
        timers.append(t)
        return t

    monkeypatch.setattr(ava_pending.thread_registry, "timer", timer)
    ava.clear_pending_action()
    yield SimpleNamespace(app=app, now=now, timers=timers, spoken=spoken, effects=effects,
                          payloads=payloads, menu=menu, executor=executor)
    ava.clear_pending_action()


def deliver(rig, data, utterance="open chrome and close this window"):
    return ava.handle_response(rig.app, json.dumps(data), original_text=utterance, generation=0)


def question(rig, **kw):
    return deliver(rig, {"type": "ask", "kind": "yes_no", "question": "Run those steps?",
                         "plan": ["open chrome", "scroll down"], **kw}, "open chrome and scroll down")


@pytest.mark.parametrize("provider", ["ollama", "deepseek", "openai", "openrouter", "anthropic"])
def test_fake_provider_plan_uses_json_and_runs_in_order_with_local_close_confirmation(rig, monkeypatch, provider):
    from plugins.commands import app_verbs
    from tests.test_ava_one_answer_536 import config
    r = rig
    data = {"type": "plan", "plan": ["open chrome", {"id": "close", "args": {"app_name": "notepad"}}]}
    if provider != "ollama":
        r.app.config = config("cloud")
        r.app.config["ava_memory"] = {"mode": "last"}
        r.app.config["cloud_llm"]["provider"] = provider
        selected = ai_preferences.provider_for(r.app.config, "cloud")
        consent = ai_preferences.accept_consent(r.app.config, features=("ava",), provider=selected,
            cloud=True, accepted_at="2026-10-16T00:00:00Z")
        r.app.config = ai_preferences.apply_preferences(r.app.config, policy="cloud", ava=True, consent=consent)

    class Response:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self):
            content = json.dumps(data)
            return {"message": {"content": content}, "choices": [{"message": {"content": content}}],
                    "content": [{"type": "text", "text": content}]}

    def post(_url, **kw):
        r.payloads.append(kw["json"])
        return Response()

    monkeypatch.setattr(ai_transport, "post_local", post)
    monkeypatch.setattr(ai_transport, "post_cloud", post)
    monkeypatch.setattr(app_verbs, "do_close", lambda name: r.effects.append(("close " + name, {})) or app_verbs.ActionResult.DONE)
    reply = ava.ask_model("open chrome and close notepad", r.app, allow_search=True)
    assert reply.ok
    payload = r.payloads[0]
    if provider == "ollama":
        assert payload["format"] == "json"
    elif provider != "anthropic":
        assert payload["response_format"] == {"type": "json_object"}
    else:
        assert "AVA_STRUCTURED_REPLY_624" in payload["system"]
        assert "response_format" not in payload
    turn = ava.handle_response(r.app, reply.text, original_text="open chrome and close notepad", generation=0)
    assert turn.state == "queued"
    assert [e[0] for e in r.effects] == ["open chrome"]
    assert policy.pending_operation().invocation.command_id == "action2:close"
    assert ava_pending.consume(r.app, "yes")
    assert [e[0] for e in r.effects] == ["open chrome", "close notepad"]
    assert policy.pending_operation() is None


def test_safe_plan_executes_all_steps_with_model_route(rig):
    assert deliver(rig, {"type": "plan", "plan": ["open chrome", "scroll down"]}).state == "completed"
    assert [e[0] for e in rig.effects] == ["open chrome", "scroll down"]
    assert all(e[1]["route"] is policy.Route.MODEL for e in rig.effects)


@pytest.mark.parametrize("plan", [[], ["open chrome"] * 5, ["open chrome", "invented"],
    ["yes"], ["open firefox"], [{"id": "close", "args": {"app_name": "firefox"}}],
    [{"id": "open chrome", "args": {"remainder": "injected"}}], ["close"]])
def test_whole_invalid_plan_is_refused_before_first_effect(rig, plan):
    assert deliver(rig, {"type": "plan", "plan": plan}).state == "refused"
    assert rig.effects == [] and policy.pending_operation() is None


def test_snapshot_menu_cannot_expand_at_execution_time(rig):
    ava._turn_local.plan_menu = ("scroll down",)
    assert deliver(rig, {"type": "plan", "plan": ["open chrome"]}).state == "refused"
    assert rig.effects == []


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_question_opens_reply_window_and_answers_once(rig, answer):
    assert question(rig).state == "queued"
    assert rig.app.app_state == "command_window" and rig.app.wake_word_triggered
    rig.app._ensure_wake_consumer.assert_called_once_with("ava_question")
    assert ava_pending.consume(rig.app, answer)
    assert not ava_pending.consume(rig.app, "yes")
    assert len(rig.effects) == (2 if answer == "yes" else 0)
    assert rig.app._ava_question is None and rig.app.app_state == "asleep"
    rig.app._release_wake_consumer.assert_called_once_with("ava_question")


def test_silence_expires_quietly_after_configured_window(rig):
    rig.app.config["ava"]["reply_window_seconds"] = 9
    question(rig)
    assert rig.app._ava_question.deadline == 109
    before = list(rig.spoken)
    rig.now[0] = 109.1
    rig.timers[-1].fire()
    # 830: a yes/no question offers one retry, then expires without running the plan.
    assert rig.spoken == before + ['Say yes or no']
    assert rig.app._ava_question.deadline == 118.1
    rig.now[0] = 118.2
    rig.timers[-1].fire()
    assert rig.app._ava_question is None and not rig.effects
    assert rig.spoken == before + ['Say yes or no']
    assert any("expired" in c.args[0] for c in rig.app.listening_indicator.show_outcome.call_args_list)


def test_speech_completion_starts_window_and_cancelled_speech_is_bounded(rig):
    done = []
    rig.app.audio_coordinator.speak = lambda _text, **kw: done.append(kw["on_done"]) or SimpleNamespace(utterance_id="fake")
    question(rig)
    assert not rig.app._ava_question.opened
    assert ava_pending.consume(rig.app, "yes") and not rig.effects
    rig.now[0] += 10
    done.pop()()
    assert rig.app._ava_question.deadline == 120  # 830: default window is ten seconds.


def test_model_question_yes_cannot_approve_a_destructive_step(rig):
    question(rig, plan=["close window"])
    assert ava_pending.consume(rig.app, "yes")
    assert rig.effects == []
    assert policy.pending_operation() is not None
    assert rig.app._ava_question.kind == "policy"
    assert ava_pending.consume(rig.app, "yes")
    assert [e[0] for e in rig.effects] == ["close window"]
    assert rig.effects[0][1]["confirmed"] is True


def test_changed_foreground_refuses_close_and_remaining_steps(rig, monkeypatch):
    deliver(rig, {"type": "plan", "plan": ["close window", "scroll down"]})
    monkeypatch.setattr(policy, "foreground_window_identity", lambda: {"hwnd": 999})
    assert ava_pending.consume(rig.app, "yes")
    assert rig.effects == [] and policy.pending_operation() is None
    assert "Step 1 failed" in rig.spoken[-1]


@pytest.mark.parametrize("change", ["off", "generation", "foreign_pending"])
def test_retired_question_consumes_reply_without_running_anything(rig, change):
    question(rig)
    if change == "off":
        rig.app.config["ava"]["provider_policy"] = "off"
    elif change == "generation":
        policy.bump_generation(rig.app, "cancel")
    else:
        policy.stage_pending(rig.app, policy.Invocation("close window", route=policy.Route.MODEL, generation=0), "Foreign?")
    assert ava_pending.consume(rig.app, "yes")
    assert not rig.effects and rig.app._ava_question is None


def test_failure_stops_and_names_the_step(rig):
    def execute(cid, app, **kw):
        rig.effects.append((cid, kw))
        return DispatchResult(DispatchState.FAILED, cid, cid)
    rig.executor.execute_canonical = execute
    assert deliver(rig, {"type": "plan", "plan": ["open chrome", "scroll down"]}).state == "failed"
    assert len(rig.effects) == 1 and "Step 1 failed: open chrome" in rig.spoken[-1]


def test_every_step_reauthorizes_and_stops_after_policy_changes(rig, monkeypatch):
    real = policy.authorize
    def authorize(inv, **kw):
        return policy.Denied("pack_disabled") if inv.command_id == "scroll down" and rig.effects else real(inv, **kw)
    monkeypatch.setattr(policy, "authorize", authorize)
    assert deliver(rig, {"type": "plan", "plan": ["open chrome", "scroll down"]}).state == "failed"
    assert len(rig.effects) == 1 and "Step 2 failed" in rig.spoken[-1]


@pytest.mark.parametrize("kind,options,answer", [("choice", ["Chrome", "Notepad"], "2"), ("clarify", None, "Notepad")])
def test_choice_and_clarification_reenter_ordered_ava_lane(rig, monkeypatch, kind, options, answer):
    data = {"type": "ask", "kind": kind, "question": "Which application?", "plan": []}
    if options is not None: data["options"] = options
    assert deliver(rig, data, "open an application").state == "queued"
    resumed = Mock()
    monkeypatch.setattr(ava, "handle_ask_ava", resumed)
    assert ava_pending.consume(rig.app, answer)
    assert resumed.call_args.kwargs["remainder"] == "open an application\nFollow-up answer: Notepad"
    assert rig.effects == []


def test_invalid_answer_stays_owned_instead_of_becoming_a_global_command(rig):
    question(rig)
    assert ava_pending.consume(rig.app, "yes and delete everything")
    assert rig.app._ava_question is not None and not rig.effects


def test_hold_ava_routes_yes_before_global_command_table(rig):
    # Execute only this existing method's source; never import/bootstrap dictation.
    source = ast.parse((Path(__file__).resolve().parents[1] / "dictation.py").read_text(encoding="utf-8"))
    cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_route_to_ava")
    scope = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "ava_hold_seam", "exec"), scope)
    global_yes = rig.executor.process_text = Mock()
    question(rig)
    scope["_route_to_ava"](rig.app, "yes")
    global_yes.assert_not_called()
    assert len(rig.effects) == 2


def test_off_never_creates_pending_state_or_provider_calls(rig, monkeypatch):
    rig.app.config["ava"]["provider_policy"] = "off"
    provider = Mock(side_effect=AssertionError("provider called"))
    monkeypatch.setattr(ai_transport, "post_local", provider)
    monkeypatch.setattr(ai_transport, "post_cloud", provider)
    assert not ava.ask_model("open chrome", rig.app, allow_search=True).ok
    assert question(rig).state == "stale"
    assert getattr(rig.app, "_ava_question", None) is None
    provider.assert_not_called()


def test_unreachable_provider_creates_no_pending_question(rig, monkeypatch):
    monkeypatch.setattr(ava, "_check_ollama_available", lambda *_a, **_kw: False)
    assert not ava.ask_model("open chrome", rig.app, allow_search=True).ok
    assert getattr(rig.app, "_ava_question", None) is None


def test_reply_window_polls_audio_even_with_wake_word_mode_off(rig, monkeypatch):
    from samsara.audio_engine import wake_consumer
    rig.app.wake_word_active = False
    rig.app.command_mode_active = False
    question(rig)
    listener = wake_consumer.WakeConsumer.__new__(wake_consumer.WakeConsumer)
    listener._app, listener._running = rig.app, True
    listener._stop_event = threading.Event()
    frame = object()
    listener._reader = SimpleNamespace(read_next=lambda: frame)
    received = []
    def process(actual):
        received.append(actual)
        listener._running = False
    listener._process_frame = process
    listener._close_capture_duck = Mock()
    listener._handle_fatal = Mock()
    # If the polling admission excludes the reply window, fail without hanging.
    monkeypatch.setattr(wake_consumer.time, "sleep", lambda _s: listener._stop_event.set())
    listener._poll_loop()
    assert received == [frame]
    listener._handle_fatal.assert_not_called()
    assert rig.app.wake_word_active is False


def test_stop_during_question_speech_still_uses_global_stop_guard(rig):
    rig.app.audio_coordinator.speak = lambda *_a, **_kw: SimpleNamespace(utterance_id="fake")
    question(rig)
    assert not rig.app._ava_question.opened
    assert ava_pending.consume(rig.app, "stop it")
    assert rig.app._ava_question is None and not rig.effects
    assert policy.current_generation(rig.app) == 1


def test_late_speech_callback_cannot_reopen_cancelled_question(rig):
    done = []
    rig.app.audio_coordinator.speak = lambda _text, **kw: done.append(kw["on_done"]) or SimpleNamespace(utterance_id="fake")
    question(rig)
    assert ava_pending.consume(rig.app, "cancel")
    done.pop()()
    assert rig.app._ava_question is None
    rig.app._ensure_wake_consumer.assert_not_called()


def test_question_cannot_replace_a_typing_approval(rig):
    inv = policy.Invocation("close window", route=policy.Route.MODEL, generation=0)
    existing = policy.stage_pending(rig.app, inv, "Existing typing approval?")
    assert question(rig).state == "refused"
    assert policy.pending_operation() is existing
    assert getattr(rig.app, "_ava_question", None) is None


def test_each_destructive_step_needs_its_own_approval(rig):
    deliver(rig, {"type": "plan", "plan": ["close window", "close window"]})
    assert ava_pending.consume(rig.app, "yes")
    assert len(rig.effects) == 1 and policy.pending_operation() is not None
    assert ava_pending.consume(rig.app, "no")
    assert len(rig.effects) == 1 and policy.pending_operation() is None


def test_policy_question_expiry_removes_policy_pending_too(rig):
    deliver(rig, {"type": "plan", "plan": ["close window"]})
    before = list(rig.spoken)
    rig.now[0] += 11  # 830: default reply window is ten seconds.
    rig.timers[-1].fire()
    assert policy.pending_operation() is None and not rig.effects
    assert rig.spoken == before


def test_wait_keeps_the_policy_question_and_reply_capture(rig):
    deliver(rig, {"type": "plan", "plan": ["close window"]})
    current = rig.app._ava_question
    rig.now[0] += 7
    assert ava_pending.consume(rig.app, "wait")
    assert rig.app._ava_question is current and current.deadline == 117  # 830: ten-second extension.
    assert policy.pending_operation().extended
    assert ava_pending.consume(rig.app, "yes") and len(rig.effects) == 1


@pytest.mark.parametrize("display", ["Full", "Compact"])
def test_waiting_question_is_visible_on_both_displays(rig, monkeypatch, qapp, tmp_path, display):
    rig.app.config["ui"] = {"live_surface": {"enabled": True}}
    if display == "Full":
        from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
        from samsara.ui import qt_runtime
        monkeypatch.setattr(qt_runtime, "post", lambda fn: fn())
        controller = LiveSurfaceController(rig.app)
        controller.start()
        indicator, widget = LiveSurfaceIndicatorAdapter(controller), controller.widget
    else:
        from samsara.ui.listening_indicator import ListeningIndicator
        indicator = widget = ListeningIndicator()
        widget.show()
    rig.app.listening_indicator = indicator
    try:
        question(rig)
        qapp.processEvents()
        if display == "Full":
            widget.refresh(controller.view(), animate=False)
            assert "waiting for your answer" in controller.view().notice.message
        else:
            assert "waiting for your answer" in indicator._chip_label
        assert widget.grab().save(str(tmp_path / f"624_{display}_waiting.png"))
    finally:
        if display == "Compact":
            # Retire the native pill and its timers while its Python owner lives.
            widget.destroy()
        else:
            widget.hide()
        widget.deleteLater()
