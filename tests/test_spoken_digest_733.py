"""Local digest contracts with synthetic replies and no provider I/O."""
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.agent_channel import spoken_digest as sd


REPLY = ("The change is ready. You can use the updated code to save your work. "
         "There are four steps on screen, with an explanation of each step.")


def app(**voice):
    return SimpleNamespace(config={"ava": {"provider_policy": "local"},
        "ollama": {"enabled": True, "host": "http://localhost:11434", "model": "fixture-model"},
        "agent_voice": voice})


@pytest.fixture
def local(monkeypatch):
    state = SimpleNamespace(output="The change is ready. There are four steps on screen.", calls=[])
    monkeypatch.setattr(sd, "_request_lock", threading.Lock())

    def post(url, **kwargs):
        state.calls.append((url, kwargs))
        if isinstance(state.output, Exception):
            raise state.output
        response = Mock()
        response.json.return_value = {"response": state.output}
        state.response = response
        return response

    monkeypatch.setattr(sd, "post_local", post)
    return state


def test_good_digest_one_local_call_and_model_override(local):
    result = sd.first_reply(app(digest_model="digest-model", digest_budget_s=.5), REPLY, source="gpt")
    assert result.spoken == local.output and result.used == "local"
    assert result.sections == sd.sections(REPLY)
    assert len(local.calls) == 1
    url, kwargs = local.calls[0]
    assert url == "http://localhost:11434/api/generate"
    assert 0 < kwargs["timeout"] <= .5
    assert kwargs["json"]["model"] == "digest-model"
    assert kwargs["json"]["stream"] is False
    assert kwargs["json"]["options"]["num_predict"] == 90
    local.response.close.assert_called_once()


@pytest.mark.parametrize("text,spoken", [
    ("The timeout is fixed. The updated code is on screen, followed by an explanation.",
     "The timeout is fixed. The code is on screen."),
    ("Save your work, close the editor, reopen it, then check the result. These are the four steps.",
     "There are four steps on screen, starting with saving your work."),
    ("Both formats can store the result. Choose the one your editor accepts. Which format do you want?",
     "Both formats work. Which format do you want?"),
])
def test_spoken_examples(local, text, spoken):
    local.output = spoken
    result = sd.digest(app(), text, source="gpt")
    assert result.used == "local" and result.spoken == spoken


def test_prompt_sanitizes_code_tables_and_paths(local):
    text = (REPLY + "\n```python\na = 1\nb = 2\n```\n"
            "| Name | Count |\n| --- | --- |\n| a | 2 |\nOpen C:\\work\\answer.py")
    assert sd.digest(app(), text, source="claude").used == "local"
    payload = local.calls[0][1]["json"]
    assert "[code block, 2 lines]" in payload["prompt"]
    assert "[table, 3 rows]" in payload["prompt"]
    assert "[file path]" in payload["prompt"]
    assert "a = 1" not in payload["prompt"] and "answer.py" not in payload["prompt"]
    assert "question" in payload["system"] and "invented facts" in payload["system"]


@pytest.mark.parametrize("output", ["", "   ", "```python\na = 1\n```", "Use `code`.",
                                     "~~~python\na = 1\n~~~", "word " * 100, None])
def test_bad_output_uses_cut(local, output):
    local.output = output
    current = app()
    result = sd.digest(current, REPLY, source="gpt")
    assert result.used == "cut" and result.spoken == REPLY
    assert current._ava_summary_retry_after > time.monotonic()


def test_markdown_is_stripped_and_words_capped(local):
    local.output = "## **Ready**\n- See [the steps](https://example.invalid/steps)."
    assert sd.digest(app(), REPLY, source="gpt").spoken == "Ready See the steps."
    local.output = "word " * 70
    result = sd.digest(app(), "source " * 120, source="gpt")
    assert result.used == "local" and len(result.spoken.split()) == 60


def test_response_cleanup_failure_still_releases_worker(local, monkeypatch):
    original = sd.post_local

    def post(*args, **kwargs):
        response = original(*args, **kwargs)
        response.close.side_effect = RuntimeError("private cleanup detail")
        return response

    monkeypatch.setattr(sd, "post_local", post)
    result = sd.digest(app(), REPLY, source="gpt")
    assert result.used == "cut" and result.spoken == REPLY
    assert not sd._request_lock.locked()


def test_timeout_defers_and_logs_metadata_once(local, caplog):
    local.output = TimeoutError("private response must never be logged")
    current = app()
    with caplog.at_level(logging.INFO, logger=sd.logger.name):
        result = sd.digest(current, REPLY, source="gpt", budget_s=.2)
        second = sd.digest(current, REPLY, source="gpt", budget_s=.2)
    assert result.used == second.used == "cut"
    assert result.spoken == REPLY and len(local.calls) == 1
    records = [r for r in caplog.records if "[SPOKEN-DIGEST]" in r.message]
    assert len(records) == 1 and "TimeoutError" in records[0].message
    assert "private response" not in caplog.text


