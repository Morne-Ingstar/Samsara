"""536: deterministic 25s/1s fake HTTP turns, no network or app bootstrap."""
import json
import logging
from collections import deque
import sys
import threading
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama
from samsara import ai_preferences, ai_transport, ava_local_warmup, ava_readiness, ava_turn_queue, execution_policy
from samsara.ava_memory import AvaMemory


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Timer:
    def __init__(self, delay, fn):
        self.delay, self.fn, self.cancelled = delay, fn, False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn()


def config(route="local"):
    cfg = {"ollama": {"host": "http://127.0.0.1:11434", "model": "synthetic-model"},
           "cloud_llm": {"provider": "deepseek", "api_key": "synthetic"}}
    provider = ai_preferences.provider_for(cfg, route)
    consent = ai_preferences.accept_consent(cfg, features=("ava",), provider=provider,
                                           cloud=route == "cloud", accepted_at="2026-10-11T00:00:00Z")
    return ai_preferences.apply_preferences(cfg, policy=route, ava=True, consent=consent,
                                            local_model="synthetic-model")


@pytest.fixture
def harness(monkeypatch, tmp_path):
    clock, timers, workers = Clock(), [], []
    app = SimpleNamespace(config=config(), _ava_memory=AvaMemory(),
                          listening_indicator=Mock(), play_sound=Mock(),
                          _schedule_ui=lambda fn, *a, **kw: fn(*a, **kw),
                          tts_engine=SimpleNamespace(speak=lambda text: app.answers.append(text)),
                          answers=[])
    app.config.update(tts={"feedback_level": "brief"}, ava_memory={"mode": "last"})
    app.config["ava"]["catalog_answers"] = False
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    monkeypatch.setattr(ava_turn_queue, "time", clock)
    monkeypatch.setattr(ask_ollama, "time", clock)
    monkeypatch.setattr(ask_ollama, "_turn_local", threading.local())
    monkeypatch.setattr(ask_ollama, "_check_ollama_available", lambda *_a, **_kw: True)
    monkeypatch.setattr(ask_ollama, "get_system_prompt", lambda app: "Synthetic instructions.")
    monkeypatch.setattr(ask_ollama.ava_profile, "build_context_section", lambda *_a: "")
    monkeypatch.setattr(ask_ollama.ava_corrections, "build_context_section", lambda **_kw: "")
    monkeypatch.setattr(ask_ollama, "_handle_persona_intent", lambda *_a: False)
    monkeypatch.setattr(ask_ollama, "_handle_working_context_intent", lambda *_a: False)
    monkeypatch.setattr(ask_ollama, "_handle_personal_memory_intent", lambda *_a: False)
    monkeypatch.setattr(ask_ollama, "_check_teaching_intent", lambda *_a: False)
    monkeypatch.setattr(ask_ollama, "_log_turn", lambda *_a, **_kw: None)
    from plugins.commands import dispatch_voice, comfy
    monkeypatch.setattr(dispatch_voice, "route_ava_utterance", lambda *_a: False)
    monkeypatch.setattr(comfy, "try_voice_command", lambda *_a: None)

    def timer(name, delay, fn, **kw):
        item = Timer(delay, fn)
        timers.append(item)
        return item

    def spawn(name, fn, **kw):
        workers.append(fn)

    monkeypatch.setattr(ava_turn_queue.thread_registry, "timer", timer)
    monkeypatch.setattr(ava_turn_queue.thread_registry, "spawn", spawn)
    return SimpleNamespace(app=app, clock=clock, timers=timers, workers=workers)


class Response:
    status_code = 200

    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": self.text}}


class SlowTransport:
    """A fake clock gives the first request 25 seconds, each later one 1."""
    def __init__(self, clock):
        self.clock = clock
        self.entered, self.release = threading.Event(), threading.Event()
        self.requests = []

    def answer(self, messages):
        index = len(self.requests) + 1
        unanswered = []
        for message in reversed(messages):
            if message["role"] == "assistant":
                break
            if message["role"] == "user":
                unanswered.insert(0, message["content"])
        self.requests.append({"order": index, "prompt_lengths": [len(m["content"]) for m in messages],
                              "roles": [m["role"] for m in messages], "latency_s": 25 if index == 1 else 1,
                              "unanswered": len(unanswered)})
        if index == 1:
            self.entered.set()
            assert self.release.wait(3)
            self.clock.advance(25)
        else:
            self.clock.advance(1)
        return " ".join("Synthetic answer one." if text == "Synthetic question one?"
                        else "Synthetic answer two." for text in unanswered)

    def post(self, url, *, json, **kwargs):
        return Response(self.answer(json["messages"]))

    def cloud(self, system, prompt, app, *, messages, **kwargs):
        return self.answer(messages)


@pytest.mark.parametrize("route", ["local", "cloud"])
def test_25_second_first_turn_and_second_question_answer_once_in_order(harness, monkeypatch, route, capsys):
    h = harness
    h.app.config = config(route)
    h.app.config.update(tts={"feedback_level": "brief"}, ava_memory={"mode": "last"})
    h.app.config["ava"]["catalog_answers"] = False
    fake = SlowTransport(h.clock)
    monkeypatch.setattr(ai_transport, "post_local", fake.post)
    monkeypatch.setattr(ask_ollama.cloud_llm, "send", fake.cloud)
    done = Mock()
    ask_ollama.handle_ask_ava(h.app, remainder="Synthetic question one?", on_done=done)
    assert h.app.listening_indicator.set_thinking.call_args.args == (True,)
    assert h.app.ava_thinking_since == 100
    worker = threading.Thread(target=h.workers.pop())
    worker.start()
    try:
        assert fake.entered.wait(2)
        h.clock.advance(1)
        h.timers[0].fire()
        ask_ollama.handle_ask_ava(h.app, remainder="Synthetic question two?", on_done=done)
        assert len(fake.requests) == 1
        assert h.workers == []
    finally:
        fake.release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert h.app.answers == ["Synthetic answer one.", "Synthetic answer two."]
    assert [row["unanswered"] for row in fake.requests] == [1, 1]
    assert [row["latency_s"] for row in fake.requests] == [25, 1]
    assert h.app.listening_indicator.set_thinking.call_args.args == (False,)
    assert h.app.ava_thinking_since is None
    assert done.call_count == 2
    assert h.app.play_sound.call_count == 1
    assert all(timer.cancelled for timer in h.timers)
    print("536 ordered fake request metadata " + json.dumps(fake.requests))


