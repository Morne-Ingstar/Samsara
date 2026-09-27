"""Claude desktop reply observation, spoken form, and voice follow-ups."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.agent_channel import claude_reply as reply


class FakeControl:
    def __init__(self, kind, name="", class_name="", children=()):
        self.ControlTypeName = kind
        self.Name = name
        self.ClassName = class_name
        self._children = list(children)

    def GetChildren(self):
        return self._children


class FakeWindow(FakeControl):
    def __init__(self, snapshots):
        super().__init__("WindowControl")
        self.snapshots = snapshots
        self.poll = -1

    def Exists(self, timeout, interval):
        self.poll += 1
        return self.snapshots[min(self.poll, len(self.snapshots) - 1)] is not None

    def GetChildren(self):
        return self.snapshots[min(self.poll, len(self.snapshots) - 1)].GetChildren()


def tree(*, assistant=None, streaming=False, tool=False):
    user = FakeControl("GroupControl", "Message 1 of 2", children=[
        FakeControl("TextControl", "You said: a short question")])
    messages = [user]
    if assistant is not None:
        children = [FakeControl("GroupControl", class_name="flex min-w-0", children=[
            FakeControl("TextControl", "header")])]
        if tool:
            children.append(FakeControl("GroupControl", class_name="w-full", children=[
                FakeControl("DocumentControl", children=[
                    FakeControl("TextControl", "tool result must not be spoken")])]))
        children.append(FakeControl("GroupControl", children=[FakeControl("TextControl", assistant)]))
        children.append(FakeControl("ToolBarControl", children=[
            FakeControl("ButtonControl", "Read aloud")]))
        row = FakeControl("GroupControl", class_name="group/message-row", children=children)
        messages.append(FakeControl("GroupControl", "Message 2 of 2", children=[row]))
    chat = FakeControl("GroupControl", "Chat messages", children=messages)
    buttons = [FakeControl("ButtonControl", "Stop response" if streaming else "Send message")]
    return FakeControl("WindowControl", children=[chat, *buttons])


class FakeCoordinator:
    def __init__(self, *, muted=False):
        self.calls = []
        self._active_handle = None
        self._last_spoken_handle = None
        self.muted = muted
        self.cancelled = False

    def speak(self, text, **kwargs):
        self.calls.append((text, kwargs))
        handle = SimpleNamespace(utterance_id="noop-ava-muted" if self.muted else len(self.calls))
        self._active_handle = handle
        self._last_spoken_handle = handle
        return handle

    def cancel_speech(self):
        self.cancelled = True


def app(*, cap=60, muted=False):
    return SimpleNamespace(config={"claude_voice": {"spoken_word_cap": cap}},
                           audio_coordinator=FakeCoordinator(muted=muted), app_state="other")


def test_streaming_then_complete_waits_for_stable_prose(monkeypatch):
    monkeypatch.setattr(reply, "_POLL_S", 0.02)
    window = FakeWindow([tree(streaming=True), tree(assistant="first", streaming=True),
                         tree(assistant="Final answer.", tool=True)])
    started = time.monotonic()
    assert reply.watch_for_reply(window, 1, 3) == "Final answer."
    assert time.monotonic() - started >= 1.5
    assert window.poll >= 3


def test_no_new_node_and_window_gone(monkeypatch):
    monkeypatch.setattr(reply, "_POLL_S", 0.005)
    with pytest.raises(reply.NoNewNode):
        reply.watch_for_reply(FakeWindow([tree()]), 1, 0.02)
    with pytest.raises(reply.WindowGone):
        reply.watch_for_reply(FakeWindow([tree(streaming=True), None]), 1, 0.2)


def test_new_user_without_assistant_times_out(monkeypatch):
    monkeypatch.setattr(reply, "_POLL_S", 0.005)
    window = FakeWindow([tree()])
    with pytest.raises(reply.ReplyTimeout):
        reply.watch_for_reply(window, 0, 0.02)


def test_missing_chat_does_not_establish_a_send_baseline():
    window = FakeWindow([FakeControl("WindowControl")])
    with pytest.raises(reply.NoNewNode):
        reply.message_index(window)


def test_invalid_reply_timeout_uses_default():
    assert reply._reply_timeout({"claude_voice": {"reply_timeout_s": "bad"}}) == 180
    assert reply._reply_timeout({"claude_voice": {"reply_timeout_s": float("nan")}}) == 180


def test_spoken_form_skips_code_tables_and_paths():
    message = ("Here is the change.\n```python\nprint('secret')\n```\n"
               "| Name | Value |\n| --- | --- |\n| a | b |\n"
               "Open C:\\work\\answer.py for details.")
    spoken, rest = reply.spoken_form(message)
    assert "there's a code block on screen" in spoken
    assert "there's a table on screen" in spoken
    assert "there's a file path on screen" in spoken
    assert "secret" not in spoken + rest and "|" not in spoken + rest
    assert reply.spoken_form("one two three four five", 3) == ("one two three", "four five")


def test_uia_code_table_and_thinking_nodes_are_not_read():
    content = FakeControl("GroupControl", children=[
        FakeControl("TextControl", "Here is the result."),
        FakeControl("GroupControl", class_name="code-block", children=[
            FakeControl("TextControl", "print('secret')")]),
        FakeControl("TableControl", children=[FakeControl("TextControl", "private cells")]),
        FakeControl("GroupControl", class_name="thinking", children=[
            FakeControl("TextControl", "hidden reasoning")]),
    ])
    row = FakeControl("GroupControl", class_name="group/message-row", children=[content])
    assistant = FakeControl("GroupControl", "Message 2 of 2", children=[row])
    chat = FakeControl("GroupControl", "Chat messages", children=[assistant])
    window = FakeWindow([FakeControl("WindowControl", children=[chat])])
    snapshot = reply._snapshot(window)
    spoken, rest = reply.spoken_form(snapshot.messages[0].text)
    assert "there's a code block on screen" in spoken
    assert "there's a table on screen" in spoken
    assert "secret" not in spoken + rest
    assert "private cells" not in spoken + rest
    assert "hidden reasoning" not in spoken + rest


def test_read_rest_chunks_repeat_and_stop():
    current = app(cap=3)
    state = reply._state(current)
    state["token"] = object()
    state["rest"] = "one two three four five six seven"
    reply._speak(current, "initial response", state["token"])
    assert reply.followup(current, "read the rest")
    assert current.audio_coordinator.calls[-1][0] == "one two three"
    assert reply.followup(current, "read the rest")
    assert current.audio_coordinator.calls[-1][0] == "four five six"
    assert state["rest"] == "seven"
    assert reply.followup(current, "repeat that")
    assert current.audio_coordinator.calls[-1][0] == "four five six"
    assert reply.followup(current, "read the rest")
    assert current.audio_coordinator.calls[-1][0] == "seven"
    assert reply.followup(current, "read the rest")
    assert current.audio_coordinator.calls[-1][0] == "That's all of Claude's reply."
    assert reply.followup(current, "stop claude")
    assert current.audio_coordinator.cancelled
    state["expires"] = time.monotonic() - 1
    assert not reply.followup(current, "repeat that")


def test_followups_expire_when_another_utterance_is_spoken():
    current = app()
    state = reply._state(current)
    state["token"] = object()
    reply._speak(current, "Claude answer", state["token"])
    current.audio_coordinator._active_handle = None
    assert reply.followup(current, "repeat that")
    current.audio_coordinator.speak("Unrelated announcement")
    assert not reply.followup(current, "repeat that")


def test_watcher_uses_registered_thread_and_conversation_speech(monkeypatch):
    current = app()
    spawned = []
    monkeypatch.setattr(reply.thread_registry, "spawn", lambda name, target: spawned.append((name, target)))
    monkeypatch.setattr(reply, "watch_for_reply", lambda hwnd, index, timeout: "Hello from Claude.")
    reply.start_after_send(current, reply.SendContext(123, 2, "Project - Claude"))
    assert spawned[0][0] == "claude.reply"
    spawned[0][1]()
    text, kwargs = current.audio_coordinator.calls[-1]
    assert text == "Hello from Claude."
    assert kwargs["category"] == "conversation" and kwargs["interruptible"] is True


@pytest.mark.parametrize("failure,spoken", [(reply.NoNewNode, "Claude didn't answer"),
                                             (reply.WindowGone, "I lost the Claude window")])
def test_failures_are_spoken(monkeypatch, failure, spoken):
    current = app()
    monkeypatch.setattr(reply.thread_registry, "spawn", lambda name, target: target())

    def fail(hwnd, index, timeout):
        raise failure()

    monkeypatch.setattr(reply, "watch_for_reply", fail)
    reply.start_after_send(current, reply.SendContext(123, 2, "Project - Claude"))
    assert current.audio_coordinator.calls[-1][0] == spoken


def test_muted_caption_reply_still_resumes_wake(monkeypatch):
    current = app(muted=True)
    current.app_state = "asleep"
    current._start_wake_session = Mock()
    monkeypatch.setattr(reply.thread_registry, "spawn", lambda name, target: target())
    monkeypatch.setattr(reply, "watch_for_reply", lambda hwnd, index, timeout: "Answer.")
    monkeypatch.setattr("win32gui.GetForegroundWindow", lambda: 123)
    reply.start_after_send(current, reply.SendContext(123, 2, "Project - Claude", "over"))
    current._start_wake_session.assert_called_once_with(mode="focus_dictate", send_word="over")


def test_wake_returns_to_listening_after_spoken_reply(monkeypatch):
    current = app()
    current.app_state = "asleep"
    current._start_wake_session = Mock()
    monkeypatch.setattr(reply.thread_registry, "spawn", lambda name, target: target())
    monkeypatch.setattr(reply, "watch_for_reply", lambda hwnd, index, timeout: "Answer.")
    monkeypatch.setattr("win32gui.GetForegroundWindow", lambda: 123)
    reply.start_after_send(current, reply.SendContext(123, 2, "Project - Claude", "over"))
    current._start_wake_session.assert_not_called()
    current.audio_coordinator.calls[-1][1]["on_done"]()
    current._start_wake_session.assert_called_once_with(mode="focus_dictate", send_word="over")
