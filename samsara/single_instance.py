"""Process-lifetime single-instance protection for Windows.

Windows named mutex objects are removed automatically after their last handle
is closed, including when a process crashes.  That makes them a better fit for
startup arbitration than a PID file, which can be left stale and is vulnerable
to PID reuse.

The mutex name follows ``SAMSARA_HOME_DIR`` when that override is present.
This lets the real profile and temporary first-run/smoke-test profiles run at
the same time, while still preventing two processes from using one profile.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
from typing import Any


ERROR_ALREADY_EXISTS = 183
DEFAULT_MUTEX_NAME = r"Local\Samsara.SingleInstance"
_PROFILE_MUTEX_PREFIX = DEFAULT_MUTEX_NAME + ".Profile."
_USE_ENVIRONMENT = object()


class AlreadyRunningError(RuntimeError):
    """Raised when a live process already holds this profile's mutex."""

    def __init__(self, mutex_name: str) -> None:
        super().__init__(f"Samsara is already running for mutex {mutex_name!r}")
        self.mutex_name = mutex_name


def _canonical_profile_dir(profile_dir: str | os.PathLike[str]) -> str:
    """Return the same canonical spelling for equivalent Windows paths."""

    return os.path.normcase(os.path.realpath(os.fspath(profile_dir)))


def mutex_name_for_profile(
    profile_dir: str | os.PathLike[str] | None | object = _USE_ENVIRONMENT,
) -> str:
    """Return the Windows mutex name for a Samsara profile.

    When ``profile_dir`` is omitted, ``SAMSARA_HOME_DIR`` is read at call time.
    Pass ``None`` explicitly to identify the normal/default profile regardless
    of the current environment.  An unset or empty override also uses that
    fixed mutex name.  Explicit profile paths are canonicalized before hashing
    so path spelling differences do not allow concurrent processes against the
    same profile.
    """

    if profile_dir is _USE_ENVIRONMENT:
        profile_dir = os.environ.get("SAMSARA_HOME_DIR")
    if not profile_dir:
        return DEFAULT_MUTEX_NAME
    if not isinstance(profile_dir, (str, os.PathLike)):
        raise TypeError("profile_dir must be a path-like value or None")

    canonical = _canonical_profile_dir(profile_dir)
    # Keep the same profile identity used by the former lock-file scheme so
    # preview/smoke tooling can derive the mutex name without a migration map.
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    return _PROFILE_MUTEX_PREFIX + digest


def _load_kernel32() -> Any:
    """Load kernel32 with per-thread last-error tracking enabled."""

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _create_windows_mutex(mutex_name: str) -> tuple[Any, bool]:
    """Create/open ``mutex_name`` and return ``(handle, already_existed)``."""

    kernel32 = _load_kernel32()
    ctypes.set_last_error(0)
    # Object existence is the guard.  We intentionally do not request mutex
    # ownership, so the handle can be closed safely from any thread.
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    return handle, error == ERROR_ALREADY_EXISTS


def _close_windows_handle(handle: Any) -> None:
    kernel32 = _load_kernel32()
    if not kernel32.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


class WindowsMutex:
    """A closeable handle that keeps a named Windows mutex object alive."""

    def __init__(self, handle: Any, name: str) -> None:
        self._handle = handle
        self.name = name

    @property
    def closed(self) -> bool:
        return self._handle is None

    def close(self) -> None:
        """Close the handle once; subsequent calls are harmless."""

        handle, self._handle = self._handle, None
        if handle is not None:
            _close_windows_handle(handle)

    def __enter__(self) -> WindowsMutex:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Interpreter shutdown and process termination release all kernel
            # handles anyway; destructors must never surface cleanup errors.
            pass


def acquire_single_instance_mutex(
    profile_dir: str | os.PathLike[str] | None | object = _USE_ENVIRONMENT,
) -> WindowsMutex | None:
    """Acquire this profile's process-lifetime mutex.

    Returns a handle that the caller must retain for the application's
    lifetime.  Raises :class:`AlreadyRunningError` when another live process
    has already created the mutex.  On non-Windows platforms this safely
    returns ``None`` without loading any Win32 libraries.

    Omitting ``profile_dir`` follows ``SAMSARA_HOME_DIR``; passing ``None``
    explicitly selects the default profile even when that variable is set.

    Other Win32 failures are intentionally allowed to propagate so the
    application can log them and choose whether to fail open.
    """

    if sys.platform != "win32":
        return None

    name = mutex_name_for_profile(profile_dir)
    handle, already_existed = _create_windows_mutex(name)
    if already_existed:
        # CreateMutexW returns a valid handle even on collision; close our
        # redundant reference so it cannot prolong the other process's guard.
        _close_windows_handle(handle)
        raise AlreadyRunningError(name)
    return WindowsMutex(handle, name)


__all__ = [
    "AlreadyRunningError",
    "DEFAULT_MUTEX_NAME",
    "WindowsMutex",
    "acquire_single_instance_mutex",
    "mutex_name_for_profile",
]
