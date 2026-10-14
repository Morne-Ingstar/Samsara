"""Connection controls below the agents pack's single switch."""
from contextlib import nullcontext
import secrets
import re

from PySide6.QtCore import QMimeData, QTimer
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from samsara.agent_server import for_app
from samsara.ui import theme

_EXCLUDE_HISTORY = 'application/x-qt-windows-mime;value="ExcludeClipboardContentFromMonitorProcessing"'
_CLIPBOARD_TIMEOUT_MS = 60_000


def claude_code_command(details, *, masked=False):
    """Format only validated local connection details; never execute the command."""
    if not isinstance(details, str):
        return None
    match = re.fullmatch(r'(http://127\.0\.0\.1:([0-9]+)/mcp)\nAuthorization: Bearer ([A-Za-z0-9_-]{40,128})', details)
    if not match or not 1 <= int(match[2]) <= 65535:
        return None
    token = '<token hidden>' if masked else match[3]
    return (f'claude mcp add --transport http --scope user samsara {match[1]} '
            f'--header "Authorization: Bearer {token}"')


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
        # STAND-IN COPY (570): wording awaits the owner's review.
        heading = QLabel("Connect an AI app")
        heading.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(heading)
        self.command_preview = QLabel()
        self.command_preview.setWordWrap(True)
        self.command_preview.setAccessibleName("Claude Code connection command, token hidden")
        self.command_preview.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(self.command_preview)
        self.code_copy = QPushButton("Copy Claude Code command")
        self.code_copy.setAccessibleName("Copy Claude Code connection command")
        self.code_copy.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.code_copy.setAutoDefault(True)
        self.code_copy.setStyleSheet(f"font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(self.code_copy)
        for text in ("Show me where it goes: paste into PowerShell. User scope connects Claude Code across your projects.",
                     "After New token: run claude mcp remove samsara --scope user, then copy and run the new command.",
                     "If you used the earlier local setup, run claude mcp remove samsara in that project first.",
                     "Claude Desktop needs a small helper - coming next"):
            label = QLabel(text)
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
            layout.addWidget(label)
            if text.startswith("Claude Desktop"):
                self.desktop = label
                label.setEnabled(False)
                label.setStyleSheet(f"color: {theme.TEXT_DISABLED}; font-size: {theme.TYPE_BODY}px;")
                label.setAccessibleName(text)
        buttons = QHBoxLayout()
        self.copy = QPushButton("Copy connection details")  # STAND-IN COPY (564)
        self.rotate = QPushButton("New token")  # STAND-IN COPY (564)
        for button in (self.copy, self.rotate):
            button.setMinimumHeight(theme.HIT_TARGET_MIN)
            button.setAutoDefault(True)
            button.setAccessibleName(button.text())
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.typing = QCheckBox("Let AI apps type for me")  # STAND-IN COPY (565)
        self.typing.setObjectName("agent_server.allow_typing")
        self.typing.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.typing.setChecked(self._typing_enabled())
        layout.addWidget(self.typing)
        # STAND-IN COPY (565c): wording awaits the owner's review.
        self.approval_label = QLabel("How I approve typing")
        layout.addWidget(self.approval_label)
        self.approval_style = QComboBox()
        self.approval_style.setObjectName("agent_server.command_key_only")
        self.approval_style.setAccessibleName("How I approve typing")
        self.approval_style.addItem("Just say yes", False)
        self.approval_style.addItem("Hold the Command key and say yes", True)
        self.approval_style.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.approval_style.setStyleSheet(f"font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(self.approval_style)
        guard = getattr(owner, "_visit_callback", lambda fn, *widgets: fn)
        enabled.toggled.connect(guard(self.toggle, self))
        self.copy.clicked.connect(guard(self.copy_details, self))
        self.code_copy.clicked.connect(guard(self.copy_code_command, self))
        self.rotate.clicked.connect(guard(self.new_token, self))
        self.typing.toggled.connect(guard(self.toggle_typing, self))
        self.approval_style.currentIndexChanged.connect(guard(self.toggle_approval_style, self))
        self.refresh()

    def refresh(self, token_failed=False):
        # STAND-IN COPY (564)
        labels = {"off": "Off.", "listening": "Ready for apps on this PC.",
                  "unavailable": "Could not start. The port or saved token may be unavailable."}
        self.status.setText("Could not save a new token." if token_failed else labels[self.server.status])
        self.copy.setEnabled(self.server.status == "listening")
        self.rotate.setEnabled(self.server.status == "listening")
        preview = claude_code_command(self.server.connection_details(), masked=True)
        unavailable = ("Turn on Let AI apps use Samsara to connect." if self.server.status == "off"
                       else "The server is not running. Connection details are unavailable.")
        self.command_preview.setText("Claude Code: " + (preview or unavailable))
        self.code_copy.setEnabled(self.server.status == "listening" and preview is not None)
        self.typing.setEnabled((self.owner.app.config.get("command_packs") or {}).get("agents") is True)
        self.typing.blockSignals(True)
        self.typing.setChecked(self._typing_enabled())
        self.typing.blockSignals(False)
        self.approval_label.setEnabled(self._typing_enabled())
        self.approval_style.setEnabled(self._typing_enabled())
        self.approval_style.blockSignals(True)
        self.approval_style.setCurrentIndex(int((self.owner.app.config.get("agent_server") or {}).get("command_key_only", False) is True))
        self.approval_style.blockSignals(False)

    def _typing_enabled(self):
        config = self.owner.app.config
        return ((config.get("command_packs") or {}).get("agents") is True
                and (config.get("agent_server") or {}).get("allow_typing") is True)

    def toggle(self, enabled):
        app = self.owner.app
        with getattr(app, "_config_lock", None) or nullcontext():
            packs = app.config.setdefault("command_packs", {})
            old = packs.get("agents", False)
            typing = app.config.setdefault("agent_server", {})
            old_typing = typing.get("allow_typing", False)
            packs["agents"] = bool(enabled)
            if not enabled:
                typing["allow_typing"] = False
            try:
                app.save_config()
            except Exception:
                packs["agents"] = old
                typing["allow_typing"] = old_typing
                self.enabled.blockSignals(True)
                self.enabled.setChecked(old)
                self.enabled.blockSignals(False)
                self.refresh()
                return
        self.server.sync()
        self.refresh()

    def toggle_typing(self, enabled):
        app = self.owner.app
        with getattr(app, "_config_lock", None) or nullcontext():
            config = app.config.setdefault("agent_server", {})
            old = config.get("allow_typing", False)
            config["allow_typing"] = bool(enabled) and self.server.enabled
            try:
                app.save_config()
            except Exception:
                config["allow_typing"] = old
        if not self.server.typing_enabled:
            self.server.cancel_typing()
        self.refresh()

    def toggle_approval_style(self, index):
        app = self.owner.app
        with getattr(app, "_config_lock", None) or nullcontext():
            config = app.config.setdefault("agent_server", {})
            old = config.get("command_key_only", False)
            config["command_key_only"] = bool(self.approval_style.itemData(index))
            try:
                app.save_config()
            except Exception:
                config["command_key_only"] = old
        # A pending question retains its wording and rule; apply changes next time.
        self.refresh()

    def copy_details(self, *_args):
        self._copy_private(self.server.connection_details())

    def copy_code_command(self, *_args):
        self._copy_private(claude_code_command(self.server.connection_details()))

    def _copy_private(self, details):
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
