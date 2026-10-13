"""Connection controls below the agents pack's single switch."""
from contextlib import nullcontext
import secrets

from PySide6.QtCore import QMimeData, QTimer
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from samsara.agent_server import for_app
from samsara.ui import theme

_EXCLUDE_HISTORY = 'application/x-qt-windows-mime;value="ExcludeClipboardContentFromMonitorProcessing"'
_CLIPBOARD_TIMEOUT_MS = 60_000


def _clear_connection_details(clipboard, details, marker):
    mime = clipboard.mimeData()
    if (mime is not None and bytes(mime.data(_EXCLUDE_HISTORY)) == marker
            and clipboard.text() == details):
        clipboard.clear()


class AgentsControls(QWidget):
    def __init__(self, owner, enabled):
        super().__init__()
        self.setObjectName("agentsConnectionControls")
        self.owner, self.enabled = owner, enabled
        self.server = for_app(owner.app)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.copy = QPushButton("Copy connection details")  # STAND-IN COPY (564)
        self.rotate = QPushButton("New token")  # STAND-IN COPY (564)
        for button in (self.copy, self.rotate):
            button.setMinimumHeight(theme.HIT_TARGET_MIN)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        guard = getattr(owner, "_visit_callback", lambda fn, *widgets: fn)
        enabled.toggled.connect(guard(self.toggle, self))
        self.copy.clicked.connect(guard(self.copy_details, self))
        self.rotate.clicked.connect(guard(self.new_token, self))
        self.refresh()

    def refresh(self, token_failed=False):
        # STAND-IN COPY (564)
        labels = {"off": "Off.", "listening": "Ready for apps on this PC.",
                  "unavailable": "Could not start. The port or saved token may be unavailable."}
        self.status.setText("Could not save a new token." if token_failed else labels[self.server.status])
        self.copy.setEnabled(self.server.status == "listening")
        self.rotate.setEnabled(self.server.status == "listening")

    def toggle(self, enabled):
        app = self.owner.app
        with getattr(app, "_config_lock", None) or nullcontext():
            packs = app.config.setdefault("command_packs", {})
            old = packs.get("agents", False)
            packs["agents"] = bool(enabled)
            try:
                app.save_config()
            except Exception:
                packs["agents"] = old
                self.enabled.blockSignals(True)
                self.enabled.setChecked(old)
                self.enabled.blockSignals(False)
                self.refresh()
                return
        self.server.sync()
        self.refresh()

    def copy_details(self, *_args):
        details = self.server.connection_details()
        if details is not None:
            clipboard = QApplication.clipboard()
            mime = QMimeData()
            mime.setText(details)
            marker = secrets.token_bytes(16)
            # Qt maps this MIME name to the native Windows clipboard format.
            mime.setData(_EXCLUDE_HISTORY, marker)
            clipboard.setMimeData(mime)
            # Keep cleanup independent of this Settings visit's widget lifetime.
            QTimer.singleShot(_CLIPBOARD_TIMEOUT_MS,
                              lambda: _clear_connection_details(clipboard, details, marker))

    def new_token(self, *_args):
        self.refresh(token_failed=not self.server.new_token())