@pytest.mark.parametrize("route", ["local", "cloud"])
def test_unserialized_legacy_path_reproduces_two_unanswered_questions(harness, monkeypatch, route):
    h = harness
    h.app.config = config(route)
    h.app.config["ava_memory"] = {"mode": "last"}
    fake, replies = SlowTransport(h.clock), []
    monkeypatch.setattr(ai_transport, "post_local", fake.post)
    monkeypatch.setattr(ask_ollama.cloud_llm, "send", fake.cloud)
    original = ask_ollama.ask_model.__wrapped__
    worker = threading.Thread(target=lambda: replies.append(original("Synthetic question one?", h.app)))
    worker.start()
    try:
        assert fake.entered.wait(2)
        second = original("Synthetic question two?", h.app)
    finally:
        fake.release.set()
        worker.join(3)
    assert second.text == "Synthetic answer one. Synthetic answer two."
    assert replies[0].text == "Synthetic answer one."
    assert [row["unanswered"] for row in fake.requests] == [1, 2]
    print("536 legacy fake request metadata " + json.dumps(fake.requests))


@pytest.mark.parametrize("route", ["local", "cloud"])
def test_timed_out_completion_is_not_stored_or_spoken(harness, monkeypatch, route):
    h = harness
    h.app.config = config(route)
    h.app.config.update(tts={"feedback_level": "brief"}, ava_memory={"mode": "last"})
    h.app.config["ava"]["catalog_answers"] = False

    def late(*args, **kwargs):
        import requests
        h.clock.advance(31)
        raise requests.exceptions.Timeout()

    monkeypatch.setattr(ai_transport, "post_local", late)
    monkeypatch.setattr(ask_ollama.cloud_llm, "send", lambda *a, **kw: late().text)
    ask_ollama.handle_ask_ava(h.app, remainder="Synthetic question one?")
    h.workers.pop()()
    assert "Synthetic abandoned answer." not in h.app.answers
    assert h.app._ava_memory.get_messages() == []
    assert h.app.listening_indicator.set_thinking.call_args.args == (False,)


@pytest.mark.parametrize("route", ["local", "cloud"])
@pytest.mark.parametrize("level, cues", [("silent", 0), ("brief", 1), ("full", 1)])
def test_waiting_by_two_seconds_cue_once_loading_after_eight(harness, route, level, cues):
    h = harness
    h.app.config["ava"]["provider_policy"] = route
    h.app.config["tts"]["feedback_level"] = level
    ava_turn_queue.submit(h.app, lambda: None, 0)
    assert h.app.ava_thinking_since == h.clock.now  # Appears immediately, before HTTP/probe.
    assert h.app.play_sound.call_count == 0
    assert h.timers[0].delay == 1
    h.clock.advance(1)
    h.timers[0].fire()
    h.timers[0].fire()
    assert h.app.play_sound.call_count == cues
    assert h.timers[1].delay == 8
    assert not h.app.listening_indicator.show_feedback.called
    h.clock.advance(7)
    h.timers[1].fire()
    h.timers[1].fire()
    assert h.app.listening_indicator.show_feedback.call_count == (1 if route == "local" else 0)
    if route == "local":
        assert h.app.listening_indicator.show_feedback.call_args.args[:3] == (
            ava_turn_queue.LOADING_LINE, "accent", None)
    assert h.app.play_sound.call_count == cues
    h.workers.pop()()
    assert h.app.ava_thinking_since is None
    assert h.app.listening_indicator.set_thinking.call_args.args == (False,)
    h.timers[0].fire()
    assert h.app.listening_indicator.show_feedback.call_count == (1 if route == "local" else 0)


def test_cancel_drops_active_and_queued_answers_and_delayed_ui(harness, monkeypatch):
    h, done = harness, Mock()
    ui = []
    h.app._schedule_ui = lambda fn: ui.append(fn)
    ava_turn_queue.submit(h.app, lambda: h.app.answers.append("late"), 0, done)
    ava_turn_queue.submit(h.app, lambda: h.app.answers.append("later"), 0, done)
    h.app._ava_cmd_generation = 1
    ava_turn_queue.cancel(h.app)
    for callback in ui:
        callback()
    assert not ava_turn_queue.has_live(h.app)
    assert h.app.ava_thinking_since is None
    h.workers.pop()()
    assert h.app.answers == [] and done.call_count == 2


def test_local_load_only_loopback_no_user_text_and_failure_silent(harness, monkeypatch, caplog):
    h, sent = harness, []
    # A previous test's readiness listeners must not run during this warm-up.
    monkeypatch.setattr(ava_readiness, "tracker", ava_readiness.ReadinessTracker(h.clock.monotonic))

    def request(method, url, **kwargs):
        sent.append((method, url, kwargs["json"]))
        return Response("")

    # Keep real address validation, substitute only the HTTP request.
    monkeypatch.setattr(ai_transport, "_request", request)
    assert h.app.config["ava"]["warm_on_boot"] is False
    assert not ava_readiness.warm_on_boot_enabled(h.app)
    assert not ava_readiness.schedule_warm_on_boot(h.app, lambda name, fn: h.workers.append(fn))
    h.app.config["ava"]["warm_on_boot"] = True
    assert ava_readiness.schedule_warm_on_boot(h.app, lambda name, fn: h.workers.append(fn))
    assert not ava_readiness.schedule_warm_on_boot(h.app, lambda name, fn: h.workers.append(fn))
    assert sent == []  # Startup was never blocked.
    h.workers.pop()()
    assert sent == [("POST", "http://127.0.0.1:11434/api/generate",
                     {"model": "synthetic-model", "stream": False})]
    assert h.app.answers == []
    h.app.config["ollama"]["host"] = "https://example.invalid"
    assert not ava_local_warmup.load(h.app)
    assert len(sent) == 1
    assert not h.app.listening_indicator.show_feedback.called
    assert h.app.answers == []
    # The queued worker runs inline above. Other workers can still log while
    # this test runs; only warnings from this call belong to its silence check.
    warnings = [(record.name, record.getMessage()) for record in caplog.records
                if record.thread == threading.get_ident() and record.levelno >= logging.WARNING]
    assert warnings == []


