"""Queue 87: the crash diagnostics must never be silent about their own gaps.

The fourth access violation in 24 hours produced this, and nothing else:

    Windows fatal exception: access violation

    --- Samsara start pid=18008 at 2026-09-15 16:16:57 ---

The frames were not lost to buffering, a closed handle, or all_threads being
off. CPython's Windows exception handler writes the header unconditionally
and then calls faulthandler_dump_traceback(), which returns immediately while
its static `reentrant` flag is set by a dump already in progress. A dump WAS
in progress: the entry above it in faulthandler.log stops mid-thread-block.

So the shape on disk is diagnosable, and these tests pin that:
  * a real induced fault still yields frames, on the main thread and on a
    worker thread (the shape the live crashes actually take);
  * the summary names the LAST fault -- the one the process died on -- not
    the first non-COM one;
  * a suppressed dump produces an explicit sentence saying why, instead of
    a fault count and silence;
  * none of it depends on stdout/stderr, which are a null stream in the
    windowless build.
Only samsara.boot is imported (stdlib-only at import); never dictation.py.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samsara import boot  # noqa: E402


def _run(code, cwd, **popen):
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=str(cwd),
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": str(ROOT)}, **popen)


# ---------------------------------------------------------------------------
# 1. A real fault still produces frames
# ---------------------------------------------------------------------------

def test_an_induced_native_fault_produces_a_thread_list_and_frames(tmp_path):
    proc = _run(f"""
        import os, faulthandler
        from pathlib import Path
        from samsara import boot
        boot._enable_faulthandler(Path(r"{tmp_path}"))
        print(os.getpid(), flush=True)
        def crash_here():
            faulthandler._sigsegv()
        crash_here()
    """, tmp_path)

    assert proc.returncode != 0
    pid = int(proc.stdout.split()[0])
    dump = (tmp_path / "faulthandler.log").read_text(encoding="utf-8")
    assert f"--- Samsara start pid={pid} at " in dump
    assert "Thread 0x" in dump or "Current thread" in dump, "no thread list"
    assert "crash_here" in dump, "no frames"

    summary = boot.summarize_faulthandler_dumps(tmp_path / "faulthandler.log", pid)
    assert summary["fatal"], summary
    assert any("crash_here" in frame for frame in summary["stack"]), summary
    assert summary["note"] == "", summary          # nothing to explain away


def test_a_real_access_violation_on_a_worker_thread_is_summarised(tmp_path):
    """The live crashes are access violations on a worker thread -- the Qt
    event loop runs on one of its own -- so that is what this induces.

    NOT faulthandler._sigsegv(), which queue 48's test used: on Windows that
    goes through the CRT's raise(SIGSEGV), and off the main thread it writes
    NOTHING AT ALL (verified: the log keeps only the start marker, exit code
    3). A real memory fault raises a structured exception and reaches the
    vectored exception handler faulthandler installs -- the path every one of
    the four real crashes actually took. The old test passed while testing a
    path the app never takes.
    """
    proc = _run(f"""
        import ctypes, os, threading
        from pathlib import Path
        from samsara import boot
        boot._enable_faulthandler(Path(r"{tmp_path}"))
        print(os.getpid(), flush=True)
        def worker_that_dies():
            ctypes.memset(0x1, 0, 4)          # a real access violation
        t = threading.Thread(target=worker_that_dies, name="worker")
        t.start()
        t.join(10)
    """, tmp_path)

    pid = int(proc.stdout.split()[0])
    dump = (tmp_path / "faulthandler.log").read_text(encoding="utf-8")
    assert "Windows fatal exception: access violation" in dump
    assert "Current thread" in dump, "no thread list for a worker-thread fault"
    assert "worker_that_dies" in dump, "no frames for a worker-thread fault"

    summary = boot.summarize_faulthandler_dumps(tmp_path / "faulthandler.log", pid)
    assert summary["fatal"] == "Windows fatal exception: access violation", summary
    assert any("worker_that_dies" in f for f in summary["stack"]), summary
    assert summary["note"] == "", summary


@pytest.mark.skipif(sys.platform != "win32", reason="Windows exception handling")
def test_sigsegv_off_the_main_thread_records_nothing_on_windows(tmp_path):
    """Pins the reason the test above does not use faulthandler._sigsegv().

    If a future CPython starts recording this, the skip can go -- but the app
    must never rely on it, because a real fault does not arrive this way.
    """
    proc = _run(f"""
        import faulthandler, os, threading
        from pathlib import Path
        from samsara import boot
        boot._enable_faulthandler(Path(r"{tmp_path}"))
        print(os.getpid(), flush=True)
        t = threading.Thread(target=faulthandler._sigsegv, name="worker")
        t.start()
        t.join(10)
    """, tmp_path)

    dump = (tmp_path / "faulthandler.log").read_text(encoding="utf-8")
    assert proc.returncode != 0
    assert "Fatal Python error" not in dump and "Thread 0x" not in dump


# ---------------------------------------------------------------------------
# 2. The diagnostics do not depend on stdout/stderr
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sabotage", [
    "sys.stderr.close(); sys.stdout.close()",
    "sys.stderr = None; sys.stdout = None",
    "import io; sys.stderr = io.StringIO(); sys.stdout = io.StringIO()",
])
def test_faults_are_still_captured_with_stdout_and_stderr_broken(tmp_path, sabotage):
    """The windowless build runs with a null stderr; a redirect or a close
    must not cost the only record of a native death."""
    marker = tmp_path / "pid.txt"
    proc = _run(f"""
        import os, sys, faulthandler
        from pathlib import Path
        from samsara import boot
        boot._enable_faulthandler(Path(r"{tmp_path}"))
        Path(r"{marker}").write_text(str(os.getpid()))
        {sabotage}
        def crash_here():
            faulthandler._sigsegv()
        crash_here()
    """, tmp_path)

    assert proc.returncode != 0
    pid = int(marker.read_text())
    dump = (tmp_path / "faulthandler.log").read_text(encoding="utf-8")
    assert "crash_here" in dump, "frames lost when stdout/stderr were broken"
    summary = boot.summarize_faulthandler_dumps(tmp_path / "faulthandler.log", pid)
    assert any("crash_here" in f for f in summary["stack"]), summary


def test_enable_never_raises_when_the_log_cannot_be_opened(tmp_path):
    """A diagnostic must not stop boot."""
    blocker = tmp_path / "logs"
    blocker.write_text("a file where the log directory should be", encoding="utf-8")
    assert boot._enable_faulthandler(blocker) is None


# ---------------------------------------------------------------------------
# 3. A suppressed dump explains itself
# ---------------------------------------------------------------------------

#: The exact shape the 2026-09-15 16:16 crash left on disk: a dump cut off
#: mid-thread-block, then a header with no frames at all.
SUPPRESSED = """\
--- Samsara start pid=16396 at 2026-09-15 16:06:26 ---
Windows fatal exception: code 0x80010108

