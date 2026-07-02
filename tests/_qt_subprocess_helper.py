"""Shared helper for the test_qt_runtime_phase*.py subprocess-isolation tests.

Not collected by pytest (module name doesn't match python_files = test_*.py).

Why these tests shell out to a subprocess instead of running in-process, and
why success is judged by stdout content instead of the subprocess exit code,
is explained in test_qt_runtime_phase1.py's module docstring. Short version:
qt_runtime.py deliberately constructs and exec()s its QApplication on a
dedicated non-main "samsara-qt" thread (see its _run_loop() docstring: runs
"exactly once per process"). That is fine for the whole life of the process,
but on this Windows/PySide6 build it reproducibly segfaults during CPython
interpreter finalization *after* qt_runtime.shutdown() has already joined the
thread cleanly and every check has already printed PASS -- confirmed by
running these scripts directly (not through pytest) and capturing the real
exit code without a shell pipe silently discarding it (`prog; echo $?`, not
`prog | tail; echo $?`, which reports tail's exit code). This is a
pre-existing characteristic of constructing/exec()ing a QApplication on a
non-main thread, not a regression from adding subprocess isolation, and not
something to paper over by changing qt_runtime.py's threading model without
that being a deliberate, separately-flagged decision.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]


def run_isolated(script_path: Path, success_marker: str, timeout: float = 60.0):
    """Run script_path in a fresh subprocess and assert it did its job.

    Success is: `success_marker` appears in stdout AND stderr has no
    Python traceback. The subprocess's return code is NOT the pass/fail
    signal -- see module docstring for why a clean 0 isn't achievable here.
    A non-zero code with the marker present and no traceback is logged,
    not failed.
    """
    result = subprocess.run(
        [sys.executable, str(script_path.resolve())],
        cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=timeout,
    )
    has_marker    = success_marker in result.stdout
    has_traceback = "Traceback (most recent call last)" in result.stderr
    if result.returncode != 0:
        print(
            f"[qt_subprocess_helper] {script_path.name} exited with code "
            f"{result.returncode} (non-zero) -- known benign Qt/PySide6 "
            f"teardown crash on this build when a QApplication is exec()'d "
            f"off the main thread; ignoring since the marker was found and "
            f"stderr has no traceback."
        )
    assert has_marker and not has_traceback, (
        f"isolated run of {script_path.name} did not complete successfully "
        f"(exit {result.returncode}, marker found={has_marker}, "
        f"traceback found={has_traceback}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
