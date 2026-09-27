"""Ava / Cloud page of the Samsara settings window.

Moved verbatim out of samsara/ui/settings_qt.py (queue 21, no behaviour change).
AvaCloudPage is mixed into settings_qt._SettingsWindow, so every method still runs
with the window as self (self._widgets, self._save_fns, self._setting_row ...).
Shared names are imported back from samsara.ui.settings_qt; see
samsara/ui/settings/__init__.py for why that import is cycle-safe.
"""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from samsara.runtime import thread_registry
from samsara import ai_preferences, ava_readiness, config_defaults
from samsara.ava_memory import PersonalMemory
from samsara.paths import samsara_home_dir
from samsara.log import get_logger
from samsara.ui import ava_consent_qt, theme

from samsara.ui.settings_qt import _CONTENT_MAX_WIDTH


_PROVIDERS = [
    ("DeepSeek (default)", "deepseek"),
    ("OpenAI",             "openai"),
    ("Anthropic",          "anthropic"),
    ("OpenRouter",         "openrouter"),
]
_PROVIDER_DISPLAY  = [p[0] for p in _PROVIDERS]
_DISPLAY_TO_CODE   = {p[0]: p[1] for p in _PROVIDERS}
_CODE_TO_DISPLAY   = {p[1]: p[0] for p in _PROVIDERS}

_DEFAULT_MODELS = {
    "deepseek":  "deepseek-chat",
    "openai":    "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-20250514",
    "openrouter": "openrouter/auto",
}

_PROVIDER_INFO = {
    "deepseek":  "deepseek-chat — Best value. Cheapest per token, strong reasoning. Recommended for most users.",
    "openai":    "gpt-4o-mini — Fast and widely supported. Good for general tasks and tool use.",
    "anthropic": "claude-sonnet-4 — Best reasoning and instruction following. Higher cost per token.",
    "openrouter": "One key, many models. Auto-picks per request, or enter a model slug below.",
}


_WEB_SEARCH_NOTE = (
    "Off by default. When on, your question to Ava (and the recent conversation) "
    "is sent to DeepSeek's servers, which search the web and read pages to answer. "
    "Ava speaks a short summary and shows the full answer and its sources on screen. "
    "Web answers are treated as information only: they can never run a command, "
    "press keys or change files. Each search uses extra DeepSeek tokens."
)
_WEB_SEARCH_UNAVAILABLE = (
    "Web search is DeepSeek-only; you're using {provider}. "
    "Local models (Ollama) and the other providers have no built-in web search."
)

logger = get_logger(__name__)


from samsara.ui.settings.presentation import CleanComboBox as QComboBox

