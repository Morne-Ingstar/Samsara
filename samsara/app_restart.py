"""Detached restart handoff, before the replacement reaches the instance mutex."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


PARENT_EXIT_TIMEOUT = 120
HELPER_READY_TIMEOUT = 8


def build_relaunch(*, executable=None, argv=None, frozen=None, source_script=None):
    """Return the application command and working directory for this install."""
    executable = str(executable or sys.executable)
    argv = list(sys.argv if argv is None else argv)
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if frozen:
        return [executable, *argv[1:]], str(Path(executable).resolve().parent)
    script = Path(source_script or Path(__file__).resolve().parents[1] / "dictation.py").resolve()
    return [executable, str(script), *argv[1:]], str(script.parent)


def clean_environment():
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def _creation_flags():
    if sys.platform != "win32":
        return 0
    return (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW)


def _open_parent(pid):
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _wait_parent(pid, handle, timeout):
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        result = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
        if result == 0:  # WAIT_OBJECT_0: exact process has terminated
            return
        if result == 0x102:
            raise TimeoutError("Samsara did not finish shutting down")
        raise ctypes.WinError(ctypes.get_last_error())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise TimeoutError("Samsara did not finish shutting down")


def _notify_failure(message):
    if sys.platform == "win32" and os.environ.get("SAMSARA_RESTART_TEST_NO_DIALOG") != "1":
        ctypes.windll.user32.MessageBoxW(None, message, "Samsara restart failed", 0x10)
    else:
        print(message, file=sys.stderr)


def run_helper(parent_pid, ready_path, cwd, target_argv, *, timeout=PARENT_EXIT_TIMEOUT):
    """Hold the exact parent process handle, then start one replacement."""
    handle = None
    try:
        handle = _open_parent(parent_pid)
        Path(ready_path).write_text("READY", encoding="ascii")
        _wait_parent(parent_pid, handle, timeout)
        child = subprocess.Popen(
            target_argv, cwd=cwd, env=clean_environment(),
            creationflags=_creation_flags(), close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Early exits include a refused single-instance lock or bad command.
        try:
            code = child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            return 0
        raise RuntimeError(f"replacement exited immediately (code {code})")
    except Exception as exc:
        _notify_failure(f"Samsara could not restart: {exc}")
        return 1
    finally:
        if handle is not None:
            ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(handle))


def start_handoff(target_argv, *, cwd, parent_pid=None, helper_argv=None,
                  ready_timeout=HELPER_READY_TIMEOUT):
    """Start helper and verify it holds the parent before the caller quits."""
    parent_pid = os.getpid() if parent_pid is None else parent_pid
    helper_argv = helper_argv or (
        [sys.executable] if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "samsara.app_restart"]
    )
    ready = Path(tempfile.gettempdir()) / f"samsara-restart-{uuid.uuid4().hex}.ready"
    command = [*helper_argv, "--restart-helper", str(parent_pid), str(ready),
               str(cwd), "--", *target_argv]
    helper_cwd = cwd if getattr(sys, "frozen", False) else str(Path(__file__).resolve().parents[1])
    helper = subprocess.Popen(
        command, cwd=helper_cwd, env=clean_environment(),
        creationflags=_creation_flags(), close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            try:
                if ready.read_text(encoding="ascii") == "READY":
                    return helper
            except (FileNotFoundError, PermissionError):
                pass
            if helper.poll() is not None:
                raise RuntimeError(f"restart helper exited early (code {helper.returncode})")
            time.sleep(0.025)
        raise TimeoutError("restart helper did not become ready")
    except Exception:
        helper.terminate()
        raise
    finally:
        # A Windows reader can observe the marker while the helper still has
        # its write handle open. Cleanup must never turn a ready handoff into
        # a failed restart.
        try:
            ready.unlink(missing_ok=True)
        except PermissionError:
            pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 6 or argv[0] != "--restart-helper" or argv[4] != "--":
        return 2
    return run_helper(int(argv[1]), argv[2], argv[3], argv[5:])


if __name__ == "__main__":
    sys.exit(main())
