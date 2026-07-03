"""Tests for samsara.runtime.thread_registry."""

import logging
import sys
import threading
import time

import pytest

from samsara.runtime import thread_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test gets an empty registry so name-dedup counters etc. don't
    leak across tests."""
    thread_registry._threads.clear()
    thread_registry._finished.clear()
    thread_registry._name_counts.clear()
    yield
    thread_registry._threads.clear()
    thread_registry._finished.clear()
    thread_registry._name_counts.clear()


def test_spawn_starts_and_runs_target():
    done = threading.Event()

    def _work():
        done.set()

    t = thread_registry.spawn("worker", _work)
    assert done.wait(2.0)
    t.join(2.0)
    assert not t.is_alive()


def test_spawn_preserves_daemon_flag():
    t_daemon = thread_registry.spawn("daemon-worker", lambda: time.sleep(0.05), daemon=True)
    t_nondaemon = thread_registry.spawn("nondaemon-worker", lambda: time.sleep(0.05), daemon=False)
    assert t_daemon.daemon is True
    assert t_nondaemon.daemon is False
    t_daemon.join(2.0)
    t_nondaemon.join(2.0)


def test_spawn_passes_args_and_kwargs():
    received = {}

    def _work(a, b, c=None):
        received["a"] = a
        received["b"] = b
        received["c"] = c

    t = thread_registry.spawn("args-worker", _work, args=(1, 2), kwargs={"c": 3})
    t.join(2.0)
    assert received == {"a": 1, "b": 2, "c": 3}


def test_duplicate_names_get_numeric_suffix():
    ev = threading.Event()
    t1 = thread_registry.spawn("dup", lambda: ev.wait(2.0))
    t2 = thread_registry.spawn("dup", lambda: ev.wait(2.0))
    t3 = thread_registry.spawn("dup", lambda: ev.wait(2.0))
    assert t1.name == "dup"
    assert t2.name == "dup-2"
    assert t3.name == "dup-3"
    ev.set()
    t1.join(2.0)
    t2.join(2.0)
    t3.join(2.0)


def test_snapshot_reports_alive_and_fields():
    ev = threading.Event()
    t = thread_registry.spawn("snap-worker", lambda: ev.wait(2.0), daemon=True)
    try:
        entries = thread_registry.snapshot()
        matches = [e for e in entries if e["name"] == "snap-worker"]
        assert len(matches) == 1
        entry = matches[0]
        assert entry["ident"] == t.ident
        assert entry["daemon"] is True
        assert entry["alive"] is True
        assert entry["started_at"] is not None
        assert entry["finished_at"] is None
    finally:
        ev.set()
        t.join(2.0)


def test_snapshot_reflects_finished_thread():
    ev = threading.Event()
    t = thread_registry.spawn("finisher", lambda: ev.wait(2.0))
    ev.set()
    t.join(2.0)
    time.sleep(0.05)  # let the wrapper's finally-block run
    entries = thread_registry.snapshot()
    matches = [e for e in entries if e["name"] == "finisher"]
    assert len(matches) == 1
    assert matches[0]["alive"] is False
    assert matches[0]["finished_at"] is not None


def test_exception_in_target_is_logged_and_reraised(caplog):
    caplog.set_level(logging.ERROR, logger="Samsara.samsara.runtime.thread_registry")

    error_holder = {}

    def _boom():
        raise ValueError("kaboom")

    # dictation.py installs a process-global threading.Thread.__init__ patch
    # (fail-loud pass) that wraps run() and logs-then-swallows any exception,
    # which would prevent it from ever reaching threading.excepthook below.
    # If some earlier test in this session already imported dictation, undo
    # that patch for the duration of this test so it observes the registry's
    # own re-raise in isolation, regardless of test execution order.
    _dictation = sys.modules.get("dictation")
    _patched_init = threading.Thread.__init__
    if _dictation is not None and hasattr(_dictation, "_original_thread_init"):
        threading.Thread.__init__ = _dictation._original_thread_init

    try:
        t = thread_registry.spawn("boom-worker", _boom)

        # The exception is raised inside the thread, not surfaced to us directly;
        # capture it by wrapping threading.excepthook for the duration of the join.
        def _hook(args):
            error_holder["exc"] = args.exc_value

        old_hook = threading.excepthook
        threading.excepthook = _hook
        try:
            t.join(2.0)
        finally:
            threading.excepthook = old_hook
    finally:
        threading.Thread.__init__ = _patched_init

    assert isinstance(error_holder.get("exc"), ValueError)
    assert any("boom-worker" in rec.message for rec in caplog.records)


def test_register_tracks_externally_constructed_thread():
    ev = threading.Event()

    class _MyThread(threading.Thread):
        def run(self):
            ev.wait(2.0)

    t = _MyThread(name="custom", daemon=True)
    t.start()
    thread_registry.register(t, "custom")
    try:
        entries = thread_registry.snapshot()
        matches = [e for e in entries if e["name"] == "custom"]
        assert len(matches) == 1
        assert matches[0]["alive"] is True
    finally:
        ev.set()
        t.join(2.0)


def test_register_dedup_name():
    ev = threading.Event()
    t1 = threading.Thread(target=lambda: ev.wait(2.0), daemon=True)
    t2 = threading.Thread(target=lambda: ev.wait(2.0), daemon=True)
    t1.start()
    t2.start()
    thread_registry.register(t1, "reg-dup")
    thread_registry.register(t2, "reg-dup")
    try:
        entries = {e["ident"]: e["name"] for e in thread_registry.snapshot()}
        names = sorted(entries.values())
        assert names == ["reg-dup", "reg-dup-2"]
    finally:
        ev.set()
        t1.join(2.0)
        t2.join(2.0)


def test_shutdown_joins_nondaemon_threads():
    finished = threading.Event()

    def _work():
        time.sleep(0.1)
        finished.set()

    thread_registry.spawn("shutdown-worker", _work, daemon=False)
    thread_registry.shutdown(timeout=2.0)
    assert finished.is_set()


def test_shutdown_logs_stragglers(caplog):
    caplog.set_level(logging.ERROR, logger="Samsara.samsara.runtime.thread_registry")
    stuck_release = threading.Event()

    def _stuck():
        stuck_release.wait(5.0)

    try:
        thread_registry.spawn("stuck-worker", _stuck, daemon=False)
        thread_registry.shutdown(timeout=0.2)
        assert any(
            "stuck-worker" in rec.message and "did not exit" in rec.message
            for rec in caplog.records
        )
    finally:
        stuck_release.set()


def test_shutdown_ignores_daemon_threads():
    ev = threading.Event()
    t = thread_registry.spawn("daemon-ignored", lambda: ev.wait(5.0), daemon=True)
    try:
        start = time.monotonic()
        thread_registry.shutdown(timeout=0.2)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0
        assert t.is_alive()
    finally:
        ev.set()
        t.join(2.0)


def test_timer_fires_and_is_marked_finished():
    fired = threading.Event()

    def _fire():
        fired.set()

    t = thread_registry.timer("timer-fire", 0.05, _fire)
    assert fired.wait(2.0)
    time.sleep(0.05)  # let the wrapper's finally-block run
    entries = thread_registry.snapshot()
    matches = [e for e in entries if e["name"] == "timer-fire"]
    assert len(matches) == 1
    assert matches[0]["alive"] is False
    assert matches[0]["finished_at"] is not None
    assert isinstance(t, threading.Timer)


def test_timer_cancel_is_observed_as_finished():
    def _fire():
        pass

    t = thread_registry.timer("timer-cancel", 5.0, _fire)
    t.cancel()
    # cancel() only sets the Timer's own `finished` Event synchronously; the
    # registry's _is_done() treats that as done immediately rather than
    # waiting on is_alive() to catch up with the OS thread unwinding.
    entries = thread_registry.snapshot()
    matches = [e for e in entries if e["name"] == "timer-cancel"]
    assert len(matches) == 1
    assert matches[0]["alive"] is False
    assert matches[0]["finished_at"] is not None
    t.join(2.0)


def test_timer_duplicate_names_get_numeric_suffix():
    t1 = thread_registry.timer("dup-timer", 5.0, lambda: None)
    t2 = thread_registry.timer("dup-timer", 5.0, lambda: None)
    t3 = thread_registry.timer("dup-timer", 5.0, lambda: None)
    assert t1.name == "dup-timer"
    assert t2.name == "dup-timer-2"
    assert t3.name == "dup-timer-3"
    t1.cancel()
    t2.cancel()
    t3.cancel()
    t1.join(2.0)
    t2.join(2.0)
    t3.join(2.0)


def test_shutdown_cancels_alive_timers():
    fired = threading.Event()

    def _fire():
        fired.set()

    # Non-daemon by default, like the raw Timer sites this replaces -- a long
    # delay proves shutdown() cancels rather than joins (joining would block
    # for the full delay).
    t = thread_registry.timer("shutdown-timer", 30.0, _fire)
    assert t.daemon is False

    start = time.monotonic()
    thread_registry.shutdown(timeout=2.0)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0
    t.join(2.0)
    assert not fired.is_set()


def test_dump_logs_alive_threads(caplog):
    caplog.set_level(logging.INFO, logger="dump-test")
    dump_logger = logging.getLogger("dump-test")
    ev = threading.Event()
    t = thread_registry.spawn("dump-worker", lambda: ev.wait(2.0), daemon=True)
    try:
        thread_registry.dump(dump_logger)
        assert any("dump-worker" in rec.message for rec in caplog.records)
    finally:
        ev.set()
        t.join(2.0)