def test_model_and_route_changes_schedule_once_and_cloud_stays_opt_in(harness, monkeypatch):
    h, sent = harness, []
    monkeypatch.setattr(ai_transport, "post_local", lambda url, **kw: sent.append(kw["json"]) or Response(""))
    monkeypatch.setattr(ava_readiness, "start_monitor", lambda *a, **kw: None)
    spawn = lambda name, fn: h.workers.append(fn)
    h.app.config["ava"]["warm_on_boot"] = True
    assert ava_readiness.schedule_warm_on_boot(h.app, spawn)
    h.workers.pop()()
    previous = ava_readiness.settings_signature(h.app)
    h.app.config["ollama"]["model"] = "synthetic-second-model"
    ava_readiness.recheck_after_settings(h.app, previous)
    assert len(h.workers) == 1
    h.workers.pop()()
    assert sent[-1]["model"] == "synthetic-second-model"
    previous = ava_readiness.settings_signature(h.app)
    h.app.config["ava"]["provider_policy"] = "off"
    ava_readiness.recheck_after_settings(h.app, previous)
    previous = ava_readiness.settings_signature(h.app)
    h.app.config["ava"]["provider_policy"] = "local"
    ava_readiness.recheck_after_settings(h.app, previous)
    assert len(h.workers) == 1
    h.workers.pop()()
    h.app.config = config("cloud")
    assert not ava_readiness.schedule_warm_on_boot(h.app, spawn)
    h.app.config["ava"]["warm_on_boot"] = True
    assert ava_readiness.schedule_warm_on_boot(h.app, spawn)
    assert len(h.workers) == 1


@pytest.mark.parametrize("display", ["Full", "Compact"])
def test_both_displays_keep_loading_until_resolution(harness, monkeypatch, qapp, tmp_path, display):
    h = harness
    h.app.config["ui"] = {"live_surface": {"enabled": True}}
    if display == "Full":
        from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
        from samsara.ui import qt_runtime
        monkeypatch.setattr(qt_runtime, "post", lambda callback: callback())
        controller = LiveSurfaceController(h.app)
        controller.start()
        indicator, widget = LiveSurfaceIndicatorAdapter(controller), controller.widget
    else:
        from samsara.ui.listening_indicator import ListeningIndicator
        indicator = widget = ListeningIndicator()
        widget.show()
    h.app.listening_indicator = indicator
    try:
        ava_turn_queue.submit(h.app, lambda: None, 0)
        qapp.processEvents()
        if display == "Full":
            widget.refresh(controller.view(), animate=False)
        if display == "Compact":
            assert indicator._resolve_colors()[2] == "Ava"
        assert widget.grab().save(str(tmp_path / f"536_{display}_thinking.png"))
        h.clock.advance(8)
        h.timers[1].fire()
        qapp.processEvents()
        if display == "Full":
            widget.refresh(controller.view(), animate=False)
        assert widget.grab().save(str(tmp_path / f"536_{display}_loading.png"))
        if display == "Full":
            assert controller.view().notice.message == ava_turn_queue.LOADING_LINE
        else:
            assert indicator._thinking and indicator._chip_label == ava_turn_queue.LOADING_LINE
        h.workers.pop()()
        qapp.processEvents()
        if display == "Full":
            assert not controller.view().notice.message
        else:
            assert not indicator._thinking and not indicator._chip_label
    finally:
        if display == "Compact":
            # Retire the native pill and timers before deferred QWidget deletion.
            widget.destroy()
        else:
            widget.hide()
        widget.deleteLater()


@pytest.mark.parametrize("path", ["voice stop", "ava cancel", "session exit",
                                  "ava command session exit", "wake sleep", "AI off"])
def test_stop_paths_clear_now_drop_queue_and_allow_new_turn(harness, monkeypatch, path):
    h, done = harness, Mock()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def post(url, *, json, **kwargs):
        calls.append([m["role"] for m in json["messages"]])
        assert "keep_alive" not in json
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            return Response("Synthetic abandoned answer.")
        return Response("Synthetic new answer.")

    monkeypatch.setattr(ai_transport, "post_local", post)
    ask_ollama.handle_ask_ava(h.app, remainder="Synthetic question one?", on_done=done)
    old = threading.Thread(target=h.workers.pop())
    old.start()
    try:
        assert entered.wait(2)
        ask_ollama.handle_ask_ava(h.app, remainder="Synthetic queued question?", on_done=done)
        if path == "wake sleep":
            execution_policy.bump_generation(h.app, "wake sleep")
        elif path == "AI off":
            previous = ava_readiness.settings_signature(h.app)
            h.app.config["ava"]["provider_policy"] = "off"
            ava_readiness.recheck_after_settings(h.app, previous)
        elif path == "ava command session exit":
            h.app._ava_cmd_generation = 1  # This path bumps under its mode lock first.
            execution_policy.stop_all(h.app, path, chip=False, bump=False)
        else:
            execution_policy.stop_all(h.app, path, chip=False)
        assert h.app.ava_thinking_since is None
        assert h.app.listening_indicator.set_thinking.call_args.args == (False,)
        assert not ava_turn_queue.has_live(h.app)
        assert done.call_count == 1  # Generic cleanup; stateful app completions are retired.
        h.app.config = config()
        h.app.config["ava_memory"] = {"mode": "last"}
        h.app.config["ava"]["catalog_answers"] = False
        ask_ollama.handle_ask_ava(h.app, remainder="Synthetic new question?")
        h.workers.pop()()
        assert h.app.answers == ["Synthetic new answer."]  # Old HTTP remains blocked.
        assert calls == [["system", "user"], ["system", "user"]]
    finally:
        release.set()
        old.join(3)
    assert not old.is_alive()
    assert h.app.answers == ["Synthetic new answer."]
    assert [m["role"] for m in h.app._ava_memory.get_messages()] == ["user", "assistant"]


