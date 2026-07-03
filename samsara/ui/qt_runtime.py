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

import logging
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

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
    log.debug(
        "[WIZ-DIAG] post(): state=RUNNING, scheduling %r, calling thread ident=%s",
        cb, threading.get_ident(),
    )
    QTimer.singleShot(0, app, cb)
    log.debug("[WIZ-DIAG] post(): QTimer.singleShot() call returned")


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
    global _app, _state
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    # processEvents() pumps any immediate init events so callers can begin
    # posting work as soon as _ready fires.
    app.processEvents()
    with _lock:
        _app   = app
        _state = _RUNNING
    _ready.set()
    log.debug("samsara-qt: event loop started")
    app.exec()
    with _lock:
        _state = _STOPPED
    log.debug("samsara-qt: event loop exited")
