"""Queue 48: the app records its own death.

  * faulthandler writes a timestamped start marker and a native-stack dump
    for a deliberately induced fault (subprocess -- never the live app)
  * the next start notices the unclean exit and copies the dump summary into
    the log (session marker)
  * worker-thread exceptions, unraisable exceptions and Qt messages are logged
  * a WARNING logged just before the process is killed is already on disk
Only samsara.boot is imported (stdlib-only at import); never dictation.py.
"""
import gc
import json
import logging
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samsara import boot  # noqa: E402


def _run(code, cwd):
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=str(cwd),
                          capture_output=True, text=True, timeout=60,
                          env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})


def test_faulthandler_file_gets_a_native_stack_for_an_induced_fault(tmp_path):
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
    assert ("Windows fatal exception: access violation" in dump) or ("Fatal Python error: Segmentation fault" in dump)
    assert "Current thread" in dump and "crash_here" in dump

    summary = boot.summarize_faulthandler_dumps(tmp_path / "faulthandler.log", pid)
    assert summary["events"] and any("crash_here" in frame for frame in summary["stack"])


def test_next_start_reports_the_unclean_exit_with_the_dump(tmp_path, caplog):
    proc = _run(f"""
        import os, faulthandler
        from pathlib import Path
        from samsara import boot
        boot._enable_faulthandler(Path(r"{tmp_path}"))
        boot.begin_session(Path(r"{tmp_path}"))
        print(os.getpid(), flush=True)
        def audio_worker():
            faulthandler._sigsegv()
        audio_worker()
    """, tmp_path)
    assert proc.returncode != 0
    pid = int(proc.stdout.split()[0])
    assert json.loads((tmp_path / boot.SESSION_MARKER).read_text(encoding="utf-8"))["pid"] == pid

    with caplog.at_level(logging.WARNING):
        previous = boot.begin_session(tmp_path, logging.getLogger("test.crashdiag"))

    assert previous["pid"] == pid
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert f"pid {pid}" in message and "did not shut down cleanly" in message
    assert "audio_worker" in message
    boot.end_session(tmp_path)
    assert not (tmp_path / boot.SESSION_MARKER).exists()


def test_clean_session_leaves_no_warning(tmp_path, caplog):
    log = logging.getLogger("test.crashdiag.clean")
    with caplog.at_level(logging.WARNING):
        assert boot.begin_session(tmp_path, log) is None
        boot.end_session(tmp_path)
        assert boot.begin_session(tmp_path, log) is None
    assert not [r for r in caplog.records if "did not shut down" in r.getMessage()]


@pytest.fixture
def restore_hooks():
    saved = (threading.excepthook, sys.unraisablehook)
    yield
    threading.excepthook, sys.unraisablehook = saved


def test_worker_thread_exception_is_logged(restore_hooks, caplog):
    log = logging.getLogger("test.crashdiag.thread")
    boot.install_exception_hooks(log)

    def worker():
        raise ValueError("calibration exploded")

    t = threading.Thread(target=worker, name="wizard-wake-calibration")
    # dictation.py's Thread.run wrapper is not installed here: this is the
    # path threading.excepthook alone has to cover.
    with caplog.at_level(logging.CRITICAL):
        t.start()
        t.join(5)
    records = [r for r in caplog.records if r.name == "test.crashdiag.thread"]
    assert records and "wizard-wake-calibration" in records[0].getMessage()
    assert "calibration exploded" in records[0].getMessage()


def test_unraisable_exception_is_logged(restore_hooks, caplog):
    log = logging.getLogger("test.crashdiag.unraisable")
    boot.install_exception_hooks(log)

    class _Leaky:
        def __del__(self):
            raise RuntimeError("raised in a callback nobody can catch")

    with caplog.at_level(logging.ERROR):
        obj = _Leaky()
        del obj
        gc.collect()
    assert any("raised in a callback nobody can catch" in r.getMessage()
               for r in caplog.records if r.name == "test.crashdiag.unraisable")


def test_qt_warnings_and_criticals_reach_the_log(caplog):
    from PySide6.QtCore import qCritical, qInstallMessageHandler, qWarning
    log = logging.getLogger("test.crashdiag.qt")
    assert boot.install_qt_message_handler(log) is not None
    try:
        with caplog.at_level(logging.WARNING):
            qWarning("wizard timer started from another thread")
            qCritical("widget touched after delete")
    finally:
        qInstallMessageHandler(None)
    got = {(r.levelno, r.getMessage()) for r in caplog.records if r.name == "test.crashdiag.qt"}
    assert (logging.WARNING, "[QT] wizard timer started from another thread") in {(l, m.split(" (")[0]) for l, m in got}
    assert any(l == logging.ERROR and "widget touched after delete" in m for l, m in got)


def test_warning_logged_right_before_a_hard_kill_is_on_disk(tmp_path):
    log_file = tmp_path / "samsara.log"
    proc = _run(f"""
        import logging, os, signal
        from logging.handlers import RotatingFileHandler
        h = RotatingFileHandler(r"{log_file}", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
        root = logging.getLogger(); root.addHandler(h); root.setLevel(logging.DEBUG)
        logging.getLogger("Samsara").warning("[CAL] last words before the process dies")
        os.kill(os.getpid(), signal.SIGTERM)   # TerminateProcess on Windows: no cleanup, no atexit
    """, tmp_path)
    assert proc.returncode != 0
    assert "[CAL] last words before the process dies" in log_file.read_text(encoding="utf-8")
