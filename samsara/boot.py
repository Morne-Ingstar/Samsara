"""Samsara boot sequence, extracted from dictation.py (27).

The steps dictation.py runs before DictationApp exists -- faulthandler, the
Visual C++ runtime check, the single-instance lock (with
its stale-PID self-heal), the legacy source-profile migration, the early UI
scale, the splash hand-off -- plus the [BOOT] stage timer DictationApp uses.
dictation.py calls these at exactly the points the inline code used to run,
in the same order; the code itself moved unchanged. Log records go to the
same "Samsara" logger, so every [BOOT-DIAG] line is byte-identical.

Import cost is part of the contract: this module imports only the standard
library at load time (samsara.* and Qt are imported inside the functions
that need them, as they were inline), and it must NEVER import dictation --
boot must not drag the app in. tests/test_boot.py asserts both.

What deliberately stayed in dictation.py (see reports/27_code.md):
  * the None-stdout guard and the ducking-host divert -- they must run
    before ANY samsara import (samsara/__init__.py imports .commands/.ui);
  * the logging bootstrap -- its handler, self-check and rollover code call
    print(), which inside dictation.py resolves to dictation's own logging
    print override; moved here it would silently become the builtin;
  * the taskbar AUMID call -- tests/test_theme_identity.py executes that exact
    module-level block of dictation.py to prove it runs before any Qt object;
  * config load and the MIGRATE steps -- DictationApp methods on
    self.config / _config_lock / save_config's three-way merge.
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("Samsara")


def _enable_faulthandler(log_dir):
    """Dump every thread's Python stack to <SAMSARA_HOME>/logs/faulthandler.log
    on a native crash (access violation, SIGSEGV/SIGFPE/SIGILL/SIGABRT).

    2026-09-13: the mic setup guide took the process down with no Python
    traceback at all -- the regular log just restarted. faulthandler is the
    only thing that can speak after a native fault, and in a windowless
    build sys.stderr is a null stream, so it gets its own file. enable()
    already covers SIGABRT; faulthandler.register() does not exist on Windows
    and refuses SIGABRT elsewhere, so it is not called. Never raises: a
    diagnostic must not stop boot. Returns the open file (kept alive for the
    process lifetime) or None."""
    import faulthandler as _faulthandler
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = open(log_dir / "faulthandler.log", "a", encoding="utf-8")
        # The timestamp (48) ties a dump to samsara.log: faulthandler itself
        # writes no times, so without it a dump cannot be matched to a run.
        fh.write(f"--- Samsara start pid={os.getpid()} at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        fh.flush()
        _faulthandler.enable(file=fh, all_threads=True)
        return fh
    except Exception as exc:
        try:
            sys.stderr.write(f"[BOOT] faulthandler not enabled: {exc!r}\n")
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Crash evidence (48)
# ---------------------------------------------------------------------------
# 2026-09-14 21:02: the mic setup guide killed the app with an access
# violation. faulthandler.log HAD the native stack, but nothing pointed at it:
# samsara.log simply stopped, the dump carried no timestamp, and every boot
# silently started a fresh log. What each layer catches that the others do not:
#
#   faulthandler (above)      native faults: access violation, illegal
#                             instruction, abort -- the only layer that speaks
#                             after PortAudio/ONNX/Qt corrupt memory. It also
#                             records exceptions native code later HANDLES
#                             (e.g. COM 0x80010108), so an entry alone is not
#                             proof of death. Cannot see TerminateProcess,
#                             os._exit, or a __fastfail that bypasses handlers.
#   sys.excepthook            (dictation.py) an uncaught Python exception on
#                             the MAIN thread.
#   threading.excepthook      an uncaught exception on ANY other thread,
#                             including threads created before dictation.py's
#                             Thread.run wrapper was installed and BaseException
#                             subclasses that wrapper does not catch.
#   sys.unraisablehook        exceptions Python cannot raise anywhere: in
#                             __del__, weakref callbacks, and cffi callbacks --
#                             sounddevice/PortAudio audio callbacks run there,
#                             and in the windowless build stderr is a null
#                             stream, so these vanished completely.
#   Qt message handler        qWarning/qCritical/qFatal from Qt itself
#                             (e.g. "QObject::startTimer: Timers cannot be
#                             started from another thread"); qFatal aborts the
#                             process right after the handler returns, so the
#                             record is flushed first.
#   session marker            the one thing none of the above can do: notice
#                             at the NEXT start that the previous run never
#                             reached quit_app, and copy that run's
#                             faulthandler dump summary into samsara.log.
#
# Log records need no extra flushing: logging.StreamHandler (and so
# RotatingFileHandler) flushes after every record, which hands the bytes to
# the OS; they survive the process dying (tests/test_crash_diagnostics.py).

SESSION_MARKER = "session.running"


def install_exception_hooks(log=None):
    """threading.excepthook + sys.unraisablehook -> the log (CRITICAL / ERROR),
    then the previous hooks. Never raises."""
    log = log or logging.getLogger("Samsara")
    import traceback as _tb

    previous_thread_hook = threading.excepthook
    previous_unraisable = sys.unraisablehook

    def _thread_hook(args):
        try:
            if args.exc_type is SystemExit:
                return
            name = args.thread.name if args.thread is not None else "?"
            log.critical(f"[CRASH-DIAG] Uncaught exception in thread {name}:\n"
                         + "".join(_tb.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:
            pass
        try:
            previous_thread_hook(args)
        except Exception:
            pass

    def _unraisable_hook(unraisable):
        try:
            where = unraisable.err_msg or "Exception ignored in"
            log.error(f"[CRASH-DIAG] {where}: {unraisable.object!r}\n"
                      + "".join(_tb.format_exception(unraisable.exc_type, unraisable.exc_value,
                                                     unraisable.exc_traceback)))
        except Exception:
            pass
        try:
            previous_unraisable(unraisable)
        except Exception:
            pass

    threading.excepthook = _thread_hook
    sys.unraisablehook = _unraisable_hook
    return _thread_hook, _unraisable_hook


def install_qt_message_handler(log=None):
    """Route Qt's own qDebug/qWarning/qCritical/qFatal into the log. Returns
    the handler, or None when Qt is unavailable. Never raises."""
    log = log or logging.getLogger("Samsara")
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        return None
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def _handler(msg_type, context, message):
        try:
            where = ""
            if context is not None and getattr(context, "file", None):
                where = f" ({context.file}:{context.line})"
            log.log(levels.get(msg_type, logging.WARNING), f"[QT] {message}{where}")
            if msg_type == QtMsgType.QtFatalMsg:
                for handler in logging.getLogger().handlers:
                    try:
                        handler.flush()
                    except Exception:
                        pass
        except Exception:
            pass

    try:
        qInstallMessageHandler(_handler)
    except Exception:
        return None
    return _handler


# ---------------------------------------------------------------------------
# DPI awareness (queue 96)
# ---------------------------------------------------------------------------

#: The five documented DPI_AWARENESS_CONTEXT pseudo-handles, as the negative
#: values Windows defines them by. AreDpiAwarenessContextsEqual is the ONLY
#: call that separates PER_MONITOR_AWARE_V2 from V1 --
#: GetAwarenessFromDpiAwarenessContext collapses both to PER_MONITOR_AWARE (2),
#: so a log line built from the enum alone cannot answer the question anyone
#: actually asks.
_DPI_CONTEXTS = (
    ("UNAWARE", -1),
    ("SYSTEM_AWARE", -2),
    ("PER_MONITOR_AWARE", -3),
    ("PER_MONITOR_AWARE_V2", -4),
    ("UNAWARE_GDISCALED", -5),
)

#: GetAwarenessFromDpiAwarenessContext's DPI_AWARENESS enum.
_DPI_AWARENESS_ENUM = {0: "UNAWARE", 1: "SYSTEM_AWARE", 2: "PER_MONITOR_AWARE"}

#: Log the answer once per process, not once per caller.
_dpi_awareness_logged = False


def resolve_dpi_awareness():
    """(context_name, enum_name) for the CALLING thread, or (None, reason).

    Reads the live answer from the API rather than inferring it from whether
    a Set* call succeeded. Windows returns ERROR_ACCESS_DENIED both when
    awareness was already set to the value asked for and when it was already
    set to a different one, so "SetProcessDpiAwarenessContext() failed:
    Access is denied" says nothing about which level the process ended up at.

    Never raises. Non-Windows, or a Windows older than 1607 (no
    GetThreadDpiAwarenessContext), returns (None, why).
    """
    if not sys.platform.startswith("win"):
        return None, "not Windows"
    try:
        import ctypes  # noqa: PLC0415  (stdlib, lazy: see the module docstring)

        user32 = ctypes.windll.user32
        if not hasattr(user32, "GetThreadDpiAwarenessContext"):
            return None, "Windows is older than 1607"
        ctx = ctypes.c_void_p(user32.GetThreadDpiAwarenessContext())
        name = "UNRECOGNISED"
        if hasattr(user32, "AreDpiAwarenessContextsEqual"):
            for candidate, value in _DPI_CONTEXTS:
                if user32.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(value)):
                    name = candidate
                    break
        enum = _DPI_AWARENESS_ENUM.get(
            user32.GetAwarenessFromDpiAwarenessContext(ctx), "UNKNOWN")
        return name, enum
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def log_dpi_awareness(log=None):
    """Log the resolved DPI awareness once, at INFO, in plain words.

    Call it AFTER Qt is up: Qt 6 asks for PER_MONITOR_AWARE_V2 when
    QApplication is constructed, and whether it gets it depends on whether
    anything else claimed process awareness first.

    Returns the context name it logged, or None when it could not be read.
    """
    global _dpi_awareness_logged
    log = log or logger
    if _dpi_awareness_logged:
        return None
    _dpi_awareness_logged = True
    name, detail = resolve_dpi_awareness()
    if name is None:
        log.info(f"[DPI] process DPI awareness could not be read: {detail}")
        return None
    if name == "PER_MONITOR_AWARE_V2":
        log.info(
            f"[DPI] process DPI awareness: {name} (enum {detail}). Win32 point "
            "and geometry calls use real screen pixels on every monitor, so "
            "WindowFromPoint finds the window the user is pointing at even "
            "when two monitors run at different scales.")
    else:
        log.info(
            f"[DPI] process DPI awareness: {name} (enum {detail}). NOT "
            "Per-Monitor V2: on a monitor whose scale differs from the system "
            "scale, Windows virtualises coordinates for this process, so "
            "WindowFromPoint and window rectangles can name the wrong window. "
            "Code that needs real screen pixels must run inside an explicit "
            "PMv2 thread context (see numbers_overlay_qt._with_physical_dpi_context).")
    return name


#: A thread block in a faulthandler dump starts with one of these.
_THREAD_PREFIX = "Thread 0x"
_CURRENT_PREFIX = "Current thread 0x"
#: COM "the object invoked has disconnected from its clients" (RPC_E_DISCONNECTED).
#: Native code RAISES and then HANDLES this; an entry is not a death. It is
#: still worth counting: every access violation in this app's history has been
#: immediately preceded by a run of them (queue 87).
COM_DISCONNECT = "0x80010108"


def _fault_entries(section):
    """Split one process's faulthandler section into its fault entries.

    Each entry is a dict:
        header     the "Windows fatal exception: ..." / "Fatal Python error:" line
        threads    [(title, [frame, ...]), ...] in the order they were written
        current    the title of the "Current thread" block, or None
        complete   False when the dump stops mid-block -- which is how a dump
                   that was interrupted by the NEXT fault looks on disk
    """
    heads = [i for i, ln in enumerate(section)
             if "fatal exception" in ln.lower() or ln.startswith("Fatal Python error")]
    entries = []
    for n, start in enumerate(heads):
        stop = heads[n + 1] if n + 1 < len(heads) else len(section)
        body = section[start:stop]
        threads, current, last_closed = [], None, True
        i = 1
        while i < len(body):
            line = body[i]
            if line.startswith(_THREAD_PREFIX) or line.startswith(_CURRENT_PREFIX):
                title = line
                frames = []
                i += 1
                while i < len(body) and body[i].startswith("  "):
                    frames.append(body[i].strip())
                    i += 1
                threads.append((title, frames))
                if line.startswith(_CURRENT_PREFIX):
                    current = title
                # A finished block is followed by a blank line. The LAST block
                # of a dump that was cut off by another fault is not.
                last_closed = i < len(body) and body[i].strip() == ""
                continue
            i += 1
        entries.append({
            "header": body[0].strip(),
            "threads": threads,
            "current": current,
            "complete": last_closed if threads else True,
        })
    return entries


def _explain_missing_frames(entry, previous):
    """Why this fault has no thread list, in one sentence, or None.

    The two causes seen in the wild, distinguished by what is on disk:

      * Another dump was still being written. CPython's exception handler
        writes the header unconditionally and then calls
        faulthandler_dump_traceback(), which returns immediately while its
        static `reentrant` flag is set by a dump already in progress. On disk
        the PREVIOUS entry stops mid-block and this one has no frames at all.
      * No usable interpreter state: the fault arrived on a thread with no
        Python state at a moment when faulthandler could not reach the
        interpreter either (typically during interpreter shutdown). CPython
        discards the error string in that path, so nothing is written.
    """
    if entry["threads"]:
        return None
    if previous is not None and not previous["complete"]:
        return ("no frames: the dump for the preceding fault (" + previous["header"] +
                ") was still being written, so CPython's faulthandler suppressed this one "
                "(its handler writes the header, then its reentrancy guard returns "
                "without dumping). The interrupted dump immediately above this entry in "
                "faulthandler.log is the last thread state captured before the crash.")
    return ("no frames: faulthandler wrote the header and then could not reach the "
            "interpreter state -- the fault was on a thread with no Python state, during "
            "interpreter shutdown, or with all_threads off.")


def summarize_faulthandler_dumps(faulthandler_log, pid):
    """What faulthandler recorded for one process start.

    Returns {} when the file or the section is missing. Otherwise:
        events        {header: count} over the whole section
        stack         the faulting thread's frames, when they can be named
        fatal         the header of the LAST fault -- the one it died on
        note          why the stack is absent or cannot be trusted, or ""
        threads       first frame of each thread in the fatal dump, as context
        com_disconnects  how many handled COM disconnects preceded it

    Queue 87 rewrote this. Queue 48's version worked when frames existed --
    it named the Qt loop thread for three of the four access violations on
    record. What it could not do was say anything when frames were ABSENT: it
    printed the fault counts and stopped, so the fourth crash looked like a
    diagnostic that had captured nothing, when in fact CPython had suppressed
    the dump for a reason that is written on disk. It also reported the FIRST
    fault that was not a COM disconnect rather than the LAST one, which is the
    one the process actually died on.
    """
    try:
        lines = Path(faulthandler_log).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"--- Samsara start pid={pid} ") or line == f"--- Samsara start pid={pid} ---":
            start = i
    if start is None:
        return {}
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("--- Samsara start pid=")),
               len(lines))
    section = lines[start + 1:end]

    entries = _fault_entries(section)
    counts = {}
    for entry in entries:
        counts[entry["header"]] = counts.get(entry["header"], 0) + 1
    if not entries:
        return {"events": counts, "stack": [], "fatal": "", "note": "",
                "threads": [], "com_disconnects": 0}

    # The LAST fault is the one the process died on. The first is usually a
    # handled COM disconnect, which is what queue 48 reported instead.
    fatal = entries[-1]
    previous = entries[-2] if len(entries) > 1 else None
    note = _explain_missing_frames(fatal, previous) or ""

    stack = []
    if fatal["current"] is not None:
        stack = [f for _title, frames in fatal["threads"]
                 if _title == fatal["current"] for f in frames]
    elif fatal["threads"]:
        # Without a "Current thread" marker the faulting thread cannot be
        # named: it had no Python state. Say so rather than presenting some
        # other thread's stack as if it were the crash.
        note = ("the faulting thread has no Python state, so faulthandler could not mark "
                "it -- the fault was inside native code (an audio callback, a COM RPC "
                "thread or a Qt internal thread). The Python threads that were running "
                "are listed below as context, not as the crash site.")

    threads = [frames[0] for _title, frames in fatal["threads"] if frames]
    disconnects = sum(count for header, count in counts.items() if COM_DISCONNECT in header)
    return {
        "events": counts,
        "stack": stack[:8],
        "fatal": fatal["header"],
        "note": note,
        "threads": threads[:8],
        "com_disconnects": disconnects,
    }

def begin_session(log_dir, log=None):
    """Record this run as in progress. If the previous run's marker is still
    there, that run ended without quit_app (a crash, a kill, a power loss):
    log it with its faulthandler summary. Returns the previous marker's data
    or None. Never raises."""
    import json as _json
    log = log or logging.getLogger("Samsara")
    previous = None
    try:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        marker = log_dir / SESSION_MARKER
        if marker.exists():
            try:
                previous = _json.loads(marker.read_text(encoding="utf-8"))
            except Exception:
                previous = {"pid": None, "started": "unknown"}
            summary = summarize_faulthandler_dumps(log_dir / "faulthandler.log", previous.get("pid"))
            log.warning(
                f"[CRASH-DIAG] The previous Samsara session (pid {previous.get('pid')}, started "
                f"{previous.get('started')}) did not shut down cleanly. "
                + (f"faulthandler.log recorded: {summary['events']}" if summary.get("events")
                   else "faulthandler.log recorded no native fault for it "
                        "(a kill, os._exit, power loss or a fast-fail leaves none).")
                + (f"\n  died on: {summary['fatal']}" if summary.get("fatal") else "")
                + (f"\n  {summary['com_disconnects']} handled COM disconnect(s) "
                   f"(RPC_E_DISCONNECTED) preceded it"
                   if summary.get("com_disconnects") else "")
                # 87: ALWAYS say why there is no stack. A crash report that is
                # silent about its own gaps sent four investigations looking
                # for frames that were never going to be there.
                + (f"\n  {summary['note']}" if summary.get("note") else "")
                + ("\n  crashing thread: " + "\n    ".join(summary["stack"]) if summary.get("stack") else "")
                + ("\n  python threads running: " + "; ".join(summary["threads"])
                   if summary.get("threads") and not summary.get("stack") else "")
                + "\n  Windows keeps its own record: Event Viewer > Windows Logs > Application "
                  "(Application Error, event 1000)."
            )
        marker.write_text(_json.dumps({"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S")}),
                          encoding="utf-8")
    except Exception as exc:
        try:
            log.debug(f"[CRASH-DIAG] session marker unavailable: {exc!r}")
        except Exception:
            pass
    return previous


def end_session(log_dir):
    """quit_app: this run is ending on purpose. Never raises."""
    try:
        (Path(log_dir) / SESSION_MARKER).unlink(missing_ok=True)
    except Exception:
        pass


def check_vc_redistributable():
    """Exit with a dialog when msvcp140.dll is missing (before faster-whisper loads)."""
    if sys.platform == 'win32':
        try:
            import ctypes as _ctypes
            _ctypes.cdll.LoadLibrary("msvcp140.dll")
        except OSError:
            from PySide6.QtWidgets import QApplication as _QApp, QMessageBox as _QMB
            _app = _QApp.instance() or _QApp(sys.argv)
            _QMB.critical(
                None,
                "Missing Dependency",
                "Samsara requires the Visual C++ Redistributable.\n\n"
                "Download it from:\n"
                "https://aka.ms/vs/17/release/vc_redist.x64.exe\n\n"
                "Install it and restart Samsara.",
            )
            sys.exit(1)


# ============================================================================
# Single Instance Check - Prevent multiple instances from running
# ============================================================================

def _is_samsara_process(pid: int) -> bool:
    """True if `pid` is alive AND looks like a Samsara process.

    Liveness alone isn't enough: PIDs get reused by Windows, so a lock file
    naming a PID that's alive right now could belong to a completely
    unrelated process that started after the real Samsara process (which
    wrote that PID) died or was killed. Checks the process image name --
    "Samsara.exe" for a frozen build, or a python*.exe running dictation.py
    for a dev-mode instance.

    Prefers psutil (already a project dependency); falls back to raw
    ctypes OpenProcess + QueryFullProcessImageNameW if psutil isn't
    importable for some reason.
    """
    try:
        import psutil
    except ImportError:
        psutil = None

    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            name = (proc.name() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        except Exception:
            return False
        if name == "samsara.exe":
            return True
        if name.startswith("python"):
            try:
                cmdline = " ".join(proc.cmdline()).lower()
            except Exception:
                return False
            return "dictation.py" in cmdline
        return False

    if sys.platform != 'win32':
        # Can't verify identity without psutil off Windows -- assume it's
        # real rather than risk stealing a live process's lock.
        return True

    import ctypes
    import ctypes.wintypes as wt

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # No handle -- process is gone (or inaccessible; treat the same,
        # since we can't confirm it's Samsara either way).
        return False
    try:
        buf_len = wt.DWORD(260)
        buf = ctypes.create_unicode_buffer(260)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len))
        if not ok:
            return False
        image_name = Path(buf.value).name.lower()
        return image_name == "samsara.exe" or image_name.startswith("python")
    finally:
        kernel32.CloseHandle(handle)


def _steal_stale_lock_if_any(lock_file_path) -> None:
    """If lock_file_path exists and names a dead or non-Samsara PID, delete
    it. If it names a live Samsara process, log and exit(0) -- never hang;
    this whole check is non-blocking liveness/identity inspection, no wait.

    Runs before the OS-level lock acquisition below, which still does the
    actual atomic locking -- this only turns "some stale file is sitting
    there from a hard kill" into a clean steal instead of a false
    already-running refusal.
    """
    if not lock_file_path.exists():
        return
    try:
        recorded_pid = int(lock_file_path.read_text().strip())
    except (OSError, ValueError):
        # Unreadable/empty/corrupt -- can't belong to a live instance we'd
        # recognize; treat as stale.
        logger.info("[LOCK] lock file unreadable, stealing")
        try:
            lock_file_path.unlink()
        except OSError as e:
            logger.debug(f"[LOCK] could not remove unreadable lock file: {e}")
        return

    if _is_samsara_process(recorded_pid):
        logger.warning(f"[WARN] Samsara is already running (PID: {recorded_pid})")
        sys.exit(0)

    logger.info(f"[LOCK] stale lock from PID {recorded_pid}, stealing")
    try:
        lock_file_path.unlink()
    except OSError as e:
        logger.debug(f"[LOCK] could not remove stale lock file: {e}")


def _check_single_instance():
    """
    Ensure only one instance of Samsara is running.
    Windows uses a process-lifetime named mutex; Unix-like systems retain the
    existing file lock. Returns the retained handle or exits if another
    instance owns the same profile identity.

    The normal profile uses a fixed identity. When SAMSARA_HOME_DIR is
    explicitly set (temp-profile tooling, the tray's "Preview First-Run"
    dev action), the identity is derived from that path, so a preview
    instance never collides with the primary instance.
    """
    if sys.platform == 'win32':
        from samsara.single_instance import (
            AlreadyRunningError,
            acquire_single_instance_mutex,
        )
        try:
            return acquire_single_instance_mutex()
        except AlreadyRunningError:
            # Keep this exact marker: frozen smoke tooling recognizes it as
            # the expected fast refusal when a real instance is already up.
            logger.warning("[WARN] Samsara is already running")
            sys.exit(0)
        except Exception as e:
            # Preserve the existing fail-open startup policy. A broken
            # single-instance check must not make an accessibility app
            # impossible to launch.
            logger.warning(f"[WARN] Could not check for existing instance: {e}")
            return None

    from pathlib import Path
    import tempfile

    home_override = os.environ.get("SAMSARA_HOME_DIR")
    if home_override:
        import hashlib
        # normcase + realpath so equivalent paths (different case, trailing
        # slash, relative vs absolute, 8.3 vs long form) hash identically --
        # otherwise two preview launches pointed at "the same" dir by a
        # human typing it two different ways would get two different locks
        # and could run concurrently against one profile.
        normalized = os.path.normcase(os.path.realpath(home_override))
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        lock_name = f"samsara-{digest}.lock"
    else:
        lock_name = "samsara.lock"
    lock_file_path = Path(tempfile.gettempdir()) / lock_name
    _steal_stale_lock_if_any(lock_file_path)

    try:
        # Open/create lock file
        if sys.platform == 'win32':
            import msvcrt
            # Open in write mode, create if doesn't exist
            lock_file = open(lock_file_path, 'w')
            try:
                # Try to get exclusive lock (non-blocking)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                # Write our PID
                lock_file.write(str(os.getpid()))
                lock_file.flush()
                return lock_file  # Keep file open to maintain lock
            except (IOError, OSError):
                # Another instance has the lock
                lock_file.close()
                # Try to read the other instance's PID
                try:
                    with open(lock_file_path, 'r') as f:
                        other_pid = f.read().strip()
                    logger.warning(f"[WARN] Samsara is already running (PID: {other_pid})")
                except Exception as e:
                    logger.debug(f"Could not read other instance PID: {e}")
                    logger.warning("[WARN] Samsara is already running")
                sys.exit(0)
        else:
            # Unix-like systems (macOS, Linux)
            import fcntl
            lock_file = open(lock_file_path, 'w')
            try:
                # Try to get exclusive lock (non-blocking)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Write our PID
                lock_file.write(str(os.getpid()))
                lock_file.flush()
                return lock_file  # Keep file open to maintain lock
            except (IOError, OSError):
                # Another instance has the lock
                lock_file.close()
                try:
                    with open(lock_file_path, 'r') as f:
                        other_pid = f.read().strip()
                    logger.warning(f"[WARN] Samsara is already running (PID: {other_pid})")
                except Exception as e:
                    logger.debug(f"Could not read other instance PID: {e}")
                    logger.warning("[WARN] Samsara is already running")
                sys.exit(0)
    except Exception as e:
        # If locking fails for any reason, log but continue
        # (better to have duplicate instances than no instances)
        logger.warning(f"[WARN] Could not check for existing instance: {e}")
        return None

# Single-instance lock is only meaningful when this file is run as the main
# program. Acquiring it at import time blocks pytest (any import of dictation
# triggers sys.exit(0) from _check_single_instance when a prior import already
# holds the lock). The lock is acquired from the __main__ block below via
# _acquire_instance_lock() -- the module-level name is kept so tests and
# helpers can reason about it without triggering the check.
_instance_lock = None


def _acquire_instance_lock():
    """Acquire the single-instance lock. Called from __main__ only."""
    global _instance_lock
    _instance_lock = _check_single_instance()
    return _instance_lock


def lock_single_instance():
    """__main__: acquire the single-instance lock, with its [BOOT-DIAG] timing."""
    _t = time.perf_counter()
    _acquire_instance_lock()
    _dt = (time.perf_counter() - _t) * 1000
    logger.debug(f"[BOOT-DIAG] instance lock (_check_single_instance): {_dt:.0f}ms")
    if _dt > 5000:
        logger.debug(f"[BOOT-DIAG] SLOW STEP: instance lock {_dt:.0f}ms")


def migrate_legacy_source_profile(source_dir):
    """__main__: carry a legacy config.json from source_dir (dictation.py's folder)
    into the per-user profile once. No-op in a frozen build."""
    from samsara.paths import migrate_legacy_source_config
    if not getattr(sys, "frozen", False):
        try:
            if migrate_legacy_source_config(Path(source_dir) / "config.json"):
                logger.info("[CONFIG] Migrated legacy source profile to the per-user profile")
        except Exception as _config_migration_exc:
            logger.warning(
                "[CONFIG] Could not migrate legacy source profile: %s",
                _config_migration_exc,
            )


def apply_early_interface_scale():
    """__main__: set QT_SCALE_FACTOR from the saved UI scale before Qt starts."""
    from samsara.paths import samsara_config_path
    try:
        from samsara.ui_scale import apply_early_ui_scale
        _early_config_path = samsara_config_path()
        _early_scale = apply_early_ui_scale(_early_config_path)
        logger.info(f"[UI] Early interface scale: {_early_scale:g}x")
    except Exception as _scale_exc:
        logger.warning(f"[UI] Could not apply interface scale: {_scale_exc}")


def apply_early_theme():
    """__main__: bind the user's palette before the first window is built.

    Must run before anything under samsara.ui builds a stylesheet -- the
    splash is the first, and it is constructed in create_splash() below. A
    theme applied later would leave whatever was already styled on the old
    palette, which is the half-themed app this exists to prevent."""
    from samsara.paths import samsara_config_path
    try:
        import json
        from samsara.ui import theme
        try:
            config = json.loads(samsara_config_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = {}
        setting = (config or {}).get("ui", {})
        setting = setting.get("theme") if isinstance(setting, dict) else None
        if setting is None:
            setting = (config or {}).get("ui.theme", theme.DEFAULT_THEME)
        resolved = theme.set_theme(setting, refresh=False)
        logger.info(f"[UI] Theme: {setting!r} -> {resolved}")
    except Exception as _theme_exc:
        logger.warning(f"[UI] Could not apply theme: {_theme_exc}")


def create_splash():
    """__main__: build the splash (on this, the main thread, as before) and return it."""
    _t = time.perf_counter()
    from samsara.ui.splash_qt import SplashScreenQt
    splash = SplashScreenQt()
    _dt = (time.perf_counter() - _t) * 1000
    logger.debug(f"[BOOT-DIAG] splash init (SplashScreenQt): {_dt:.0f}ms")
    if _dt > 5000:
        logger.debug(f"[BOOT-DIAG] SLOW STEP: splash init {_dt:.0f}ms")
    splash.set_status("Initializing...")
    # Queue 96. The first boot step guaranteed to run AFTER QApplication
    # exists: SplashScreenQt.__init__ calls qt_runtime.ensure_started(), which
    # constructs it and waits. Logging here rather than from dictation.py
    # keeps the whole DPI question inside one file.
    log_dpi_awareness()
    return splash


def show_startup_failure(splash, e):
    """__main__: put a synchronous DictationApp() failure on the splash.

    Keeps the shared Qt runtime and splash alive so the failure is visible;
    the caller re-raises."""
    try:
        splash.set_status("Startup could not finish")
        if hasattr(splash, "set_detail"):
            splash.set_detail(str(e))
        if hasattr(splash, "set_error"):
            splash.set_error("Startup could not finish", str(e))
    except Exception as _splash_error:
        logger.debug(f"Could not show synchronous startup error: {_splash_error}")


class _BootStageTimer:
    """[BOOT] stage timer with one "last mark" per thread.

    The old closure shared a single last-timestamp between the boot thread
    and the model thread, so each lane's step durations absorbed the other
    lane's work (perf_artifacts/boot_profile.md section 4: "ACE audio engine
    start: 828ms" was really 13,371 ms, "Silero VAD load: 12547ms" was 212 ms).
    Each thread's first mark measures from begin_thread() if that thread
    called it, else from timer creation. "total" is always since creation.
    """

    def __init__(self, log=None):
        self._t0 = time.monotonic()
        self._last: dict[int, float] = {}
        self._lock = threading.Lock()
        self._log = log or logger.info

    def begin_thread(self) -> None:
        with self._lock:
            self._last[threading.get_ident()] = time.monotonic()

    def __call__(self, label: str) -> None:
        now = time.monotonic()
        tid = threading.get_ident()
        with self._lock:
            last = self._last.get(tid, self._t0)
            self._last[tid] = now
        self._log(
            f"[BOOT] {label}: {(now - last) * 1000:.0f}ms  "
            f"(total {(now - self._t0) * 1000:.0f}ms, thread={threading.current_thread().name})"
        )
