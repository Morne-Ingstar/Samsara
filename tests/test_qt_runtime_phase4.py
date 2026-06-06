"""Phase 4 regression tests for the qt_runtime centralisation.

Acceptance criteria (from the original code prompt):
  1. show_numbers overlay renders with NO other Qt window open at startup.
  2. task_overlay reopens repeatably after close (extended regression, 10 cycles).
  3. App startup/shutdown lifecycle: runtime comes up clean, shuts down cleanly,
     is_alive() reflects state correctly.

Run:
    python tests/test_qt_runtime_phase4.py

All tests share one runtime lifecycle (start once, shutdown at the end).
"""

import sys
import threading
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))

from PySide6.QtWidgets import QApplication

from samsara.ui import qt_runtime
from samsara.ui.numbers_overlay_qt import NumbersOverlayWindow
from samsara.ui.task_overlay import TaskOverlay


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait(fn, timeout: float = 3.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


def _post_wait(cb, timeout: float = 3.0) -> bool:
    """Post cb to the Qt thread and block until it fires."""
    done = threading.Event()
    def _wrapped():
        cb()
        done.set()
    qt_runtime.post(_wrapped)
    return done.wait(timeout=timeout)


# ---------------------------------------------------------------------------
# Test 1: show_numbers renders with NO other Qt window open
#
# Root cause of the original bug: _draw_overlay() called
# QApplication.instance() at dispatch time. If no other component had started
# a Qt loop, instance() returned None and the overlay silently dropped.
#
# After the fix, qt_runtime is always running before any command fires, so
# post() always reaches the Qt thread.  This test verifies that window
# creation succeeds even when topLevelWidgets() is empty beforehand.
# ---------------------------------------------------------------------------

def test_numbers_overlay_no_prior_window() -> None:
    qt_runtime.ensure_started()

    # Verify no top-level widgets exist yet (clean slate).
    check = {}
    assert _post_wait(lambda: check.update(
        before=list(QApplication.instance().topLevelWidgets())
    )), "Qt thread did not respond for pre-check"
    assert check["before"] == [], (
        f"Expected no windows before test; found {check['before']}"
    )
    print("  pre-condition: 0 top-level widgets — OK")

    # Hardcoded test labels (same coords used in the existing diagnostic).
    labels = [
        [100, 100, 40, 30, "1"],
        [200, 100, 40, 30, "2"],
        [300, 100, 40, 30, "3"],
    ]

    window_holder: list = []

    def _create():
        win = NumbersOverlayWindow(labels)
        win.show()
        window_holder.append(win)

    qt_runtime.post(_create)
    assert _wait(lambda: bool(window_holder)), "NumbersOverlayWindow not created within 3 s"
    win = window_holder[0]
    assert _wait(lambda: win.isVisible()), "NumbersOverlayWindow not visible within 3 s"
    print(f"  NumbersOverlayWindow visible with {len(labels)} labels — OK")

    # Tear down for subsequent tests.
    assert _post_wait(lambda: win.close()), "close() did not fire"
    assert _post_wait(lambda: win.deleteLater()), "deleteLater() did not fire"
    # Give Qt one cycle to process deleteLater.
    time.sleep(0.1)
    print("PASS: show_numbers overlay shows with no prior Qt window")


# ---------------------------------------------------------------------------
# Test 2: task_overlay reopens repeatably — extended regression (10 cycles)
#
# The original bug: second close destroyed the widget; subsequent show() tried
# to call methods on a deleted C++ object -> crash.  HIDE policy keeps the
# window alive so show → close → show works indefinitely.
# ---------------------------------------------------------------------------

REOPEN_CYCLES = 10

def test_task_overlay_reopen_regression() -> None:
    overlay = TaskOverlay()

    for cycle in range(1, REOPEN_CYCLES + 1):
        # Trigger show (posts _init_window on first cycle).
        overlay.show(tasks=[])

        assert _wait(lambda: overlay._window is not None), (
            f"cycle {cycle}: window not created within 3 s"
        )
        assert _wait(lambda: overlay._window is not None and overlay._window.isVisible()), (
            f"cycle {cycle}: window not visible within 3 s"
        )

        # Simulate close-button: fires closeEvent -> hide() (not destroy).
        closed = threading.Event()
        def _close(ev=closed):
            overlay._window.close()
            ev.set()
        qt_runtime.post(_close)
        assert closed.wait(timeout=3.0), f"cycle {cycle}: close post timed out"

        assert _wait(lambda: overlay._window is not None and not overlay._window.isVisible()), (
            f"cycle {cycle}: window not hidden within 3 s"
        )

        # Window reference MUST survive close (HIDE, not destroy).
        assert overlay._window is not None, (
            f"cycle {cycle}: window reference lost after close (destroy happened)"
        )

    print(f"PASS: task_overlay reopened {REOPEN_CYCLES} times without restart")


# ---------------------------------------------------------------------------
# Test 3: startup/shutdown lifecycle
#
# Verifies:
#   - is_alive() is True while running
#   - post() works right up to shutdown
#   - shutdown() joins cleanly (thread no longer alive)
#   - is_alive() is False after shutdown
#   - One QApplication throughout
# ---------------------------------------------------------------------------

def test_startup_shutdown_lifecycle() -> None:
    assert qt_runtime.is_alive(), "runtime should still be alive at test-3 start"

    # Confirm QApplication is healthy.
    app_check: list = []
    assert _post_wait(lambda: app_check.append(QApplication.instance())), (
        "Qt thread unresponsive before shutdown"
    )
    assert app_check[0] is not None, "QApplication.instance() is None mid-run"
    print(f"  QApplication alive: {app_check[0]} — OK")

    # Fire a last batch of callbacks to stress the pre-shutdown queue.
    order: list = []
    batch_done = threading.Event()

    def _make(n, last=False):
        def _cb():
            order.append(n)
            if last:
                batch_done.set()
        return _cb

    for i in range(8):
        qt_runtime.post(_make(i, last=(i == 7)))

    assert batch_done.wait(timeout=5.0), "batch callbacks did not fire before shutdown"
    assert order == list(range(8)), f"callback order wrong: {order}"
    print(f"  {len(order)} pre-shutdown callbacks fired in order — OK")

    # Now shut down.
    qt_runtime.shutdown(timeout=5.0)

    assert not qt_runtime.is_alive(), "is_alive() should be False after shutdown"
    print("  is_alive() False after shutdown — OK")

    # The runtime thread must have joined.
    thr = qt_runtime._thread
    assert thr is not None, "internal _thread reference is None"
    assert not thr.is_alive(), "samsara-qt thread still alive after shutdown"
    print(f"  samsara-qt thread joined cleanly — OK")

    print("PASS: startup/shutdown lifecycle clean")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("--- Phase 4: Regression tests ---\n")

    print("Test 1: show_numbers overlay renders with no prior Qt window")
    test_numbers_overlay_no_prior_window()
    print()

    print(f"Test 2: task_overlay reopens repeatably ({REOPEN_CYCLES} cycles)")
    test_task_overlay_reopen_regression()
    print()

    print("Test 3: startup/shutdown lifecycle")
    test_startup_shutdown_lifecycle()
    print()

    print("All Phase 4 regression tests PASSED.")
    # qt_runtime.shutdown() was called inside test 3; do not call again.
    sys.exit(0)
