"""1128: deterministic slow CLI failures; no processes, live homes or network."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from samsara import integration_health as health


@pytest.fixture
def cli(monkeypatch):
    state = {"version": "1", "failure": None, "phase": "--version", "calls": []}
    monkeypatch.setattr(health.shutil, "which", lambda _: "fake-hermes")

    def run(argv, **kwargs):
        state["calls"].append((argv, kwargs))
        assert kwargs["timeout"] == 15
        if state["failure"] and argv[-1] == state["phase"]:
            if state["failure"] == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            if state["failure"] == "start":
                raise OSError("private executable path")
            if state["failure"] == "transient":
                raise RuntimeError("private transient details")
            return SimpleNamespace(returncode=1, stdout="private CLI reply")
        output = (state["version"] if argv[-1] == "--version" else
                  "-p --format --query-file --resume -m --reasoning")
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(health.subprocess, "run", run)
    return state


@pytest.mark.parametrize("failure", ["timeout", "start", "transient", "nonzero"])
def test_unavailable_version_keeps_exact_previous_state(tmp_path, monkeypatch, cli, caplog, failure):
    item = health.hermes_integration({})
    assert health.run_checks(tmp_path, [item]) == []
    before = (tmp_path / health.STATE_FILE).read_bytes()
    cli["failure"] = failure
    times = iter([100, 115.25])
    monkeypatch.setattr(health.time, "monotonic", lambda: next(times))
    pending = []
    with caplog.at_level("INFO", logger=health.__name__):
        assert health.run_checks(tmp_path, [item, item], unavailable=pending) == []
    assert pending == [item]
    assert (tmp_path / health.STATE_FILE).read_bytes() == before
    assert "hermes check_unavailable attempt=1 elapsed_ms=15250" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize("previous_ok", [True, False])
def test_help_timeout_keeps_old_fingerprint_and_ok(tmp_path, cli, previous_ok):
    item = health.hermes_integration({})
    health.run_checks(tmp_path, [item])
    path = tmp_path / health.STATE_FILE
    saved = json.loads(path.read_text())
    saved["integrations"]["hermes"]["ok"] = previous_ok
    path.write_text(json.dumps(saved))
    before = path.read_bytes()
    cli.update(version="2", failure="timeout", phase="--help")
    pending = []
    assert health.run_checks(tmp_path, [item], unavailable=pending) == []
    assert path.read_bytes() == before
    assert pending == [item]
    cli["failure"] = None
    assert health.run_checks(tmp_path, [item], attempt=2) == []
    assert json.loads(path.read_text())["integrations"]["hermes"]["ok"] is True


def test_changed_version_and_nonzero_help_warns_once(tmp_path, cli):
    item = health.hermes_integration({})
    health.run_checks(tmp_path, [item])
    cli.update(version="2", failure="nonzero", phase="--help")
    pending = []
    notices = health.run_checks(tmp_path, [item], unavailable=pending)
    assert len(notices) == 1
    assert notices[0].text == ("Hermes changed and Samsara can't use it yet. "
                               "Open Settings to check the connection.")
    assert "version check" not in notices[0].text
    assert pending == []
    assert health.run_checks(tmp_path, [item]) == []
    assert "private" not in (tmp_path / health.STATE_FILE).read_text()


def test_first_install_failed_probe_does_not_claim_change(tmp_path, cli):
    cli.update(failure="nonzero", phase="--help")
    assert health.run_checks(tmp_path, [health.hermes_integration({})]) == []


def test_fast_unchanged_cli_does_not_probe_or_retry(tmp_path, cli):
    item = health.hermes_integration({})
    health.run_checks(tmp_path, [item])
    cli["calls"].clear()
    pending = []
    assert health.run_checks(tmp_path, [item], unavailable=pending) == []
    assert pending == []
    assert [argv for argv, _ in cli["calls"]] == [["fake-hermes", "--version"]]


@pytest.mark.parametrize("recovery", ["unchanged", "changed_bad", "still_slow"])
def test_startup_defers_one_retry_off_ui_thread(tmp_path, monkeypatch, cli, caplog, recovery):
    from PySide6.QtCore import QTimer
    from samsara import paths
    from samsara.runtime import thread_registry

    item = health.hermes_integration({})
    other = health.Integration("other", "Other", lambda: "1", lambda: None)
    health.run_checks(tmp_path, [item, other])
    before = (tmp_path / health.STATE_FILE).read_bytes()
    cli.update(failure="timeout")
    workers, ui, timers, shown = [], [], [], []
    monkeypatch.setattr(paths, "samsara_home_dir", lambda: tmp_path)
    monkeypatch.setattr(health, "enabled_integrations", lambda _: [item, other])
    monkeypatch.setattr(thread_registry, "spawn",
                        lambda name, fn, **kw: workers.append((name, fn, kw)))
    monkeypatch.setattr(QTimer, "singleShot", lambda delay, fn: timers.append((delay, fn)))
    app = SimpleNamespace(config={}, _schedule_ui=ui.append,
                          _show_outcome_chip=lambda *args: shown.append(args))
    cli["calls"].clear()
    health.schedule_on_startup(app)
    health.schedule_on_startup(app)
    assert len(workers) == 1 and cli["calls"] == []
    workers.pop()[1]()
    assert (tmp_path / health.STATE_FILE).read_bytes() == before
    assert shown == [] and len(ui) == 1
    ui.pop()()
    assert len(timers) == 1 and timers[0][0] == 90_000
    calls = len(cli["calls"])
    timers.pop()[1]()
    assert len(cli["calls"]) == calls  # UI timer only dispatches a worker.
    name, retry, kwargs = workers.pop()
    assert name == "integration_health.retry" and kwargs["daemon"] is True
    if recovery != "still_slow":
        cli["failure"] = None
    if recovery == "changed_bad":
        cli.update(version="2", failure="nonzero", phase="--help")
    with caplog.at_level("INFO", logger=health.__name__):
        retry()
    if recovery == "changed_bad":
        assert len(ui) == 1
        ui.pop()()
        assert len(shown) == 1 and shown[0][1] == "warning"
    else:
        assert ui == [] and shown == []
    if recovery == "still_slow":
        assert "check_unavailable attempt=2 elapsed_ms=" in caplog.text
        assert (tmp_path / health.STATE_FILE).read_bytes() == before
    assert workers == [] and timers == []
    assert "other" in json.loads((tmp_path / health.STATE_FILE).read_text())["integrations"]


def test_timeout_without_cache_does_not_save_a_failure(tmp_path, cli):
    cli["failure"] = "timeout"
    item = health.hermes_integration({})
    pending = []
    assert health.run_checks(tmp_path, [item], unavailable=pending) == []
    assert pending == [item]
    assert json.loads((tmp_path / health.STATE_FILE).read_text())["integrations"] == {}
    cli["failure"] = None
    assert health.run_checks(tmp_path, [item], attempt=2) == []
    assert json.loads((tmp_path / health.STATE_FILE).read_text())["integrations"]["hermes"]["ok"]
