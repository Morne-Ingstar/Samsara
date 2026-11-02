"""Quiet Home entry to the existing updater; never schedules a network check."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget
from shiboken6 import isValid

from samsara.ui import theme, update_qt
from samsara.ui.update_channel_qt import channel_token


# PLACEHOLDER COPY (996)
RELEASE_NOTE = "Samsara is newly released — expect frequent updates."


class HomeUpdateLine(QWidget):
    def __init__(self, app, parent=None, notes_button=None):
        super().__init__(parent)
        self.setObjectName("homeUpdateLine")
        self._app = app
        self._controller = None
        self._release = None
        self._channel = channel_token(app)
        self._seen_auto_release = None
        row = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.status = QLabel(RELEASE_NOTE)
        self.status.setObjectName("homeUpdateStatus")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
        self.status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.status.setMinimumWidth(0)
        self.button = QPushButton("Check for updates")
        self.button.setObjectName("homeCheckForUpdatesButton")
        self.button.setAccessibleName(self.button.text())
        theme.make_secondary(self.button)
        from samsara.ui.home_qt import MIN_TARGET, _px
        self.button.setStyleSheet(self.button.styleSheet() +
                                 f"QPushButton {{ min-height: {max(MIN_TARGET, _px(30))}px; }}")
        self.button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.button.clicked.connect(self._clicked)
        row.addWidget(self.status, 1)
        self.actions = QWidget(self)
        actions = QHBoxLayout(self.actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(12)
        actions.addWidget(self.button)
        self.notes_button = notes_button
        if notes_button is not None:
            actions.addWidget(notes_button)
        row.addWidget(self.actions, 0, Qt.AlignmentFlag.AlignRight)
        # Observe already-completed checks only. This timer does no I/O.
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        self._set(RELEASE_NOTE)
        self.refresh()

    def resizeEvent(self, event):
        # 1014: preserve complete action targets; wrap the row in narrow hosts.
        stacked = self.width() < self.actions.sizeHint().width() + self.status.minimumSizeHint().width() + 12
        self.layout().setDirection(QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight)
        super().resizeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _set(self, text, button="Check for updates", enabled=True):
        self.status.setText(text)
        self.button.setText(button)
        self.button.setAccessibleName(button)
        self.button.setEnabled(enabled)
        self.button.setMinimumWidth(self.button.sizeHint().width())

    def _available(self, release):
        self._release = release
        action = ("Get the installer" if getattr(release, "installer_link_only", False)
                  else f"Install v{release.version}…")
        self._set(f"Version {release.version} is available.", action)

    def refresh(self):
        if self.notes_button is not None:
            self.notes_button.refresh()
        token = channel_token(self._app)
        tray = getattr(self._app, "tray_icon", None)
        automatic = getattr(tray, "_available_update", None)
        if token != self._channel:
            self._channel = token
            self._release = None
            self._seen_auto_release = automatic
            self._set(RELEASE_NOTE)
        dialog = update_qt._dialog
        if dialog is None or not isValid(dialog) or dialog.app is not self._app:
            dialog = self._controller
        if dialog is not None and isValid(dialog):
            phase = dialog._phase
            if phase == "checking":
                self._set("Checking for updates…", enabled=False)
                return
            if phase == "preparing":
                self._set(dialog._status.text(), enabled=False)
                return
        settings = self._app.config.get("updates", {})
        if (isinstance(settings, dict) and settings.get("automatic_checks", False)
                and automatic is not None and automatic is not self._seen_auto_release):
            self._available(automatic)
            return
        if dialog is not None and isValid(dialog):
            if getattr(dialog, "_check_channel", token) == token:
                if phase == "available" and dialog._release is not None:
                    self._available(dialog._release)
                    return
                if phase == "installer":
                    self._set(dialog._status.text(), "Get the installer")
                    return
                if dialog._status.text() != "Ready to check for a packaged update.":
                    self._release = None
                    self._set(dialog._status.text())
                    return

    def _clicked(self):
        self.refresh()
        if not self.button.isEnabled():
            return
        if self._release is not None or self.button.text() == "Get the installer":
            # Reuse the existing confirmation, verification and install flow.
            update_qt.show_update_dialog(self._app, initial_release=self._release)
            return
        dialog = update_qt._dialog
        if dialog is None or not isValid(dialog) or dialog.app is not self._app:
            if self._controller is None or not isValid(self._controller):
                self._controller = update_qt._UpdateDialog(self._app)
                # closeEvent keeps an in-flight worker alive until it reports.
                self.destroyed.connect(self._controller.close)
            dialog = self._controller
        self._seen_auto_release = getattr(getattr(self._app, "tray_icon", None),
                                          "_available_update", None)
        dialog.start_check()
        self.refresh()
