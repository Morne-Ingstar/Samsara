"""478: fresh clock facts in both sends, outside retained conversation data."""

import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from plugins.commands import ask_ollama
from samsara import ava_local_context, cloud_llm
from samsara.ava_memory import AvaMemory

LA = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("before, after, expected_before, expected_after", [
    ("2026-10-10T06:59:00+00:00", "2026-10-10T07:00:00+00:00",
     "Friday, 2026-10-09; 23:59; PDT (UTC-07:00)",
     "Saturday, 2026-10-10; 00:00; PDT (UTC-07:00)"),
    ("2026-03-08T09:59:00+00:00", "2026-03-08T10:00:00+00:00",
     "Sunday, 2026-03-08; 01:59; PST (UTC-08:00)",
     "Sunday, 2026-03-08; 03:00; PDT (UTC-07:00)"),
    ("2026-11-01T08:59:00+00:00", "2026-11-01T09:00:00+00:00",
     "Sunday, 2026-11-01; 01:59; PDT (UTC-07:00)",
     "Sunday, 2026-11-01; 01:00; PST (UTC-08:00)"),
])
def test_fixed_clock_boundaries(before, after, expected_before, expected_after):
    times = iter(datetime.fromisoformat(t).astimezone(LA) for t in (before, after))
    clock = lambda: next(times)
    assert expected_before in ava_local_context.local_clock_line(clock)
    assert expected_after in ava_local_context.local_clock_line(clock)


def test_default_clock_reads_now_and_local_zone_each_call(monkeypatch):
    calls = []
    moment = datetime(2026, 10, 9, 20, 31, tzinfo=LA)

    class NaiveNow:
        def astimezone(self):
            calls.append("local zone")
            return moment

    class Clock:
        @staticmethod
        def now():
            calls.append("now")
            return NaiveNow()

    monkeypatch.setattr(ava_local_context, "datetime", Clock)
    assert "20:31; PDT (UTC-07:00)" in ava_local_context.local_clock_line()
    moment = datetime(2026, 11, 1, 1, 0, tzinfo=LA, fold=1)
    assert "01:00; PST (UTC-08:00)" in ava_local_context.local_clock_line()
    assert calls == ["now", "local zone", "now", "local zone"]


@pytest.mark.parametrize("output", ["context", "spoken"])
def test_missing_local_zone_falls_back_to_utc_with_warning(output, monkeypatch, caplog):
    from PySide6.QtCore import QLocale

    moment = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)
    calls = []

    class MissingZone:
        def astimezone(self):
            raise ZoneInfoNotFoundError("synthetic missing zone")

    class Clock:
        @staticmethod
        def now(tz=None):
            calls.append(tz)
            return MissingZone() if tz is None else moment

    monkeypatch.setattr(ava_local_context, "datetime", Clock)
    monkeypatch.setattr(QLocale, "system", lambda: QLocale("en_GB"))
    if output == "context":
        assert "Saturday, 2026-10-10; 07:00; UTC (UTC+00:00)" in (
            ava_local_context.local_clock_line())
    else:
        assert ava_local_context.clock_answer("what time is it") == "It's 07:00."
    assert calls == [None, timezone.utc]
    assert "Local time zone unavailable; using UTC" in caplog.text


