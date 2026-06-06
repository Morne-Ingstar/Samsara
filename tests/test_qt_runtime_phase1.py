"""Phase 1 smoke test: qt_runtime starts, reports ready, post() runs on Qt thread."""

import sys
import threading
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))

from samsara.ui import qt_runtime


def test_ensure_started_idempotent():
    qt_runtime.ensure_started()
    qt_runtime.ensure_started()  # second call must not raise or hang
    assert qt_runtime.is_alive(), "is_alive() must be True after ensure_started()"
    print("PASS: ensure_started is idempotent, is_alive() True")


def test_post_runs_on_qt_thread():
    result = {}
    done   = threading.Event()

    def _cb():
        result["thread"] = threading.current_thread().name
        done.set()

    qt_runtime.post(_cb)
    assert done.wait(timeout=3.0), "post() callback did not fire within 3 s"
    assert result["thread"] == "samsara-qt", (
        f"callback ran on {result['thread']!r}, expected 'samsara-qt'"
    )
    print(f"PASS: post() callback ran on thread '{result['thread']}'")


def test_post_multiple_callbacks_ordered():
    order  = []
    done   = threading.Event()

    def _make(n, last=False):
        def _cb():
            order.append(n)
            if last:
                done.set()
        return _cb

    for i in range(5):
        qt_runtime.post(_make(i, last=(i == 4)))

    assert done.wait(timeout=3.0), "callbacks did not all fire within 3 s"
    assert order == [0, 1, 2, 3, 4], f"unexpected order: {order}"
    print(f"PASS: 5 callbacks fired in order: {order}")


if __name__ == "__main__":
    test_ensure_started_idempotent()
    test_post_runs_on_qt_thread()
    test_post_multiple_callbacks_ordered()
    print("\nAll Phase 1 checks passed.")
    qt_runtime.shutdown(timeout=3.0)
    sys.exit(0)
