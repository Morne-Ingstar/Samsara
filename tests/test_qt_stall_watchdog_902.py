"""902: observe stalled Qt/native hook work without touching desktop input."""
import sys
import threading
import weakref
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from samsara import mouse_hook
from samsara.ui import qt_runtime as qt


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(qt, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(qt, "_qt_input_snapshot", lambda: {"popup": None})
    monkeypatch.setattr(qt, "_mouse_input_snapshot", lambda: ())
    monkeypatch.setattr(qt, "_async_input_snapshot", lambda: {"left_ctrl": False})
    return now


def blocks(caplog):
    return [record.getMessage() for record in caplog.records
            if "[QT-STALL]" in record.getMessage()]


def test_healthy_heartbeat_logs_nothing(clock, caplog):
    watchdog = qt._StallWatchdog()
    for _ in range(40):
        clock[0] += 1.0
        watchdog.heartbeat()
        watchdog.check()
    assert not blocks(caplog)


def test_qt_stall_one_dump_per_episode_and_rearms(clock, monkeypatch, caplog):
    dumps = []

    def dump(*, file, all_threads):
        dumps.append(all_threads)
        file.write(b"all-thread test stack\n")

    monkeypatch.setattr(qt.faulthandler, "dump_traceback", dump)
    watchdog = qt._StallWatchdog()
    watchdog.heartbeat()
    clock[0] += 9.999
    watchdog.check()
    assert not blocks(caplog)
    clock[0] = 110.0
    watchdog.check()
    for _ in range(50):
        clock[0] += 1.0
        watchdog.check()
    assert len(blocks(caplog)) == 1
    assert dumps == [True]
    assert "qt_last_serviced={'popup': None}" in blocks(caplog)[0]
    assert "async_input={'left_ctrl': False}" in blocks(caplog)[0]
    assert "all-thread test stack" in blocks(caplog)[0]
    watchdog.heartbeat()
    watchdog.check()
    clock[0] += 10.0
    watchdog.check()
    assert len(blocks(caplog)) == 2
    assert dumps == [True, True]


def test_real_qt_block_is_observed_off_thread(clock, monkeypatch, caplog):
    # A real queued Qt slot blocks its owner. The monitor must not query Qt
    # or wait for another posted callback to capture an all-thread dump.
    app = QApplication.instance() or QApplication([])
    watchdog = qt._StallWatchdog()
    watchdog.heartbeat()
    captured = threading.Event()
    errors = []
    original = qt._report_stall

    def report(*args):
        try:
            original(*args)
        except BaseException as exc:
            errors.append(exc)
        finally:
            captured.set()

    monkeypatch.setattr(qt, "_report_stall", report)
    # Any accidental Qt query from the monitor fails this test.
    def forbidden():
        raise AssertionError("Qt metadata requested while its owner was blocked")
    monkeypatch.setattr(qt, "_qt_input_snapshot", forbidden)
    observed = []
    worker = threading.Thread(target=watchdog.run, daemon=True)

    def blocked_slot():
        clock[0] += 10.0
        worker.start()
        observed.append(captured.wait(4.0))

    QTimer.singleShot(0, app, blocked_slot)
    try:
        app.processEvents()
    finally:
        watchdog.stop()
        if worker.ident is not None:
            worker.join(2.0)
    assert observed == [True]
    assert not worker.is_alive()
    assert not errors
    assert len(blocks(caplog)) == 1
    assert "test_real_qt_block_is_observed_off_thread" in blocks(caplog)[0]


def test_inflight_downstream_hook_triggers_with_healthy_qt(clock, monkeypatch, caplog):
    hook = mouse_hook.MouseHook(lambda *_: None)
    monkeypatch.setattr(mouse_hook, "_live_hooks", weakref.WeakSet([hook]))
    monkeypatch.setattr(mouse_hook, "_perf", lambda: clock[0])
    monkeypatch.setattr(qt, "_mouse_input_snapshot", mouse_hook.stall_snapshot)
    entered, release = threading.Event(), threading.Event()

    def downstream(*_):
        entered.set()
        release.wait(4.0)
        return 42

    monkeypatch.setattr(mouse_hook, "_call_next_hook_ex", downstream, raising=False)
    result = []
    worker = threading.Thread(target=lambda: result.append(hook._hook_callback(-1, 0, 0)),
                              daemon=True)
    watchdog = qt._StallWatchdog()
    try:
        worker.start()
        assert entered.wait(2.0)
        clock[0] += 1.0
        watchdog.heartbeat()
        watchdog.check()
        assert not blocks(caplog)  # strictly longer than one second
        clock[0] += 0.01
        watchdog.heartbeat()
        watchdog.check()
        watchdog.check()
        assert len(blocks(caplog)) == 1
        assert "heartbeat_age_s=0.000" in blocks(caplog)[0]
        assert "callback_running_s" in blocks(caplog)[0]
        assert "last_callback_age_s" in blocks(caplog)[0]
    finally:
        release.set()
        worker.join(2.0)
    assert result == [42]
    assert hook._callback_started_at is None
    assert hook.last_callback_ms == pytest.approx(1010.0)
    assert hook.slow_callbacks == 1
    watchdog.check()
    assert not watchdog._reported
    assert mouse_hook.stall_snapshot()[0]["callback_running_s"] == 0.0


def test_hook_exception_clears_inflight_and_passes_event(clock, monkeypatch):
    hook = mouse_hook.MouseHook(lambda *_: None)
    monkeypatch.setattr(mouse_hook, "_perf", lambda: clock[0])

    def broken(*_):
        clock[0] += 2.0
        raise OSError("synthetic native failure")

    monkeypatch.setattr(mouse_hook, "_call_next_hook_ex", broken, raising=False)
    assert hook._hook_callback(-1, 0, 0) == 0
    assert hook.callback_errors == 1
    assert hook._callback_started_at is None
    assert hook.last_callback_ms == pytest.approx(2000.0)


def test_qt_and_hook_stall_share_one_episode(clock, monkeypatch, caplog):
    hooks = [{"callback_running_s": 2.0}]
    monkeypatch.setattr(qt, "_mouse_input_snapshot", lambda: hooks)
    watchdog = qt._StallWatchdog()
    clock[0] += 10.0
    watchdog.check()
    watchdog.heartbeat()  # Qt recovers but hook is still blocked
    watchdog.check()
    assert len(blocks(caplog)) == 1
    hooks.clear()
    watchdog.check()
    hooks.append({"callback_running_s": 2.0})
    watchdog.check()
    assert len(blocks(caplog)) == 2


def test_metadata_is_class_only_and_handles_failed_queries(monkeypatch):
    widget = SimpleNamespace(metaObject=lambda: SimpleNamespace(className=lambda: "QMenu"))
    monkeypatch.setattr(qt.QApplication, "activeWindow", lambda: widget)
    monkeypatch.setattr(qt.QApplication, "activePopupWidget", lambda: widget)
    def unavailable():
        raise RuntimeError("deleted widget")
    monkeypatch.setattr(qt.QApplication, "activeModalWidget", unavailable)
    monkeypatch.setattr(qt.QWidget, "mouseGrabber", lambda: None)
    assert qt._qt_input_snapshot() == {
        "window": "QMenu", "popup": "QMenu", "modal": "unavailable", "mouse_grabber": None}


def test_async_input_reads_high_bit_for_all_modifier_sides_and_buttons(monkeypatch):
    calls = []
    def get_state(vk):
        calls.append(vk)
        return 0x8000 if vk in {0xA2, 0x05} else 1  # low bit alone is not down
    monkeypatch.setattr(qt, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(qt.ctypes, "windll", SimpleNamespace(
        user32=SimpleNamespace(GetAsyncKeyState=get_state)), raising=False)
    result = qt._async_input_snapshot()
    assert {name for name, down in result.items() if down} == {"left_ctrl", "mouse4"}
    assert set(calls) == {0xA2, 0xA3, 0xA0, 0xA1, 0xA4, 0xA5,
                          0x5B, 0x5C, 1, 2, 4, 5, 6}


def test_observer_tolerates_absent_or_partially_imported_hook_module(monkeypatch):
    monkeypatch.delitem(sys.modules, "samsara.mouse_hook")
    assert qt._mouse_input_snapshot() == ()
    monkeypatch.setitem(sys.modules, "samsara.mouse_hook", SimpleNamespace())
    assert qt._mouse_input_snapshot() == ()
    monkeypatch.setitem(sys.modules, "samsara.mouse_hook", SimpleNamespace(
        stall_snapshot=lambda: ({"callback_running_s": 2.0},)))
    assert qt._mouse_input_snapshot() == ({"callback_running_s": 2.0},)


def test_dump_failure_still_logs_metadata(clock, monkeypatch, caplog):
    def fail(**_):
        raise OSError("synthetic failure")
    monkeypatch.setattr(qt.faulthandler, "dump_traceback", fail)
    qt._report_stall(10.0, {"popup": None}, ())
    assert "traceback unavailable: OSError" in blocks(caplog)[0]


def test_observer_waits_one_second_and_stops(clock):
    watchdog = qt._StallWatchdog()
    waits = []
    watchdog._stop = SimpleNamespace(wait=lambda seconds: waits.append(seconds) or True)
    watchdog.run()
    assert waits == [1.0]
