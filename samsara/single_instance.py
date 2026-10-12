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
import time
import logging
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


# A hidden HWND belongs to the same Qt thread as setup, tutorial and Home.
# Its identity uses the mutex profile, so preview launches cannot surface the
# owner's normal instance. There are no sockets, config files or PID files.
_activation_receiver = None
_ACTIVATION_MESSAGE = 'Samsara.ShowExistingWindow.549'
# Stand-in copy (549), reviewed through ASK.json.
ACTIVATION_FAILURE_TEXT = (
    'Samsara is already running, but its window could not be brought forward. '
    'Please try again.'
)
_WINDOW_PRIORITY = ('_WizardWindow', 'TutorialWindow', '_SettingsWindow', '_MainWindow')


def _receiver_title(mutex_name):
    return mutex_name + '.ActivationReceiver'


def _activation_user32():
    from ctypes import wintypes
    api = ctypes.WinDLL('user32', use_last_error=True)
    signatures = {
        'FindWindowW': ([wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND),
        'GetWindowThreadProcessId': ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        'AllowSetForegroundWindow': ([wintypes.DWORD], wintypes.BOOL),
        'RegisterWindowMessageW': ([wintypes.LPCWSTR], wintypes.UINT),
        'SendMessageTimeoutW': ([wintypes.HWND, wintypes.UINT, ctypes.c_size_t,
                                ctypes.c_ssize_t, wintypes.UINT, wintypes.UINT,
                                ctypes.POINTER(ctypes.c_size_t)], ctypes.c_ssize_t),
        'SetForegroundWindow': ([wintypes.HWND], wintypes.BOOL),
        'GetForegroundWindow': ([], wintypes.HWND),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(api, name)
        fn.argtypes, fn.restype = args, result
    return api


def request_existing_activation(mutex_name, timeout=2.0, *, api=None,
                                clock=time.monotonic, sleep=time.sleep):
    """Grant the first PID foreground rights BEFORE requesting a Qt-thread raise.

    Startup may not have created the receiver yet. Discovery and replies share
    one two-second budget. A reply of 1 means a real window became foreground.
    """
    from ctypes import wintypes
    api = api or _activation_user32()
    message = api.RegisterWindowMessageW(_ACTIVATION_MESSAGE)
    if not message:
        return False
    deadline = clock() + timeout
    while clock() < deadline:
        hwnd = api.FindWindowW(None, _receiver_title(mutex_name))
        if hwnd:
            pid = wintypes.DWORD()
            api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                # A grant can be refused by Windows; still request the raise,
                # but only the receiver's verified foreground result is success.
                api.AllowSetForegroundWindow(pid.value)
                remaining = max(1, int((deadline - clock()) * 1000))
                reply = ctypes.c_size_t()
                delivered = api.SendMessageTimeoutW(
                    hwnd, message, 0, 0, 0x0001 | 0x0002 | 0x0020,
                    remaining, ctypes.byref(reply))
                if delivered and reply.value == 1:
                    return True
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(0.05, remaining))
    return False


def _activation_failure_dialog():
    from PySide6.QtWidgets import QMessageBox
    from samsara.ui import theme
    dialog = QMessageBox()
    dialog.setStyleSheet(theme.build_stylesheet())
    dialog.setWindowTitle('Samsara')
    dialog.setIcon(QMessageBox.Icon.Information)
    dialog.setText(ACTIVATION_FAILURE_TEXT)
    dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
    return dialog


def _show_activation_failure():
    """The refused second process owns this small, dismissible Qt message."""
    import threading
    from samsara.ui import qt_runtime
    dismissed = threading.Event()

    def show():
        try:
            dialog = _activation_failure_dialog()
            dialog.exec()
        finally:
            dismissed.set()

    qt_runtime.ensure_started()
    qt_runtime.post(show)
    dismissed.wait()
    qt_runtime.shutdown()


def bring_existing_forward(mutex_name):
    """Never turn a failed activation into a second application startup."""
    log = logging.getLogger('Samsara')
    try:
        if request_existing_activation(mutex_name):
            return True
    except Exception:
        log.exception('[INSTANCE] Could not contact existing window')
    try:
        _show_activation_failure()
    except Exception:
        log.exception('[INSTANCE] Could not show activation failure message')
    return False


def _choose_activation_window(windows, *, modal=None, active=None, last=None):
    """Prefer the current task; if all task windows are hidden, open Home."""
    if modal is not None:
        return modal
    eligible = [w for w in windows if type(w).__name__ in _WINDOW_PRIORITY]
    for window in (active, last):
        if window in eligible and window.isVisible():
            return window
    visible = [w for w in eligible if w.isVisible()]
    if visible:
        return min(visible, key=lambda w: _WINDOW_PRIORITY.index(type(w).__name__))
    return next((w for w in eligible if type(w).__name__ == '_MainWindow'), None)


def _activate_existing_window(last=None):
    from PySide6.QtWidgets import QApplication
    window = _choose_activation_window(
        QApplication.topLevelWidgets(), modal=QApplication.activeModalWidget(),
        active=QApplication.activeWindow(), last=last)
    if window is None:
        # The normal entry point publishes app after construction; Home itself
        # is lazy when start_hidden is enabled. Its public opener posts to Qt.
        # During setup app is still None, so discovery simply keeps waiting.
        app = getattr(sys.modules.get('__main__'), 'app', None)
        opener = getattr(app, 'show_main_window', None)
        if callable(opener):
            opener()
        return False
    if window.isMinimized():
        window.showNormal()
    else:
        window.show()
    window.raise_()
    window.activateWindow()
    api = _activation_user32()
    hwnd = int(window.winId())
    api.SetForegroundWindow(hwnd)
    return api.GetForegroundWindow() == hwnd


def _create_activation_receiver(mutex_name):
    """Only called on qt_runtime's thread; retained until process exit."""
    global _activation_receiver
    from ctypes import wintypes
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication, QWidget
    from shiboken6 import isValid
    if _activation_receiver is not None:
        return
    message_id = _activation_user32().RegisterWindowMessageW(_ACTIVATION_MESSAGE)
    if not message_id:
        raise ctypes.WinError(ctypes.get_last_error())

    class ActivationReceiver(QWidget):
        def __init__(self):
            super().__init__()
            self.last = None
            self.setWindowTitle(_receiver_title(mutex_name))
            self.winId()  # Create a discoverable native handle, without showing.
            QApplication.instance().installEventFilter(self)

        def closeEvent(self, event):
            event.ignore()
            self.hide()

        def eventFilter(self, watched, event):
            if (event.type() == QEvent.Type.WindowActivate
                    and type(watched).__name__ in _WINDOW_PRIORITY):
                self.last = watched
            return False

        def nativeEvent(self, event_type, message):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == message_id:
                try:
                    last = self.last if self.last is not None and isValid(self.last) else None
                    return True, int(_activate_existing_window(last))
                except Exception:
                    logging.getLogger('Samsara').exception('[INSTANCE] Window activation failed')
                    return True, 0
            return super().nativeEvent(event_type, message)

    _activation_receiver = ActivationReceiver()


def start_activation_listener():
    if sys.platform != 'win32':
        return
    from samsara.ui import qt_runtime
    name = mutex_name_for_profile()

    def install():
        try:
            _create_activation_receiver(name)
        except Exception:
            logging.getLogger('Samsara').exception('[INSTANCE] Activation receiver unavailable')

    qt_runtime.post(install)
