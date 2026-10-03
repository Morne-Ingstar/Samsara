"""362: exercise dispatch through the real Ava session lane."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import dispatch_voice
from samsara import execution_policy, plugin_commands
from samsara.agent_channel.dispatch_voice import DispatchDraft
from samsara.ava_memory import AvaMemory
from samsara.paths import samsara_home_dir
from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals


SIGNALS = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.3,))


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    monkeypatch.setattr(dispatch_voice.thread_registry, "timer", lambda *a, **kw: None)
    plugin_commands._REGISTRY["send prompt"] = (
        plugin_commands._MODULE_ENTRIES[dispatch_voice.__name__]["send prompt"])
    spoken, drafted = [], []
    model = Mock()

    class Client:
        def draft(self, pid, identity):
            drafted.append((pid, identity))
            return DispatchDraft(f"Plan for {pid} with {identity}", "task-362", "a" * 64, pid)

    class ReadyQueue:
        paths = SimpleNamespace(queue_root=Path("fake-queue"))
        note = SimpleNamespace(label=lambda path: path.name.split("_", 1)[0])
        ids = ("345a", "353a", "353b", "353c")

        def _prompt_files(self, _folder):
            return [Path(f"{pid}_test.md") for pid in self.ids]

    class App:
        config = {"dispatch_voice": {"enabled": True}, "ava_memory": {"mode": "clear"}}
        _ava_memory = AvaMemory()
        _dispatch_voice_client = Client()
        _queue_voice_view = ReadyQueue()
        _show_outcome_chip = Mock()

        def __init__(self):
            def speak(text, **kw):
                spoken.append(text)
                if kw.get("on_done"):
                    kw["on_done"]()
                return SimpleNamespace(utterance_id="played")
            self.audio_coordinator = SimpleNamespace(speak=speak)

        def ava_dispatch(self, text, context):
            # Same local-dispatch-before-model wiring as dictation.py.
            if not dispatch_voice.route_ava_utterance(self, text):
                model(text, context)

    app = App()
    manager = SessionModeManager(
        abort_phrases=["cancel", "abort"],
        foreground_exe_resolver=lambda: "notepad.exe",
        inject_fn=Mock(), remove_chars_fn=Mock(),
        command_dispatch_fn=Mock(), agent_dispatch_fn=app.ava_dispatch,
    )
    manager.force_mode(SessionMode.AVA)
    yield SimpleNamespace(app=app, manager=manager, spoken=spoken,
                          drafted=drafted, model=model)
    op = execution_policy.pending_operation()
    if op is not None:
        execution_policy._release_slot(op)


def test_follow_up_number_passes_ava_substance_gate(lane):
    assert lane.manager.dispatch_utterance("Send to Luna", SIGNALS).kind == "ava_dispatched"
    assert lane.spoken[-1] == "Which prompt: 345a, 353a, 353b or 353c?"
    result = lane.manager.dispatch_utterance("3-5-3-B", SIGNALS)
    assert result.kind == "ava_dispatched"
    assert lane.drafted == [("353b", "Luna")]
    lane.model.assert_not_called()
    turns = [json.loads(line) for line in
             (samsara_home_dir() / "logs" / "ava_conversation.log")
             .read_text(encoding="utf-8").splitlines()]
    assert turns[-1]["user"] == "3-5-3-B"
    assert turns[-1]["lane"] == "dispatch"


@pytest.mark.parametrize("heard,expected,identity", [
    ("Send three five be the soul", "353b", "Sol"),
    ("Send 3538 to Luna.", "353a", "Luna"),
    ("Send 353 eight to Luna", "353a", "Luna"),
    ("Send 353 ay to Luna", "353a", "Luna"),
    ("Send 353 hey to Luna", "353a", "Luna"),
    ("Send 353 bee to Sol", "353b", "Sol"),
    ("Send 3533 to Sol", "353b", "Sol"),
    ("Send three five three three be to Sol", "353b", "Sol"),
    ("Send 353 sea to Luna", "353c", "Luna"),
    ("Send 345a to Luna", "345a", "Luna"),
])
def test_queue_resolves_heard_id_before_plan(lane, heard, expected, identity):
    result = lane.manager.dispatch_utterance(heard, SIGNALS)
    assert result.kind == "ava_dispatched"
    assert lane.drafted == [(expected, identity)]
    assert expected in lane.spoken[-1]
    lane.model.assert_not_called()


@pytest.mark.parametrize("suffix", ["the", "dee", "delta"])
def test_d_suffix_uses_spoken_and_nato_letter_table(lane, suffix):
    lane.app._queue_voice_view.ids += ("353d",)
    lane.manager.dispatch_utterance(f"Send 353 {suffix} to Sol", SIGNALS)
    assert lane.drafted == [("353d", "Sol")]
    lane.model.assert_not_called()


def test_multiple_fuzzy_matches_are_named_before_selection(lane):
    lane.app._queue_voice_view.ids = ("353b", "355b")
    lane.manager.dispatch_utterance("Send three five be to Luna", SIGNALS)
    assert lane.spoken[-1] == "353b or 355b?"
    assert not lane.drafted
    lane.manager.dispatch_utterance("353b", SIGNALS)
    assert lane.drafted == [("353b", "Luna")]
    lane.model.assert_not_called()


def test_ambiguous_prefix_names_candidates_and_accepts_letter(lane):
    assert lane.manager.dispatch_utterance("Send 353 to Luna", SIGNALS).kind == "ava_dispatched"
    assert lane.spoken[-1] == "353a, 353b or 353c?"
    assert lane.manager.dispatch_utterance("bee", SIGNALS).kind == "ava_dispatched"
    assert lane.drafted == [("353b", "Luna")]
    lane.model.assert_not_called()


def test_absent_id_names_ready_queue_without_model(lane):
    assert lane.manager.dispatch_utterance("Send 999 to Luna", SIGNALS).kind == "ava_dispatched"
    assert lane.spoken[-1].startswith("I don't see that prompt in the queue.")
    assert "353b" in lane.spoken[-1]
    assert not lane.drafted
    lane.model.assert_not_called()


def test_short_noise_without_follow_up_stays_rejected(lane):
    result = lane.manager.dispatch_utterance("uh", SIGNALS)
    assert result.kind == "ava_rejected_not_substantive"
    assert not lane.spoken
    lane.model.assert_not_called()


@pytest.mark.parametrize("reply", ["3-5-3-B", "three five three b"])
def test_follow_up_expires_after_thirty_seconds(lane, monkeypatch, reply):
    now = [100.0]
    monkeypatch.setattr(dispatch_voice.time, "monotonic", lambda: now[0])
    lane.manager.dispatch_utterance("Send to Luna", SIGNALS)
    now[0] = 131.0
    result = lane.manager.dispatch_utterance(reply, SIGNALS)
    assert result.kind == "ava_dispatched"
    assert lane.spoken[-1] == "That dispatch question expired. Say the full request again."
    assert not lane.drafted
    lane.model.assert_not_called()


def test_noise_after_expired_follow_up_is_not_an_answer(lane, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(dispatch_voice.time, "monotonic", lambda: now[0])
    lane.manager.dispatch_utterance("Send to Luna", SIGNALS)
    now[0] = 131.0
    assert lane.manager.dispatch_utterance("uh", SIGNALS).kind == "ava_rejected_not_substantive"
    assert not lane.drafted
    lane.model.assert_not_called()


def test_invalid_follow_up_gets_one_more_spoken_chance(lane):
    lane.manager.dispatch_utterance("Send to Luna", SIGNALS)
    assert lane.manager.dispatch_utterance("uh", SIGNALS).kind == "ava_dispatched"
    assert "Which prompt" in lane.spoken[-1]
    assert dispatch_voice.awaiting_follow_up(lane.app)
    assert lane.manager.dispatch_utterance("uh", SIGNALS).kind == "ava_dispatched"
    assert "Which prompt" in lane.spoken[-1]
    assert not dispatch_voice.awaiting_follow_up(lane.app)
    assert lane.manager.dispatch_utterance("uh", SIGNALS).kind == "ava_rejected_not_substantive"
    lane.model.assert_not_called()
