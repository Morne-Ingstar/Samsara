"""Phase 1 smoke test: qt_runtime starts, reports ready, post() runs on Qt thread.

qt_runtime owns exactly one non-daemon "samsara-qt" thread that constructs
the process's single QApplication and runs exec() on it (see
samsara/ui/qt_runtime.py -- _run_loop() is documented to run "exactly once
per process"). That model only works in a process where nothing else has
already constructed a QApplication on a different thread first: PySide6
raises "Please destroy the QApplication singleton before creating a new
QApplication instance" if _run_loop() tries to build a second one, which
silently kills the samsara-qt thread before it ever calls _ready.set() --
manifesting here as "did not become ready within 10 seconds".

Under pytest's full suite, tests/conftest.py's session-scoped `qapp` fixture
(used by e.g. test_command_mode.py, which sorts before this file) may
already have constructed a QApplication on the *main* pytest thread by the
time this module's checks would run in-process, poisoning qt_runtime's
dedicated-thread model for the rest of the session. Rather than fight that
cross-file ordering dependency (or weaken qt_runtime.py's one-QApplication
contract to accommodate it), each check below runs inside a fresh
subprocess -- a brand-new process has no pre-existing QApplication, so this
always matches how the module is designed to run standalone
(`python tests/test_qt_runtime_phase1.py`), regardless of what ran before it
in the same pytest session.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parent))  # for the bare _qt_subprocess_helper import below

from samsara.ui import qt_runtime
from _qt_subprocess_helper import run_isolated


def check_ensure_started_idempotent():
    qt_runtime.ensure_started()
    qt_runtime.ensure_started()  # second call must not raise or hang
    assert qt_runtime.is_alive(), "is_alive() must be True after ensure_started()"
    print("PASS: ensure_started is idempotent, is_alive() True")


def check_post_runs_on_qt_thread():
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


def check_post_multiple_callbacks_ordered():
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


def test_phase1_in_isolated_subprocess():
    """Run this file's own __main__ checks in a clean subprocess.

    See module docstring: qt_runtime's dedicated-thread QApplication model
    requires no pre-existing QApplication in the process, which the shared
    pytest process running the full suite cannot guarantee. See
    _qt_subprocess_helper.py for why success is judged by the printed
    completion marker rather than the subprocess's own exit code.
    """
    run_isolated(Path(__file__), "All Phase 1 checks passed.")


if __name__ == "__main__":
    check_ensure_started_idempotent()
    check_post_runs_on_qt_thread()
    check_post_multiple_callbacks_ordered()
    print("\nAll Phase 1 checks passed.")
    qt_runtime.shutdown(timeout=3.0)
    sys.exit(0)
