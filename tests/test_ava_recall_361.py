"""361: archived recall, privacy, durable retry and local scoring."""

import json
import time
from types import SimpleNamespace

from plugins.commands import ask_ollama
from samsara import ava_persona
from samsara.ava_memory import PersonalMemory


def _app(home):
    return SimpleNamespace(config={"ava_memory": {"mode": "assistant"},
                                   "ollama": {"model": "local-test"}},
                           config_path=str(home / "config.json"))


def test_archive_relevance_forget_and_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    memory = PersonalMemory(sessions_kept=3)
    memory.add_summary("We planned the cedar kite project", "2026-08-01")
    for n in range(40):
        memory.add_summary(f"We discussed ordinary topic {n}", f"2026-09-{n % 28 + 1:02d}")
    assert "cedar kite" in memory.archive_path.read_text(encoding="utf-8")
    assert "cedar kite" in memory.prompt_block(utterance="What about the cedar kite?")
    assert "cedar kite" not in memory.prompt_block(utterance="What is the weather?")
    assert memory.forget("cedar kite")
    assert "cedar kite" not in memory.archive_path.read_text(encoding="utf-8")
    assert not memory.recall("cedar kite")
    assert len(memory.prompt_block(utterance="ordinary topic").encode("utf-8")) <= 1500
    memory.erase()
    assert "ordinary topic" not in memory.archive_path.read_text(encoding="utf-8")


def test_sensitive_facts_stay_local_and_exclusions_always_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    memory = PersonalMemory()
    assert memory.remember("remember that I have a medical diagnosis")
    memory.add_summary("We discussed cedar kites")
    # Owner-written exclusions must filter even records still present on disk.
    sections = memory._read()
    sections["## About you"].append("I like cedar pottery")
    sections["## Do not remember"].append("cedar")
    sections["## Recent sessions"].append("2026-08-01: We discussed arthritis")
    memory._write(sections)
    memory._write(sections, memory.archive_path)
    assert not memory.add_summary("We discussed a medical diagnosis")
    for cloud in (False, True):
        block = memory.prompt_block(utterance="medical diagnosis cedar arthritis", cloud=cloud)
        hits = memory.recall("medical diagnosis cedar arthritis", cloud=cloud)
        assert ("medical diagnosis" in block) is not cloud
        assert any("medical diagnosis" in item for _, item in hits) is not cloud
        assert "cedar" not in block
        assert "arthritis" not in block
        assert not any("cedar" in item or "arthritis" in item for _, item in hits)


def test_cloud_without_memory_ack_gets_persona_but_no_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    memory = PersonalMemory()
    memory.add_summary("We planned the cedar kite project")
    assert memory.remember("remember that I have arthritis")
    ava_persona.append("sound curious about cedar kites")
    ava_persona.append("discuss my medical diagnosis")
    app = _app(tmp_path)
    app.config.update({"ava": {"provider_policy": "cloud", "consent": {}},
                       "cloud_llm": {"enabled": True}})
    state = SimpleNamespace(allowed=True, provider=SimpleNamespace(identity="deepseek"))
    monkeypatch.setattr(ask_ollama, "_ai_state", lambda _app: state)
    seen = []
    monkeypatch.setattr(ask_ollama.cloud_llm, "send",
                        lambda system, *_args, **_kwargs: seen.append(system) or "Hello")
    assert ask_ollama.ask_model("cedar kite arthritis", app).ok
    assert "Who Ava is" in seen[-1]
    assert "cedar kite project" not in seen[-1]
    assert "sound curious about cedar kites" in seen[-1]
    assert "medical diagnosis" not in seen[-1]
    assert "arthritis" not in seen[-1]
    app.config["ava"]["consent"]["memory_cloud_accepted"] = True
    assert ask_ollama.ask_model("cedar kite arthritis", app).ok
    assert "cedar kite project" in seen[-1]
    assert "arthritis" not in seen[-1]
    assert "arthritis" not in app._ava_last_context
    assert "cedar kite project" in app._ava_last_context
    spoken = []
    monkeypatch.setattr(ask_ollama, "speak", lambda _app, message: spoken.append(message))
    assert ask_ollama._handle_working_context_intent(app, "what are you working from")
    assert "cedar kite project" in spoken[-1]
    ask_ollama._log_turn(app, "cedar kite", outcome="answered")
    records = (tmp_path / "logs" / "ava_conversation.log").read_text(encoding="utf-8")
    assert "cedar kite project" in json.loads(records.splitlines()[-1])["working_context"]


def test_explicit_sensitive_fact_reaches_local_model(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    memory = PersonalMemory()
    assert memory.remember("remember that I have arthritis")
    app = _app(tmp_path)
    state = SimpleNamespace(allowed=True, provider=SimpleNamespace(identity="ollama"))
    monkeypatch.setattr(ask_ollama, "_ai_state", lambda _app: state)
    monkeypatch.setattr(ask_ollama, "_check_ollama_available", lambda *_args, **_kwargs: True)
    seen = []
    response = SimpleNamespace(raise_for_status=lambda: None)
    monkeypatch.setattr(ask_ollama.ai_transport, "post_local",
                        lambda _url, **kw: seen.append(kw["json"]) or response)
    monkeypatch.setattr(ask_ollama.ava_readiness, "ollama_response_content", lambda _response: "Hello")
    assert ask_ollama.ask_model("How should we plan today?", app).ok
    assert "About you: I have arthritis" in seen[-1]["messages"][0]["content"]
    assert "I have arthritis" in app._ava_last_context


def test_pending_survives_outage_and_local_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    app = _app(tmp_path)
    app._ava_memory = ask_ollama._build_ava_memory(app)
    app._ava_memory.add_user("I like cedar kites")
    app._ava_memory.add_user("My salary is private")
    calls = []

    def unavailable(url, **kw):
        calls.append((url, kw))
        raise OSError("local model down")

    monkeypatch.setattr(ask_ollama.ai_transport, "post_local", unavailable)
    ask_ollama._finish_ava_session(app, wait=True)
    memory = PersonalMemory()
    assert len(memory.pending_sessions()) == 1
    assert "salary" not in str(memory.pending_sessions())
    assert all("localhost" in url for url, _ in calls)

    class Response:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(ask_ollama.ai_transport, "post_local",
                        lambda url, **kw: calls.append((url, kw)) or Response())
    monkeypatch.setattr(ask_ollama.ava_readiness, "ollama_response_content",
                        lambda _response: "Cedar kites were discussed.\nWe planned a paper prototype.")
    new_app = _app(tmp_path)
    new_app._ava_memory = ask_ollama._build_ava_memory(new_app)
    ask_ollama._finish_ava_session(new_app, wait=True)
    assert len(memory.pending_sessions()) == 0
    assert "paper prototype" in memory.path.read_text(encoding="utf-8")


def test_recall_1000_lines_under_ten_ms(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    memory = PersonalMemory(sessions_kept=1)
    archive = {"## About you": [], "## Recent sessions": [
        f"2026-08-01: Synthetic topic {n} meadow artifact." for n in range(1000)],
        "## Do not remember": []}
    memory._write(archive, memory.archive_path)
    memory.recall("meadow artifact 17")  # warm the filesystem; measure scoring
    timings = []
    for _ in range(5):
        start = time.perf_counter()
        hits = memory.recall("meadow artifact 17")
        timings.append(time.perf_counter() - start)
        assert hits
    assert min(timings) < 0.01
