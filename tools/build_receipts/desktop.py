"""Monitor-3 placement and focus isolation, confined to the copied process."""
DEVICE = r'\\.\DISPLAY3'


def monitor_rect(monitors):
    matches = [m['Work'] for m in monitors if m['Device'].upper() == DEVICE.upper()]
    if len(matches) != 1:
        raise ValueError('Monitor3Unavailable')
    return matches[0]


def startup_options(rect):
    import subprocess
    info = subprocess.STARTUPINFO()
    info.dwFlags = subprocess.STARTF_USESHOWWINDOW | 4  # STARTF_USEPOSITION
    # The splash predates plugin services and explicitly chooses primaryScreen.
    # Hide the FIRST ShowWindow through STARTUPINFO. The guarded plugin later
    # positions and shows it with SW_SHOWNOACTIVATE, never flashing on primary.
    info.wShowWindow = 0  # SW_HIDE
    info.dwX, info.dwY = rect[:2]
    return dict(startupinfo=info, creationflags=subprocess.CREATE_NO_WINDOW)


def install():
    from PySide6.QtCore import QObject, QEvent, Qt
    from PySide6.QtWidgets import QApplication, QWidget
    screens = [s for s in QApplication.screens() if s.name().upper() == DEVICE.upper()]
    if len(screens) != 1:
        raise ValueError('Monitor3Unavailable')
    rect = screens[0].availableGeometry()
    # Reject all focus/activation requests in this process, including queued
    # product openers. Never restore focus by activating an owner's window.
    QWidget.activateWindow = lambda self: None
    QWidget.raise_ = lambda self: None
    QWidget.setFocus = lambda self, *args: None

    class Guard(QObject):
        def prepare(self, widget):
            if not isinstance(widget, QWidget) or not widget.isWindow():
                return
            widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            if not widget.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus:
                widget.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
            widget.move(rect.x() + 24, rect.y() + 24)

        def eventFilter(self, obj, event):
            if event.type() in (QEvent.Type.Polish, QEvent.Type.Show):
                self.prepare(obj)
            elif event.type() == QEvent.Type.Move and isinstance(obj, QWidget) and obj.isWindow():
                if not rect.contains(obj.frameGeometry()):
                    self.prepare(obj)
            return False

    guard = Guard(QApplication.instance())
    QApplication.instance().installEventFilter(guard)
    for window in QApplication.topLevelWidgets():
        was_splash = window.objectName() == 'samsaraStartupSplash'
        guard.prepare(window)
        if was_splash:
            window.show()
    return guard


def observe():
    """Only copied-process window metadata; no owner titles or UI text."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    screens = [s for s in QApplication.screens() if s.name().upper() == DEVICE.upper()]
    if len(screens) != 1:
        return dict(monitor_present=False, windows_contained=False, nonactivating=False)
    rect = screens[0].availableGeometry()
    windows = [w for w in QApplication.topLevelWidgets() if w.isVisible()]
    return dict(monitor_present=True,
                windows_contained=all(rect.contains(w.frameGeometry()) for w in windows),
                nonactivating=all(w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
                                 and bool(w.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
                                 for w in windows))
