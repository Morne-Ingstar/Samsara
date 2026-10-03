"""356: dispatch-shaped speech stays local and its outcomes enter Ava history."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama, dispatch_voice
from samsara import execution_policy, plugin_commands
from samsara.agent_channel.dispatch_voice import DispatchDraft
from samsara.ava_memory import AvaMemory
from samsara.paths import samsara_home_dir


@pytest.mark.parametrize("heard,canonical", [
    ("sol", "sol"), ("soul", "sol"), ("sole", "sol"),
    ("Seoul", "sol"), ("the soul", "sol"), ("the sun", "sol"),
    ("S-O-L", "sol"), ("s o l", "sol"), ("Sol.", "sol"),
    ("solomon", "sol"), ("luna", "luna"), ("lunar", "luna"),
    ("Luna.", "luna"), ("loona", "luna"), ("astra", "astra"),
    ("astro", "astra"), ("aster", "astra"), ("astrid", "astra"),
    ("claude code", "claude code"), ("cloud code", "claude code"),
    ("Claude.", "claude code"),
])
def test_identity_aliases_are_canonical(heard, canonical):
    assert dispatch_voice.parse_send(f"send 353b to {heard}") == ("353b", canonical)


@pytest.mark.parametrize("heard", [
    "send 353b to soul", "send prompt number 353B to Seoul",
    "send number 353-b to S-O-L", "dispatch three five three b to the sun",
    "send 353 bee to the soul", "send 353b the soul",
])
def test_prompt_shapes_and_live_spellings(heard):
    assert dispatch_voice.parse_send(heard) == ("353b", "sol")
    assert dispatch_voice.match_send(heard)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    monkeypatch.setattr(dispatch_voice.thread_registry, "timer", lambda *a, **kw: None)
    plugin_commands._REGISTRY["send prompt"] = (
        plugin_commands._MODULE_ENTRIES[dispatch_voice.__name__]["send prompt"])
    ask_ollama.clear_pending_action()
    spoken, drafted, approved = [], [], []
    events = []

    def speak(text, **kw):
        spoken.append(text)
        if kw.get("on_done"):
            kw["on_done"]()
        return SimpleNamespace(utterance_id="played")

    class Client:
        def draft(self, pid, identity):
            drafted.append((pid, identity))
            return DispatchDraft(f"Plan for {pid} with {identity}", "task-1", "a" * 64, pid)

        def approve(self, task, plan_hash):
            approved.append((task, plan_hash))
            return {"spoken": "353b approved for Sol.", "state": "queued"}

        def events(self, _since=None):
            return list(events)

    class ReadyQueue:
        paths = SimpleNamespace(queue_root=Path("fake-queue"))
        note = SimpleNamespace(label=lambda path: path.name.split("_", 1)[0])

        def _prompt_files(self, _folder):
            return [Path(f"{pid}_test.md") for pid in ("353a", "353b")]

    instance = SimpleNamespace(config={"dispatch_voice": {"enabled": True},
                                       "ava_memory": {"mode": "clear"}},
                               _ava_memory=AvaMemory(),
                               _dispatch_voice_client=Client(),
                               _queue_voice_view=ReadyQueue(),
                               audio_coordinator=SimpleNamespace(speak=speak),
                               _show_outcome_chip=Mock())
    yield SimpleNamespace(app=instance, spoken=spoken, drafted=drafted,
                          approved=approved, events=events)
    ask_ollama.clear_pending_action()


@pytest.mark.parametrize("heard", [
    "Send 353b the soul.", "Send prompt number 353B to soul.",
    "Send 353B to Seoul.",
])
def test_live_sends_take_dispatch_lane_never_model(app, heard):
    model = Mock()
    if not dispatch_voice.route_ava_utterance(app.app, heard):
        model(heard)
    assert app.drafted == [("353b", "Sol")]
    model.assert_not_called()


def test_cut_off_send_asks_again_without_model(app):
    model = Mock()
    if not dispatch_voice.route_ava_utterance(app.app, "Send three-"):
        model("Send three-")
    assert app.spoken[-1] == "Say that again, I lost the end."
    assert not app.drafted
    model.assert_not_called()


def test_direct_ava_entry_uses_local_dispatch_before_model(app, monkeypatch):
    monkeypatch.setattr(ask_ollama, "_ai_state",
                        lambda _app: (_ for _ in ()).throw(AssertionError("model reached")))
    done = Mock()
    ask_ollama.handle_ask_ava(app.app, "Send prompt number 353b to Seoul", on_done=done)
    assert app.drafted == [("353b", "Sol")]
    done.assert_called_once()


def test_near_miss_one_turn_follow_up_and_garbled_dispatch(app):
    assert dispatch_voice.route_ava_utterance(app.app, "Send 353b to person")
    assert app.spoken[-1] == "To Luna, Sol, Astra or Claude Code?"
    assert dispatch_voice.route_ava_utterance(app.app, "the sun, S-O-L")
    assert app.drafted == [("353b", "Sol")]
    assert dispatch_voice.route_ava_utterance(app.app, "count number 353a")
    assert app.spoken[-1] == "To Luna, Sol, Astra or Claude Code?"


def test_identity_only_correction_never_retargets_pending_plan(app):
    dispatch_voice.route_ava_utterance(app.app, "send 353b to Luna")
    assert dispatch_voice.route_ava_utterance(app.app, "Sol")
    assert app.drafted == [("353b", "Luna")]
    assert "pending plan is for Luna" in app.spoken[-1]


def test_missing_id_follow_up_accepts_only_number(app):
    assert dispatch_voice.route_ava_utterance(app.app, "Send to Luna")
    assert app.spoken[-1] == "Which prompt: 353a or 353b?"
    assert dispatch_voice.route_ava_utterance(app.app, "three five three b")
    assert app.drafted == [("353b", "Luna")]


def test_rejected_plan_and_needs_me_event_are_assistant_history(app):
    dispatch_voice.route_ava_utterance(app.app, "send 353b to Sol")
    assert execution_policy.answer_pending(app.app, "no") == "rejected"
    app.events.append({"task_id": "task-1", "state": "blocked",
                       "spoken": "353b needs you", "at": "2026-10-03T00:35:00Z"})
    dispatch_voice.poll_once(app.app, app.app._dispatch_voice_client,
                             dispatch_voice._feed_state(app.app))
    lines = [row["content"] for row in app.app._ava_memory.get_messages()
             if row["role"] == "assistant"]
    assert any("cancelled" in line for line in lines)
    assert any("blocked" in line for line in lines)


def test_unrecognized_feed_state_cannot_claim_an_approval(app):
    app.events.append({"task_id": "unknown", "prompt_id": "999", "state": "approve",
                       "spoken": "approve", "at": "2026-10-03T00:35:00Z"})
    dispatch_voice.poll_once(app.app, app.app._dispatch_voice_client,
                             dispatch_voice._feed_state(app.app))
    assert not any("999" in row["content"] for row in app.app._ava_memory.get_messages())


def test_dispatch_history_survives_mode_switch_and_reaches_model_context(app, monkeypatch):
    dispatch_voice.route_ava_utterance(app.app, "send 353b to Sol")
    assert execution_policy.answer_pending(app.app, "approve") == "approved"
    rows = [json.loads(line) for line in
            (samsara_home_dir() / "logs" / "ava_conversation.log")
            .read_text(encoding="utf-8").splitlines()]
    assert any(row["action"] == "approved" and row["user"] == "approve" for row in rows)
    app.events.extend([
        {"task_id": "task-1", "prompt_id": "353b", "state": "running",
         "spoken": "353b started with Sol", "at": "2026-10-03T00:33:00Z"},
        {"task_id": "task-1", "prompt_id": "353b", "state": "reported",
         "spoken": "353b finished, STATUS DONE", "at": "2026-10-03T00:34:00Z"},
    ])
    dispatch_voice.poll_once(app.app, app.app._dispatch_voice_client,
                             dispatch_voice._feed_state(app.app))
    app.app._ava_memory.take_session()  # 342's assistant mode switch behavior
    context = app.app._ava_memory.get_messages()
    assert any(row["role"] == "assistant" and "[action]" in row["content"]
               and "STATUS DONE" in row["content"] for row in context)

    captured = []
    provider = SimpleNamespace(identity="deepseek")
    monkeypatch.setattr(ask_ollama, "_ai_state", lambda _app: SimpleNamespace(allowed=True, provider=provider))
    monkeypatch.setattr(ask_ollama, "get_system_prompt", lambda _app: "You are Ava. {COMMAND_LIST}")
    monkeypatch.setattr(ask_ollama, "_assistant_recall_allowed", lambda *_: False)
    monkeypatch.setattr(ask_ollama, "_disabled_since_request", lambda _app: False)
    monkeypatch.setattr(ask_ollama, "_remember_alias_turn", lambda *_: None)
    monkeypatch.setattr(ask_ollama.cloud_llm, "send",
                        lambda system, prompt, _app, messages: captured.extend(messages)
                        or "I dispatched 353b to Sol and it finished DONE.")
    monkeypatch.setattr(ask_ollama.ava_readiness.tracker, "record_turn", lambda *_: None)
    app.app.command_executor = SimpleNamespace(ava_menu=lambda *_a, **_k: ["open chrome"])
    reply = ask_ollama.ask_model("what did you just do", app.app)
    assert reply.ok and "finished DONE" in reply.text
    assert any(row["role"] == "assistant" and "[action]" in row["content"]
               for row in captured)


def test_model_cannot_turn_garbled_dispatch_into_other_action(app, monkeypatch):
    monkeypatch.setattr(ask_ollama, "speak", lambda _app, text, **kw: app.spoken.append(text))
    app.app.command_executor = Mock()
    result = ask_ollama.handle_response(app.app,
                                        "CONFIRM Show numbers.\nACTION show numbers",
                                        original_text="count number 353a")
    assert result.reason == "dispatch_unresolved"
    app.app.command_executor.execute_canonical.assert_not_called()
