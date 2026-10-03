"""One explicit, Qt-thread window opening attempt.

Windows can refuse foreground activation from a background process. In that
case only, put the requested window in view without taking keyboard focus and
    flash its taskbar button. This never sends input or requests topmost status.
"""

import sys

from PySide6.QtWidgets import QApplication

from samsara.log import get_logger

logger = get_logger(__name__)


def _active_modal():
    return QApplication.activeModalWidget()


def surface_requested(window, *, confirm=None):
    """Restore/show, then request activation once for an explicit open.

    Call only on the window's Qt thread. A modal dialog retains focus; the
    requested window may become visible behind it but is not raised over it.
    """
    if _active_modal() is not None:
        return False
    if window.isMinimized():
        window.showNormal()
    else:
        window.show()
    window.raise_()
    window.activateWindow()
    if confirm is None:
        confirm_foreground(window)
    else:
        confirm()
    return True


def confirm_foreground(window):
    """Truthfully report Windows activation refusal and use one visual fallback."""
    if sys.platform != "win32" or _active_modal() is not None:
        return
    try:
        if _is_foreground(window):
            return
        raised = _raise_without_activation(window)
        QApplication.alert(window, 0)
        logger.warning(
            "Explicit window activation refused by Windows; no-activation "
            "raise succeeded=%s, taskbar button flashed", raised)
    except Exception:
        logger.exception("Could not confirm explicit window activation")


def _is_foreground(window):
    import ctypes

    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    return (user32.GetForegroundWindow() or 0) == int(window.winId())


def _raise_without_activation(window):
    import ctypes

    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    flags = 0x0001 | 0x0002 | 0x0010 | 0x0040  # NOSIZE|NOMOVE|NOACTIVATE|SHOWWINDOW
    hwnd = int(window.winId())
    # HWND_TOP raises in Z order without the persistent HWND_TOPMOST state.
    # One call has no demotion step that could fail and strand a topmost window.
    return bool(user32.SetWindowPos(hwnd, ctypes.c_void_p(0), 0, 0, 0, 0, flags))
