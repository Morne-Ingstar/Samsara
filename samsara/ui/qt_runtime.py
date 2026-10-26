"""Centralised Qt event-loop runtime for Samsara.

Exactly one persistent non-daemon thread ("samsara-qt") owns the single
QApplication and runs exec().  All Qt widget construction must happen on
that thread; use post() to schedule work there from any other thread.

Public API
----------
ensure_started()          -- start the runtime (idempotent; blocks until ready)
post(callable)            -- marshal a zero-delay callback onto the Qt thread
is_alive() -> bool        -- True while the event loop is running
shutdown(timeout=5.0)     -- hide windows, quit loop, join thread
"""

import ctypes
import faulthandler
import logging
import sys
import tempfile
import threading
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from samsara.runtime import thread_registry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_IDLE     = 0
_STARTING = 1
_RUNNING  = 2
_SHUTTING = 3
_STOPPED  = 4

_lock:   threading.Lock                = threading.Lock()
_ready:  threading.Event               = threading.Event()
_state:  int                           = _IDLE
_thread: "threading.Thread | None"     = None
_app:    "QApplication | None"         = None
_watchdog = None
_ready_callbacks = []


def _qt_input_snapshot():
    """Qt-thread-only metadata; never retain widget wrappers or user text."""
    result = {}
    for name, query in (
        ("window", QApplication.activeWindow),
        ("popup", QApplication.activePopupWidget),
        ("modal", QApplication.activeModalWidget),
        ("mouse_grabber", QWidget.mouseGrabber),
    ):
        try:
            widget = query()
            result[name] = None if widget is None else widget.metaObject().className()
        except Exception:
            result[name] = "unavailable"
    return result


def _mouse_input_snapshot():
    # Do not load/install hooks just to observe them. No hook-thread calls,
    # locks, native round trips, or waits are involved in this snapshot.
    module = sys.modules.get("samsara.mouse_hook")
    snapshot = getattr(module, "stall_snapshot", None)
    return snapshot() if snapshot is not None else ()


def _async_input_snapshot():
    if sys.platform != "win32":
        return "unavailable"
    try:
        get_state = ctypes.windll.user32.GetAsyncKeyState
        keys = {"left_ctrl": 0xA2, "right_ctrl": 0xA3,
                "left_shift": 0xA0, "right_shift": 0xA1,
                "left_alt": 0xA4, "right_alt": 0xA5,
                "left_win": 0x5B, "right_win": 0x5C,
                "left_button": 0x01, "right_button": 0x02,
                "middle_button": 0x04, "mouse4": 0x05, "mouse5": 0x06}
        return {name: bool(get_state(vk) & 0x8000) for name, vk in keys.items()}
    except Exception:
        return "unavailable"


def _report_stall(age, qt_state, hooks):
    """Emit one record via the existing rotating live-log handler chain."""
    try:
        # faulthandler requires a real fd. Capture then log as ONE record, so
        # rotation cannot split the header from its dump or strand a stale fd.
        with tempfile.TemporaryFile() as trace:
            faulthandler.dump_traceback(file=trace, all_threads=True)
            trace.seek(0)
            frames = trace.read().decode("utf-8", errors="replace")
    except Exception as exc:
        frames = "traceback unavailable: " + type(exc).__name__
    log.error("[QT-STALL] heartbeat_age_s=%.3f qt_last_serviced=%r "
              "mouse_hooks=%r async_input=%r\n%s",
              age, qt_state, hooks, _async_input_snapshot(), frames)