def test_interpreter_behind_25_second_question_returns_within_own_deadline(harness, monkeypatch):
    import time
    from samsara import ava_command_session
    h = harness
    fake = SlowTransport(h.clock)
    monkeypatch.setattr(ai_transport, "post_local", fake.post)
    monkeypatch.setattr(ava_readiness, "readiness_for", lambda app: SimpleNamespace(ready=True))
    monkeypatch.setattr(ava_command_session, "_match_action2_grammar", lambda text: None)
    monkeypatch.setattr(ava_command_session, "_build_shortlist", lambda *a: ["switch window"])
    ava_command_session.reset_cancellations()
    ask_ollama.handle_ask_ava(h.app, remainder="Synthetic question one?")
    worker = threading.Thread(target=h.workers.pop())
    worker.start()
    try:
        assert fake.entered.wait(2)
        started = time.monotonic()
        # Real lock and clock: exercise the production resolver, not a stub of the wait.
        monkeypatch.setattr(ava_turn_queue, "time", time)
        assert ava_command_session.resolve(h.app, "Synthetic missed instruction", "536", started + 2.5) is None
        assert 2.3 <= time.monotonic() - started < 2.9
        assert len(fake.requests) == 1
    finally:
        fake.release.set()
        worker.join(3)


def test_valid_several_post_web_search_over_30_seconds_is_kept(harness, monkeypatch):
    h = harness
    h.app.config = config("cloud")
    h.app.config["ava_memory"] = {"mode": "last"}
    monkeypatch.setattr(ask_ollama.cloud_llm, "web_search_available", lambda app: True)

    def search(*args, **kwargs):
        for _ in range(3):
            h.clock.advance(20)  # Three valid posts, each within its 30s timeout.
        return SimpleNamespace(ok=True, text="Synthetic searched answer.", searched=True,
                               search_requests=3, sources=(), queries=())

    monkeypatch.setattr(ask_ollama.cloud_llm, "send_web_search", search)
    reply = ask_ollama.ask_model("Synthetic search question?", h.app, allow_search=True)
    assert reply.ok and reply.text == "Synthetic searched answer."
    assert h.app._ava_memory.get_messages()[-1]["role"] == "assistant"
    assert h.clock.now == 160


@pytest.mark.parametrize("failure", ["timer", "spawn"])
def test_failed_scheduling_clears_sign_and_next_submit_runs(harness, monkeypatch, failure):
    h = harness
    target = getattr(ava_turn_queue.thread_registry, failure)
    monkeypatch.setattr(ava_turn_queue.thread_registry, failure, Mock(side_effect=RuntimeError("synthetic")))
    with pytest.raises(RuntimeError):
        ava_turn_queue.submit(h.app, lambda: None, 0)
    assert not ava_turn_queue.has_live(h.app)
    assert h.app.ava_thinking_since is None
    monkeypatch.setattr(ava_turn_queue.thread_registry, failure, target)
    ava_turn_queue.submit(h.app, lambda: h.app.answers.append("next"), 0)
    h.workers.pop()()
    assert h.app.answers == ["next"]


def test_instant_answer_has_no_cue_and_level_is_read_at_play_time(harness):
    h = harness
    ava_turn_queue.submit(h.app, lambda: None, 0)
    h.workers.pop()()
    h.clock.advance(1)
    h.timers[0].fire()
    assert not h.app.play_sound.called
    ava_turn_queue.submit(h.app, lambda: None, 0)
    h.app.config["tts"]["feedback_level"] = "silent"
    h.clock.advance(1)
    h.timers[2].fire()
    assert not h.app.play_sound.called
    h.workers.pop()()


def test_compact_wait_updates_label_when_another_producer_takes_over(qapp):
    from samsara.ui.listening_indicator import ListeningIndicator
    indicator = ListeningIndicator()
    try:
        indicator.set_thinking(True)
        assert indicator._resolve_colors()[2] == "Vision"
        indicator.set_thinking(True, label="Ava")
        assert indicator._resolve_colors()[2] == "Ava"
        indicator.set_thinking(False)
        indicator.set_thinking(True, label="Ava")
        assert indicator._resolve_colors()[2] == "Ava"
    finally:
        indicator.destroy()
        indicator.deleteLater()


@pytest.fixture
def app_dispatch(harness):
    # The maintained adapter supplies the unchanged production class without
    # executing dictation's bootstrap. Never import the application here.
    adapted = sys.modules.get("dictation")
    assert adapted is not None, "run with the maintained no-bootstrap gate adapter"
    h = harness
    for name in ("_ava_session_agent_dispatch_fn", "_start_ava_session_worker",
                 "_on_ava_session_request_done", "_try_stop_utterance"):
        setattr(h.app, name, MethodType(getattr(adapted.DictationApp, name), h.app))
    h.app._ava_session_dispatch_lock = threading.Lock()
    h.app._ava_session_dispatch_queue = deque(maxlen=3)
    h.app._ava_session_request_in_flight = False
    h.app._try_cancel_pending_ava_utterance = lambda text: False
    h.app._try_ava_edit = lambda text: False
    h.app._touch_session_activity = Mock()
    h.app._show_outcome_chip = Mock()
    return h


