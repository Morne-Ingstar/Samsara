"""Ava / Cloud page of the Samsara settings window.

Moved verbatim out of samsara/ui/settings_qt.py (queue 21, no behaviour change).
AvaCloudPage is mixed into settings_qt._SettingsWindow, so every method still runs
with the window as self (self._widgets, self._save_fns, self._setting_row ...).
Shared names are imported back from samsara.ui.settings_qt; see
samsara/ui/settings/__init__.py for why that import is cycle-safe.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from samsara.runtime import thread_registry
from samsara import ava_readiness
from samsara.ui import theme

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
    "Not available right now: web search needs Cloud AI enabled with DeepSeek as "
    "the provider. Local models (Ollama) and the other providers have no built-in "
    "web search, so Ava answers from what the model already knows."
)


class AvaCloudPage:
    """Methods of the Ava / Cloud settings page (moved from _SettingsWindow)."""

    def _build_ava_cloud_tab(self):
        from samsara import premium

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

        # ---- Section: Cloud AI (bring your own key) -- always visible ------
        layout.addWidget(self._section_title("Cloud AI (bring your own key)"))
        layout.addSpacing(4)

        cloud_enabled = QCheckBox("Enable Cloud AI (Ava sends requests to your chosen provider)")
        cloud_enabled.setChecked(bool(cfg.get('enabled', False)))
        self._widgets['cloud_enabled'] = cloud_enabled
        layout.addWidget(cloud_enabled)

        enable_note = QLabel(
            "Sends voice requests to your chosen provider; falls back to local on error. "
            "Free with your own API key."
        )
        enable_note.setWordWrap(True)
        enable_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px; margin-left: 26px;")
        layout.addWidget(enable_note)
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
        personality_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px; margin-left: 4px;")
        layout.addWidget(personality_note)
        layout.addSpacing(8)

        # Conversation Memory
        layout.addWidget(self._section_title("Conversation Memory"))
        layout.addSpacing(4)
        mem_cfg = self.app.config.get("ava_memory", {})

        memory_combo = QComboBox()
        memory_combo.addItems(["Clear on close", "Keep last session"])
        mem_mode = mem_cfg.get("mode", "clear")
        memory_combo.setCurrentText(
            "Keep last session" if mem_mode == "last" else "Clear on close"
        )
        self._widgets['ava_memory_mode'] = memory_combo
        layout.addWidget(memory_combo)

        memory_note = QLabel(
            "Clear on close: Ava forgets the conversation when Samsara exits. "
            "Keep last session: the conversation is restored on next launch."
        )
        memory_note.setWordWrap(True)
        memory_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px; margin-left: 4px;")
        layout.addWidget(memory_note)
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
        show_btn = QPushButton("Show")
        show_btn.setCheckable(True)
        show_btn.setFixedWidth(60)
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
        web_search_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px; margin-left: 26px;")
        layout.addWidget(web_search_note)
        web_search_unavailable = QLabel(_WEB_SEARCH_UNAVAILABLE)
        web_search_unavailable.setWordWrap(True)
        web_search_unavailable.setTextFormat(Qt.TextFormat.PlainText)
        web_search_unavailable.setStyleSheet(f"color: {theme.WARNING}; font-size: {theme.TYPE_MIN}px; margin-left: 26px;")
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
        test_btn.setMinimumWidth(150)
        test_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        test_btn.clicked.connect(self._run_test_connection)
        test_status = QLabel("")
        test_status.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        self._widgets['cloud_test_status'] = test_status
        test_row.addWidget(test_btn)
        test_row.addWidget(test_status)
        test_row.addStretch()
        layout.addLayout(test_row)
        layout.addSpacing(20)

        # ---- Section: Support Samsara ---------------------------------------
        layout.addWidget(self._section_title("Support Samsara"))
        layout.addSpacing(4)

        support_text = QLabel(
            "Samsara is free — every feature, forever. Supporting is optional and never "
            "unlocks features. morneis.com/samsara/support"
        )
        support_text.setWordWrap(True)
        support_text.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(support_text)
        layout.addSpacing(8)

        key = premium.get_license_key(self.app)
        has_key = premium.validate_key(key)

        # Plain QWidget, not a bordered/filled QFrame: the "supporter key
        # active" state is read-only text (heading + masked key), and the
        # "no key" state already has its own real QLineEdit with its own
        # input-field chrome -- an outer field-styled box around either
        # made static text read as a disabled input and doubled up the
        # chrome around the real one.
        supporter_frame = QWidget()
        supporter_frame_layout = QVBoxLayout(supporter_frame)
        supporter_frame_layout.setContentsMargins(16, 16, 16, 16)
        supporter_frame_layout.setSpacing(0)

        # QStackedWidget: index 0 = no key, index 1 = key stored. This never
        # gates anything -- it only decides which of these two rows to show.
        # Bare, unqualified "background: transparent" (no type/ID selector)
        # can leak into the ancestor cascade and strip a descendant's own
        # app-level type-selector styling -- this previously stripped
        # activate_btn's QPushButton background/color rule, leaving it
        # rendering near-invisible (same class of bug fixed for QLabel/
        # QCheckBox in 9b7f00f/addaa88). Scoped ID selectors avoid that.
        supporter_stack = QStackedWidget()
        supporter_stack.setObjectName("supporterKeyStack")
        supporter_stack.setStyleSheet(
            "QStackedWidget#supporterKeyStack { background-color: transparent; }"
        )
        self._widgets['cloud_license_stack'] = supporter_stack

        # -- Page 0: no supporter key ---
        no_key_page = QWidget()
        no_key_page.setObjectName("supporterKeyNoKeyPage")
        no_key_page.setStyleSheet(
            "QWidget#supporterKeyNoKeyPage { background-color: transparent; }"
        )
        nk_layout = QVBoxLayout(no_key_page)
        nk_layout.setContentsMargins(0, 0, 0, 0)
        nk_layout.setSpacing(10)

        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        key_row_lbl = QLabel("Supporter key (optional):")
        key_row_lbl.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px; background: transparent;")
        key_row_lbl.setFixedWidth(150)
        license_entry = QLineEdit()
        license_entry.setPlaceholderText("SAMSARA-XXXX-XXXX-XXXX")
        self._widgets['cloud_license_entry'] = license_entry
        activate_btn = QPushButton("Activate")
        activate_btn.setMinimumWidth(110)  # sizeHint is 100; a few px of margin
        activate_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        activate_btn.clicked.connect(self._activate_license)
        key_row.addWidget(key_row_lbl)
        key_row.addWidget(license_entry, stretch=1)
        key_row.addWidget(activate_btn)
        nk_layout.addLayout(key_row)

        license_status = QLabel("")
        license_status.setStyleSheet(f"color: {theme.ERROR}; font-size: {theme.TYPE_MIN}px; background: transparent;")
        self._widgets['cloud_license_status'] = license_status
        nk_layout.addWidget(license_status)

        supporter_stack.addWidget(no_key_page)  # index 0

        # -- Page 1: supporter key stored ---
        has_key_page = QWidget()
        has_key_page.setObjectName("supporterKeyHasKeyPage")
        has_key_page.setStyleSheet(
            "QWidget#supporterKeyHasKeyPage { background-color: transparent; }"
        )
        hk_layout = QVBoxLayout(has_key_page)
        hk_layout.setContentsMargins(0, 0, 0, 0)
        hk_layout.setSpacing(6)

        active_lbl = QLabel("Supporter key active")
        active_lbl.setStyleSheet(
            f"color: {theme.ACCENT}; font-size: {theme.TYPE_BODY}px; font-weight: bold; background: transparent;"
        )
        hk_layout.addWidget(active_lbl)

        masked_lbl = QLabel(premium.masked_key(key) if has_key else "")
        masked_lbl.setStyleSheet(
            f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px; "
            "font-family: 'Consolas', 'Courier New', monospace; background: transparent;"
        )
        self._widgets['cloud_masked_key'] = masked_lbl
        hk_layout.addWidget(masked_lbl)

        remove_btn = QPushButton("Remove Key")
        remove_btn.setFixedWidth(120)
        remove_btn.setStyleSheet(
            f"QPushButton {{ background-color: transparent; color: {theme.ICON_IDLE}; "
            f"border: 1px solid {theme.wash(0.14)}; border-radius: 6px; "
            f"padding: 7px 14px; font-size: {theme.TYPE_BODY}px; }}"
            f"QPushButton:hover {{ background-color: {theme.wash(0.04)}; color: {theme.TEXT_PRIMARY}; }}"
        )
        remove_btn.clicked.connect(self._remove_license)
        hk_layout.addWidget(remove_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        supporter_stack.addWidget(has_key_page)  # index 1

        supporter_stack.setCurrentIndex(1 if has_key else 0)
        supporter_frame_layout.addWidget(supporter_stack)
        layout.addWidget(supporter_frame)

        supporter_instant_note = QLabel("Activating or removing a supporter key applies immediately.")
        supporter_instant_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(supporter_instant_note)

        def _save(_acc):
            updates = {}
            if 'cloud_enabled' in self._widgets:
                provider_display = self._widgets['cloud_provider'].currentText()
                provider = _DISPLAY_TO_CODE.get(provider_display, 'deepseek')
                api_key = self._widgets['cloud_api_key'].text().strip()
                model_override = self._widgets['cloud_model'].text().strip()
                cloud_cfg = dict(self.app.config.get('cloud_llm', {}) or {})
                cloud_cfg['enabled'] = self._widgets['cloud_enabled'].isChecked()
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

            if 'ava_warm_on_boot' in self._widgets:
                ava_cfg = dict(self.app.config.get('ava', {}) or {})
                ava_cfg['warm_on_boot'] = self._widgets['ava_warm_on_boot'].isChecked()
                updates['ava'] = ava_cfg

            if 'ava_memory_mode' in self._widgets:
                mem_updates = dict(self.app.config.get('ava_memory', {}))
                mode_text = self._widgets['ava_memory_mode'].currentText()
                mem_updates['mode'] = 'last' if mode_text == 'Keep last session' else 'clear'
                if 'ava_memory_max_turns' in self._widgets:
                    mem_updates['max_turns'] = int(
                        self._widgets['ava_memory_max_turns'].value()
                    )
                updates['ava_memory'] = mem_updates
            return updates
        self._save_fns.append(_save)

        layout.addStretch()
        scroll.setWidget(container)
        return scroll
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
        provider = _DISPLAY_TO_CODE.get(provider_combo.currentText(), 'deepseek')
        available = enabled_cb.isChecked() and provider == 'deepseek'
        if not available:
            cb.setChecked(False)
        cb.setEnabled(available)
        if note is not None:
            note.setVisible(not available)

    def _provider_info(self, display_name: str) -> str:
        code = _DISPLAY_TO_CODE.get(display_name, 'deepseek')
        return _PROVIDER_INFO.get(code, "")

    def _on_cloud_provider_changed(self, display_name: str):
        info_label = self._widgets.get('cloud_info_label')
        if info_label:
            info_label.setText(self._provider_info(display_name))
        model_entry = self._widgets.get('cloud_model')
        if model_entry:
            code = _DISPLAY_TO_CODE.get(display_name, 'deepseek')
            model_entry.setPlaceholderText(f"Default: {_DEFAULT_MODELS.get(code, '')}")

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
        # Switch supporter-key panel to the "key stored" state
        stack = self._widgets.get('cloud_license_stack')
        if stack:
            stack.setCurrentIndex(1)
        masked_lbl = self._widgets.get('cloud_masked_key')
        if masked_lbl:
            masked_lbl.setText(premium.masked_key(key))
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
        stack = self._widgets.get('cloud_license_stack')
        if stack:
            stack.setCurrentIndex(0)
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