class AvaCloudPage:
    """Methods of the Ava / Cloud settings page (moved from _SettingsWindow)."""

    def _build_ava_cloud_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
        )

        container = QWidget()
        container.setMaximumWidth(_CONTENT_MAX_WIDTH)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        cfg = self.app.config.get('cloud_llm', {}) or {}
        self._ava_route_touched = False
        self._ava_route_confirmation_pending = ai_preferences.cloud_route_confirmation_needed(self.app.config)
        if self._ava_route_confirmation_pending and not getattr(self.app, "_ava_route_confirmation_logged", False):
            logger.warning("[AVA-ROUTE] Cloud was previously enabled with consent while policy stayed local; explicit route confirmation required")
            self.app._ava_route_confirmation_logged = True

        def _staged_ava_config():
            """Current page choices as one config, without persisting them."""
            effective = dict(self.app.config)
            cloud_cfg = dict(effective.get("cloud_llm", {}) or {})
            route = self._widgets.get("providerRoute")
            policy = route.currentData() if route is not None else (effective.get("ava", {}) or {}).get("provider_policy", "off")
            cloud_cfg["enabled"] = policy == "cloud"
            provider_widget = self._widgets.get("cloud_provider")
            if provider_widget is not None:
                cloud_cfg["provider"] = _DISPLAY_TO_CODE.get(
                    provider_widget.currentText(), "deepseek"
                )
            key_widget = self._widgets.get("cloud_api_key")
            if key_widget is not None:
                cloud_cfg["api_key"] = key_widget.text().strip()
            model_widget = self._widgets.get("cloud_model")
            if model_widget is not None:
                model = model_widget.text().strip()
                if model:
                    cloud_cfg["model"] = model
                else:
                    cloud_cfg.pop("model", None)
            effective["cloud_llm"] = cloud_cfg

            session = dict(effective.get("ava_command_session", {}) or {})
            session["backend"] = "cloud" if cloud_cfg.get("enabled") else "ollama"
            session["enabled"] = policy != "off"
            modes_model = self._widgets.get("ava_cmd_model")
            if modes_model is not None and modes_model.text().strip():
                session["model"] = modes_model.text().strip()
            effective["ava_command_session"] = session

            ava_cfg = dict(effective.get("ava", {}) or {})
            ava_cfg["provider_policy"] = policy
            staged = getattr(self, "_ava_consent_staged", None)
            if staged is not None:
                ava_cfg["consent"] = staged
            effective["ava"] = ava_cfg
            return effective

        self._ava_staged_config = _staged_ava_config

        # ---- Master switch: the one place Ava is turned on or off ---------
        layout.addWidget(self._section_title("Ava"))
        layout.addSpacing(4)
        ava_enabled = QCheckBox("Ava", container)
        ava_enabled.setChecked(ai_preferences.ava_master_enabled(self.app.config))
        ava_enabled.setAccessibleName("Turn Ava on or off")
        self._widgets["ava_enabled"] = ava_enabled
        status = QLabel(ai_preferences.ava_route_status(self.app.config), container)
        status.setAccessibleName("Effective Ava route")
        status.setWordWrap(True)
        self._widgets["ava_route_status"] = status
        layout.addWidget(status)
        interpret_misses = QCheckBox("Ava interprets commands")
        interpret_misses.setChecked(bool(
            (self.app.config.get("ava_command_session", {}) or {}).get("interpret_misses", False)
        ))
        interpret_misses.setEnabled((self.app.config.get("ava", {}) or {}).get("provider_policy") != "off")
        interpret_misses.setAccessibleName("Let Ava interpret unmatched commands")
        interpret_note = QLabel(
            "After an ordinary command miss, Ava may choose one available command."
        )
        interpret_note.setWordWrap(True)
        interpret_note.setStyleSheet(
            f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px; margin-left: 26px;"
        )
        self._widgets["ava_interpret_misses"] = interpret_misses
        layout.addWidget(interpret_misses)
        layout.addWidget(interpret_note)
        layout.addSpacing(12)

        # ---- Section: Cloud AI (bring your own key) -- always visible ------
        layout.addWidget(self._section_title("Cloud AI (bring your own key)"))
        layout.addSpacing(4)

        cloud_enabled = QCheckBox("Cloud route active", container)
        cloud_enabled.setChecked((self.app.config.get("ava", {}) or {}).get("provider_policy") == "cloud")
        self._widgets['cloud_enabled'] = cloud_enabled

        consent_btn = QPushButton("Read this again")
        consent_btn.setAccessibleName("Read what turning Ava on means again")
        theme.make_secondary(consent_btn)
        consent_btn.clicked.connect(lambda: ava_consent_qt.show_consent_copy(self))
        layout.addWidget(consent_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(8)

        # OWNER COPY — REVIEW: This sends a small request at startup so Ava's
        # first reply is fast.
        warm_on_boot = QCheckBox(
            "Warm Ava at startup (sends a small request so the first reply is fast)"
        )
        warm_on_boot.setChecked(ava_readiness.warm_on_boot_enabled(self.app))
        warm_choice_is_explicit = {
            "value": "warm_on_boot" in (self.app.config.get("ava", {}) or {})
        }

        def _remember_warm_choice(_checked):
            warm_choice_is_explicit["value"] = True

        def _default_warm_for_provider(cloud_is_enabled):
            if cloud_is_enabled:
                effective = dict(self.app.config)
                ava_cfg = dict(effective.get("ava", {}) or {})
                staged = getattr(self, "_ava_consent_staged", None)
                if staged is not None:
                    ava_cfg["consent"] = staged
                effective["ava"] = ava_cfg
                if ava_consent_qt.consent_required(effective, cloud_enabled=True):
                    return
            if not warm_choice_is_explicit["value"]:
                was_blocked = warm_on_boot.blockSignals(True)
                warm_on_boot.setChecked(not cloud_is_enabled)
                warm_on_boot.blockSignals(was_blocked)

        warm_on_boot.toggled.connect(_remember_warm_choice)
        cloud_enabled.toggled.connect(_default_warm_for_provider)

        self._widgets['ava_warm_on_boot'] = warm_on_boot
        layout.addWidget(warm_on_boot)
        layout.addSpacing(8)

        # Ava Personality toggle
        layout.addWidget(self._section_title("Ava Personality"))
        layout.addSpacing(4)
        personality_combo = QComboBox()
        personality_combo.addItems(["Relaxed", "Strict"])
        current = self.app.config.get("ava_personality", "relaxed")
        personality_combo.setCurrentText("Strict" if current == "strict" else "Relaxed")
        self._widgets['ava_personality'] = personality_combo
        layout.addWidget(personality_combo)
        personality_note = QLabel(
            "Relaxed: natural conversation, longer answers, self-aware. "
            "Strict: tight persona, 1-3 sentences, stays in character."
        )
        personality_note.setWordWrap(True)
        personality_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px; margin-left: 4px;")
        layout.addWidget(personality_note)
        layout.addSpacing(8)

        # Conversation Memory
        layout.addWidget(self._section_title("Conversation Memory"))
        layout.addSpacing(4)
        mem_cfg = self.app.config.get("ava_memory", {})

        memory_combo = QComboBox()
        memory_combo.addItems(["Clear on close", "Keep last session", "Ava remembers me"])
        mem_mode = mem_cfg.get("mode", "clear")
        memory_combo.setCurrentText(
            {"last": "Keep last session", "assistant": "Ava remembers me"}.get(mem_mode, "Clear on close")
        )
        self._widgets['ava_memory_mode'] = memory_combo
        layout.addWidget(memory_combo)

        memory_note = QLabel(
            "Clear on close: your conversation ends when Samsara closes. "
            "Keep last session: your last conversation returns next time. "
            "Ava remembers me: Ava saves facts you tell her and short session summaries "
            "in a file you can read and erase. Say 'what do you remember about me' or 'forget ...'. "
            "Switching modes leaves that file in place until you erase it."
        )  # OWNER COPY
        memory_note.setWordWrap(True)
        memory_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px; margin-left: 4px;")
        layout.addWidget(memory_note)
        memory_actions = QHBoxLayout()
        open_memory = QPushButton("Open memory file")
        open_memory.setAccessibleName("Open Ava memory file")
        theme.make_secondary(open_memory)
        open_memory.clicked.connect(self._open_ava_memory_file)
        memory_actions.addWidget(open_memory)
        erase_memory = QPushButton("Erase memory")
        erase_memory.setAccessibleName("Erase Ava memory")
        theme.make_secondary(erase_memory)
        erase_memory.clicked.connect(self._erase_ava_memory_file)
        memory_actions.addWidget(erase_memory)
        memory_actions.addStretch()
        layout.addLayout(memory_actions)
        layout.addSpacing(6)

        turns_spin = QSpinBox()
        turns_spin.setRange(5, 500)
        turns_spin.setSingleStep(5)
        turns_spin.setValue(int(mem_cfg.get("max_turns", 20)))
        self._widgets['ava_memory_max_turns'] = turns_spin
        layout.addLayout(self._setting_row(
            "Max turns remembered",
            "How many back-and-forth turns Ava keeps. Higher = more context, "
            "slightly higher cost per reply.",
            turns_spin,
        ))
        layout.addSpacing(8)

        # Provider
        layout.addWidget(self._section_title("Provider"))
        layout.addSpacing(4)

        current_provider = cfg.get('provider', 'deepseek')
        current_display = _CODE_TO_DISPLAY.get(current_provider, _PROVIDER_DISPLAY[0])
        provider_combo = QComboBox()
        provider_combo.addItems(_PROVIDER_DISPLAY)
        provider_combo.setCurrentText(current_display)
        self._ava_previous_provider_display = current_display
        self._widgets['cloud_provider'] = provider_combo
        layout.addLayout(self._setting_row(
            "Provider",
            "Cloud AI provider that processes your voice requests",
            provider_combo,
        ))

        # Provider blurb: plain read-only text, not a framed/bordered box --
        # a QFrame styled like an input field here made static text read as
        # a disabled QLineEdit.
        info_label = QLabel(_PROVIDER_INFO.get(current_provider, ""))
        info_label.setWordWrap(True)
        info_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px; background: transparent;")
        self._widgets['cloud_info_label'] = info_label
        layout.addWidget(info_label)

        provider_combo.currentTextChanged.connect(self._on_cloud_provider_changed)
        layout.addSpacing(8)

        # Explainer above the API key row
        explainer = QLabel(
            "Getting a key from your chosen provider is the only requirement — "
            "no Samsara account, no payment to us."
        )
        explainer.setWordWrap(True)
        explainer.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(explainer)
        setup_link = QLabel("Setup guide: morneis.com/samsara")
        setup_link.setStyleSheet(f"color: {theme.ACCENT}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(setup_link)
        layout.addSpacing(4)

        # API Key (entry + show/hide button as a container widget)
        api_key_container = QWidget()
        ak_layout = QHBoxLayout(api_key_container)
        ak_layout.setContentsMargins(0, 0, 0, 0)
        ak_layout.setSpacing(6)
        api_key_entry = QLineEdit()
        api_key_entry.setEchoMode(QLineEdit.EchoMode.Password)
        api_key_entry.setText(cfg.get('api_key', ''))
        api_key_entry.setPlaceholderText("Paste your API key here")
        api_key_entry.setMinimumWidth(260)
        self._widgets['cloud_api_key'] = api_key_entry
        api_key_entry.textChanged.connect(lambda _text: self._refresh_ava_route_status())
        show_btn = QPushButton("Show")
        show_btn.setCheckable(True)
        show_btn.setFixedWidth(60)
        show_btn.setMinimumHeight(theme.HIT_TARGET_MIN)
        show_btn.setStyleSheet(
            f"QPushButton {{ background-color: transparent; color: {theme.TEXT_SECONDARY}; "
            f"border: 1px solid {theme.wash(0.14)}; border-radius: 6px; "
            f"padding: 6px 10px; font-size: {theme.TYPE_BODY}px; }}"
            f"QPushButton:hover {{ color: {theme.TEXT_PRIMARY}; }}"
            f"QPushButton:checked {{ border-color: {theme.tint(theme.ACCENT, 0.4)}; color: {theme.ACCENT}; }}"
        )
        show_btn.toggled.connect(
            lambda checked: self._toggle_api_key_show(checked, api_key_entry, show_btn)
        )
        ak_layout.addWidget(api_key_entry, stretch=1)
        ak_layout.addWidget(show_btn)
        layout.addLayout(self._setting_row(
            "API Key",
            "Stored locally, never sent to us — only to the provider you chose.",
            api_key_container,
            # api_key_container needs its entry's own 260 minimum + 6px
            # spacing + the 60px Show button (326 total) -- _setting_row's
            # 260 default clamped the container narrower than its own
            # children's combined minimum, so the Show button ended up
            # drawn over the entry's last ~60px instead of beside it.
            control_width=330,
        ))
        layout.addSpacing(8)

        # Model override
        current_model = cfg.get('model', '')
        model_entry = QLineEdit()
        model_entry.setText(current_model)
        model_entry.setPlaceholderText(
            f"Default: {_DEFAULT_MODELS.get(current_provider, '')}"
        )
        model_entry.setMinimumWidth(220)
        self._widgets['cloud_model'] = model_entry
        layout.addLayout(self._setting_row(
            "Model override",
            "Leave blank to use the provider's default model shown in the placeholder",
            model_entry,
        ))
        layout.addSpacing(8)

        # Web search (queue 59) -- explicit cloud opt-in, DeepSeek only.
        layout.addWidget(self._section_title("Web search"))
        layout.addSpacing(4)
        web_search_cb = QCheckBox("Let Ava search the web (DeepSeek only)")
        web_search_cb.setChecked(bool(cfg.get('web_search', False)))
        self._widgets['cloud_web_search'] = web_search_cb
        layout.addWidget(web_search_cb)
        web_search_note = QLabel(_WEB_SEARCH_NOTE)
        web_search_note.setWordWrap(True)
        web_search_note.setTextFormat(Qt.TextFormat.PlainText)
        web_search_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px; margin-left: 26px;")
        layout.addWidget(web_search_note)
        web_search_unavailable = QLabel(_WEB_SEARCH_UNAVAILABLE)
        web_search_unavailable.setWordWrap(True)
        web_search_unavailable.setTextFormat(Qt.TextFormat.PlainText)
        web_search_unavailable.setStyleSheet(f"color: {theme.WARNING}; font-size: {theme.TYPE_BODY}px; margin-left: 26px;")
        self._widgets['cloud_web_search_unavailable'] = web_search_unavailable
        layout.addWidget(web_search_unavailable)
        cloud_enabled.toggled.connect(lambda _checked: self._sync_web_search_availability())
        provider_combo.currentTextChanged.connect(lambda _text: self._sync_web_search_availability())
        self._sync_web_search_availability()
        layout.addSpacing(8)

        # Timeout
        timeout_spin = QSpinBox()
        timeout_spin.setRange(5, 120)
        timeout_spin.setSingleStep(5)
        timeout_spin.setSuffix(" s")
        timeout_spin.setValue(int(cfg.get('timeout_seconds', 30)))
        self._widgets['cloud_timeout'] = timeout_spin
        layout.addLayout(self._setting_row(
            "Timeout",
            "Seconds to wait for the cloud provider before showing an error",
            timeout_spin,
        ))
        layout.addSpacing(12)

        # Test connection
        test_row = QHBoxLayout()
        test_row.setSpacing(12)
        test_btn = QPushButton("Test Connection")
        self._widgets['cloud_test_button'] = test_btn
        test_btn.setMinimumWidth(150)
        test_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        test_btn.clicked.connect(self._run_test_connection)
        test_status = QLabel("")
        test_status.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px;")
        self._widgets['cloud_test_status'] = test_status
        test_row.addWidget(test_btn)
        test_row.addWidget(test_status)
        test_row.addStretch()
        layout.addLayout(test_row)
        layout.addSpacing(20)

        # Prompt 244: supporter-key UI removed because no feature is paywalled;
        # stored premium_license and its handlers remain honoured.
        layout.addWidget(self._section_title("Voice editing (experimental)"))
        layout.addSpacing(4)

        edit_cfg = self.app.config.get("ava_edit", {}) or {}
        if not isinstance(edit_cfg, dict):
            edit_cfg = {}
        edit_touched = set()

        edit_enabled = QCheckBox("Let Ava edit what you just dictated")
        edit_enabled.setChecked(bool(edit_cfg.get(
            "enabled", config_defaults.DEFAULTS["ava_edit.enabled"])))
        self._widgets["ava_edit_enabled"] = edit_enabled
        layout.addWidget(edit_enabled)

        edit_note = QLabel(
            "Say 'Ava, make that more formal'. Ava shows the change; say 'apply' to type it. "
            "Experimental: leave the cursor where it was until you say apply."
        )
        edit_note.setWordWrap(True)
        edit_note.setStyleSheet(
            f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px; margin-left: 26px;"
        )
        layout.addWidget(edit_note)

        edit_options = QWidget()
        edit_options_layout = QVBoxLayout(edit_options)
        edit_options_layout.setContentsMargins(26, 0, 0, 0)
        edit_options_layout.setSpacing(8)

        from samsara.config_schema import SETTINGS_SCHEMA
        timeout_schema = SETTINGS_SCHEMA["ava_edit.timeout_seconds"]
        edit_timeout = QDoubleSpinBox()
        edit_timeout.setRange(
            float(timeout_schema.get("min", 1.0)),
            float(timeout_schema.get("max", 120.0)),
        )
        edit_timeout.setSingleStep(1.0)
        edit_timeout.setDecimals(1)
        edit_timeout.setSuffix(" s")
        edit_timeout.setValue(float(edit_cfg.get(
            "timeout_seconds", config_defaults.DEFAULTS["ava_edit.timeout_seconds"])))
        self._widgets["ava_edit_timeout"] = edit_timeout
        timeout_row = QWidget()
        timeout_row.setLayout(self._setting_row(
            "Timeout",
            "Seconds Ava waits for the edit model to answer",
            edit_timeout,
            control_width=180,
        ))
        self._widgets["ava_edit_timeout_row"] = timeout_row
        edit_options_layout.addWidget(timeout_row)

        edit_model = QComboBox()
        edit_model.addItem("Ava's model", "")
        for provider_display, provider_code in _PROVIDERS:
            edit_model.addItem(
                f"{provider_display}: {_DEFAULT_MODELS[provider_code]}",
                _DEFAULT_MODELS[provider_code],
            )
        current_edit_model = str(edit_cfg.get("model", "") or "")
        current_model_index = edit_model.findData(current_edit_model)
        if current_model_index < 0 and current_edit_model:
            edit_model.addItem(current_edit_model, current_edit_model)
            current_model_index = edit_model.count() - 1
        edit_model.setCurrentIndex(max(0, current_model_index))
        self._widgets["ava_edit_model"] = edit_model
        model_row = QWidget()
        model_row.setLayout(self._setting_row(
            "Model",
            "Choose a provider model, or leave blank for Ava's model",
            edit_model,
        ))
        self._widgets["ava_edit_model_row"] = model_row
        edit_options_layout.addWidget(model_row)

        demo_pacing = QCheckBox("Use demo pacing (slower)")
        demo_pacing.setChecked(edit_cfg.get(
            "demo_pacing", config_defaults.DEFAULTS["ava_edit.demo_pacing"]) == "cinematic")
        self._widgets["ava_edit_demo_pacing"] = demo_pacing
        pacing_row = QWidget()
        pacing_layout = QHBoxLayout(pacing_row)
        pacing_layout.setContentsMargins(0, 0, 0, 0)
        pacing_layout.addWidget(demo_pacing)
        pacing_layout.addStretch()
        self._widgets["ava_edit_demo_pacing_row"] = pacing_row
        edit_options_layout.addWidget(pacing_row)
        layout.addWidget(edit_options)

        def _sync_edit_options(checked):
            edit_options.setVisible(checked)
            for key in (
                "ava_edit_timeout_row",
                "ava_edit_model_row",
                "ava_edit_demo_pacing_row",
            ):
                widget = self._widgets.get(key)
                if widget is not None:
                    widget.setVisible(checked)

        def _mark_edit_touched(key):
            edit_touched.add(key)

        edit_enabled.toggled.connect(lambda checked: (
            _mark_edit_touched("enabled"), _sync_edit_options(checked)))
        edit_timeout.valueChanged.connect(lambda _value: _mark_edit_touched("timeout_seconds"))
        edit_model.currentIndexChanged.connect(lambda _index: _mark_edit_touched("model"))
        demo_pacing.toggled.connect(lambda _checked: _mark_edit_touched("demo_pacing"))
        _sync_edit_options(edit_enabled.isChecked())

        def _save(_acc):
            updates = {}
            if 'cloud_enabled' in self._widgets:
                policy = self._widgets['providerRoute'].currentData()
                provider_display = self._widgets['cloud_provider'].currentText()
                provider = _DISPLAY_TO_CODE.get(provider_display, 'deepseek')
                api_key = self._widgets['cloud_api_key'].text().strip()
                model_override = self._widgets['cloud_model'].text().strip()
                cloud_cfg = dict(self.app.config.get('cloud_llm', {}) or {})
                cloud_cfg['enabled'] = policy == 'cloud'
                cloud_cfg['provider'] = provider
                cloud_cfg['api_key'] = api_key
                cloud_cfg['timeout_seconds'] = self._widgets['cloud_timeout'].value()
                web_search_cb = self._widgets.get('cloud_web_search')
                if web_search_cb is not None:
                    # Stored only while it can apply (Cloud AI on, DeepSeek);
                    # never left silently armed for a later provider switch.
                    cloud_cfg['web_search'] = bool(
                        web_search_cb.isEnabled() and web_search_cb.isChecked())
                if model_override:
                    cloud_cfg['model'] = model_override
                elif 'model' in cloud_cfg:
                    del cloud_cfg['model']
                updates['cloud_llm'] = cloud_cfg

            if 'ava_personality' in self._widgets:
                updates['ava_personality'] = self._widgets['ava_personality'].currentText().lower()

            if 'ava_interpret_misses' in self._widgets:
                session_cfg = dict(_acc.get(
                    'ava_command_session', self.app.config.get('ava_command_session', {})) or {})
                session_cfg['interpret_misses'] = self._widgets['ava_interpret_misses'].isChecked()
                updates['ava_command_session'] = session_cfg

            if 'ava_warm_on_boot' in self._widgets and warm_choice_is_explicit['value']:
                ava_cfg = dict(_acc.get('ava', self.app.config.get('ava', {})) or {})
                ava_cfg['warm_on_boot'] = self._widgets['ava_warm_on_boot'].isChecked()
                updates['ava'] = ava_cfg

            staged_consent = getattr(self, "_ava_consent_staged", None)
            if staged_consent is not None:
                ava_cfg = dict(updates.get('ava', _acc.get('ava', self.app.config.get('ava', {}))) or {})
                ava_cfg['consent'] = staged_consent
                updates['ava'] = ava_cfg

            if 'ava_memory_mode' in self._widgets:
                mem_updates = dict(self.app.config.get('ava_memory', {}))
                mode_text = self._widgets['ava_memory_mode'].currentText()
                mem_updates['mode'] = {"Keep last session": "last", "Ava remembers me": "assistant"}.get(mode_text, "clear")
                if 'ava_memory_max_turns' in self._widgets:
                    mem_updates['max_turns'] = int(
                        self._widgets['ava_memory_max_turns'].value()
                    )
                updates['ava_memory'] = mem_updates

            if edit_touched:
                ava_edit_cfg = dict(_acc.get(
                    "ava_edit", self.app.config.get("ava_edit", {})) or {})
                if "enabled" in edit_touched:
                    ava_edit_cfg["enabled"] = self._widgets[
                        "ava_edit_enabled"].isChecked()
                if "timeout_seconds" in edit_touched:
                    ava_edit_cfg["timeout_seconds"] = self._widgets[
                        "ava_edit_timeout"].value()
                if "model" in edit_touched:
                    ava_edit_cfg["model"] = self._widgets[
                        "ava_edit_model"].currentData()
                if "demo_pacing" in edit_touched:
                    ava_edit_cfg["demo_pacing"] = (
                        "cinematic" if self._widgets[
                            "ava_edit_demo_pacing"].isChecked() else "instant")
                updates["ava_edit"] = ava_edit_cfg

            if self._ava_route_touched:
                effective = dict(self.app.config)
                effective.update(_acc)
                effective.update(updates)
                ava_cfg = dict(effective.get("ava", {}) or {})
                staged = getattr(self, "_ava_consent_staged", None)
                if staged is not None:
                    ava_cfg["consent"] = staged
                effective["ava"] = ava_cfg
                try:
                    preferred = ai_preferences.apply_ava_route_choice(
                        effective, policy=policy, consent=staged)
                except ValueError:
                    # A programmatic or stale control change cannot bypass consent.
                    policy = (self.app.config.get("ava", {}) or {}).get("provider_policy", "off")
                    if policy == "cloud":
                        policy = "off"
                    preferred = ai_preferences.apply_ava_route_choice(effective, policy=policy)
                    self._restore_ava_route(policy)
                    self._widgets['ava_route_status'].setText(
                        "Cloud was not selected because consent was not accepted.")
                for key in ava_consent_qt._PREFERENCE_KEYS:
                    if key in preferred:
                        updates[key] = preferred[key]
            # OWNER COPY: acknowledge sending the optional memory block on
            # cloud requests, including when cloud was enabled before memory.
            mem_choice = updates.get("ava_memory", {}).get("mode")
            cloud_choice = (updates.get("ava", self.app.config.get("ava", {})) or {}).get("provider_policy")
            if mem_choice == "assistant" and cloud_choice == "cloud":
                ava_cfg = dict(updates.get("ava", self.app.config.get("ava", {})) or {})
                consent = dict(ava_cfg.get("consent", {}) or {})
                if not consent.get("memory_cloud_accepted"):
                    answer = QMessageBox.question(
                        self, "Ava memory in cloud requests",
                        "When you use cloud AI, Ava sends your saved facts and recent session summaries "
                        "with each request. Turn on remembered facts for cloud requests?",  # OWNER COPY
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if answer == QMessageBox.StandardButton.Yes:
                        consent["memory_cloud_accepted"] = True
                        ava_cfg["consent"] = consent
                        updates["ava"] = ava_cfg
                    else:
                        previous = (self.app.config.get("ava_memory", {}) or {}).get("mode", "clear")
                        updates["ava_memory"]["mode"] = previous
                        self._widgets["ava_memory_mode"].setCurrentText(
                            {"last": "Keep last session", "assistant": "Ava remembers me"}.get(
                                previous, "Clear on close"))
            return updates
        self._save_fns.append(_save)

        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def _open_ava_memory_file(self):
        memory = PersonalMemory()
        try:
            memory.ensure_file()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(memory.path)))
        except (OSError, ValueError) as exc:
            logger.warning("[AVA MEMORY] open failed: %s", type(exc).__name__)
            QMessageBox.warning(self, "Ava memory", "I couldn't open your memory file.")  # OWNER COPY

    def _erase_ava_memory_file(self):
        answer = QMessageBox.question(
            self, "Erase Ava memory",
            "Erase the facts and session summaries Ava saved about you?",  # OWNER COPY
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.app._ava_memory_generation = getattr(self.app, "_ava_memory_generation", 0) + 1
            PersonalMemory().erase()
            legacy = samsara_home_dir() / "ava_memory.json"
            if legacy.exists():
                legacy.unlink()
            if hasattr(self.app, "_ava_memory"):
                self.app._ava_memory.clear()
                self.app._ava_memory._legacy_path = None
            self.app._ava_summary_pending = []
            self.app._ava_legacy_history = []
        except (OSError, ValueError) as exc:
            logger.warning("[AVA MEMORY] erase failed: %s", type(exc).__name__)
            QMessageBox.warning(self, "Ava memory", "I couldn't erase your memory file.")  # OWNER COPY

    def _configure_ava_route_master(self, route):
        route.addItem("Off", "off")
        route.addItem("On this PC (Ollama)", "local")
        display = self._widgets['cloud_provider'].currentText().replace(" (default)", "")
        route.addItem(f"In the cloud ({display})", "cloud")
        policy = (self.app.config.get("ava", {}) or {}).get("provider_policy", "off")
        route.setCurrentIndex({"off": 0, "local": 1, "cloud": 2}.get(policy, 0))
        route.setAccessibleName("Ava runs")
        self._ava_last_route = route.currentData()
        route.currentIndexChanged.connect(self._on_ava_route_choice)
        self._sync_ava_cloud_controls()
        self._refresh_ava_route_status()

    def _restore_ava_route(self, policy):
        route = self._widgets['providerRoute']
        blocked = route.blockSignals(True)
        route.setCurrentIndex({"off": 0, "local": 1, "cloud": 2}[policy])
        route.blockSignals(blocked)
        self._ava_last_route = policy
        self._sync_ava_cloud_controls()

    def _sync_ava_cloud_controls(self):
        policy = self._widgets['providerRoute'].currentData()
        self._widgets['cloud_enabled'].setChecked(policy == 'cloud')
        self._widgets['ava_enabled'].setChecked(policy != 'off')
        self._widgets['ava_interpret_misses'].setEnabled(policy != 'off')
        self._widgets['ava_cmd_backend'].setCurrentText('Cloud' if policy == 'cloud' else 'Local (Ollama)')
        for key in ('cloud_provider', 'cloud_api_key', 'cloud_model', 'cloud_timeout', 'cloud_test_button'):
            self._widgets[key].setEnabled(policy == 'cloud')
        self._sync_web_search_availability()
        self._refresh_conditions()

    def _refresh_ava_route_status(self):
        status = self._widgets.get('ava_route_status')
        if status is None:
            return
        if self._ava_route_confirmation_pending:
            # OWNER COPY
            status.setText("Cloud chosen, consent recorded - confirm to switch. Ava still runs on this PC.")
            return
        status.setText(ai_preferences.ava_route_status(self._ava_staged_config()))

    def _on_ava_route_choice(self, _index):
        policy = self._widgets['providerRoute'].currentData()
        previous = self._ava_last_route
        if policy == previous:
            return
        if policy == 'cloud':
            effective = self._ava_staged_config()
            if ava_consent_qt.consent_required(effective, cloud_enabled=True):
                consent = ava_consent_qt.request_consent(self, effective, cloud_enabled=True)
                if consent is None:
                    self._restore_ava_route(previous)
                    # OWNER COPY
                    self._widgets['ava_route_status'].setText(
                        "Cloud was not selected because consent was not accepted. Ava's previous route is unchanged.")
                    return
                self._ava_consent_staged = consent
        self._ava_route_touched = True
        self._ava_route_confirmation_pending = False
        self._ava_last_route = policy
        self._sync_ava_cloud_controls()
        self._refresh_ava_route_status()
    def _sync_web_search_availability(self):
        """Web search exists only for Cloud AI with DeepSeek. Otherwise the
        box is disabled AND unchecked, and the reason is shown -- never a
        control that silently does nothing (local Ollama has no search)."""
        cb = self._widgets.get('cloud_web_search')
        note = self._widgets.get('cloud_web_search_unavailable')
        enabled_cb = self._widgets.get('cloud_enabled')
        provider_combo = self._widgets.get('cloud_provider')
        if cb is None or enabled_cb is None or provider_combo is None:
            return
        provider_display = provider_combo.currentText()
        provider = _DISPLAY_TO_CODE.get(provider_display, 'deepseek')
        available = enabled_cb.isChecked() and provider == 'deepseek'
        if not available:
            cb.setChecked(False)
        cb.setEnabled(available)
        if note is not None:
            if provider == "deepseek" and not enabled_cb.isChecked():
                note.setText(
                    "Web search is DeepSeek-only; you're using DeepSeek. "
                    "Turn on Cloud AI to enable it."
                )
            else:
                note.setText(_WEB_SEARCH_UNAVAILABLE.format(provider=provider_display))
            note.setVisible(not available)

    def _provider_info(self, display_name: str) -> str:
        code = _DISPLAY_TO_CODE.get(display_name, 'deepseek')
        return _PROVIDER_INFO.get(code, "")

    def _on_cloud_provider_changed(self, display_name: str):
        route = self._widgets.get('providerRoute')
        previous = getattr(self, '_ava_previous_provider_display', display_name)
        if route is not None and route.currentData() == 'cloud' and display_name != previous:
            effective = self._ava_staged_config()
            if ava_consent_qt.consent_required(effective, cloud_enabled=True):
                consent = ava_consent_qt.request_consent(self, effective, cloud_enabled=True)
                if consent is None:
                    provider = self._widgets['cloud_provider']
                    blocked = provider.blockSignals(True)
                    provider.setCurrentText(previous)
                    provider.blockSignals(blocked)
                    self._widgets['ava_route_status'].setText(
                        "Provider was not changed because consent was not accepted.")  # OWNER COPY
                    return
                self._ava_consent_staged = consent
        self._ava_previous_provider_display = display_name
        if route is not None:
            route.setItemText(2, f"In the cloud ({display_name.replace(' (default)', '')})")
        info_label = self._widgets.get('cloud_info_label')
        if info_label:
            info_label.setText(self._provider_info(display_name))
        model_entry = self._widgets.get('cloud_model')
        if model_entry:
            code = _DISPLAY_TO_CODE.get(display_name, 'deepseek')
            model_entry.setPlaceholderText(f"Default: {_DEFAULT_MODELS.get(code, '')}")
        self._refresh_ava_route_status()

    def _activate_license(self):
        """Store an optional supporter key. Unlocks no capability -- purely
        a display-state toggle (masked-key page) plus a slot for the future
        managed-key feature. Cloud AI itself needs only enabled + api_key."""
        from samsara import premium
        entry = self._widgets.get('cloud_license_entry')
        if not entry:
            return
        key = entry.text().strip()
        status_lbl = self._widgets.get('cloud_license_status')
        if not premium.validate_key(key):
            if status_lbl:
                status_lbl.setText("Invalid key format. Expected: SAMSARA-XXXX-XXXX-XXXX")
            return
        premium.set_license_key(self.app, key)
        with self.app._config_lock:
            self.app.config['premium_license'] = key
            self.app.save_config()
        if status_lbl:
            status_lbl.setText("")

    def _remove_license(self):
        """Remove the optional supporter key. Does not touch cloud_llm --
        BYOK cloud access is independent of this key."""
        from samsara import premium
        premium.set_license_key(self.app, "")
        with self.app._config_lock:
            self.app.config['premium_license'] = ""
            self.app.save_config()
        entry = self._widgets.get('cloud_license_entry')
        if entry:
            entry.clear()
        status_lbl = self._widgets.get('cloud_license_status')
        if status_lbl:
            status_lbl.setText("")

    @staticmethod
    def _toggle_api_key_show(checked: bool, entry: QLineEdit, btn: QPushButton):
        entry.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        btn.setText("Hide" if checked else "Show")

    def _run_test_connection(self):
        api_key_entry = self._widgets.get('cloud_api_key')
        api_key = api_key_entry.text().strip() if api_key_entry else ""
        if not api_key:
            self._test_result.emit("No API key entered.", theme.WARNING)
            return
        provider_combo = self._widgets.get('cloud_provider')
        provider_display = provider_combo.currentText() if provider_combo else _PROVIDER_DISPLAY[0]
        provider = _DISPLAY_TO_CODE.get(provider_display, 'deepseek')
        self._test_result.emit("Testing...", theme.ICON_IDLE)

        class _FakeApp:
            config = {"cloud_llm": {
                "enabled": True, "api_key": api_key,
                "provider": provider, "timeout_seconds": 5,
            }}

        fake = _FakeApp()

        def _do():
            try:
                from samsara import cloud_llm
                ok, info = cloud_llm.check_available(fake)
                msg = f"Connected to {provider}." if ok else f"Failed: {info}"
                color = theme.ACCENT if ok else theme.ERROR
            except Exception as exc:
                msg = f"Error: {exc}"
                color = theme.ERROR
            self._test_result.emit(msg, color)

        thread_registry.spawn("settings_qt._do", _do, daemon=True)