class _StallWatchdog:
    """A Qt heartbeat and an independent observer of Qt AND hook stalls.

    Widget queries run only in the heartbeat. During a blocked loop the
    observer reports that last serviced snapshot, without waiting for Qt.
    Neither diagnostic path attempts recovery or synthesizes input.
    """

    def __init__(self):
        self._heartbeat = (time.monotonic(), {})
        self._stop = threading.Event()
        self._reported = False

    def heartbeat(self):
        self._heartbeat = (time.monotonic(), _qt_input_snapshot())

    def check(self):
        stamp, qt_state = self._heartbeat
        age = time.monotonic() - stamp
        hooks = _mouse_input_snapshot()
        stalled = age >= 10.0 or any(h["callback_running_s"] > 1.0 for h in hooks)
        if not stalled:
            self._reported = False
        elif not self._reported:
            self._reported = True
            _report_stall(age, qt_state, hooks)

    def run(self):
        while not self._stop.wait(1.0):
            self.check()

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_started() -> None:
    """Start the Qt runtime thread and block until the event loop is pumping.

    Idempotent — safe to call repeatedly from multiple threads.
    Raises RuntimeError if called after shutdown().
    """
    global _state, _thread
    with _lock:
        if _state == _RUNNING:
            return
        if _state == _STARTING:
            pass  # already starting; fall through to wait on _ready
        elif _state == _IDLE:
            _state = _STARTING
            _thread = threading.Thread(
                target=_run_loop, daemon=False, name="samsara-qt"
            )
            _thread.start()
            thread_registry.register(_thread, "samsara-qt")
        else:
            raise RuntimeError(
                "Qt runtime has already been shut down and cannot be restarted"
            )
    if not _ready.wait(timeout=10.0):
        raise RuntimeError("Qt runtime did not become ready within 10 seconds")


def post(cb) -> None:
    """Post a callable onto the Qt event loop via a zero-delay timer.

    Uses QTimer.singleShot(0, app, cb) — the 3-arg form that binds the
    callback lifetime to the QApplication object so it fires on the Qt
    thread.  Silently drops cb and logs a warning if the runtime is not
    in RUNNING state.
    """
    with _lock:
        if _state != _RUNNING:
            log.warning("qt_runtime.post: dropping callback (state=%d) %r", _state, cb)
            return
        app = _app
    QTimer.singleShot(0, app, cb)


def post_when_ready(cb) -> bool:
    """Retain startup feedback until the UI loop runs, without starting it."""
    with _lock:
        if _state in (_IDLE, _STARTING):
            _ready_callbacks.append(cb)
            return True
        if _state != _RUNNING:
            return False
    post(cb)
    return True


def is_alive() -> bool:
    """Return True while the Qt event loop is active."""
    with _lock:
        return _state == _RUNNING


def shutdown(timeout: float = 5.0) -> None:
    """Orderly shutdown: hide all windows, quit the loop, join the thread.

    Transitions to SHUTTING immediately so post() drops any subsequent
    work.  The actual quit sequence is posted to the Qt thread so all Qt
    object access stays on the correct thread.
    """
    global _state
    with _lock:
        if _state != _RUNNING:
            return
        _state = _SHUTTING
        app   = _app
        thr   = _thread
        watchdog = _watchdog

    if watchdog is not None:
        watchdog.stop()

    def _do_quit():
        for w in app.topLevelWidgets():
            w.hide()
        app.quit()

    QTimer.singleShot(0, app, _do_quit)

    if thr is not None:
        thr.join(timeout=timeout)
        if thr.is_alive():
            log.warning(
                "samsara-qt thread did not stop within %.1f s", timeout
            )


# ---------------------------------------------------------------------------
# Runtime thread
# ---------------------------------------------------------------------------

def _run_loop() -> None:
    """Entry point for the samsara-qt thread.  Runs exactly once per process."""
    global _app, _state, _watchdog
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    # processEvents() pumps any immediate init events so callers can begin
    # posting work as soon as _ready fires.
    app.processEvents()
    watchdog = _StallWatchdog()
    heartbeat = QTimer(app)
    heartbeat.setInterval(1000)
    heartbeat.timeout.connect(watchdog.heartbeat)
    watchdog.heartbeat()
    heartbeat.start()
    with _lock:
        _app   = app
        _state = _RUNNING
        _watchdog = watchdog
        callbacks = list(_ready_callbacks)
        _ready_callbacks.clear()
    for callback in callbacks:
        post(callback)
    thread_registry.spawn("qt-stall-watchdog", watchdog.run, daemon=True)
    _ready.set()
    log.debug("samsara-qt: event loop started")
    try:
        app.exec()
    finally:
        watchdog.stop()
        heartbeat.stop()
        heartbeat.deleteLater()
        with _lock:
            _watchdog = None
            _state = _STOPPED
    log.debug("samsara-qt: event loop exited")