@pytest.mark.parametrize("cancel", ["voice stop", "generation", "no bump"])
@pytest.mark.parametrize("queued", [False, True])
def test_real_dispatch_stop_replacement_owns_state_after_old_http_returns(
        app_dispatch, monkeypatch, cancel, queued):
    h = app_dispatch
    old_entered, old_release = threading.Event(), threading.Event()
    new_entered, new_release = threading.Event(), threading.Event()
    requests = []

    def post(url, *, json, **kwargs):
        requests.append([m["role"] for m in json["messages"]])
        if len(requests) == 1:
            old_entered.set()
            assert old_release.wait(5)
            return Response("Synthetic abandoned answer.")
        new_entered.set()
        assert new_release.wait(5)
        return Response("Synthetic replacement answer.")

    monkeypatch.setattr(ai_transport, "post_local", post)
    dispatch = h.app._ava_session_agent_dispatch_fn
    dispatch("Synthetic question A?", None)
    old = threading.Thread(target=h.workers.pop())
    old.start()
    new = None
    try:
        assert old_entered.wait(2)
        if queued:
            dispatch("Synthetic queued question B?", None)
            assert len(h.app._ava_session_dispatch_queue) == 1
        if cancel == "voice stop":
            dispatch("stop", None)
        elif cancel == "generation":
            execution_policy.bump_generation(h.app, "synthetic session exit")
        else:
            before = execution_policy.current_generation(h.app)
            cleared = execution_policy.stop_all(h.app, chip=False, bump=False)
            assert cleared["in_flight"] and cleared["queued"] == int(queued)
            assert execution_policy.current_generation(h.app) == before
        assert not h.app._ava_session_request_in_flight
        assert not h.app._ava_session_dispatch_queue
        assert h.app.ava_thinking_since is None
        h.app._show_outcome_chip.reset_mock()
        dispatch("Synthetic replacement question C?", None)
        new = threading.Thread(target=h.workers.pop())
        new.start()
        assert new_entered.wait(2)  # C starts while A is still inside transport.
        owner = h.app._ava_dispatch_owner
        h.app._ava_turn_outcome = ("synthetic pending replacement", "pending")
        old_release.set()
        old.join(2)
        assert not old.is_alive()
        assert h.app._ava_dispatch_owner is owner
        assert h.app._ava_session_request_in_flight
        assert h.app._ava_turn_outcome == ("synthetic pending replacement", "pending")
        assert not h.app._touch_session_activity.called
        assert not h.app._show_outcome_chip.called
        assert h.app.ava_thinking_since is not None
        assert h.app.answers == []
        new_release.set()
        new.join(2)
        assert not new.is_alive()
        assert h.app.answers == ["Synthetic replacement answer."]
        assert h.app._touch_session_activity.call_count == 1
        assert h.app._show_outcome_chip.call_count == 1
        assert not h.app._ava_session_request_in_flight
        assert h.app._ava_turn_outcome is None
        owner()  # An accidental duplicate completion cannot emit another receipt.
        assert h.app._touch_session_activity.call_count == 1
        assert requests == [["system", "user"], ["system", "user"]]
    finally:
        old_release.set()
        new_release.set()
        old.join(2)
        if new is not None:
            new.join(2)


def test_real_dispatch_ordinary_questions_complete_once_in_order(app_dispatch, monkeypatch):
    h = app_dispatch
    fake = SlowTransport(h.clock)
    monkeypatch.setattr(ai_transport, "post_local", fake.post)
    h.app._ava_session_agent_dispatch_fn("Synthetic question one?", None)
    worker = threading.Thread(target=h.workers.pop())
    worker.start()
    try:
        assert fake.entered.wait(2)
        h.app._ava_session_agent_dispatch_fn("Synthetic question two?", None)
        assert len(h.app._ava_session_dispatch_queue) == 1
    finally:
        fake.release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert h.app.answers == ["Synthetic answer one.", "Synthetic answer two."]
    assert h.app._touch_session_activity.call_count == 2
    assert h.app._show_outcome_chip.call_count == 2
    assert not h.app._ava_session_request_in_flight
    assert not h.app._ava_session_dispatch_queue
    assert [row["unanswered"] for row in fake.requests] == [1, 1]


@pytest.mark.parametrize("exit", ["disabled", "persona", "memory", "image"])
def test_real_dispatch_current_generation_early_exit_completes_once(
        app_dispatch, monkeypatch, exit):
    h = app_dispatch
    if exit == "disabled":
        h.app.config["ava"]["provider_policy"] = "off"
    elif exit == "persona":
        monkeypatch.setattr(ask_ollama, "_handle_persona_intent", lambda *a: True)
    elif exit == "memory":
        monkeypatch.setattr(ask_ollama, "_handle_personal_memory_intent", lambda *a: True)
    else:
        from plugins.commands import comfy
        from samsara.command_registry import DispatchState
        monkeypatch.setattr(comfy, "try_voice_command", lambda *a: SimpleNamespace(state=DispatchState.COMPLETED))
    h.app._ava_session_agent_dispatch_fn("Synthetic local question?", None)
    assert h.workers == []
    assert h.app._touch_session_activity.call_count == 1
    assert h.app._show_outcome_chip.call_count == 1
    assert not h.app._ava_session_request_in_flight
    h.app._ava_dispatch_owner()
    assert h.app._touch_session_activity.call_count == 1


@pytest.mark.parametrize("failure", ["timer", "spawn"])
def test_real_dispatch_scheduling_failure_releases_current_request(
        app_dispatch, monkeypatch, failure):
    h = app_dispatch
    original = getattr(ava_turn_queue.thread_registry, failure)
    monkeypatch.setattr(ava_turn_queue.thread_registry, failure,
                        Mock(side_effect=RuntimeError("synthetic scheduling failure")))
    with pytest.raises(RuntimeError, match="synthetic scheduling failure"):
        h.app._ava_session_agent_dispatch_fn("Synthetic failed question?", None)
    assert not h.app._ava_session_request_in_flight
    assert not ava_turn_queue.has_live(h.app)
    assert h.app.ava_thinking_since is None
    assert h.app._touch_session_activity.call_count == 1
    monkeypatch.setattr(ava_turn_queue.thread_registry, failure, original)
    monkeypatch.setattr(ai_transport, "post_local", lambda *a, **kw: Response("Synthetic next answer."))
    h.app._ava_session_agent_dispatch_fn("Synthetic next question?", None)
    h.workers.pop()()
    assert h.app.answers == ["Synthetic next answer."]
    assert h.app._touch_session_activity.call_count == 2
    assert not h.app._ava_session_request_in_flight


