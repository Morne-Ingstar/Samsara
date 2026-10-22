"""Recorded desktop structure with synthetic prose; no live UI or profiles."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.agent_channel import agent_reply, chatgpt_reply as reply, claude_reply
from samsara.agent_channel import spoken_digest
from plugins.commands import message_chatgpt as command
from samsara.command_registry import DispatchState


class Control:
    def __init__(self, kind="GroupControl", name="", children=()):
        self.ControlTypeName = kind
        self._name = name
        self.ClassName = ""
        self.children = list(children)
        self.reads = 0
        self.parent = None

    @property
    def Name(self):
        self.reads += 1
        return self._name

    def GetChildren(self):
        return self.children

    def GetParentControl(self):
        return self.parent


def recorded():
    data = json.loads((Path(__file__).parent / "fixtures/chatgpt_uia_740.json").read_text())
    controls = [Control(n["type"], n["label"] or " ".join(["sample"] * n["name_words"]))
                for n in data["nodes"]]
    for index, node in enumerate(data["nodes"]):
        if node["parent"] >= 0:
            controls[node["parent"]].children.append(controls[index])
            controls[index].parent = controls[node["parent"]]
    return controls


def tree(*, text="Answer.", stop=False, finished=False):
    user = Control("TextControl", "You said:", [Control("TextControl", "You said:")])
    marker = Control("TextControl", "ChatGPT said:", [Control("TextControl", "ChatGPT said:")])
    body = Control("TextControl", text)
    buttons = [Control("ButtonControl", "Copy")] if finished else []
    container = Control(children=[user, Control(children=[Control("TextControl", "Question.")]),
                                  marker, Control(children=[body]), *buttons])
    chat = Control(name="Conversation", children=[Control(children=[container])])
    chrome = Control("ButtonControl", "Stop generating" if stop else "Send prompt")
    root = Control("WindowControl", children=[chat, chrome])
    return root, body, chrome, container


class Clock:
    def __init__(self, tick=None):
        self.now = 0.0
        self.tick = tick

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.tick:
            self.tick(self.now)


def clock(monkeypatch, tick=None):
    fake = Clock(tick)
    monkeypatch.setattr(reply, "time", fake)
    return fake


def app():
    current = SimpleNamespace(config={"agent_voice": {"digest": "off"}}, app_state="other")
    coordinator = SimpleNamespace(calls=[], _last_spoken_handle=None, cancel_speech=Mock())

    def speak(text, **kwargs):
        coordinator.calls.append((text, kwargs))
        coordinator._last_spoken_handle = object()
        return coordinator._last_spoken_handle

    coordinator.speak = speak
    current.audio_coordinator = coordinator
    return current


def test_recorded_flat_and_grouped_roles_are_deduplicated():
    nodes = recorded()
    snapshot = reply._snapshot(nodes[0])
    assert len(snapshot.messages) == 11
    assert [m.assistant for m in snapshot.messages] == [False, True] * 5 + [True]
    assert snapshot.messages[-1].text
    assert not snapshot.messages[-1].finished  # old Copy buttons never leak
    assert all(not m.text for m in snapshot.messages[:-1])
    assert not snapshot.streaming


def test_polling_never_reads_older_prose():
    nodes = recorded()
    reader = reply._Reader(nodes[0])
    reader.snapshot()
    older = nodes[560]
    before = older.reads
    for _ in range(3):
        reader.snapshot()
    assert older.reads == before


def test_recorded_composer_sibling_stop_blocks_completion(monkeypatch):
    nodes = recorded()
    nodes[768]._name = "Stop streaming"
    clock(monkeypatch)
    with pytest.raises(reply.ReplyTimeout):
        reply.watch_for_reply(nodes[0], 10, 2)


def test_streaming_pause_requires_stop_to_disappear(monkeypatch):
    root, body, stop, _ = tree(stop=True)

    def tick(now):
        if now >= 2.1:
            body._name = "Final answer."
            stop._name = "Send prompt"

    fake = clock(monkeypatch, tick)
    assert reply.watch_for_reply(root, 1, 10) == "Final answer."
    assert 3.6 <= fake.now <= 4.0


def test_own_finished_button_can_win_after_settle(monkeypatch):
    root, _, _, _ = tree(stop=True, finished=True)
    fake = clock(monkeypatch)
    assert reply.watch_for_reply(root, 1, 10) == "Answer."
    assert 1.5 <= fake.now <= 1.9


def test_stability_without_exposed_stop_and_configurable_settle(monkeypatch):
    root, _, _, _ = tree()
    fake = clock(monkeypatch)
    assert reply.watch_for_reply(root, 1, 10, settle_s=0.6) == "Answer."
    assert 0.6 <= fake.now <= 0.9


def test_old_copy_does_not_finish_new_streaming_message(monkeypatch):
    root, _, _, container = tree(stop=True, finished=True)
    container.children.extend([Control("TextControl", "You said:"),
                               Control("TextControl", "ChatGPT said:"),
                               Control(children=[Control("TextControl", "Still writing.")])])
    clock(monkeypatch)
    with pytest.raises(reply.ReplyTimeout):
        reply.watch_for_reply(root, 2, 2)


def test_new_user_turn_cannot_reuse_previous_assistant(monkeypatch):
    root, _, _, container = tree(finished=True)
    container.children.append(Control("TextControl", "You said:"))
    clock(monkeypatch)
    with pytest.raises(reply.ReplyTimeout):
        reply.watch_for_reply(root, 1, 2)


def test_timeout_is_hard_capped_and_no_new_node_is_distinct(monkeypatch):
    root, _, _, _ = tree(stop=True)
    fake = clock(monkeypatch)
    with pytest.raises(reply.ReplyTimeout):
        reply.watch_for_reply(root, 1, 500)
    assert 120 <= fake.now < 120.4
    with pytest.raises(reply.NoNewNode):
        reply.watch_for_reply(root, 2, 1)


def test_missing_chat_and_disappeared_window(monkeypatch):
    with pytest.raises(reply.NoNewNode):
        reply.message_index(Control("WindowControl"))
    monkeypatch.setattr(reply, "_control", Mock(side_effect=reply.WindowGone))
    with pytest.raises(reply.WindowGone):
        reply.watch_for_reply(Control("WindowControl"), 1)


def test_long_reply_newest_subtree_and_spoken_first_chunk(monkeypatch):
    nodes = recorded()
    nodes[735]._name = " ".join(f"word{i}" for i in range(1500))
    fake = clock(monkeypatch)
    text = reply.watch_for_reply(nodes[0], 10, 10)
    first, rest = reply.spoken_form(text)
    assert len(first.split()) == 60
    assert len(rest.split()) >= 1440
    assert fake.now >= 1.5


def test_shared_digest_followups_and_source_replacement(monkeypatch):
    current = app()
    monkeypatch.setattr(agent_reply.thread_registry, "spawn", lambda name, target: target())
    monkeypatch.setattr(reply, "watch_for_reply", lambda *a, **kw: "Full answer with details.")
    monkeypatch.setattr(spoken_digest, "first_reply", lambda *a, **kw:
                        spoken_digest.Digest("A short digest.", [], "local"))
    reply.start_after_send(current, reply.SendContext(42, 2, ""))
    assert current.audio_coordinator.calls[-1][0] == "A short digest."
    state = agent_reply.state(current)
    assert state is claude_reply._state(current)
    assert state["full"] == "Full answer with details."
    assert reply.followup(current, "read the rest")
    assert current.audio_coordinator.calls[-1][0] == "Full answer with details."
    assert reply.followup(current, "repeat that")
    assert reply.followup(current, "stop chat gpt")
    current.audio_coordinator.cancel_speech.assert_called_once()


def test_replaced_watcher_cannot_speak_late_result(monkeypatch):
    current = app()
    pending = []
    monkeypatch.setattr(agent_reply.thread_registry, "spawn", lambda name, target: pending.append(target))
    monkeypatch.setattr(reply, "watch_for_reply", lambda *a, **kw: "Old answer.")
    reply.start_after_send(current, reply.SendContext(42, 1, ""))
    old = pending[0]
    claude_reply.start_after_send(current, reply.SendContext(43, 1, ""))
    old()
    assert not current.audio_coordinator.calls
    assert agent_reply.state(current)["source"] == "claude"


def test_timeout_has_spoken_fallback(monkeypatch):
    current = app()
    monkeypatch.setattr(agent_reply.thread_registry, "spawn", lambda name, target: target())
    monkeypatch.setattr(reply, "watch_for_reply", Mock(side_effect=reply.ReplyTimeout))
    reply.start_after_send(current, reply.SendContext(42, 1, ""))
    assert current.audio_coordinator.calls[-1][0] == "The reply is still going."


def test_before_send_only_accepts_chatgpt_process(monkeypatch):
    import psutil
    import win32process
    monkeypatch.setattr(win32process, "GetWindowThreadProcessId", lambda hwnd: (1, 2))
    process = SimpleNamespace(name=lambda: "chrome.exe")
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    monkeypatch.setattr(reply, "message_index", lambda hwnd: 11)
    assert reply.before_send(app(), 42) is None
    process.name = lambda: "ChatGPT.exe"
    assert reply.before_send(app(), 42).after_message_index == 11


def test_absent_desktop_uses_arc_alias_without_sending_here(monkeypatch):
    from plugins.commands import quick_ask
    adapter = SimpleNamespace(resolve_window=lambda: None)
    monkeypatch.setattr(command, "_adapter", adapter)
    send = Mock(return_value=True)
    monkeypatch.setattr(quick_ask, "_send_to_arc", send)
    assert command.handle_chatgpt_message_prepare(app(), "Explain this.")
    send.assert_called_once_with("gpt", "Explain this")


def test_prepare_stages_confirmation_without_paste(monkeypatch):
    current = app()
    adapter = Mock()
    adapter.resolve_window.return_value = (42, "ChatGPT", "fingerprint")
    monkeypatch.setattr(command, "_adapter", adapter)
    stage = Mock()
    monkeypatch.setattr(command.execution_policy, "stage_pending", stage)
    monkeypatch.setattr(command.execution_policy, "current_generation", lambda app: 1)
    result = command.handle_chatgpt_message_prepare(current, "Explain this.")
    assert result.state == DispatchState.QUEUED
    stage.assert_called_once()
    adapter.paste.assert_not_called()


def test_send_once_observe_new_user_and_start_watcher(monkeypatch):
    current = app()
    draft = command.Draft("test", "Question.", "hash", 42, "ChatGPT", "fingerprint")
    monkeypatch.setattr(command, "_drafts", {draft.id: draft})
    adapter = Mock()
    adapter.resolve_window.return_value = (42, "ChatGPT", "fingerprint")
    adapter.read_composer.side_effect = ["", "Question."]
    adapter.user_messages.return_value = [(3, "Question.")]
    monkeypatch.setattr(command, "_adapter", adapter)
    monkeypatch.setattr(command.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(reply, "before_send", lambda *a: reply.SendContext(42, 2, ""))
    start = Mock()
    monkeypatch.setattr(reply, "start_after_send", start)
    result = command.handle_chatgpt_message_submit(current, draft.id)
    assert result.state == DispatchState.COMPLETED
    start.assert_called_once()
    assert command.handle_chatgpt_message_submit(current, draft.id).state == DispatchState.REJECTED
    adapter.invoke.assert_called_once()


@pytest.mark.parametrize("case", ["conversation", "composer", "focus"])
def test_send_guards_refuse_before_invoke(monkeypatch, case):
    current = app()
    draft = command.Draft("guard", "Question.", "hash", 42, "ChatGPT", "fingerprint")
    monkeypatch.setattr(command, "_drafts", {draft.id: draft})
    adapter = Mock()
    adapter.resolve_window.return_value = (42, "ChatGPT", "changed" if case == "conversation" else "fingerprint")
    adapter.focus.return_value = case != "focus"
    adapter.read_composer.return_value = "Existing draft."
    monkeypatch.setattr(command, "_adapter", adapter)
    assert command.handle_chatgpt_message_submit(current, draft.id).state == DispatchState.FAILED
    adapter.invoke.assert_not_called()
