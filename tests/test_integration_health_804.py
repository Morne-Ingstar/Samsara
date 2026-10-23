"""804: isolated compatibility metadata only; never touch a live integration."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from samsara import integration_health as health


@pytest.fixture(autouse=True)
def refuse_live_calls(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("live I/O is forbidden in 804 tests")
    monkeypatch.setattr(health.subprocess, "run", refuse)
    monkeypatch.setattr(health.shutil, "which", lambda name: None)
    import socket
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_first_install_changed_and_unchanged_are_quiet_when_healthy(tmp_path, caplog):
    calls = []
    version = ["1"]
    item = health.Integration("fake", "Fake", lambda: version[0], lambda: calls.append("probe"))
    with caplog.at_level("INFO", logger=health.__name__):
        assert health.run_checks(tmp_path, [item]) == []
        assert health.run_checks(tmp_path, [item]) == []
        assert calls == ["probe"]
        version[0] = "2"
        assert health.run_checks(tmp_path, [item]) == []
    assert calls == ["probe", "probe"]
    assert "fake unchanged" in caplog.text and "fake ok" in caplog.text


def test_failure_notices_once_per_fingerprint_and_records_no_private_output(tmp_path, caplog):
    def fail():
        raise RuntimeError("private response must never be persisted or logged")
    item = health.Integration("fake", "Fake", lambda: "private setting", fail)
    with caplog.at_level("INFO", logger=health.__name__):
        notices = health.run_checks(tmp_path, [item])
        assert health.run_checks(tmp_path, [item]) == []
    assert notices[0].text == "Fake changed and Samsara can't talk to it: compatibility check unavailable"
    saved = (tmp_path / health.STATE_FILE).read_text()
    assert "private" not in saved + caplog.text
    assert json.loads(saved)["integrations"]["fake"]["ok"] is False


def test_version_failure_is_deduplicated_but_recovery_probes(tmp_path):
    version = [None]
    calls = []
    def fingerprint():
        if version[0] is None:
            raise OSError("untrusted error")
        return version[0]
    item = health.Integration("fake", "Fake", fingerprint, lambda: calls.append(1))
    assert len(health.run_checks(tmp_path, [item])) == 1
    assert health.run_checks(tmp_path, [item]) == []
    assert not calls
    version[0] = "recovered"
    assert health.run_checks(tmp_path, [item]) == []
    assert calls == [1]


@pytest.mark.parametrize("state", ["broken", "[]", '{"schema":1,"integrations":[]}'])
def test_corrupt_cache_checks_again(tmp_path, state):
    (tmp_path / health.STATE_FILE).write_text(state)
    calls = []
    item = health.Integration("fake", "Fake", lambda: "1", lambda: calls.append(1))
    health.run_checks(tmp_path, [item, item])
    assert calls == [1]


def test_one_failed_integration_does_not_skip_another(tmp_path):
    calls = []
    def fail():
        raise health.CheckFailure("a fixed reason")
    notices = health.run_checks(tmp_path, [
        health.Integration("bad", "Bad", lambda: "1", fail),
        health.Integration("good", "Good", lambda: "1", lambda: calls.append(1)),
    ])
    assert calls == [1]
    assert notices == [health.Notice("Bad", "a fixed reason")]


def fake_hermes(monkeypatch, missing=""):
    calls = []
    monkeypatch.setattr(health.shutil, "which", lambda _: "fake-hermes")
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-1] == "--version":
            output = "0.22.4"
        elif argv == ["fake-hermes", "--help"]:
            output = "-p PROFILE"
        else:
            assert argv == ["fake-hermes", "chat", "--help"]
            output = "--format --query-file --resume -m --reasoning".replace(missing, "") if missing else "--format --query-file --resume -m --reasoning"
        return SimpleNamespace(returncode=0, stdout=output)
    monkeypatch.setattr(health.subprocess, "run", run)
    return calls


def test_hermes_missing_real_flag_warns_with_flag_name(tmp_path, monkeypatch):
    calls = fake_hermes(monkeypatch, "--query-file")
    notices = health.run_checks(tmp_path, [health.hermes_integration({})])
    assert notices == [health.Notice("Hermes", "CLI help is missing --query-file")]
    assert all(argv[-1] in {"--help", "--version"} for argv, _ in calls)
    assert all(kwargs["shell"] is False and kwargs["timeout"] == health.TIMEOUT for _, kwargs in calls)
    assert all(kwargs["stdin"] == health.subprocess.DEVNULL for _, kwargs in calls)


def test_hermes_current_shape_passes_and_unchanged_does_not_request_help(tmp_path, monkeypatch):
    calls = fake_hermes(monkeypatch)
    item = health.hermes_integration({})
    assert health.run_checks(tmp_path, [item]) == []
    first_count = len(calls)
    assert health.run_checks(tmp_path, [item]) == []
    assert len(calls) == first_count + 1
    assert calls[-1][0][-1] == "--version"
    assert "--source" not in str(calls) and "--run-budget" not in str(calls)


def test_flag_matching_is_exact():
    with pytest.raises(health.CheckFailure, match="--query"):
        health._require_flags("--query-file", ["--query"])


@pytest.mark.parametrize("available,reason", [(["example:latest"], None), ([], "not installed")])
def test_ollama_uses_version_and_tags_only(tmp_path, monkeypatch, available, reason):
    calls = []
    def get(host, endpoint):
        calls.append(endpoint)
        return {"version": "1"} if endpoint == "/api/version" else {"models": [{"name": name} for name in available]}
    monkeypatch.setattr(health, "_ollama_json", get)
    notices = health.run_checks(tmp_path, [health.ollama_integration("http://localhost:11434", ["example"])])
    assert calls == ["/api/version", "/api/tags"]
    assert (reason in notices[0].reason) if reason else notices == []


def test_configured_model_change_triggers_probe(tmp_path, monkeypatch):
    calls = []
    def get(host, endpoint):
        calls.append(endpoint)
        return {"version": "1"} if endpoint == "/api/version" else {"models": []}
    monkeypatch.setattr(health, "_ollama_json", get)
    health.run_checks(tmp_path, [health.ollama_integration("http://localhost:11434", ["first"])])
    health.run_checks(tmp_path, [health.ollama_integration("http://localhost:11434", ["second"])])
    assert calls.count("/api/tags") == 2


def test_no_enabled_integrations_does_not_contact_services(tmp_path, monkeypatch):
    config = {"command_packs": {"core": False, "music": False}, "tts": {"enabled": False}}
    assert health.enabled_integrations(config) == []
    assert health.run_checks(tmp_path, []) == []


@pytest.fixture
def fake_async_metadata(monkeypatch):
    # Windows creates a socket pair even for an otherwise fake async call.
    # Drive these immediate fake coroutines directly; keep every socket refused.
    async def wait_for(awaitable, timeout):
        assert timeout == health.TIMEOUT
        return await awaitable
    def run(coroutine):
        try:
            coroutine.send(None)
        except StopIteration as completed:
            return completed.value
        finally:
            coroutine.close()
        pytest.fail("fake metadata unexpectedly needed an event loop")
    monkeypatch.setattr(health.asyncio, "wait_for", wait_for)
    monkeypatch.setattr(health.asyncio, "run", run)


def test_edge_handshake_lists_voices_without_synthesis(tmp_path, monkeypatch, fake_async_metadata):
    import sys
    calls = []
    async def voices():
        calls.append("list")
        return [{"ShortName": "fake-voice"}]
    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(list_voices=voices))
    monkeypatch.setattr(health, "_package", lambda _: "1")
    assert health.run_checks(tmp_path, [health.edge_integration("fake-voice")]) == []
    assert calls == ["list"]


def test_todoist_uses_read_only_existing_endpoint(tmp_path, monkeypatch):
    from samsara import todoist_client
    calls = []
    def request(token, url, **kwargs):
        calls.append((url, kwargs))
        return {"results": []}
    monkeypatch.setattr(todoist_client, "_request", request)
    assert health.run_checks(tmp_path, [health.todoist_integration("fake-credential")]) == []
    assert calls == [(todoist_client.FILTER_URL + "?query=today", {})]
    assert "fake-credential" not in (tmp_path / health.STATE_FILE).read_text()


def test_startup_schedules_once_and_is_silent_for_healthy_result(tmp_path, monkeypatch):
    from samsara.runtime import thread_registry
    from samsara import paths
    pending = []
    monkeypatch.setattr(thread_registry, "spawn", lambda name, work, **kwargs: pending.append(work))
    monkeypatch.setattr(paths, "samsara_home_dir", lambda: tmp_path)
    monkeypatch.setattr(health, "enabled_integrations", lambda config: [])
    app = SimpleNamespace(config={}, _schedule_ui=lambda fn: pytest.fail("healthy check displayed a notice"))
    health.schedule_on_startup(app)
    health.schedule_on_startup(app)
    assert len(pending) == 1
    assert not (tmp_path / health.STATE_FILE).exists()
    pending[0]()
    assert (tmp_path / health.STATE_FILE).is_file()


@pytest.mark.parametrize("name", ["gpt", "claude"])
def test_connected_cli_checks_only_help_and_version(tmp_path, monkeypatch, name):
    from samsara.agent_channel import cli_agents
    monkeypatch.setattr(cli_agents, "resolve_executable", lambda _: ["fake-cli"])
    calls = []
    def cli(argv):
        calls.append(argv)
        assert argv[-1] in {"--help", "--version"}
        return "1 --json --skip-git-repo-check -C --sandbox -c -p --output-format --verbose --restricted --tools --permission-mode --resume"
    monkeypatch.setattr(health, "_cli", cli)
    assert health.run_checks(tmp_path, [health.cli_integration(name)]) == []
    assert len(calls) == (3 if name == "gpt" else 2)


def test_ollama_adapter_uses_safe_transport_and_closes_response(monkeypatch):
    from samsara import ai_transport
    calls = []
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"version": "1"},
                               close=lambda: calls.append("close"))
    def get(url, timeout, **kwargs):
        calls.append((url, timeout, kwargs))
        return response
    monkeypatch.setattr(ai_transport, "get_local", get)
    assert health._ollama_json("http://localhost:11434/", "/api/version") == {"version": "1"}
    assert calls == [("http://localhost:11434/api/version", health.TIMEOUT,
                      {"ollama_host": "http://localhost:11434/"}), "close"]


def test_enabled_integrations_obey_consent_and_collect_real_configured_models(monkeypatch):
    from samsara import ai_preferences
    monkeypatch.setattr(ai_preferences, "runtime_state",
                        lambda config, feature, **kwargs: SimpleNamespace(allowed=feature == "editing"))
    config = {"command_packs": {"core": False, "music": False, "hermes": True},
              "hermes": {"notice_accepted": False},
              "tts": {"enabled": True, "engine": "edge", "network_voice_accepted": False},
              "ava_edit": {"model": "edit-model"},
              "smart_corrections": {"enabled": True, "ollama_model": "correct-model"}}
    captured = []
    monkeypatch.setattr(health, "ollama_integration", lambda host, models: captured.append(models) or "ollama")
    assert health.enabled_integrations(config) == ["ollama"]
    assert captured == [["edit-model", "correct-model"]]


def test_spotify_probe_only_reads_media_session(tmp_path, monkeypatch, fake_async_metadata):
    import sys
    executable = tmp_path / "fake-spotify.exe"
    executable.write_bytes(b"fake")
    calls = []
    session = SimpleNamespace(source_app_user_model_id="Spotify", get_playback_info=lambda: calls.append("read"))
    async def request():
        calls.append("handshake")
        return SimpleNamespace(get_sessions=lambda: [session])
    monkeypatch.setitem(sys.modules, "winsdk.windows.media.control", SimpleNamespace(
        GlobalSystemMediaTransportControlsSessionManager=SimpleNamespace(request_async=request)))
    monkeypatch.setattr(health, "_package", lambda _: "1")
    assert health.run_checks(tmp_path, [health.spotify_integration(str(executable))]) == []
    assert calls == ["handshake", "read"]


def test_startup_delivers_notices_through_normal_display_in_order(tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer
    from samsara import paths
    from samsara.runtime import thread_registry
    monkeypatch.setattr(paths, "samsara_home_dir", lambda: tmp_path)
    monkeypatch.setattr(health, "enabled_integrations", lambda config: [])
    notices = [health.Notice("One", "reason"), health.Notice("Two", "reason")]
    monkeypatch.setattr(health, "run_checks", lambda *args: notices)
    monkeypatch.setattr(thread_registry, "spawn", lambda name, work, **kwargs: work())
    timers, shown = [], []
    monkeypatch.setattr(QTimer, "singleShot", lambda delay, fn: timers.append((delay, fn)))
    app = SimpleNamespace(config={}, _schedule_ui=lambda fn: fn(),
                          _show_outcome_chip=lambda text, kind: shown.append((text, kind)))
    health.schedule_on_startup(app)
    assert shown == [(notices[0].text, "warning")]
    assert timers[0][0] == 8500
    timers[0][1]()
    assert shown == [(notice.text, "warning") for notice in notices]


@pytest.mark.parametrize("schedule_fails", [False, True])
def test_real_startup_seam_runs_after_completion_and_isolates_failure(monkeypatch, schedule_fails):
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / "dictation.py").read_text(encoding="utf-8"))
    startup = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "load_model_async")
    worker = next(node for node in ast.walk(startup)
                  if isinstance(node, ast.FunctionDef) and node.name == "load")
    body = worker.body[0].body  # load() wraps the startup lane in its outer try.
    index = next(index for index, node in enumerate(body)
                 if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                 and isinstance(node.value.func, ast.Name) and node.value.func.id == "_boot_log"
                 and node.value.args and isinstance(node.value.args[0], ast.Constant)
                 and node.value.args[0].value == "async: startup complete")
    seam = ast.Module(body=body[index:index + 2], type_ignores=[])
    events = []
    app = object()

    def schedule(actual):
        assert actual is app
        events.append("schedule")
        if schedule_fails:
            raise RuntimeError("private exception must not reach the boot log")

    monkeypatch.setattr(health, "schedule_on_startup", schedule)
    namespace = {"self": app, "_boot_log": events.append,
                 "logger": SimpleNamespace(warning=lambda message: events.append("warning"))}
    # Execute only the real completion marker and guarded hook, never app bootstrap.
    exec(compile(seam, "<804-startup-health-seam>", "exec"), namespace)
    assert events == ["async: startup complete", "schedule"] + (["warning"] if schedule_fails else [])
