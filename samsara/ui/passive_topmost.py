"""Repair native topmost state without activating a passive status window."""
import ctypes
import sys

from PySide6.QtCore import QCoreApplication, QObject, QEvent, QTimer
from shiboken6 import isValid


def restore(widget):
    if sys.platform != 'win32' or not widget.isVisible():
        return
    # Explicit pointer types are required for 64-bit HWNDs. Never activate,
    # reposition, resize, or reorder an owner as part of repairing this HWND.
    from ctypes import wintypes
    set_pos = ctypes.windll.user32.SetWindowPos
    set_pos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, wintypes.UINT]
    set_pos.restype = wintypes.BOOL
    set_pos(int(widget.winId()), -1, 0, 0, 0, 0, 0x0010 | 0x0001 | 0x0002 | 0x0200)


class PassiveTopmost(QObject):
    """Reapply after handle/display changes and repair later native demotion."""

    def __init__(self, widget):
        super().__init__(widget)
        self.widget = widget
        self._stopped = self._filtering = self._refreshing = False
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        widget.destroyed.connect(self.stop)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)
        widget.installEventFilter(self)

    def stop(self, *_):
        if self._stopped:
            return
        self._stopped = True
        if isValid(self.timer):
            self.timer.stop()
        if isValid(self) and isValid(self.widget):
            self.widget.removeEventFilter(self)

    def refresh(self):
        if self._stopped or self._refreshing or sys.is_finalizing():
            return
        self._refreshing = True
        try:
            if not QCoreApplication.closingDown() and isValid(self.widget):
                restore(self.widget)
        finally:
            self._refreshing = False

    def eventFilter(self, watched, event):
        if self._stopped or self._filtering or sys.is_finalizing():
            return False
        self._filtering = True
        try:
            if QCoreApplication.closingDown() or not isValid(self.widget):
                return False
            return self._filter_event(event)
        finally:
            self._filtering = False

    def _filter_event(self, event):
        kind = event.type()
        if kind in (QEvent.Type.Show, QEvent.Type.WinIdChange,
                            QEvent.Type.ScreenChangeInternal):
            if self.widget.isVisible():
                self.timer.start()
                QTimer.singleShot(0, self, self.refresh)
        elif kind == QEvent.Type.Hide:
            self.timer.stop()
        return False
