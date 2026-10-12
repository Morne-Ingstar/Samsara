"""Final process termination after application-owned cleanup is complete."""

import ctypes
import logging
import os
import sys
import threading

_exit_fds = ()


def _retain_exit_descriptors():
    """Keep append descriptors alive after logging.shutdown closes handlers."""
    global _exit_fds
    retained = []
    try:
        log = logging.getLogger("Samsara")
        seen = set()
        while log is not None:
            for handler in log.handlers:
                try:
                    fd = handler.stream.fileno()
                    if fd not in seen:
                        seen.add(fd)
                        retained.append(os.dup(fd))
                except Exception:
                    pass
            log = log.parent if log.propagate else None
    except Exception:
        pass
    _exit_fds = tuple(retained)


def trace_exit(reason, code=None, *, log=None, target_pid=None):
    """Fixed reason codes only: never include exception messages or speech.

    Flush the emitting logger's chain, including non-propagating loggers.
    Diagnostics must never prevent the original exit.
    """
    try:
        log = log or logging.getLogger("Samsara")
        if reason == "worker-system-exit-thread-only":
            tag = "THREAD-EXIT"
        elif reason in ("ducking-child-tree-kill", "agent-child-tree-kill",
                        "restart-helper-terminate", "model-pull-child-terminate"):
            tag = "CHILD-END"
        else:
            tag = "PROCESS-END"
        log.warning("[%s] reason=%s code=%s target_pid=%s", tag, reason,
                    code if type(code) is int else "none",
                    target_pid if type(target_pid) is int else "self")
    except Exception:
        pass
    try:
        seen = set()
        while log is not None:
            for handler in log.handlers:
                if id(handler) not in seen:
                    seen.add(id(handler))
                    try:
                        handler.flush()
                    except Exception:
                        pass
            log = log.parent if log.propagate else None
    except Exception:
        pass


def _raw_exit_trace(reason, code):
    """Last-chance breadcrumb without logging/stdio locks.

    In particular the watchdog MUST NOT re-enter the handler that hung.
    FileHandler streams are already append-mode; os.write is unbuffered.
    A closed/unavailable descriptor is best effort, never an exit veto.
    """
    try:
        import time
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')},000 - WARNING - "
                f"[PROCESS-END] reason={reason} code={code if type(code) is int else 'none'}\n").encode("ascii")
        if _exit_fds:
            for fd in _exit_fds:
                try:
                    os.write(fd, line)
                except Exception:
                    pass
            return
        seen = set()
        log = logging.getLogger("Samsara")
        while log is not None:
            for handler in log.handlers:
                try:
                    fd = handler.stream.fileno()
                    if fd not in seen:
                        seen.add(fd)
                        os.write(fd, line)
                except Exception:
                    pass
            log = log.parent if log.propagate else None
    except Exception:
        pass


def exit_with_trace(code, reason, *, log=None):
    trace_exit(reason, code, log=log)
    sys.exit(code)


def _watchdog_terminate(code):
    _raw_exit_trace("flush-watchdog", code)
    _terminate(code)


def _terminate(code):
    _raw_exit_trace("terminate-process" if sys.platform == "win32" else "os-exit", code)
    try:
        if sys.platform == "win32":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.argtypes = ()
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
            kernel32.TerminateProcess.restype = ctypes.c_int
            kernel32.TerminateProcess(kernel32.GetCurrentProcess(), code)
    finally:
        # Self-termination never returns on success. Retain the old fallback.
        _raw_exit_trace("os-exit-fallback", code)
        os._exit(code)


def hard_exit(code=0):
    """Flush Python output, then exit without Windows DLL detach callbacks.

    A daemon watchdog bounds a blocked log/stdio flush to three seconds.
    Application cleanup and session-marker removal must precede this call.
    """
    watchdog = threading.Timer(3.0, _watchdog_terminate, args=(code,))
    watchdog.daemon = True
    watchdog.start()
    try:
        _retain_exit_descriptors()
        trace_exit("hard-exit", code)
        try:
            logging.shutdown()
        except Exception:
            pass
        for stream in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
            try:
                if stream is not None:
                    stream.flush()
            except Exception:
                pass
    finally:
        _terminate(code)
