"""Final process termination after application-owned cleanup is complete."""

import ctypes
import logging
import os
import sys
import threading


def _terminate(code):
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
        os._exit(code)


def hard_exit(code=0):
    """Flush Python output, then exit without Windows DLL detach callbacks.

    A daemon watchdog bounds a blocked log/stdio flush to three seconds.
    Application cleanup and session-marker removal must precede this call.
    """
    watchdog = threading.Timer(3.0, _terminate, args=(code,))
    watchdog.daemon = True
    watchdog.start()
    try:
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
