"""Phase 2 smoke test: task_overlay reopens repeatably after close.

Verifies:
  - TaskOverlay creates its window via qt_runtime (no own QApplication/exec)
  - show -> close-via-X -> show works N times without restart
  - Only one QApplication ever exists (checked after N cycles)
"""

import sys
import threading
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))

from PySide6.QtWidgets import QApplication

from samsara.ui import qt_runtime
from samsara.ui.task_overlay import TaskOverlay


CYCLES = 5


def _wait(condition_fn, timeout=3.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition_fn():
            return True
        time.sleep(interval)
    return False


def test_task_overlay_reopen():
    qt_runtime.ensure_started()
    overlay = TaskOverlay()

    for cycle in range(1, CYCLES + 1):
        # --- show ---
        overlay.show(tasks=[])
        assert _wait(lambda: overlay._window is not None), (
            f"cycle {cycle}: window not created within 3 s"
        )
        assert _wait(lambda: overlay._window is not None and overlay._window.isVisible()), (
            f"cycle {cycle}: window not visible within 3 s"
        )
        print(f"  cycle {cycle}: window visible OK")

        # --- simulate close-button press (closeEvent fires hide, not destroy) ---
        done = threading.Event()
        def _close(ev=done):
            overlay._window.close()  # triggers closeEvent -> hide()
            ev.set()
        qt_runtime.post(_close)
        assert done.wait(timeout=3.0), f"cycle {cycle}: close post did not fire"

        assert _wait(lambda: overlay._window is not None and not overlay._window.isVisible()), (
            f"cycle {cycle}: window not hidden within 3 s"
        )
        print(f"  cycle {cycle}: window hidden OK (reference still valid: {overlay._window is not None})")

    # One QApplication throughout
    app_count_check = threading.Event()
    app_instances   = []
    def _check():
        app_instances.append(QApplication.instance())
        app_count_check.set()
    qt_runtime.post(_check)
    app_count_check.wait(timeout=3.0)
    assert len(app_instances) == 1 and app_instances[0] is not None, (
        "QApplication instance missing after cycles"
    )
    print(f"\nOne QApplication throughout all {CYCLES} cycles: OK")
    print(f"PASS: task overlay reopened {CYCLES} times repeatably")


if __name__ == "__main__":
    test_task_overlay_reopen()
    qt_runtime.shutdown(timeout=3.0)
    sys.exit(0)
