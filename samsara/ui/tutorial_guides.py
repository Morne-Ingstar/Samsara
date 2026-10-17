"""Tutorial guide policy: open in front, return to the tutorial on close."""
import sys

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from samsara.ui import qt_runtime


def activate(window):
    if window.isMinimized():
        window.showNormal()
    window.raise_()
    window.activateWindow()
    if sys.platform == 'win32' and QApplication.platformName() == 'windows':
        import ctypes
        user32 = ctypes.windll.user32
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_int
        user32.SetForegroundWindow(int(window.winId()))
        from samsara.ui.window_activation import confirm_foreground
        confirm_foreground(window)


class _ReturnToTutorial(QObject):
    def __init__(self, tutorial, guide):
        super().__init__(guide)
        self.tutorial = tutorial
        guide.installEventFilter(self)

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Type.Hide and isValid(self.tutorial)
                and self.tutorial.isVisible()):
            QTimer.singleShot(0, self.tutorial, lambda: activate(self.tutorial))
        return False


def open_guide(tutorial, method):
    """The wrapper posts construction first; ownership/activation follow it."""
    getattr(tutorial._app, method, lambda: None)()

    def finish_open():
        name = {'open_mic_setup_guide': 'mic_setup_wizard',
                'open_ava_guide': 'ava_guide'}[method]
        wrapper = getattr(tutorial._app, name, None)
        guide = getattr(wrapper, 'window', None)
        if guide is None or not guide.isVisible() or not tutorial.isVisible():
            return
        guide.windowHandle().setTransientParent(tutorial.windowHandle())
        if not hasattr(guide, '_tutorial_return'):
            guide._tutorial_return = _ReturnToTutorial(tutorial, guide)
        else:
            guide._tutorial_return.tutorial = tutorial
        activate(guide)

    qt_runtime.post(finish_open)