def test_real_dispatch_cancel_during_front_door_never_rearms_wait_or_sends(app_dispatch, monkeypatch):
    h = app_dispatch
    entered, release = threading.Event(), threading.Event()

    def local_check(*args):
        entered.set()
        assert release.wait(3)
        return False

    monkeypatch.setattr(ask_ollama, "_handle_persona_intent", local_check)
    post = Mock(return_value=Response("Synthetic replacement answer."))
    monkeypatch.setattr(ai_transport, "post_local", post)
    old = threading.Thread(target=lambda: h.app._ava_session_agent_dispatch_fn("Synthetic old question?", None))
    old.start()
    try:
        assert entered.wait(2)
        execution_policy.stop_all(h.app, chip=False, bump=False)
        monkeypatch.setattr(ask_ollama, "_handle_persona_intent", lambda *a: False)
        h.app._ava_session_agent_dispatch_fn("Synthetic new question?", None)
        h.workers.pop()()
        assert not h.app._ava_session_request_in_flight
        assert h.app._touch_session_activity.call_count == 1
        h.app.listening_indicator.reset_mock()
    finally:
        release.set()
        old.join(2)
    assert not old.is_alive()
    assert not h.workers  # The retired front door cannot submit a new job.
    assert post.call_count == 1
    assert not h.app.listening_indicator.set_thinking.called
    assert h.app._touch_session_activity.call_count == 1
    assert h.app._show_outcome_chip.call_count == 1
    assert h.app._ava_turn_outcome is None
    assert h.app.answers == ["Synthetic replacement answer."]


def test_real_dispatch_partial_spawn_failure_does_not_block_replacement(app_dispatch, monkeypatch):
    h = app_dispatch
    entered, release = threading.Event(), threading.Event()
    original_spawn = ava_turn_queue.thread_registry.spawn
    workers, requests = [], []

    def post(*args, **kwargs):
        requests.append(1)
        if len(requests) == 1:
            entered.set()
            assert release.wait(5)
            return Response("Synthetic abandoned startup answer.")
        return Response("Synthetic replacement answer.")

    def partial_spawn(name, fn, **kwargs):
        thread = threading.Thread(target=fn)
        workers.append(thread)
        thread.start()
        assert entered.wait(2)
        raise RuntimeError("synthetic partial startup failure")

    monkeypatch.setattr(ai_transport, "post_local", post)
    monkeypatch.setattr(ava_turn_queue.thread_registry, "spawn", partial_spawn)
    try:
        with pytest.raises(RuntimeError, match="synthetic partial startup failure"):
            h.app._ava_session_agent_dispatch_fn("Synthetic failed startup question?", None)
        assert not h.app._ava_session_request_in_flight
        assert h.app.ava_thinking_since is None
        assert h.app._touch_session_activity.call_count == 1
        monkeypatch.setattr(ava_turn_queue.thread_registry, "spawn", original_spawn)
        h.app._ava_session_agent_dispatch_fn("Synthetic replacement question?", None)
        h.workers.pop()()
        assert h.app.answers == ["Synthetic replacement answer."]
        assert h.app._touch_session_activity.call_count == 2
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
    assert all(not worker.is_alive() for worker in workers)
    assert h.app.answers == ["Synthetic replacement answer."]
    assert h.app._touch_session_activity.call_count == 2
    assert h.app._show_outcome_chip.call_count == 2
    assert not h.app._ava_session_request_in_flight
    assert [m["role"] for m in h.app._ava_memory.get_messages()] == ["user", "assistant"]


@pytest.mark.parametrize("failure", [False, True])
def test_cancelled_searched_cloud_turn_does_not_poison_readiness(app_dispatch, monkeypatch, caplog, failure):
    h = app_dispatch
    h.app.config = config("cloud")
    h.app.config["ava"]["catalog_answers"] = False
    tracker = ava_readiness.ReadinessTracker()
    tracker.record_turn("deepseek", None)
    before = tracker.snapshot()
    monkeypatch.setattr(ava_readiness, "tracker", tracker)
    monkeypatch.setattr(ask_ollama.cloud_llm, "web_search_available", lambda app: True)
    def search(*args, **kwargs):
        execution_policy.stop_all(h.app, chip=False)
        return SimpleNamespace(ok=not failure, text="Synthetic searched answer.", searched=True,
                               failure_kind=ava_readiness.PROVIDER_ERROR,
                               search_requests=1, sources=(), queries=())
    monkeypatch.setattr(ask_ollama.cloud_llm, "send_web_search", search)
    h.app._ava_session_agent_dispatch_fn("Synthetic searched question?", None)
    h.workers.pop()()
    assert tracker.snapshot() == before
    assert not h.app.answers
    assert not any("[AVA-CLOUD]" in r.message and r.levelname == "WARNING" for r in caplog.records)
    monkeypatch.setattr(ask_ollama.cloud_llm, "web_search_available", lambda app: False)
    monkeypatch.setattr(ask_ollama.cloud_llm, "send", lambda *a, **kw: "Synthetic next answer.")
    h.app._ava_session_agent_dispatch_fn("Synthetic next question?", None)
    h.workers.pop()()
    assert h.app.answers == ["Synthetic next answer."]
    assert tracker.snapshot().ready


def test_real_dispatch_bump_between_capture_and_inflight_releases_stale_entry(app_dispatch, monkeypatch):
    h = app_dispatch
    original = execution_policy.current_generation
    armed = True
    def capture(app):
        nonlocal armed
        generation = original(app)
        if armed:
            armed = False
            execution_policy.bump_generation(app, "synthetic capture interleaving")
        return generation
    monkeypatch.setattr(execution_policy, "current_generation", capture)
    h.app._ava_session_agent_dispatch_fn("Synthetic stale question?", None)
    assert not h.app._ava_session_request_in_flight
    assert not h.workers
    assert not h.app._touch_session_activity.called
    assert not h.app._show_outcome_chip.called
    monkeypatch.setattr(ai_transport, "post_local", lambda *a, **kw: Response("Synthetic next answer."))
    h.app._ava_session_agent_dispatch_fn("Synthetic next question?", None)
    h.workers.pop()()
    assert h.app.answers == ["Synthetic next answer."]
    assert not h.app._ava_session_request_in_flight


def test_stale_entry_and_submit_release_generic_waiter_but_preserve_new_owner(app_dispatch):
    h = app_dispatch
    execution_policy.bump_generation(h.app)
    done = Mock()
    ask_ollama.handle_ask_ava(h.app, remainder="Synthetic stale?", on_done=done, generation=0)
    done.assert_called_once()
    skipped = Mock()
    ava_turn_queue.submit(h.app, lambda: None, 0, skipped)
    skipped.assert_called_once()
    h.app._ava_session_agent_dispatch_fn("Synthetic new question?", None)
    owner = h.app._ava_dispatch_owner
    ask_ollama.handle_ask_ava(h.app, remainder="Synthetic stale?",
                            on_done=h.app._on_ava_session_request_done, generation=0)
    assert h.app._ava_dispatch_owner is owner and h.app._ava_session_request_in_flight