Thread 0x00001b64 (most recent call first):
  File "threading.py", line 331 in wait
  File "dictation.py", line 1605 in _wrapped_run

Current thread 0x00002eb8 (most recent call first):
  File "qt_runtime.py", line 150 in _run_loop
  File "threading.py", line 982 in run

Windows fatal exception: code 0x80010108

Thread 0x0000437c (most recent call first):
  File "threading.py", line 327 in wait
  File "thread_registry.py", line 154 in timer
Windows fatal exception: access violation

--- Samsara start pid=18008 at 2026-09-15 16:16:57 ---
"""


def test_a_suppressed_dump_says_why_it_has_no_frames(tmp_path):
    log = tmp_path / "faulthandler.log"
    log.write_text(SUPPRESSED, encoding="utf-8")

    summary = boot.summarize_faulthandler_dumps(log, 16396)

    assert summary["fatal"] == "Windows fatal exception: access violation"
    assert summary["stack"] == []
    # The deliverable: never silent about the gap.
    assert summary["note"], "a fault with no frames must say why"
    assert "reentrancy guard" in summary["note"]
    assert "still being written" in summary["note"]


def test_the_summary_names_the_fault_it_died_on_not_the_first(tmp_path):
    """Queue 48 reported the first fault that was not a COM disconnect. The
    one that matters is the LAST."""
    log = tmp_path / "faulthandler.log"
    log.write_text(SUPPRESSED, encoding="utf-8")
    summary = boot.summarize_faulthandler_dumps(log, 16396)
    assert summary["fatal"] == "Windows fatal exception: access violation"
    assert summary["events"]["Windows fatal exception: code 0x80010108"] == 2
    assert summary["com_disconnects"] == 2


def test_a_complete_dump_is_not_reported_as_suppressed(tmp_path):
    """The truncation test must not fire on a healthy dump."""
    log = tmp_path / "faulthandler.log"
    log.write_text(
        "--- Samsara start pid=1 at 2026-01-01 00:00:00 ---\n"
        "Windows fatal exception: code 0x80010108\n\n"
        "Current thread 0x0001 (most recent call first):\n"
        '  File "qt_runtime.py", line 150 in _run_loop\n'
        "\n"
        "Windows fatal exception: access violation\n\n"
        "Current thread 0x0001 (most recent call first):\n"
        '  File "qt_runtime.py", line 150 in _run_loop\n'
        "\n"
        "--- Samsara start pid=2 at 2026-01-01 00:01:00 ---\n",
        encoding="utf-8")

    summary = boot.summarize_faulthandler_dumps(log, 1)

    assert summary["note"] == "", summary
    assert any("_run_loop" in f for f in summary["stack"]), summary


def test_a_fault_with_threads_but_no_current_marker_says_the_stack_is_context(tmp_path):
    """Without a "Current thread" block the faulting thread cannot be named.
    Presenting some other thread's stack as the crash site would be worse
    than saying nothing."""
    log = tmp_path / "faulthandler.log"
    log.write_text(
        "--- Samsara start pid=3 at 2026-01-01 00:00:00 ---\n"
        "Windows fatal exception: access violation\n\n"
        "Thread 0x0001 (most recent call first):\n"
        '  File "alarms.py", line 521 in _check_loop\n'
        "\n"
        "--- Samsara start pid=4 at 2026-01-01 00:01:00 ---\n",
        encoding="utf-8")

    summary = boot.summarize_faulthandler_dumps(log, 3)

    assert summary["stack"] == []
    assert "no Python state" in summary["note"]
    assert "context" in summary["note"]
    assert any("_check_loop" in t for t in summary["threads"])


def test_a_missing_file_or_section_is_not_an_error(tmp_path):
    assert boot.summarize_faulthandler_dumps(tmp_path / "nope.log", 1) == {}
    log = tmp_path / "faulthandler.log"
    log.write_text("--- Samsara start pid=9 at 2026-01-01 00:00:00 ---\n", encoding="utf-8")
    assert boot.summarize_faulthandler_dumps(log, 9)["events"] == {}
    assert boot.summarize_faulthandler_dumps(log, 999) == {}


# ---------------------------------------------------------------------------
# 4. The crash notice carries the explanation
# ---------------------------------------------------------------------------

def test_the_next_start_writes_the_reason_into_the_log(tmp_path, caplog):
    import json
    (tmp_path / "faulthandler.log").write_text(SUPPRESSED, encoding="utf-8")
    (tmp_path / boot.SESSION_MARKER).write_text(
        json.dumps({"pid": 16396, "started": "2026-09-15 16:06:26"}), encoding="utf-8")

    with caplog.at_level("WARNING", logger="Samsara"):
        boot.begin_session(tmp_path)

    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "[CRASH-DIAG]" in message
    assert "died on: Windows fatal exception: access violation" in message
    assert "handled COM disconnect" in message
    assert "reentrancy guard" in message


# ---------------------------------------------------------------------------
# 5. The implicated teardown path closes on the thread that owns the widget
# ---------------------------------------------------------------------------

def test_the_streaming_overlay_is_destroyed_on_the_qt_thread(monkeypatch):
    """Two of the three access violations with frames had a worker thread
    inside _update_streaming_preview while the Qt loop faulted. That path
    already marshals its destroy; this pins it so it cannot regress into the
    cross-thread destroy that caused an earlier crash."""
    pytest.importorskip("PySide6")
    from samsara import streaming

    posted = []
    overlay = streaming.StreamingOverlayQt.__new__(streaming.StreamingOverlayQt)
    overlay._post = posted.append
    overlay._dispose_on_qt = lambda: posted.append("DIRECT-CALL")

    streaming.StreamingOverlayQt.close(overlay)

    assert posted, "close() did nothing"
    assert "DIRECT-CALL" not in posted, "the widget was destroyed on the calling thread"
    assert posted[0] is overlay._dispose_on_qt