@pytest.mark.parametrize("route", ["ollama", "deepseek", "anthropic", "search"])
def test_both_routes_send_fresh_system_facts_without_storing_them(
        route, tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    provider = "deepseek" if route == "search" else route
    app = SimpleNamespace(config={
        "ava_memory": {"mode": "last"}, "ollama": {},
        "cloud_llm": {"enabled": True, "provider": provider,
                      "api_key": "test-placeholder", "web_search": route == "search"},
        "ava": {"provider_policy": "local" if provider == "ollama" else "cloud",
                "conversation_log_enabled": True},
    })
    memory_path = tmp_path / "history.json"
    app._ava_memory = AvaMemory(persist_path=str(memory_path))
    app._ava_memory._mode = "last"
    monkeypatch.setattr(ask_ollama, "_ai_state", lambda _app: SimpleNamespace(
        allowed=True, provider=SimpleNamespace(identity=provider)))
    monkeypatch.setattr(ask_ollama, "_disabled_since_request", lambda _app: False)
    monkeypatch.setattr(ask_ollama, "_turn_local", ask_ollama.threading.local())
    monkeypatch.setattr(ask_ollama.ava_profile, "build_context_section", lambda *_a: "")
    monkeypatch.setattr(ask_ollama.ava_corrections, "build_context_section", lambda **_kw: "")
    monkeypatch.setattr(ask_ollama, "_apply_communication_preferences", lambda system, _app: system)
    current = [datetime(2026, 10, 9, 23, 59, tzinfo=LA)]
    real_line = ava_local_context.local_clock_line
    reads, sent = [], []

    def line():
        reads.append(current[0])
        return real_line(lambda: current[0])

    monkeypatch.setattr(ava_local_context, "local_clock_line", line)

    def available(*_a, **_kw):
        assert len(reads) == len(sent), "read clock after the availability probe"
        return True

    monkeypatch.setattr(ask_ollama, "_check_ollama_available", available)

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "A short answer."},
                    "choices": [{"message": {"content": "A short answer."}}],
                    "content": [{"type": "text", "text": "A short answer."}],
                    "stop_reason": "end_turn"}

    def post(_url, **kwargs):
        sent.append(kwargs["json"])
        return Response()

    monkeypatch.setattr(ask_ollama.ai_transport, "post_local", post)
    monkeypatch.setattr(cloud_llm.ai_transport, "post_cloud", post)
    monkeypatch.setattr(cloud_llm, "web_search_available", lambda _app: route == "search")
    base = ask_ollama.get_system_prompt(app)
    assert "natural spoken form" in base
    for index in range(2):
        if index:
            current[0] = datetime(2026, 10, 10, 0, 0, tzinfo=LA)
        reply = ask_ollama.ask_model("What day is it?", app, allow_search=route == "search")
        assert reply.ok
        body = sent[-1]
        system = body.get("system") or body["messages"][0]["content"]
        assert real_line(lambda: current[0]) in system
        assert system.count("Local clock facts (PC, not user text):") == 1
        assert all("Local clock facts" not in m["content"] for m in body["messages"]
                   if m["role"] != "system")
        assert "Local clock facts" not in app._ava_last_context
        ask_ollama._turn_local.reply = reply
        ask_ollama._log_turn(app, "What day is it?", outcome="answered")
    assert len(reads) == 2
    assert "23:59" in (sent[0].get("system") or sent[0]["messages"][0]["content"])
    assert "00:00" in (sent[1].get("system") or sent[1]["messages"][0]["content"])
    assert "Local clock facts" not in json.dumps(app._ava_memory.get_messages())
    assert "Local clock facts" not in memory_path.read_text(encoding="utf-8")
    assert "Local clock facts" not in (tmp_path / "logs" / "ava_conversation.log").read_text(encoding="utf-8")
    assert ask_ollama.get_system_prompt(app) == base, "clock must not accumulate in shared instructions"


def test_interpreter_and_edit_keep_their_prompt_assembly():
    # Interpreter intentionally inherits shared clock-aware conversational instructions;
    # its shortlist and wire protocol, and the independent rewrite prompt, stay intact.
    tree = ast.parse((ROOT / "samsara/ava_command_session.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert any(isinstance(n.func, ast.Attribute) and n.func.attr == "get_system_prompt" for n in calls)
    assert any(isinstance(n.func, ast.Attribute) and n.func.attr == "ask_ollama"
               and any(k.arg == "system" for k in n.keywords) for n in calls)
    from samsara.ava_edit import model
    assert "Never add information" in model.SYSTEM_PROMPT
    assert "Local clock facts" not in model.SYSTEM_PROMPT