def test_hard_deadline_ignores_late_result(local, monkeypatch):
    release, finished = threading.Event(), threading.Event()
    original = sd.post_local

    def slow(*args, **kwargs):
        release.wait(1)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(sd, "post_local", slow)
    current = app()
    started = time.monotonic()
    try:
        result = sd.digest(current, REPLY, source="gpt", budget_s=.03)
        assert time.monotonic() - started < .5
        assert result.used == "cut" and result.spoken == REPLY
        assert sd.digest(app(), REPLY, source="gpt", budget_s=.03).used == "cut"
    finally:
        release.set()
        assert finished.wait(1)
    assert result.used == "cut"  # completed worker cannot replace returned speech


@pytest.mark.parametrize("mode", ["off", "deepseek", "disabled", "deferred"])
def test_policy_never_calls_any_transport(local, mode):
    current = app()
    if mode == "off":
        current.config["agent_voice"]["digest"] = "off"
    elif mode == "deepseek":
        current.config.update(ava={"provider_policy": "cloud"}, cloud_llm={"provider": "deepseek"})
    elif mode == "disabled":
        current.config["ollama"]["enabled"] = False
    else:
        current._ava_summary_retry_after = time.monotonic() + 300
    result = sd.digest(current, REPLY, source="gpt")
    assert result.used == "cut" and result.spoken == REPLY and not local.calls


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf"), "bad"])
def test_invalid_budget_still_speaks(local, budget):
    result = sd.digest(app(), "", source="gpt", budget_s=budget)
    assert result.used == "cut" and result.spoken and not local.calls


@pytest.mark.parametrize("text,kinds,titles", [
    ("A paragraph.\n\n```py\nx = 1\n```\nDone.",
     ["paragraph", "code", "paragraph"], ["", "", ""]),
    ("# Plan\nIntro.\n\n## Steps\n1. Save\n2. Check\n\nFinish.",
     ["heading", "paragraph", "heading", "list", "paragraph"],
     ["Plan", "Plan", "Steps", "Steps", "Steps"]),
    ("Results\n=======\n\n| Name | Count |\n| --- | --- |\n| a | 2 |\n\nDone.",
     ["heading", "table", "paragraph"], ["Results", "Results", "Results"]),
])
def test_section_maps(text, kinds, titles):
    result = sd.sections(text)
    assert [s.kind for s in result] == kinds
    assert [s.title for s in result] == titles
    assert [s.index for s in result] == list(range(len(kinds)))
    assert all(s.text in text for s in result)


def test_sections_preserve_fenced_content_and_unclosed_code():
    text = "~~~py\n# not a heading\n| not a table |\n~~~\n\n```py\nunclosed"
    assert [s.kind for s in sd.sections(text)] == ["code", "code"]
    assert sd.sections("") == []


@pytest.mark.parametrize("name", ["gpt", "claude"])
def test_cli_first_speech_and_full_reply_followup(local, monkeypatch, name):
    from plugins.commands import ask_agent
    from samsara.agent_channel import cli_agents
    current = app(digest="off")
    current.audio_coordinator = SimpleNamespace(speak=Mock())
    result = SimpleNamespace(text=REPLY)
    monkeypatch.setattr(cli_agents, "agent", lambda _: SimpleNamespace(ask=lambda *args: result))
    seen = []

    def first(app, text, *, source):
        seen.append((text, source))
        return sd.Digest("There are four steps on screen.", sd.sections(text), "local")

    monkeypatch.setattr(cli_agents, "first_reply", first)
    monkeypatch.setattr(ask_agent.thread_registry, "spawn", lambda name, run: run())
    ask_agent.ask_cli(current, name, "synthetic question")
    assert seen == [(REPLY, name)]
    assert current.audio_coordinator.speak.call_args.args[0] == "There are four steps on screen."
    assert current._cli_agent_voice_state["rest"] == REPLY
    assert cli_agents.followup(current, "read the rest")
    assert current.audio_coordinator.speak.call_args.args[0] == REPLY


def test_hermes_first_speech_and_full_reply_followup(monkeypatch):
    from samsara.agent_channel import hermes_voice as voice
    current = app(digest="off")
    turn = object()
    agent = SimpleNamespace(begin=lambda: turn, valid=lambda _: True,
        ask=lambda *args, **kw: SimpleNamespace(text=REPLY), finish=lambda _: None,
        _state_lock=threading.RLock())
    current._hermes_channel = agent
    monkeypatch.setattr(voice, "permitted", lambda _: True)
    monkeypatch.setattr(voice, "waiting", Mock())
    monkeypatch.setattr(voice, "sound", Mock())
    monkeypatch.setattr(voice, "chip", Mock())
    play = Mock()
    monkeypatch.setattr(voice, "play", play)
    first = Mock(return_value=sd.Digest("There are four steps on screen.", [], "local"))
    monkeypatch.setattr(voice, "first_reply", first)
    monkeypatch.setattr(voice.thread_registry, "spawn", lambda name, run: run())
    voice.ask(current, "synthetic question")
    first.assert_called_once_with(current, REPLY, source="hermes")
    assert play.call_args.args[1] == "There are four steps on screen."
    state = current._cli_agent_voice_state
    assert state["rest"] == REPLY
    assert voice.followup(current, "read the rest", state)
    assert play.call_args.args[1] == REPLY