@pytest.mark.parametrize("display", ["Compact", "Full"])
@pytest.mark.parametrize("agent", ["generic", "Hermes"])
def test_agent_wait_survives_ava_turn_completion_and_generation_bump(harness, monkeypatch, qapp, display, agent):
    h = harness
    if display == "Full":
        from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
        from samsara.ui import qt_runtime
        monkeypatch.setattr(qt_runtime, "post", lambda fn: fn())
        controller = LiveSurfaceController(h.app)
        indicator = LiveSurfaceIndicatorAdapter(controller)
    else:
        from samsara.ui.listening_indicator import ListeningIndicator
        indicator = ListeningIndicator()
    h.app.listening_indicator = indicator
    try:
        if agent == "Hermes":
            indicator.set_agent_waiting("Hermes", True)
        else:
            from plugins.commands.ask_agent import _thinking
            _thinking(h.app, True)  # The actual GPT/Claude waiting seam.
        before = (indicator._resolve_colors()[2] if display == "Compact" else controller.view().notice.message)
        ava_turn_queue.submit(h.app, lambda: None, 0)
        h.clock.advance(8)
        h.timers[1].fire()
        h.workers.pop()()
        execution_policy.bump_generation(h.app)
        after = (indicator._resolve_colors()[2] if display == "Compact" else controller.view().notice.message)
        assert before == after
        assert before == ("Vision" if display == "Compact" else "Ava thinking") if agent == "generic" else "Hermes" in before
    finally:
        if display == "Compact":
            indicator.destroy()
            indicator.deleteLater()


@pytest.mark.parametrize("choice, expected", [("absent", False), ("flat-off", False), ("flat-on", True), ("stored-off", False)])
def test_warm_default_agrees_and_explicit_load_reports_readiness(harness, monkeypatch, choice, expected):
    h = harness
    h.app.config["ava"].pop("warm_on_boot", None)
    if choice.startswith("flat"):
        h.app.config["ava.warm_on_boot"] = choice == "flat-on"
    elif choice == "stored-off":
        h.app.config["ava"]["warm_on_boot"] = False
    tracker = ava_readiness.ReadinessTracker()
    monkeypatch.setattr(ava_readiness, "tracker", tracker)
    sent = []
    def post(url, **kwargs):
        assert tracker.snapshot().warming
        sent.append(kwargs["json"])
        return Response("")
    monkeypatch.setattr(ai_transport, "post_local", post)
    assert ava_readiness.warm_on_boot_enabled(h.app) == expected
    assert ava_local_warmup.schedule(h.app, lambda name, fn: h.workers.append(fn)) == expected
    h.workers.clear()
    result = ava_readiness.warm_configured_provider(h.app)
    assert result.ready and result.source == "warmup" and result.provider == "ollama"
    assert sent == [{"model": "synthetic-model", "stream": False}]


def test_explicit_keep_warm_loads_when_startup_warm_is_off(harness, monkeypatch):
    from samsara import ava_command_session
    h = harness
    h.app.config["ava_command_session"]["keep_warm"] = True
    sent = []
    monkeypatch.setattr(ai_transport, "post_local", lambda *a, **kw: sent.append(kw["json"]) or Response(""))
    ava_command_session.warm_up(h.app)
    assert len(h.workers) == 1
    h.workers.pop()()
    assert sent == [{"model": "synthetic-model", "stream": False}]


@pytest.mark.parametrize("route", ["local", "cloud"])
def test_successful_slow_reads_keep_per_request_timeout_semantics(harness, monkeypatch, route):
    h = harness
    h.app.config = config(route)
    h.app.config["ava"]["catalog_answers"] = False
    def slow(*a, **kw):
        h.clock.advance(35)
        return Response("Synthetic complete answer.")
    monkeypatch.setattr(ai_transport, "post_local", slow)
    monkeypatch.setattr(ask_ollama.cloud_llm, "send", lambda *a, **kw: slow().text)
    ask_ollama.handle_ask_ava(h.app, remainder="Synthetic question?")
    h.workers.pop()()
    assert h.app.answers == ["Synthetic complete answer."]


def test_cue_and_memory_save_run_outside_lane_lock(harness, monkeypatch):
    h = harness
    lane = ava_turn_queue._lane(h.app)
    def cue(*args):
        assert not lane.lock._is_owned()
    def save(self):
        assert not lane.lock._is_owned()
    h.app.play_sound = cue
    monkeypatch.setattr(AvaMemory, "save", save)
    monkeypatch.setattr(ai_transport, "post_local", lambda *a, **kw: Response("Synthetic answer."))
    ask_ollama.handle_ask_ava(h.app, remainder="Synthetic question?")
    h.timers[0].fire()
    h.workers.pop()()
    assert h.app.answers == ["Synthetic answer."]


@pytest.mark.parametrize("refusal", [False, True])
def test_local_load_failure_is_quiet_except_refused_redirect(harness, monkeypatch, refusal):
    import requests
    h = harness
    tracker = ava_readiness.ReadinessTracker()
    monkeypatch.setattr(ava_readiness, "tracker", tracker)
    h.app._show_outcome_chip = Mock()
    spoken = Mock()
    monkeypatch.setattr(ask_ollama, "speak", spoken)
    def request(*args, **kwargs):
        assert kwargs["json"] == {"model": "synthetic-model", "stream": False}
        if refusal:
            raise ai_transport.AITransportRefused("local AI service tried to redirect - refused")
        raise requests.exceptions.ConnectionError()
    monkeypatch.setattr(ai_transport, "_request", request)
    assert not ava_local_warmup.load(h.app)
    assert tracker.snapshot().offline and tracker.snapshot().failure_kind == ava_readiness.UNREACHABLE
    if refusal:
        h.app._show_outcome_chip.assert_called_once_with("local AI service tried to redirect - refused", "error")
        spoken.assert_called_once_with(h.app, "local AI service tried to redirect - refused", category="confirmation")
    else:
        h.app._show_outcome_chip.assert_not_called()
        spoken.assert_not_called()


