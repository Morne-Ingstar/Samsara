"""Queue 175c: Ava warm-up is deferred until after startup."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from samsara import ava_readiness, config_schema
from samsara.ui import home_qt, home_signals


@pytest.fixture(autouse=True)
def _fresh_tracker(monkeypatch):
    ava_readiness.tracker.reset()
    with ava_readiness.tracker._lock:
        ava_readiness.tracker._listeners.clear()
    monkeypatch.setattr(ava_readiness, "_monitor", None)
    yield
    ava_readiness.tracker.reset()


def _app(*, warm_on_boot=True, cloud_enabled=False):
    config = {
        "ollama": {"enabled": True, "host": "http://ollama.test", "model": "test", "timeout_seconds": 1},
        "cloud_llm": {"enabled": cloud_enabled, "api_key": "test-key" if cloud_enabled else ""},
    }
    if warm_on_boot is not None:
        config["ava"] = {"warm_on_boot": warm_on_boot}
    return SimpleNamespace(config=config)


def test_warming_is_neither_ready_nor_offline_and_has_its_own_copy():
    tracker = ava_readiness.ReadinessTracker()
    generation = tracker.begin_warming("ollama")
    assert isinstance(generation, int)
    warming = tracker.snapshot()
    assert warming.warming
    assert not warming.ready
    assert not warming.offline
    assert warming.badge_label() == "Ava: warming"
    assert warming.sentence() == "Ava is warming up."


def test_stale_warm_result_cannot_overwrite_a_later_failed_real_turn():
    tracker = ava_readiness.ReadinessTracker()
    generation = tracker.begin_warming("ollama")
    real_turn = tracker.record_turn("ollama", ava_readiness.UNREACHABLE)
    result = tracker.record_warm_result("ollama", None, generation)

    assert real_turn.offline
    assert result == real_turn
    assert tracker.snapshot() == real_turn


def test_monitor_probe_does_not_demote_an_inflight_warmup():
    tracker = ava_readiness.ReadinessTracker()
    tracker.begin_warming("ollama")
    monitor = ava_readiness.ReadinessMonitor(
        tracker, lambda: ("ollama", ava_readiness.UNREACHABLE))
    assert monitor.run_once().warming
    assert tracker.snapshot().warming


def test_warm_completion_holds_warming_until_the_worker_returns(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "ready"}}

    def _post(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return _Response()

    import requests
    monkeypatch.setattr(requests, "post", _post)
    worker = threading.Thread(target=ava_readiness.warm_configured_provider, args=(_app(),))
    worker.start()
    assert entered.wait(1)
    assert ava_readiness.tracker.snapshot().warming
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert ava_readiness.tracker.snapshot().ready


def test_warm_ollama_empty_completion_records_provider_error(monkeypatch):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {}

    import requests
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: _Response())

    result = ava_readiness.warm_configured_provider(_app())

    assert result.offline
    assert result.provider == "ollama"
    assert result.failure_kind == ava_readiness.PROVIDER_ERROR


def test_schedule_warm_on_boot_runs_completion_in_the_spawned_worker(monkeypatch):
    boot_thread = threading.get_ident()
    ran = threading.Event()
    worker_thread = []

    def _warm(_app):
        worker_thread.append(threading.get_ident())
        ran.set()

    def _spawn(_name, fn):
        thread = threading.Thread(target=fn)
        thread.start()
        return thread

    monkeypatch.setattr(ava_readiness, "warm_configured_provider", _warm)
    assert ava_readiness.schedule_warm_on_boot(_app(), _spawn)
    assert ran.wait(1)
    assert worker_thread == [worker_thread[0]]
    assert worker_thread[0] != boot_thread


def test_warm_on_boot_is_not_scheduled_when_disabled():
    calls = []
    assert not ava_readiness.schedule_warm_on_boot(_app(warm_on_boot=False), lambda *args: calls.append(args))
    assert calls == []


def test_warm_on_boot_defaults_local_on_and_cloud_off():
    assert ava_readiness.warm_on_boot_enabled(_app(warm_on_boot=None))
    assert not ava_readiness.warm_on_boot_enabled(
        _app(warm_on_boot=None, cloud_enabled=True))


def test_schema_registers_the_warm_on_boot_default():
    assert config_schema.SETTINGS_SCHEMA["ava.warm_on_boot"] == {
        "type": "bool", "default": False, "tab": "ava"}


def test_home_signals_maps_readiness_warming(monkeypatch):
    monkeypatch.setattr(
        ava_readiness, "readiness_for",
        lambda _app: ava_readiness.Readiness(ava_readiness.WARMING, provider="ollama"),
    )
    state = home_signals.ava_state(_app())
    assert state.status == home_signals.WARMING
    assert home_signals.ava_presence(_app()) == (
        home_signals.AVA_WARMING, "Ava is warming up.")


@pytest.mark.parametrize("state, word", [
    (home_signals.AVA_AWAKE, "ready"),
    (home_signals.AVA_THINKING, "working"),
    (home_signals.AVA_DIM, "unavailable"),
    (home_signals.AVA_ASLEEP, "off"),
    (home_signals.AVA_WARMING, "warming"),
])
def test_header_pill_has_a_visible_label_for_every_ava_state(qapp, monkeypatch, state, word):
    monkeypatch.setattr(home_qt.home_signals, "ava_presence",
                        lambda _app: (state, f"Ava is {word}."))
    control = home_qt.AvaHeaderControl(_app())
    control.show()
    qapp.processEvents()
    assert control.visible_text == f"Ava {home_qt.EM_DASH} {word}"
    assert control.accessibleName() == control.visible_text
    assert control.sizeHint().width() >= control.layout().sizeHint().width()
    control.close()
