"""Repair native topmost state without activating a passive status window."""
import ctypes
import sys

from PySide6.QtCore import QObject, QEvent, QTimer


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
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        widget.installEventFilter(self)

    def refresh(self):
        restore(self.widget)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.WinIdChange,
                            QEvent.Type.ScreenChangeInternal):
            if self.widget.isVisible():
                self.timer.start()
                QTimer.singleShot(0, self, self.refresh)
        elif event.type() == QEvent.Type.Hide:
            self.timer.stop()
        return False
