"""Hermes 0.22 compatibility, using captured error text and in-memory processes."""
import logging

import pytest

from samsara.agent_channel import hermes, hermes_voice as voice
from tests.test_hermes_pack_539 import fake, run_worker


ERROR_RESULTS = (
    ('{"type":"result","exit_code":2,"error":"Unsupported gateway CLI options: '
     '--run-budget. No local fallback or policy changes were made. --run-budget: '
     'use `agent.run_budget_seconds` in config.yaml"}',
     2, 'Unsupported gateway CLI options: --run-budget. No local fallback or policy '
     'changes were made. --run-budget: use `agent.run_budget_seconds` in config.yaml'),
    ('{"type":"result","exit_code":1,"error":"Gateway does not support source \'samsara\'"}',
     1, "Gateway does not support source 'samsara'"),
)


def test_argv_keeps_profile_resume_model_and_effort_without_rejected_flags():
    argv = hermes.hermes_argv(["hermes"], "session_802", {
        "profile": "morne-gpt", "model": "provider/model", "effort": "high"}, 240)
    assert argv == ["hermes", "-p", "morne-gpt", "chat", "--format", "stream-json",
                    "--query-file", "-", "--resume", "session_802", "-m", "provider/model",
                    "--reasoning", "high"]


@pytest.mark.parametrize("payload,code,detail", ERROR_RESULTS)
def test_parse_real_gateway_errors(payload, code, detail):
    with pytest.raises(hermes.HermesFailure) as caught:
        hermes.parse_hermes([payload], ["less useful stderr"])
    assert str(caught.value) == detail
    assert caught.value.exit_code == code
    assert voice.failure(caught.value) == "Hermes couldn't answer: " + detail[:180]


@pytest.mark.parametrize("payload,code,detail", ERROR_RESULTS)
@pytest.mark.parametrize("process_code", [0, None])
def test_gateway_failure_reaches_chip_and_log_without_retry(
        fake, caplog, payload, code, detail, process_code):
    fake.output, fake.errors = [payload], ""
    fake.code = code if process_code is None else process_code
    with caplog.at_level(logging.INFO, logger=hermes.__name__):
        voice.ask(fake.app, "synthetic private question")
        run_worker(fake)
    assert len(fake.calls) == 1
    assert "--source" not in fake.calls[0][0]
    assert "--run-budget" not in fake.calls[0][0]
    assert fake.chips[-1][0] == "Hermes: Hermes couldn't answer: " + detail[:180]
    lines = [r.getMessage() for r in caplog.records if "[HERMES]" in r.getMessage()]
    assert lines == [f"[HERMES] failed exit={code} error={detail}"]
    assert "synthetic private question" not in caplog.text


def test_success_result_parses():
    assert hermes.parse_hermes([
        '{"type":"result","exit_code":0,"text":"pong"}'
    ]) == ("pong", None)


def test_success_logs_elapsed_time_without_message_or_answer(fake, caplog):
    with caplog.at_level(logging.INFO, logger=hermes.__name__):
        fake.agent.ask("synthetic private question", fake.app.config)
    lines = [r.getMessage() for r in caplog.records if "[HERMES]" in r.getMessage()]
    assert len(lines) == 1 and lines[0].startswith("[HERMES] answered in ")
    assert lines[0].endswith(" ms")
    assert "synthetic private question" not in caplog.text
    assert "Four." not in caplog.text


def test_result_error_uses_only_first_line_and_caps_log_at_200(fake, caplog):
    import json

    detail = "x" * 230
    fake.output = [json.dumps({"type": "result", "exit_code": 2,
                               "error": detail + "\nsecond diagnostic"})]
    fake.errors, fake.code = "", 2
    with caplog.at_level(logging.ERROR, logger=hermes.__name__):
        voice.ask(fake.app, "synthetic private question")
        run_worker(fake)
    assert [r.getMessage() for r in caplog.records] == [
        "[HERMES] failed exit=2 error=" + detail[:200]]


def test_process_failure_without_result_logs_actual_exit(fake, caplog):
    fake.output, fake.errors, fake.code = [], "first error\nsecond error", 3
    with caplog.at_level(logging.ERROR, logger=hermes.__name__):
        voice.ask(fake.app, "synthetic private question")
        run_worker(fake)
    assert [r.getMessage() for r in caplog.records] == [
        "[HERMES] failed exit=3 error=first error"]


@pytest.mark.parametrize("pipe_timeout", [False, True])
def test_timeout_remains_local_and_logs_once(fake, monkeypatch, caplog, pipe_timeout):
    if pipe_timeout:
        fake.app.config["agent_channel"]["timeout_s"] = .01
        monkeypatch.setattr(hermes.queue.Queue, "get",
                            lambda *a, **kw: (_ for _ in ()).throw(hermes.queue.Empty()))
    else:
        def timeout():
            raise hermes.subprocess.TimeoutExpired("hermes", 240)
        fake.after_wait = timeout
    with caplog.at_level(logging.ERROR, logger=hermes.__name__):
        voice.ask(fake.app, "synthetic private question")
        run_worker(fake)
    assert [r.getMessage() for r in caplog.records] == [
        "[HERMES] failed exit=-1 error=Hermes timed out."]
    assert fake.processes[0].killed
    assert fake.chips[-1][0] == "Hermes: Hermes took too long; your conversation is kept."
