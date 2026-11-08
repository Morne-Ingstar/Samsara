"""A stale marker is not proof of a crash. Boot timing is read-only."""
from datetime import datetime
import sys
import time


RESTART = "Samsara was closed by a Windows restart"
UNKNOWN = "Previous session ended; the cause is unknown (it may have been ended in Task Manager)."


def windows_boot_time():
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        tick = ctypes.windll.kernel32.GetTickCount64
        tick.restype = ctypes.c_ulonglong
        return time.time() - tick() / 1000
    except (OSError, AttributeError):
        return None


def classify(previous, fault_lines, *, boot_time=None):
    if previous is None:
        return "clean", ""
    boot_time = windows_boot_time() if boot_time is None else boot_time
    try:
        started = datetime.strptime(previous.get("started", ""), "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, TypeError, AttributeError):
        started = None
    # Task Manager, power loss and an unrecorded fault cannot be distinguished
    # reliably from the marker. Never accuse an unknown termination of crashing.
    if any(line.startswith(("Windows fatal exception:", "Fatal Python error:"))
           or line == "Traceback (most recent call last):" for line in fault_lines):
        return "crash", "Previous session recorded a fault."
    if started is not None and boot_time is not None and started < boot_time - 2:
        return "restart", RESTART
    return "unknown", UNKNOWN
