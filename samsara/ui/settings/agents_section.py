"""Connection controls below the agents pack's single switch."""
from contextlib import nullcontext
import base64
import json
import os
from pathlib import Path
import secrets
import re
import sys

from PySide6.QtCore import QBuffer, QIODevice, QMimeData, Qt, QTimer
from PySide6.QtGui import QMovie
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from samsara.agent_server import for_app
from samsara.ui import theme
from .presentation import Disclosure
from samsara.ui.widgets.code_snippet import CodeSnippet
from assets.anim.connect_ai import GIF_DATA

_EXCLUDE_HISTORY = 'application/x-qt-windows-mime;value="ExcludeClipboardContentFromMonitorProcessing"'
_CLIPBOARD_TIMEOUT_MS = 60_000


class _RouteStack(QWidget):
    """Only the selected route participates in layout or keyboard traversal."""
    def __init__(self):
        super().__init__()
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(0, 0, 0, 0)
        self.panels = []

    def addWidget(self, panel):
        self.panels.append(panel)
        self.column.addWidget(panel)
        panel.setVisible(len(self.panels) == 1)

    def setCurrentIndex(self, index):
        for i, panel in enumerate(self.panels):
            panel.setVisible(i == index)
        self.updateGeometry()


class SetupIllustration(QWidget):
    """Three stills from the illustrated asset; stack on narrow layouts."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Open Start, open PowerShell, paste then press Enter")
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.cards = []
        data = QBuffer(self)
        data.setData(base64.b64decode(GIF_DATA))
        data.open(QIODevice.OpenModeFlag.ReadOnly)
        movie = QMovie(data, b"gif", self)
        movie.setCacheMode(QMovie.CacheMode.CacheAll)
        for index, caption in zip((0, 8, 19), ("Open Start", "Open PowerShell", "Paste, then press Enter")):
            movie.jumpToFrame(index)
            card = QWidget()
            column = QVBoxLayout(card)
            column.setContentsMargins(0, 0, 0, 0)
            picture = QLabel()
            picture.setPixmap(movie.currentPixmap().scaledToWidth(200, Qt.TransformationMode.SmoothTransformation))
            picture.setAlignment(Qt.AlignmentFlag.AlignLeft)
            column.addWidget(picture)
            label = QLabel(caption)
            label.setWordWrap(True)
            column.addWidget(label)
            self.cards.append(card)
        self._narrow = None
        self._arrange(False)

    def _arrange(self, narrow):
        if narrow == self._narrow:
            return
        self._narrow = narrow
        for index, card in enumerate(self.cards):
            self.grid.removeWidget(card)
            self.grid.addWidget(card, index if narrow else 0, 0 if narrow else index)

    def resizeEvent(self, event):
        self._arrange(self.width() < 660)
        super().resizeEvent(event)


def claude_desktop_entry(port=47832):
    """No secret: frozen paths follow the installed or portable executable."""
    if getattr(sys, 'frozen', False):
        entry = {'command': str(Path(sys.executable).with_name('samsara-mcp.exe'))}
    else:
        relay = Path(__file__).resolve().parents[3] / 'tools' / 'mcp_relay.py'
        entry = {'command': sys.executable, 'args': ['-u', str(relay)]}
    if type(port) is not int or not 1 <= port <= 65535:
        return None
    if port != 47832:
        entry.setdefault('args', []).extend(['--url', f'http://127.0.0.1:{port}/mcp'])
    if os.environ.get('SAMSARA_HOME_DIR'):
        entry['env'] = {'SAMSARA_HOME_DIR': os.environ['SAMSARA_HOME_DIR']}
    return json.dumps({'mcpServers': {'samsara': entry}}, indent=2)


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
        self.setStyleSheet(f"QWidget#agentsConnectionControls {{ background: {theme.BG0}; }}")
        self.owner, self.enabled = owner, enabled
        self.server = for_app(owner.app)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        # 663: delegated copy; external-client setup still awaits owner review.
        heading = QLabel("Connect an AI app")
        heading.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(heading)
        self._add_spoken_replies(layout)
        self.enable_row = QHBoxLayout()
        self.enable_row.addWidget(QLabel("Let AI apps use Samsara"))
        self.enable_row.addStretch()
        self.enable_row.addWidget(enabled)
        layout.addLayout(self.enable_row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        def note(text, target=layout):
            label = QLabel()
            label.setWordWrap(True)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setStyleSheet(
                f"QLabel {{ color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px; border: 1px solid transparent; text-decoration: none; }}"
                f"QLabel:focus {{ border-color: {theme.ACCENT}; text-decoration: none; }}")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
            label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setText(text)
            label.linkActivated.connect(getattr(owner, "_visit_callback", lambda fn, *widgets: fn)(self._follow_setting, label))
            label.linkHovered.connect(lambda url: label.setCursor(Qt.CursorShape.PointingHandCursor if url else Qt.CursorShape.ArrowCursor))
            target.addWidget(label)
            return label

        self._note = note
        note("Sign in to the app you choose below.")
        note("Keep Samsara open and turn on " + self._setting_link("Let AI apps use Samsara", "agents") + ".")
        self.app_selector = QComboBox()
        self.app_selector.setAccessibleName("AI app to connect")
        self.app_selector.addItems(["Claude Code", "Claude Desktop"])
        self.app_selector.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.app_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.app_selector.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.app_selector)
        self.routes = _RouteStack()
        self.routes.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.routes)
        self.code_panel, self.desktop_panel = QWidget(), QWidget()
        code, desktop = QVBoxLayout(self.code_panel), QVBoxLayout(self.desktop_panel)
        for column in (code, desktop):
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(10)
            column.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.routes.addWidget(self.code_panel)
        self.routes.addWidget(self.desktop_panel)
        # Keep installation advice before the first command, without inventing
        # a destination for an external client we have not verified.
        note("If the command is not recognised, install Claude Code first.", code)
        note("Claude Code — sign in first", code)
        note("1. Open the Windows Start menu. Type PowerShell, then open Windows PowerShell.", code)
        self.illustration = SetupIllustration()
        code.addWidget(self.illustration)
        note("2. Copy this short command. Right-click inside PowerShell to paste it, then press Enter. "
             "Follow Claude Code's sign-in instructions.", code)
        code.addWidget(CodeSnippet("claude", name="Claude Code sign-in command"))
        note("3. After signing in, open a new PowerShell window.", code)
        note("Select Copy beside the connection command below. Right-click in PowerShell "
             "to paste it, then press Enter. This connection works across your Claude Code projects.", code)
        self.command_preview = CodeSnippet(name="Claude Code connection command, token hidden", copy_action=self.copy_code_command)
        code.addWidget(self.command_preview)
        self.code_copy = self.command_preview.copy_button
        self.code_copy.setAccessibleName("Copy Claude Code connection command")
        self.connection_hint = note("", code)
        self.desktop = QLabel("Claude Desktop")
        self.desktop.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px;")
        desktop.addWidget(self.desktop)
        note("Sign in to Claude Desktop first. Its setup uses a configuration file, with different steps from Claude Code.", desktop)
        note("1. In Claude Desktop, open Settings, then Developer, then Edit Config. "
             "This opens the file named claude_desktop_config.json in your Claude app-data folder.", desktop)
        note("2. Copy the configuration below. In that file, add the samsara entry under mcpServers, "
             "keeping any other entries. If the file is empty, paste the whole block. Save the file.", desktop)
        note("Empty file / {}", desktop)
        self.desktop_preview = CodeSnippet(name="Claude Desktop connection JSON", copy_action=self.copy_desktop_entry)
        desktop.addWidget(self.desktop_preview)
        self.desktop_copy = self.desktop_preview.copy_button
        self.desktop_copy.setAccessibleName("Copy Claude Desktop connection JSON")
        note("If the file is empty, paste the whole block.", desktop)
        note("Existing settings — before", desktop)
        desktop.addWidget(CodeSnippet('{\n  "mcpServers": {\n    "other-app": {"command": "other-app"}\n  },\n  "preferences": {}\n}', name="Existing Desktop settings example"))
        note("Existing settings — after", desktop)
        self.merge_example = CodeSnippet(name="Desktop settings with Samsara added, highlighted comma and entry")
        desktop.addWidget(self.merge_example)
        note("3. Quit and reopen Claude Desktop to load the connection. Keep Samsara open.", desktop)
        layout.addWidget(QLabel("Optional typing"))
        self.typing = QCheckBox("Let AI apps type for me")
        self.typing.setObjectName("agent_server.allow_typing")
        self.typing.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.typing.setChecked(self._typing_enabled())
        layout.addWidget(self.typing)
        note("Samsara asks before typing. Say yes after it finishes asking.")
        self.approval_label = QLabel("How I approve typing")
        layout.addWidget(self.approval_label)
        self.approval_style = QComboBox()
        self.approval_style.setObjectName("agent_server.command_key_only")
        self.approval_style.setAccessibleName("How I approve typing")
        self.approval_style.addItem("Say yes after Samsara asks", False)
        self.approval_style.addItem("Hold the Command key and say yes", True)
        self.approval_style.setMinimumHeight(theme.HIT_TARGET_MIN)
        layout.addWidget(self.approval_style)
        note(self._setting_link("Command key", "command_hotkey"))
        self.help = Disclosure("Connection help")
        layout.addWidget(self.help)
        self.help_routes = _RouteStack()
        self.help.rows.addWidget(self.help_routes)
        code_help, desktop_help = QWidget(), QWidget()
        code_repairs, desktop_repairs = QVBoxLayout(code_help), QVBoxLayout(desktop_help)
        for column in (code_repairs, desktop_repairs):
            column.setContentsMargins(0, 0, 0, 0)
        self.help_routes.addWidget(code_help)
        self.help_routes.addWidget(desktop_help)
        note("After choosing " + self._setting_link("New token", "agent_token") + ", run this removal command in PowerShell. "
             "Then copy and run the connection command again.", code_repairs)
        code_repairs.addWidget(CodeSnippet("claude mcp remove samsara --scope user", name="Remove old Claude Code connection"))
        note("If you connected Samsara for one project before, open PowerShell in that project's folder and run this command first.", code_repairs)
        code_repairs.addWidget(CodeSnippet("claude mcp remove samsara", name="Remove earlier project connection"))
        note("After choosing " + self._setting_link("New token", "agent_token") + ", quit and reopen Claude Desktop.", desktop_repairs)
        buttons = QHBoxLayout()
        self.copy, self.rotate = QPushButton("Copy connection details"), QPushButton("New token")
        for button in (self.copy, self.rotate):
            button.setMinimumHeight(theme.HIT_TARGET_MIN)
            button.setAutoDefault(True)
            button.setAccessibleName(button.text())
            buttons.addWidget(button)
        buttons.addStretch()
        self.help.rows.addLayout(buttons)
        self.app_selector.currentIndexChanged.connect(self._select_route)
        self._select_route(0)
        guard = getattr(owner, "_visit_callback", lambda fn, *widgets: fn)
        enabled.toggled.connect(guard(self.toggle, self))
        self.copy.clicked.connect(guard(self.copy_details, self))
        self.command_preview.copy_action = guard(self.copy_code_command, self)
        self.desktop_preview.copy_action = guard(self.copy_desktop_entry, self)
        self.rotate.clicked.connect(guard(self.new_token, self))
        self.typing.toggled.connect(guard(self.toggle_typing, self))
        self.approval_style.currentIndexChanged.connect(guard(self.toggle_approval_style, self))
        self.refresh()

    def _add_spoken_replies(self, layout):
        """Controls for the local spoken digest, beside the AI-app controls."""
        from samsara import ava_readiness, hardware_check

        title = QLabel("Spoken replies")
        title.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px; font-weight: 600;")
        layout.addWidget(title)
        self.digest_choice = QComboBox()
        self.digest_choice.setAccessibleName("Spoken replies")
        self.digest_choice.addItem("Auto", "auto")
        self.digest_choice.addItem("Off", "off")
        self.digest_choice.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.digest_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.digest_choice.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.digest_choice)
        note = QLabel("A local model can make spoken replies shorter and smarter. Without it, Samsara speaks up to 60 words.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(note)
        self.digest_status = QLabel()
        self.digest_status.setWordWrap(True)
        self.digest_status.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(self.digest_status)
        self.digest_guide = QPushButton("Set it up in the Ava guide")
        self.digest_guide.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.digest_guide.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.digest_guide.clicked.connect(lambda: getattr(self.owner.app, "open_ava_guide", lambda: None)())
        self.digest_try = QPushButton("Try it")
        self.digest_try.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.digest_try.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.digest_try.clicked.connect(self.try_digest)
        digest_actions = QHBoxLayout()
        digest_actions.setAlignment(Qt.AlignmentFlag.AlignLeft)
        digest_actions.addWidget(self.digest_guide)
        digest_actions.addWidget(self.digest_try)
        layout.addLayout(digest_actions)
        self.digest_choice.currentIndexChanged.connect(self._save_digest_choice)
        self._digest_ava_readiness = ava_readiness
        self._digest_hardware_check = hardware_check
        self._refresh_digest_status()

    def _refresh_digest_status(self):
        app = self.owner.app
        config = app.config
        local_model = (config.get("ollama", {}) or {}).get("model")
        configured = bool(local_model and (config.get("ollama", {}) or {}).get("enabled", True))
        readiness = self._digest_ava_readiness.readiness_for(app)
        # Only report a hardware recommendation when the user has already
        # explicitly checked this PC and the facts remain available in memory.
        facts = getattr(app, "_hardware_facts", None)
        recommendation = self._digest_hardware_check.recommend(facts) if facts is not None else None
        if configured and readiness.ready and self._digest_ava_readiness.configured_provider(app) == "ollama":
            status = "Ready - local model on this PC"
        elif not configured:
            status = "Local model not set up - replies are cut to 60 words. Set it up in the Ava guide"
        else:
            status = "Local model is set up, but not ready yet. Check its setup in the Ava guide."
        if recommendation is not None and recommendation.route != "local":
            status += " " + recommendation.reason
        self.digest_status.setText(status)
        self.digest_guide.setVisible(not configured or not readiness.ready)
        choice = (config.get("agent_voice", {}) or {}).get("digest", "auto")
        self.digest_choice.blockSignals(True)
        self.digest_choice.setCurrentIndex(max(0, self.digest_choice.findData(choice)))
        self.digest_choice.blockSignals(False)

    def _save_digest_choice(self, index):
        app = self.owner.app
        config = app.config.setdefault("agent_voice", {})
        old = config.get("digest", "auto")
        config["digest"] = self.digest_choice.itemData(index)
        try:
            app.save_config()
        except Exception:
            config["digest"] = old
            self.digest_choice.blockSignals(True)
            self.digest_choice.setCurrentIndex(max(0, self.digest_choice.findData(old)))
            self.digest_choice.blockSignals(False)

    def try_digest(self):
        """Speak one canned long reply through the same router as tutorial preview."""
        from samsara.agent_channel.spoken_digest import first_reply

        sentence = ("Samsara keeps the full answer on screen while it speaks a short summary. "
                    "You can ask to read the rest whenever you want. This example shows how a long answer "
                    "can be easier to follow by voice, while the details remain available to read.")
        reply = " ".join((sentence.split() * 20)[:400])
        app = self.owner.app
        result = first_reply(app, reply, source="gpt")
        coordinator = getattr(app, "audio_coordinator", None)
        if coordinator is None:
            return False
        cfg = app.config.get("tts", {}) or {}
        return coordinator.speak(result.spoken, voice_id=cfg.get("voice_id"), speed=cfg.get("speed", 1),
                                 volume=cfg.get("volume", .8), category="readback")

    @staticmethod
    def _setting_link(text, key):
        return f'<a href="{key}" style="color: {theme.ACCENT}; text-decoration: none;">{text}</a>'

    def _follow_setting(self, key):
        navigate = getattr(self.owner, "focus_setting", None)
        if navigate is not None:
            navigate(key)

    def _select_route(self, index):
        self.routes.setCurrentIndex(index)
        self.help_routes.setCurrentIndex(index)
        self.routes.adjustSize()
        self.routes.updateGeometry()

    def showEvent(self, event):
        # The source pack factory appends this SAME checkbox after constructing
        # us. Move it back from that row only when the lazy tool is exposed.
        if self.enabled.parentWidget() is not self:
            self.enable_row.addWidget(self.enabled)
            self.enabled.setAccessibleName("Let AI apps use Samsara")
            self.enabled.show()
        super().showEvent(event)

    def refresh(self, token_failed=False):
        # STAND-IN COPY (564)
        labels = {"off": "Off.", "listening": "Ready for an AI app to connect.",
                  "unavailable": "Could not start. The port or saved token may be unavailable."}
        self.status.setText("Could not save a new token." if token_failed else labels[self.server.status])
        self.copy.setEnabled(self.server.status == "listening")
        self.rotate.setEnabled(self.server.status == "listening")
        preview = claude_code_command(self.server.connection_details(), masked=True)
        unavailable = ("Turn on Let AI apps use Samsara to connect." if self.server.status == "off"
                       else "The server is not running. Connection details are unavailable.")
        self.command_preview.setText(preview or "")
        self.connection_hint.setText(unavailable.replace("Let AI apps use Samsara", self._setting_link("Let AI apps use Samsara", "agents")))
        self.connection_hint.setVisible(preview is None)
        self.code_copy.setEnabled(self.server.status == "listening" and preview is not None)
        port = (self.server.port if self.server.status == 'listening' else
                (self.owner.app.config.get('agent_server') or {}).get('port', 47832))
        entry = claude_desktop_entry(port)
        self.desktop_preview.setText(entry or "Connection configuration is unavailable.")
        self.desktop_copy.setEnabled(entry is not None)
        merged = json.loads(entry) if entry else {"mcpServers": {}}
        merged["mcpServers"] = {"other-app": {"command": "other-app"}, **merged["mcpServers"]}
        merged["preferences"] = {}
        merged_text = json.dumps(merged, indent=2)
        lines = merged_text.splitlines()
        if entry:
            start = next(i for i, line in enumerate(lines) if line.startswith('    "samsara":'))
            end = next(i for i, line in enumerate(lines) if i > start and line == '    }')
            self.merge_example.setText(merged_text, highlight_lines=range(start - 1, end + 1))
        else:
            self.merge_example.setText(merged_text)
        self.merge_example.copy_button.setEnabled(entry is not None)
        self.typing.setEnabled((self.owner.app.config.get("command_packs") or {}).get("agents") is True)
        self.typing.blockSignals(True)
        self.typing.setChecked(self._typing_enabled())
        self.typing.blockSignals(False)
        self.approval_label.setEnabled(self._typing_enabled())
        self.approval_style.setEnabled(self._typing_enabled())
        self.approval_style.blockSignals(True)
        self.approval_style.setCurrentIndex(int((self.owner.app.config.get("agent_server") or {}).get("command_key_only", False) is True))
        self.approval_style.blockSignals(False)
        self._refresh_digest_status()

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
        return self._copy_private(claude_code_command(self.server.connection_details()))

    def copy_desktop_entry(self, *_args):
        # Use the live port when running, configured port while the pack is off.
        port = (self.server.port if self.server.status == 'listening' else
                (self.owner.app.config.get('agent_server') or {}).get('port', 47832))
        return self._copy_private(claude_desktop_entry(port))

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
            return True
        return False

    def new_token(self, *_args):
        self.refresh(token_failed=not self.server.new_token())