def test_rejected_ui_schedule_does_not_prevent_lane_cancellation(harness):
    h = harness
    done = Mock()
    ava_turn_queue.submit(h.app, lambda: h.app.answers.append("Synthetic abandoned answer."), 0, done)
    old_lane = ava_turn_queue._lane(h.app)
    def rejected(fn):
        raise RuntimeError("synthetic UI dispatch rejection")
    h.app._schedule_ui = rejected
    execution_policy.stop_all(h.app, chip=False)
    assert not ava_turn_queue.has_live(h.app)
    assert h.app.ava_thinking_since is None
    assert ava_turn_queue._lane(h.app) is not old_lane
    assert not h.app._ava_session_request_in_flight
    h.workers.pop()()
    assert h.app.answers == []
    done.assert_called_once()


@pytest.mark.parametrize("display", ["Compact", "Full"])
@pytest.mark.parametrize("agent", ["generic", "GPT", "Claude"])
@pytest.mark.parametrize("already_loading", [False, True])
def test_agent_takes_over_ava_wait_and_keeps_it_after_ava_finishes(
        harness, monkeypatch, qapp, display, agent, already_loading):
    h = harness
    if display == "Full":
        from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
        from samsara.ui import qt_runtime
        monkeypatch.setattr(qt_runtime, "post", lambda fn: fn())
        controller = LiveSurfaceController(h.app)
        indicator = LiveSurfaceIndicatorAdapter(controller)
    else:
        from samsara.ui.listening_indicator import ListeningIndicator
        indicator = ListeningIndicator()
    h.app.listening_indicator = indicator
    try:
        ava_turn_queue.submit(h.app, lambda: None, 0)
        assert indicator._ava_wait_owner is ava_turn_queue._lane(h.app)
        if already_loading:
            h.clock.advance(8)
            h.timers[1].fire()
        if agent == "generic":
            from plugins.commands.ask_agent import _thinking
            _thinking(h.app, True)  # The actual GPT/Claude waiting call.
        else:
            indicator.set_thinking(True, label=agent)
        expected = ("Vision" if display == "Compact" else "Ava") if agent == "generic" else agent
        def assert_agent_wait():
            assert indicator._thinking
            assert indicator._ava_wait_owner is None
            if display == "Compact":
                assert indicator._resolve_colors()[2] == expected
            else:
                assert indicator._thinking_owner == "thinking"
                assert controller.view().notice.owner == "thinking"
                assert controller.view().notice.message == expected + " thinking"
        assert_agent_wait()
        # The loading timer must not reclaim an agent-owned sign either.
        h.clock.advance(8)
        h.timers[1].fire()
        assert_agent_wait()
        h.workers.pop()()
        assert_agent_wait()
        execution_policy.bump_generation(h.app)
        assert_agent_wait()
        if agent == "generic":
            _thinking(h.app, False)
        else:
            indicator.set_thinking(False, label=agent)
        assert not indicator._thinking
        if display == "Full":
            assert controller.view().notice.message == ""
    finally:
        if display == "Compact":
            indicator.destroy()
            indicator.deleteLater()


@pytest.mark.parametrize("failure", ["timeout", "connection", "empty"])
def test_stopped_local_failure_leaves_readiness_unchanged_and_next_question_answers(
        app_dispatch, monkeypatch, failure):
    import requests
    h = app_dispatch
    tracker = ava_readiness.ReadinessTracker()
    tracker.record_turn("ollama", None)
    before = tracker.snapshot()
    monkeypatch.setattr(ava_readiness, "tracker", tracker)
    def abandoned(*args, **kwargs):
        execution_policy.stop_all(h.app, chip=False)
        h.clock.advance(25)
        if failure == "timeout":
            raise requests.exceptions.Timeout()
        if failure == "connection":
            raise requests.exceptions.ConnectionError()
        return Response("")
    monkeypatch.setattr(ai_transport, "post_local", abandoned)
    h.app._ava_session_agent_dispatch_fn("Synthetic abandoned local question?", None)
    h.workers.pop()()
    assert tracker.snapshot() == before
    assert not h.app.answers
    assert not h.app._ava_session_request_in_flight
    monkeypatch.setattr(ai_transport, "post_local", lambda *a, **kw: Response("Synthetic next local answer."))
    h.app._ava_session_agent_dispatch_fn("Synthetic next local question?", None)
    h.workers.pop()()
    assert h.app.answers == ["Synthetic next local answer."]
    assert tracker.snapshot().ready
    assert not h.app._ava_session_request_in_flight


@pytest.mark.parametrize("display", ["Compact", "Full"])
def test_next_ava_turn_shows_its_sign_after_an_agent_took_it_over(harness, monkeypatch, qapp, display):
    # Read 1013c: Ava turn -> GPT takes the sign -> Ava done -> GPT done -> the NEXT Ava turn on the same lane must show 'Ava' again.
    h = harness
    if display == "Full":
        from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
        from samsara.ui import qt_runtime
        monkeypatch.setattr(qt_runtime, "post", lambda fn: fn())
        controller = LiveSurfaceController(h.app)
        indicator = LiveSurfaceIndicatorAdapter(controller)
    else:
        from samsara.ui.listening_indicator import ListeningIndicator
        indicator = ListeningIndicator()
    h.app.listening_indicator = indicator
    try:
        ava_turn_queue.submit(h.app, lambda: None, 0)
        lane = ava_turn_queue._lane(h.app)
        assert indicator._ava_wait_owner is lane
        indicator.set_thinking(True, label="GPT")
        h.workers.pop()()
        assert indicator._thinking
        indicator.set_thinking(False, label="GPT")
        assert not indicator._thinking
        ava_turn_queue.submit(h.app, lambda: None, 0)
        assert ava_turn_queue._lane(h.app) is lane
        assert indicator._thinking
        assert indicator._ava_wait_owner is lane
        if display == "Compact":
            assert indicator._resolve_colors()[2] == "Ava"
        h.workers.pop()()
        assert not indicator._thinking
    finally:
        if display == "Compact":
            indicator.destroy()
            indicator.deleteLater()
