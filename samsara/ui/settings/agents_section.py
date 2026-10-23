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
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from samsara.agent_server import for_app
from samsara.ui import theme
from .presentation import Disclosure, SettingRow
from .calm_controls import switch
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


def hermes_profile_env(details, *, masked=False):
    """Use the same validated local credentials as the Claude Code route."""
    if claude_code_command(details) is None:
        return None
    token = '<token hidden>' if masked else details.rsplit(' ', 1)[1]
    return f'SAMSARA_MCP_TOKEN={token}'


def hermes_config(port):
    if type(port) is not int or not 1 <= port <= 65535:
        return None
    return ('mcp_servers:\n  samsara:\n'
            f'    url: http://127.0.0.1:{port}/mcp\n'
            '    headers:\n      Authorization: "Bearer ${SAMSARA_MCP_TOKEN}"')


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
        layout.setSpacing(theme.SPACE_2)
        heading = QLabel("Connect an AI app")
        heading.setProperty("role", "title")
        layout.addWidget(heading)

        def note(text, target=layout):
            label = QLabel(text)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setProperty("role", "description")
            target.addWidget(label)
            return label

        def group(title):
            label = QLabel(title)
            label.setProperty("role", "settingsGroup")
            layout.addSpacing(theme.SPACE_2)
            layout.addWidget(label)
            return label

        def compact(control):
            control.setMinimumHeight(theme.HIT_TARGET_MIN)
            control.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            if isinstance(control, QComboBox):
                control.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            return control

        self._note = note
        note("Connect Claude Code, Claude Desktop or Hermes to use Samsara's spoken replies and optional typing.")
        divider = QWidget(self)
        divider.setObjectName("settingsPageDivider")
        divider_layout = QVBoxLayout(divider)
        divider_layout.setContentsMargins(0, theme.SPACE_1 // 2, 0, theme.SPACE_1 // 2)
        hairline = QFrame(divider)
        hairline.setObjectName("settingsPageHairline")
        hairline.setFixedHeight(1)
        divider_layout.addWidget(hairline)
        layout.addWidget(divider)

        self.connection_heading = group("Connection")
        switch(enabled)
        enabled.setObjectName("command_packs.agents")
        enabled.setAccessibleName("Let AI apps use Samsara")
        self.enable_row = SettingRow("Let AI apps use Samsara", "Keep Samsara open while using a connected app.", [enabled])
        self.enable_row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.enable_row)
        self.status = note("")

        self.connect_heading = group("Connect an app")
        self.app_selector = compact(QComboBox())
        self.app_selector.setAccessibleName("AI app to connect")
        self.app_selector.addItems(["Claude Code", "Claude Desktop", "Hermes"])
        layout.addWidget(self.app_selector, alignment=Qt.AlignmentFlag.AlignLeft)
        self.routes = _RouteStack()
        self.routes.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.routes)
        self.code_panel, self.desktop_panel, self.hermes_panel = QWidget(), QWidget(), QWidget()
        code, desktop, hermes = (QVBoxLayout(panel) for panel in (self.code_panel, self.desktop_panel, self.hermes_panel))
        for panel, column in zip((self.code_panel, self.desktop_panel, self.hermes_panel), (code, desktop, hermes)):
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(theme.SPACE_2)
            column.setAlignment(Qt.AlignmentFlag.AlignTop)
            self.routes.addWidget(panel)

        note("1. Open Start, type PowerShell, then open Windows PowerShell. If Claude Code is not installed, install it first.", code)
        note("2. Copy this command, paste it into PowerShell, then press Enter. Follow Claude Code's sign-in instructions.", code)
        code.addWidget(CodeSnippet("claude", name="Claude Code sign-in command"))
        note("3. After signing in, open a new PowerShell window.", code)
        note("Select Copy beside the connection command below. Right-click in PowerShell "
             "to paste it, then press Enter. This connection works across your Claude Code projects.", code)
        self.command_preview = CodeSnippet(name="Claude Code connection command, token hidden", copy_action=self.copy_code_command)
        code.addWidget(self.command_preview)
        self.code_copy = self.command_preview.copy_button
        self.code_copy.setAccessibleName("Copy Claude Code connection command")
        self.connection_hint = note("", code)

        self.desktop = note("1. Sign in to Claude Desktop. Open Settings > Developer > Edit Config.", desktop)
        self.desktop.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px;")
        note("2. In claude_desktop_config.json, add the samsara entry under mcpServers, keeping any other entries. "
             "If the file is empty, paste the whole block. Save the file.", desktop)
        self.desktop_preview = CodeSnippet(name="Claude Desktop connection JSON", copy_action=self.copy_desktop_entry)
        desktop.addWidget(self.desktop_preview)
        self.desktop_copy = self.desktop_preview.copy_button
        self.desktop_copy.setAccessibleName("Copy Claude Desktop connection JSON")
        self.desktop_examples = Disclosure("Example: keep existing settings")
        desktop.addWidget(self.desktop_examples)
        note("Before", self.desktop_examples.rows)
        self.desktop_examples.rows.addWidget(CodeSnippet('{\n  "mcpServers": {\n    "other-app": {"command": "other-app"}\n  },\n  "preferences": {}\n}', name="Existing Desktop settings example"))
        note("After: Samsara added alongside the existing app", self.desktop_examples.rows)
        self.merge_example = CodeSnippet(name="Desktop settings with Samsara added")
        self.desktop_examples.rows.addWidget(self.merge_example)
        note("3. Quit and reopen Claude Desktop to load the connection. Keep Samsara open.", desktop)

        note("Use Hermes Agent's MCP connection. The experimental Hermes integration is in Settings > Integrations.", hermes)
        note("1. Open your Hermes profile's .env file. Add or replace this line, then save. Copy includes your private token.", hermes)
        self.hermes_env = CodeSnippet(name="Hermes profile token, token hidden", copy_action=self.copy_hermes_env)
        hermes.addWidget(self.hermes_env)
        note("2. Open config.yaml in the same profile. Add samsara under mcp_servers, keeping any other servers, then save.", hermes)
        self.hermes_preview = CodeSnippet(name="Hermes MCP configuration", copy_action=self.copy_hermes_config)
        hermes.addWidget(self.hermes_preview)
        note("3. Restart your Hermes session to load the connection. Keep Samsara open.", hermes)
        self.hermes_hint = note("", hermes)

        self.help = Disclosure("Connection help")
        layout.addWidget(self.help)
        self.help_routes = _RouteStack()
        self.help.rows.addWidget(self.help_routes)
        for text in (
            "After choosing New token, run this removal command in PowerShell. Then copy and run the connection command again.",
            "After choosing New token, quit and reopen Claude Desktop.",
            "After choosing New token, replace SAMSARA_MCP_TOKEN in your Hermes profile's .env file using step 1, then restart your Hermes session.",
        ):
            panel = QWidget()
            column = QVBoxLayout(panel)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(theme.SPACE_2)
            self.help_routes.addWidget(panel)
            note(text, column)
        code_repairs = self.help_routes.panels[0].layout()
        code_repairs.addWidget(CodeSnippet("claude mcp remove samsara --scope user", name="Remove old Claude Code connection"))
        note("If you connected Samsara for one project before, open PowerShell in that project's folder and run this command first.", code_repairs)
        code_repairs.addWidget(CodeSnippet("claude mcp remove samsara", name="Remove earlier project connection"))
        buttons = QHBoxLayout()
        self.copy, self.rotate = compact(QPushButton("Copy connection details")), compact(QPushButton("New token"))
        for button in (self.copy, self.rotate):
            button.setAutoDefault(True)
            button.setAccessibleName(button.text())
            buttons.addWidget(button)
        buttons.addStretch()
        self.help.rows.addLayout(buttons)

        self.capabilities_heading = group("What connected apps can do")
        self._add_spoken_replies(layout)
        self.typing = switch(QCheckBox())
        self.typing.setAccessibleName("Let AI apps type for me")
        self.typing.setObjectName("agent_server.allow_typing")
        self.typing.setChecked(self._typing_enabled())
        self.typing_row = SettingRow("Typing", "Let AI apps type for me. Samsara asks before typing; say yes after it finishes asking.", [self.typing])
        layout.addWidget(self.typing_row)
        self.approval_style = compact(QComboBox())
        self.approval_style.setObjectName("agent_server.command_key_only")
        self.approval_style.setAccessibleName("How I approve typing")
        self.approval_style.addItem("Say yes after Samsara asks", False)
        self.approval_style.addItem("Hold the Command key and say yes", True)
        self.approval_row = SettingRow("How I approve typing", "Choose how to give permission each time an app asks to type.", [self.approval_style])
        self.approval_label = self.approval_row.heading
        layout.addWidget(self.approval_row)
        self.command_key_button = compact(QPushButton("Command key settings"))
        self.command_key_button.clicked.connect(lambda: self._follow_setting("command_hotkey"))
        layout.addWidget(self.command_key_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.app_selector.currentIndexChanged.connect(self._select_route)
        self._select_route(0)
        guard = getattr(owner, "_visit_callback", lambda fn, *widgets: fn)
        enabled.toggled.connect(guard(self.toggle, self))
        self.copy.clicked.connect(guard(self.copy_details, self))
        self.command_preview.copy_action = guard(self.copy_code_command, self)
        self.desktop_preview.copy_action = guard(self.copy_desktop_entry, self)
        self.hermes_env.copy_action = guard(self.copy_hermes_env, self)
        self.hermes_preview.copy_action = guard(self.copy_hermes_config, self)
        self.rotate.clicked.connect(guard(self.new_token, self))
        self.typing.toggled.connect(guard(self.toggle_typing, self))
        self.approval_style.currentIndexChanged.connect(guard(self.toggle_approval_style, self))
        self.refresh()

    def _add_spoken_replies(self, layout):
        """Controls for the local spoken digest, beside the AI-app controls."""
        from samsara import ava_readiness, hardware_check

        self.digest_choice = QComboBox()
        self.digest_choice.setObjectName("agent_voice.digest")
        self.digest_choice.setAccessibleName("Spoken replies")
        self.digest_choice.addItem("Auto", "auto")
        self.digest_choice.addItem("Off", "off")
        self.digest_choice.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.digest_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.digest_choice.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.digest_description = "Auto uses a local model to shorten replies. Off limits replies to 60 words."
        self.digest_status = QLabel()
        self.digest_status.setWordWrap(True)
        self.digest_status.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
        self.digest_guide = QPushButton("Set up a local model in the Ava guide")
        self.digest_guide.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.digest_guide.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.digest_guide.clicked.connect(lambda: getattr(self.owner.app, "open_ava_guide", lambda: None)())
        self.digest_try = QPushButton("Try it")
        self.digest_try.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.digest_try.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.digest_try.clicked.connect(self.try_digest)
        self.digest_controls = QWidget()
        self.digest_controls.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.digest_controls_layout = QHBoxLayout(self.digest_controls)
        self.digest_controls_layout.setContentsMargins(0, 0, 0, 0)
        self.digest_controls_layout.setSpacing(theme.SPACE_1)
        self.digest_controls_layout.addWidget(self.digest_choice)
        self.digest_controls_layout.addWidget(self.digest_try)
        self.digest_row = SettingRow("Spoken replies", self.digest_description,
                                     [self.digest_controls])
        self.digest_row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.digest_row.grid.addWidget(self.digest_status, 1, 0, 1, 3)
        layout.addWidget(self.digest_row)
        layout.addWidget(self.digest_guide, alignment=Qt.AlignmentFlag.AlignLeft)
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
            status = "Local model not set up - replies are cut to 60 words."
        else:
            status = "Local model is set up, but not ready yet. Check that its local service is running."
        if recommendation is not None and recommendation.route != "local":
            status += " " + recommendation.reason
        self.digest_status.setText(status)
        self.digest_guide.setVisible(not configured)
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
        if not self.isAncestorOf(self.enabled):
            self.enable_row.values.addWidget(self.enabled)
            self.enabled.setAccessibleName("Let AI apps use Samsara")
            self.enabled.show()
        super().showEvent(event)

    def refresh(self, token_failed=False):
        # STAND-IN COPY (564)
        labels = {"off": "Connection is off.", "listening": "Ready for an AI app to connect.",
                  "unavailable": "Could not start. The port or saved token may be unavailable."}
        self.status.setText("Could not save a new token." if token_failed else labels[self.server.status])
        self.copy.setEnabled(self.server.status == "listening")
        self.rotate.setEnabled(self.server.status == "listening")
        preview = claude_code_command(self.server.connection_details(), masked=True)
        unavailable = ("Turn on Let AI apps use Samsara to connect." if self.server.status == "off"
                       else "The server is not running. Connection details are unavailable.")
        self.command_preview.setText(preview or "")
        self.connection_hint.setText(unavailable)
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
        self.merge_example.setText(merged_text)
        self.merge_example.copy_button.setEnabled(entry is not None)
        env = hermes_profile_env(self.server.connection_details(), masked=True)
        self.hermes_env.setText(env or "")
        self.hermes_env.copy_button.setEnabled(self.server.status == "listening" and env is not None)
        self.hermes_preview.setText(hermes_config(port) or "Connection configuration is unavailable.")
        self.hermes_preview.copy_button.setEnabled(hermes_config(port) is not None)
        self.hermes_hint.setText(unavailable)
        self.hermes_hint.setVisible(env is None)
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

    def copy_hermes_env(self, *_args):
        return self._copy_private(hermes_profile_env(self.server.connection_details()))

    def copy_hermes_config(self, *_args):
        port = (self.server.port if self.server.status == 'listening' else
                (self.owner.app.config.get('agent_server') or {}).get('port', 47832))
        return self._copy_private(hermes_config(port))

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
