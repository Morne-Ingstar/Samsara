"""356: the private rotating conversation record keeps complete turn text."""

import json
from types import SimpleNamespace

from plugins.commands import ask_ollama
from samsara.ava_memory import AvaMemory, log_conversation


def _records(tmp_path):
    path = tmp_path / "logs" / "ava_conversation.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_full_user_and_ava_text_without_preview_truncation(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    user = "U" * 3000
    spoken = "S" * 3200
    shown = "D" * 3300
    log_conversation(lane="model", provider="deepseek", user_text=user,
                     ava_spoken=spoken, ava_shown=shown, action="answered")
    row = _records(tmp_path)[0]
    assert row["lane"] == "model" and row["provider"] == "deepseek"
    assert row["user"] == user and row["ava_spoken"] == spoken
    assert row["ava_shown"] == shown and row["action"] == "answered"
    assert row["at"].endswith("+00:00")


def test_model_turn_logger_keeps_full_reply_while_preview_stays_short(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    user, answer = "question " * 300, "answer " * 400
    ask_ollama._turn_local.reply = ask_ollama.AvaReply(answer, "deepseek", 4)
    ask_ollama._turn_local.speech = [(True, "tts", len(answer))]
    ask_ollama._turn_local.spoken_text = [answer]
    ask_ollama._log_turn(SimpleNamespace(), user, outcome="answered")
    row = _records(tmp_path)[0]
    assert row["user"] == user
    assert row["ava_spoken"] == answer
    assert row["ava_shown"] == answer


def test_action_history_is_assistant_side_and_survives_take_session():
    memory = AvaMemory()
    memory.add_user("send a task")
    memory.add_action_event("[action] 353b to Sol: approved at 00:33")
    memory.take_session()
    memory.add_user("what did you just do")
    messages = memory.get_messages("You are Ava")
    assert messages[-2] == {"role": "assistant",
                            "content": "[action] 353b to Sol: approved at 00:33"}
    assert messages[-1]["role"] == "user"
    memory.clear()
    assert memory.get_messages() == []


def test_take_session_clears_normal_turns_without_actions():
    memory = AvaMemory()
    memory.add_user("ordinary question")
    assert memory.take_session() == [{"role": "user", "content": "ordinary question"}]
    assert memory.get_messages() == []




def test_conversation_file_rotates_without_truncating_records(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    large = "A" * 710_000
    for number in range(4):
        log_conversation(lane="model", provider="local", user_text=str(number),
                         ava_spoken=large, ava_shown=large, action="answered")
    base = tmp_path / "logs" / "ava_conversation.log"
    assert base.exists() and (tmp_path / "logs" / "ava_conversation.log.1").exists()
    assert _records(tmp_path)[-1]["ava_spoken"] == large
