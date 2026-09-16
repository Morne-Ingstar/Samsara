"""General page of the Samsara settings window.

Moved verbatim out of samsara/ui/settings_qt.py (queue 21, no behaviour change).
GeneralPage is mixed into settings_qt._SettingsWindow, so every method still runs
with the window as self (self._widgets, self._save_fns, self._setting_row ...).
Shared names are imported back from samsara.ui.settings_qt; see
samsara/ui/settings/__init__.py for why that import is cycle-safe.
"""

import copy
import shutil

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from datetime import datetime

from samsara.audio_devices import pick_index_by_name
from samsara.config_transfer import (
    ConfigTransferError,
    export_config,
    load_config_export,
    merge_import,
)
from samsara.ui import theme
from samsara.ui import theme
from samsara.ui_scale import UI_SCALE_OPTIONS, ui_scale_label

#: The three values `ui.theme` may hold, in the words the user reads. "System"
#: names what it follows, because "System" alone does not say whose setting.
THEME_LABELS = {
    "dark": "Dark",
    "light": "Light",
    "system": "Follow Windows",
}


def _saved_theme(config) -> str:
    """The stored `ui.theme`, as one of THEME_CHOICES.

    Read from the nested `ui` dict, falling back to a flat `ui.theme` key and
    then to the default -- a hand-edited config must not cost the user their
    Settings window."""
    stored = (config or {}).get("ui")
    value = stored.get("theme") if isinstance(stored, dict) else None
    if value is None:
        value = (config or {}).get("ui.theme")
    value = str(value or "").strip().lower()
    return value if value in theme.THEME_CHOICES else theme.DEFAULT_THEME

# Paths below stay relative to settings_qt.py, where this code was written.
from samsara.ui.settings_qt import __file__ as _SETTINGS_QT_FILE
from samsara.ui.settings_qt import _CONTENT_MAX_WIDTH, logger


class GeneralPage:
    """Methods of the General settings page (moved from _SettingsWindow)."""

    def _build_general_tab(self):
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
        layout.setSpacing(16)

        def _add_row(card_layout, label, description, widget, width=320):
            card_layout.addLayout(
                self._setting_row(
                    label,
                    description,
                    widget,
                    control_width=width,
                )
            )

        # ---- Card: Accessibility ------------------------------------------
        accessibility_card, accessibility_layout = self._section_card(
            "Accessibility",
            "Sizing and readability options for easier interaction.",
        )
        layout.addWidget(accessibility_card)

        ui_scale_combo = QComboBox()
        ui_scale_combo.addItems(list(UI_SCALE_OPTIONS))
        ui_scale_combo.setCurrentText(ui_scale_label(
            self.app.config.get('ui_scale', 1.0)
        ))
        self._widgets['ui_scale_combo'] = ui_scale_combo
        _add_row(
            accessibility_layout,
            "Interface size",
            "Scales text, menus, and controls throughout Samsara after restart",
            ui_scale_combo,
            width=260,
        )

        # Dark is not universally accessible -- astigmatism, some low-vision
        # conditions and a bright room all read light-on-dark worse, not
        # better -- so the theme is a setting, not a given. Apply & Close
        # changes every open surface immediately.
        theme_combo = QComboBox()
        for value in theme.THEME_CHOICES:
            theme_combo.addItem(THEME_LABELS[value], value)
        saved = _saved_theme(self.app.config)
        theme_combo.setCurrentIndex(max(0, list(theme.THEME_CHOICES).index(saved)))
        theme_combo.setAccessibleName("Theme")
        self._widgets['theme_combo'] = theme_combo
        _add_row(
            accessibility_layout,
            "Theme",
            "Dark, light, or follow Windows. Applies immediately.",
            theme_combo,
            width=260,
        )

        # ---- Card: Audio -------------------------------------------------
        audio_card, audio_layout = self._section_card(
            "Audio",
            "Select input/output devices and quick setup actions.",
        )
        layout.addWidget(audio_card)

        mics = list(getattr(self.app, 'available_mics', None) or [])
        current_mic_id = self.app.config.get('microphone')

        mic_combo = QComboBox()
        # "System default" mirrors output_combo's pattern (~line 1211):
        # index 0, userData=None, so config['microphone']=None round-trips
        # through currentData() at save time instead of being unselectable
        # (see the save-fn below). Fixes the incident where a stale
        # PortAudio index (29, a Focusrite line input) silently
        # transcribed system audio for days -- there was previously no way
        # to say "just use whatever Windows considers default" instead of
        # pinning a specific numbered device.
        mic_combo.addItem("System default", userData=None)
        default_idx = 0
        for i, dev in enumerate(mics):
            mic_combo.addItem(dev['name'], userData=dev['id'])
            if current_mic_id is not None and dev['id'] == current_mic_id:
                default_idx = i + 1
        mic_combo.setCurrentIndex(default_idx)
        self._widgets['mic_combo'] = mic_combo

        mic_row_widget = QWidget()
        # Same cascade cause as the QCheckBox/QLabel fixes: a bare QWidget
        # used purely as a row layout container has no background-color of
        # its own, so it otherwise paints the QMainWindow, QWidget rule's
        # BG0 behind the combo/button gap inside this card. Scoped by
        # objectName rather than a bare/unqualified "background:
        # transparent;" -- the unqualified form leaks into the ancestor
        # stylesheet cascade and strips mic_refresh_btn's app-level
        # QPushButton background-color, rendering it unstyled/grey.
        mic_row_widget.setObjectName("micRowWidget")
        mic_row_widget.setStyleSheet("QWidget#micRowWidget { background-color: transparent; }")
        mic_row_layout = QHBoxLayout(mic_row_widget)
        mic_row_layout.setContentsMargins(0, 0, 0, 0)
        mic_row_layout.setSpacing(6)
        mic_row_layout.addWidget(mic_combo, stretch=1)
        mic_refresh_btn = QPushButton("Refresh")
        mic_refresh_btn.setObjectName("microphoneRefreshButton")
        mic_refresh_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        # 104 stays the touch-target FLOOR (the 40px+ accessibility rule --
        # never narrower than it renders today), but the effective minimum is
        # now driven by the button's own metrics. The bare 104 assumed Segoe
        # UI: under any wider font stack the label needs 146 and was clipped,
        # because sizeHint() scales with the font and a literal does not.
        # max() means real-platform rendering is unchanged (there sizeHint is
        # 95, so 104 still wins).
        mic_refresh_btn.setMinimumWidth(104)  # Includes inherited 24px side padding.
        mic_row_layout.addWidget(mic_refresh_btn)

        mic_refresh_hint = QLabel("Stop dictation to refresh devices.")
        mic_refresh_hint.setStyleSheet(f"color: {theme.WARNING}; font-size: {theme.TYPE_MIN}px;")
        mic_refresh_hint.setVisible(False)

        def _on_refresh_mics():
            if self.app._mic_refresh_blocked():
                mic_refresh_hint.setVisible(True)
                return
            mic_refresh_hint.setVisible(False)
            try:
                fresh_mics = self.app.refresh_audio_devices()
            except Exception as exc:
                logger.debug(f"[MIC] refresh failed: {exc}")
                return

            preserved_name = mic_combo.currentText()
            mic_combo.blockSignals(True)
            mic_combo.clear()
            mic_combo.addItem("System default", userData=None)
            for dev in fresh_mics:
                mic_combo.addItem(dev['name'], userData=dev['id'])
            # +1 to account for the "System default" item at index 0
            # (mirrors mic_setup_wizard_qt.py's _on_refresh_devices). A
            # preserved_name of "System default" itself never matches a
            # real device (pick_index_by_name only searches fresh_mics),
            # so idx is None and the fallback 0 correctly reselects the
            # System-default row -- no special case needed for that name.
            idx = pick_index_by_name(fresh_mics, preserved_name)
            mic_combo.setCurrentIndex(idx + 1 if idx is not None else 0)
            mic_combo.blockSignals(False)

        mic_refresh_btn.clicked.connect(_on_refresh_mics)
        _add_row(
            audio_layout,
            "Microphone",
            "Audio input device used for speech recognition",
            mic_row_widget,
            width=420,
        )
        audio_layout.addWidget(mic_refresh_hint)

        # Samsara-only output routing. This never changes the Windows default.
        outputs = list(getattr(self.app, 'available_outputs', None) or [])
        output_map = {"System default": (None, None)}
        for output in outputs:
            output_map[output['name']] = (output['id'], output['name'])
        output_combo = QComboBox()
        stored_output_id = self.app.config.get('output_device')
        stored_output_name = self.app.config.get('output_device_name')
        from samsara.output_devices import reconcile_output_device
        _resolved_output_id, resolved_output_name, output_missing = (
            reconcile_output_device(
                outputs, stored_output_id, stored_output_name,
            )
        )
        selected_label = "System default"
        if not output_missing and resolved_output_name in output_map:
            selected_label = resolved_output_name
        elif output_missing:
            saved_identity = stored_output_name or f"device {stored_output_id}"
            selected_label = (
                f"System default (saved output unavailable: {saved_identity})"
            )
            # Preserve the unavailable preference if Apply is clicked without
            # changing this control. Selecting the plain System default entry
            # still clears it explicitly.
            output_map[selected_label] = (stored_output_id, stored_output_name)
        output_combo.addItems(list(output_map))
        output_combo.setCurrentText(selected_label)
        self._widgets['output_combo'] = output_combo
        self._widgets['output_label_map'] = output_map
        _add_row(
            audio_layout,
            "Samsara sounds",
            "Speaker used for Samsara's sounds, speech, and alarms; other apps are unaffected.",
            output_combo,
            width=420,
        )

        setup_btn = QPushButton("Run Mic Setup Guide...")
        # No own width: this button goes through _add_row(width=210) below,
        # which unconditionally sets its own minimum width -- a fixed width
        # set here would just be overridden.
        setup_btn.clicked.connect(
            lambda: getattr(self.app, 'open_mic_setup_guide', lambda: None)()
        )
        _add_row(
            audio_layout,
            "Microphone setup guide",
            "Walk through microphone selection and basic voice quality checks.",
            setup_btn,
            width=210,
        )

        tutorial_btn = QPushButton("Replay Tutorial")
        # No own width: goes through _add_row(width=210) below, see setup_btn.
        tutorial_btn.clicked.connect(
            lambda: getattr(self.app, 'show_tutorial', lambda: None)()
        )
        _add_row(
            audio_layout,
            "Interactive tutorial",
            "Practice dictation, command mode, show-numbers, and Ava.",
            tutorial_btn,
            width=210,
        )

        ava_guide_btn = QPushButton("Ava Setup Guide...")
        # No own width: goes through _add_row(width=210) below, see setup_btn.
        ava_guide_btn.clicked.connect(
            lambda: getattr(self.app, 'open_ava_guide', lambda: None)()
        )
        _add_row(
            audio_layout,
            "Ava setup guide",
            "Set up Ava — the optional local or cloud AI assistant.",
            ava_guide_btn,
            width=210,
        )

        vt_btn = QPushButton("Voice Training...")
        # No own width: goes through _add_row(width=210) below, see setup_btn.
        vt_btn.clicked.connect(
            lambda: getattr(self.app, 'open_voice_training', lambda: None)()
        )
        _add_row(
            audio_layout,
            "Voice training",
            "Improve recognition of custom names, places, and phrasing.",
            vt_btn,
            width=210,
        )

        # ---- Card: Speech model -------------------------------------------
        model_card, model_layout = self._section_card(
            "Speech model",
            "Select recognition model size and language.",
        )
        layout.addWidget(model_card)

        from samsara.languages import LANGUAGES, is_english_only_model

        model_sizes = [
            'tiny', 'tiny.en', 'base', 'base.en',
            'small', 'small.en', 'medium', 'medium.en', 'large-v3',
        ]
        model_label_to_size = {
            size: (f"{size} (English only)" if is_english_only_model(size) else f"{size} (multilingual)")
            for size in model_sizes
        }
        model_label_to_actual_size = {v: k for k, v in model_label_to_size.items()}
        model_labels = [model_label_to_size[s] for s in model_sizes]
        current_model = self.app.config.get('model_size', 'base')
        model_combo = QComboBox()
        model_combo.addItems(model_labels)
        current_model_label = model_label_to_size.get(current_model)
        if current_model_label in model_labels:
            model_combo.setCurrentText(current_model_label)
        self._widgets['model_combo'] = model_combo
        self._widgets['model_label_to_size'] = model_label_to_actual_size
        _add_row(
            model_layout,
            "Model Size",
            "Larger models are more accurate but slower. Restart required to apply.",
            model_combo,
            width=260,
        )

        model_lang_hint = QLabel("")
        model_lang_hint.setWordWrap(True)
        model_lang_hint.setStyleSheet(f"color: {theme.WARNING}; font-size: {theme.TYPE_MIN}px; margin-left: 4px;")
        model_lang_hint.setVisible(False)
        model_layout.addWidget(model_lang_hint)

        lang_name_to_code = {name: code for name, code in LANGUAGES}
        lang_code_to_name = {code: name for name, code in LANGUAGES}
        lang_names = [name for name, _ in LANGUAGES]
        current_lang_code = self.app.config.get('language', 'en')
        current_lang_display = lang_code_to_name.get(current_lang_code, 'English (en)')
        lang_combo = QComboBox()
        lang_combo.addItems(lang_names)
        if current_lang_display in lang_names:
            lang_combo.setCurrentText(current_lang_display)
        self._widgets['lang_combo'] = lang_combo
        self._widgets['lang_name_to_code'] = lang_name_to_code
        _add_row(
            model_layout,
            "Language",
            "Transcription language. Non-English needs a multilingual model (one without '.en').",
            lang_combo,
            width=260,
        )

        def _update_model_lang_hint(_=None):
            model_label = model_combo.currentText()
            model_size = model_label_to_actual_size.get(model_label, model_label)
            lang_display = lang_combo.currentText()
            lang_code = lang_name_to_code.get(lang_display, 'en')
            mismatch = lang_code != 'en' and is_english_only_model(model_size)
            if mismatch:
                model_lang_hint.setText(
                    f"English-only model — switch to small / large-v3 for {lang_display}"
                )
            model_lang_hint.setVisible(mismatch)

        model_combo.currentTextChanged.connect(_update_model_lang_hint)
        lang_combo.currentTextChanged.connect(_update_model_lang_hint)
        _update_model_lang_hint()

        # ---- Card: Text behavior -----------------------------------------
        text_card, text_layout = self._section_card(
            "Text behavior",
            "Control how transcribed text and commands look after capture.",
        )
        layout.addWidget(text_card)

        auto_paste = QCheckBox()
        auto_paste.setChecked(bool(self.app.config.get('auto_paste', True)))
        self._widgets['auto_paste'] = auto_paste
        _add_row(
            text_layout,
            "Auto-paste",
            "Automatically paste transcribed text into the focused application",
            auto_paste,
        )

        trailing_space = QCheckBox()
        trailing_space.setChecked(bool(self.app.config.get('add_trailing_space', True)))
        self._widgets['trailing_space'] = trailing_space
        _add_row(
            text_layout,
            "Add trailing space",
            "Append a space after each transcription so the next word joins cleanly",
            trailing_space,
        )

        auto_capitalize = QCheckBox()
        auto_capitalize.setChecked(bool(self.app.config.get('auto_capitalize', True)))
        self._widgets['auto_capitalize'] = auto_capitalize
        _add_row(
            text_layout,
            "Auto-capitalize",
            "Capitalize the first letter of each transcription",
            auto_capitalize,
        )

        format_numbers = QCheckBox()
        format_numbers.setChecked(bool(self.app.config.get('format_numbers', True)))
        self._widgets['format_numbers'] = format_numbers
        _add_row(
            text_layout,
            "Format numbers",
            "Convert spoken numbers to digits (e.g. 'three' to '3')",
            format_numbers,
        )

        cleanup_combo = QComboBox()
        cleanup_combo.addItems(['clean', 'verbatim'])
        current_cleanup = self.app.config.get('cleanup_mode', 'clean')
        cleanup_combo.setCurrentText(current_cleanup)
        self._widgets['cleanup_mode'] = cleanup_combo
        _add_row(
            text_layout,
            "Cleanup mode",
            "clean: remove filler words and fix spacing.  verbatim: transcribe exactly as spoken.",
            cleanup_combo,
            width=240,
        )

        # ---- Card: Hints -------------------------------------------------
        hint_card, hint_layout = self._section_card(
            "Hints and reminders",
            "How often Samsara shows non-intrusive guidance.",
        )
        layout.addWidget(hint_card)

        hints_enabled = QCheckBox()
        hints_enabled.setChecked(bool(self.app.config.get('hints_enabled', True)))
        self._widgets['hints_enabled'] = hints_enabled
        _add_row(
            hint_layout,
            "Show contextual hints",
            "One-time tips that appear after key actions (first dictation, wake word, etc.)",
            hints_enabled,
        )

        reset_hints_btn = QPushButton("Reset hints")
        # No own width: goes through _add_row(width=180) below, see setup_btn.
        reset_hints_btn.clicked.connect(self._reset_hints)
        _add_row(
            hint_layout,
            "Reset tip history",
            "Clear already shown contextual hints so they can appear again.",
            reset_hints_btn,
            width=180,
        )

        # ---- Card: Profiles and backup -----------------------------------
        profile_card, profile_layout = self._section_card(
            "Profiles and backup",
            "Import, export, and profile management settings.",
        )
        layout.addWidget(profile_card)

        prof_desc = QLabel(
            "Save and load vocabulary, correction, and command profiles."
        )
        prof_desc.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
        profile_layout.addWidget(prof_desc)

        manage_btn = QPushButton("Manage Profiles…")
        # No own width: goes through _add_row(width=190) below, see setup_btn.
        manage_btn.clicked.connect(self._open_profile_manager)
        _add_row(
            profile_layout,
            "Profiles",
            "Open profile management for vocabulary and corrections.",
            manage_btn,
            width=190,
        )

        # ---- Card: Configuration backup ----------------------------------
        backup_card, backup_layout = self._section_card(
            "Configuration backup",
            "Save, restore, or share your settings securely.",
        )
        layout.addWidget(backup_card)

        backup_desc = QLabel(
            "Backups include private values like API keys — keep the files private."
        )
        backup_desc.setObjectName("configBackupPrivacyWarning")
        backup_desc.setStyleSheet(f"color: {theme.WARNING}; font-size: {theme.TYPE_BODY}px;")
        backup_desc.setWordWrap(True)
        backup_layout.addWidget(backup_desc)

        backup_buttons = QHBoxLayout()
        backup_buttons.setSpacing(8)

        export_btn = QPushButton("Export configuration…")
        export_btn.setObjectName("exportConfigurationButton")
        export_btn.setMinimumWidth(200)  # sizeHint is 191; a few px of margin
        export_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        export_btn.clicked.connect(self._export_configuration)
        backup_buttons.addWidget(export_btn)

        import_btn = QPushButton("Import configuration…")
        import_btn.setObjectName("importConfigurationButton")
        import_btn.setMinimumWidth(200)  # sizeHint is 193; a few px of margin
        import_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        import_btn.clicked.connect(self._import_configuration)
        backup_buttons.addWidget(import_btn)

        # 2026-07-2x config-backup safeguard: restores from the automatic
        # rolling backups (samsara.config_backups -- see dictation.py's
        # save_config()/_rotate_config_backup()/_write_last_known_good()),
        # not a user-picked file like Import above.
        restore_btn = QPushButton("Restore from backup…")
        restore_btn.setObjectName("restoreConfigBackupButton")
        restore_btn.setMinimumWidth(200)
        restore_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        restore_btn.clicked.connect(self._restore_from_backup)
        backup_buttons.addWidget(restore_btn)

        backup_buttons.addStretch()
        backup_layout.addLayout(backup_buttons)

        def _save(_acc):
            stored_updates = self.app.config.get('updates', {})
            if not isinstance(stored_updates, dict):
                stored_updates = {}
            updates = {
                'ui_scale':          UI_SCALE_OPTIONS[self._widgets['ui_scale_combo'].currentText()],
                'ui':                {**(self.app.config.get('ui') or {}),
                                      'theme': self._widgets['theme_combo'].currentData()},
                'auto_paste':         self._widgets['auto_paste'].isChecked(),
                'add_trailing_space': self._widgets['trailing_space'].isChecked(),
                'auto_capitalize':    self._widgets['auto_capitalize'].isChecked(),
                'format_numbers':     self._widgets['format_numbers'].isChecked(),
                'cleanup_mode':       self._widgets['cleanup_mode'].currentText(),
                'model_size':         self._widgets['model_label_to_size'].get(
                                          self._widgets['model_combo'].currentText(),
                                          self._widgets['model_combo'].currentText()),
                'language':           self._widgets['lang_name_to_code'].get(
                                          self._widgets['lang_combo'].currentText(), 'en'),
                'hints_enabled':      self._widgets['hints_enabled'].isChecked(),
                'updates': {
                    **stored_updates,
                    'automatic_checks': self._widgets[
                        'automatic_update_checks'
                    ].isChecked(),
                },
            }
            # Sync hints enabled/disabled state on the live HintManager.
            # hints_enabled is already persisted to config above (this call
            # just syncs the already-constructed instance's in-memory flag).
            hints = getattr(self.app, 'hints', None)
            if hints is not None:
                hints.set_enabled(self._widgets['hints_enabled'].isChecked())

            # currentData() (userData set at addItem() time -- see
            # _populate_devices-equivalent block above) rather than the
            # old name->id dict: always write both keys, including
            # microphone=None for "System default", so selecting it is
            # actually persistable. The old `if mic_id is not None:` guard
            # made None unsavable -- root cause of a stale-device incident
            # (no way to fall back to "whatever Windows considers
            # default" once a pinned device id went stale).
            mic_combo = self._widgets['mic_combo']
            updates['microphone'] = mic_combo.currentData()
            updates['microphone_name'] = mic_combo.currentText()

            output_label = self._widgets['output_combo'].currentText()
            output_id, output_name = self._widgets['output_label_map'].get(
                output_label, (None, None)
            )
            saved_output = (
                self.app.config.get('output_device'),
                self.app.config.get('output_device_name'),
            )
            if (output_id, output_name) != saved_output:
                updates['output_device'] = output_id
                updates['output_device_name'] = output_name
            return updates
        self._save_fns.append(_save)

        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def _reset_hints(self):
        hints = getattr(self.app, 'hints', None)
        if hints is not None:
            hints.reset()
        print("[HINTS] History reset — all hints will fire again on next trigger")

    def _export_configuration(self):
        """Export a complete, private configuration snapshot."""
        reply = QMessageBox.question(
            self,
            "Export full configuration?",
            "This backup contains all settings, including private values such "
            "as API keys, supporter keys, and webhook details. Keep it private.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        default_name = f"samsara-config-backup-{datetime.now():%Y-%m-%d}.json"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Samsara configuration",
            default_name,
            "JSON files (*.json)",
        )
        if not path:
            return
        if not path.lower().endswith('.json'):
            path += '.json'

        try:
            with self.app._config_lock:
                snapshot = copy.deepcopy(self.app.config)
            export_config(path, snapshot)
        except (ConfigTransferError, OSError) as exc:
            logger.exception("[CONFIG] Configuration export failed")
            QMessageBox.critical(self, "Export failed", str(exc))
            return

        QMessageBox.information(
            self,
            "Configuration exported",
            "Your full Samsara configuration was exported. Keep the backup private.",
        )

    def _import_configuration(self):
        """Validate, merge, and persist a configuration backup."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Samsara configuration",
            "",
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return

        try:
            imported = load_config_export(path)
        except ConfigTransferError as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return

        reply = QMessageBox.question(
            self,
            "Import configuration?",
            "Imported values will replace matching settings. Settings that are "
            "not present in the backup will be kept. Samsara will also preserve "
            "your current config.json as config.json.bak.\n\n"
            "Backups can contain private values such as API keys and webhooks. "
            "Only import a file you trust. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            with self.app._config_lock:
                original = copy.deepcopy(self.app.config)
                merged = merge_import(original, imported)
                try:
                    self.app.config.clear()
                    self.app.config.update(merged)
                    self.app.save_config()
                except Exception:
                    self.app.config.clear()
                    self.app.config.update(original)
                    raise
        except (ConfigTransferError, OSError, TypeError, ValueError) as exc:
            logger.exception("[CONFIG] Configuration import failed")
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        except Exception as exc:
            logger.exception("[CONFIG] Configuration import failed")
            QMessageBox.critical(self, "Import failed", str(exc))
            return

        restart = QMessageBox.question(
            self,
            "Configuration imported",
            "The configuration was imported successfully. Restart Samsara now "
            "to apply every setting?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        self.close()  # Do not leave stale pre-import controls able to overwrite the import.
        if restart == QMessageBox.StandardButton.Yes:
            from plugins.commands.core_utils import restart_app
            restart_app(self.app)

    def _restore_from_backup(self):
        """Restore config.json from one of the automatic rolling backups
        (samsara.paths.samsara_home_dir()/config_backups -- see
        dictation.py's list_config_backups()/_rotate_config_backup()/
        _write_last_known_good()). Unlike Import above, this is a plain
        file copy into place, not a merge -- the whole point is recovering
        a known-good snapshot exactly as it was, including from a session
        where saving is currently latched off (see DictationApp.
        _config_load_failed) -- restoring is exactly how that latch gets
        cleared.
        """
        list_backups = getattr(self.app, 'list_config_backups', None)
        entries = list_backups() if list_backups is not None else []
        if not entries:
            QMessageBox.information(
                self,
                "No backups found",
                "No configuration backups are available yet.",
            )
            return

        labels = [label for label, _path in entries]
        label, ok = QInputDialog.getItem(
            self,
            "Restore from backup",
            "Choose a backup to restore. Newest first:",
            labels,
            0,
            False,
        )
        if not ok or not label:
            return
        chosen_path = dict(entries)[label]

        reply = QMessageBox.question(
            self,
            "Restore this backup?",
            f"This will replace your current settings with the backup from "
            f"\"{label}\". Your current config.json will itself be backed "
            f"up first.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            with self.app._config_lock:
                if self.app.config_path.exists():
                    shutil.copy2(
                        self.app.config_path,
                        self.app.config_path.with_suffix('.json.bak'),
                    )
                shutil.copy2(chosen_path, self.app.config_path)
                # The user just fixed the problem the latch exists for --
                # clear it so the restored config can be saved again after
                # restart (load_config() re-reads from disk on the next
                # boot regardless, but clearing here means this session's
                # in-memory flag doesn't linger stale until then).
                self.app._config_load_failed = False
                self.app._config_corrupt_backup_name = None
        except OSError as exc:
            logger.exception("[CONFIG] Restore from backup failed")
            QMessageBox.critical(self, "Restore failed", str(exc))
            return

        restart = QMessageBox.question(
            self,
            "Backup restored",
            "The backup was restored. Restart Samsara now to apply it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        self.close()
        if restart == QMessageBox.StandardButton.Yes:
            from plugins.commands.core_utils import restart_app
            restart_app(self.app)

    def _open_profile_manager(self):
        from pathlib import Path
        from samsara.profiles import ProfileManager
        from samsara.ui.profile_manager_qt import ProfileManagerQt
        if not hasattr(self, '_profile_manager_qt'):
            app_dir = str(Path(_SETTINGS_QT_FILE).parent)
            pm = ProfileManager(app_dir)
            def _on_changed():
                if hasattr(self.app, 'load_commands'):
                    try:
                        self.app.load_commands()
                    except Exception as e:
                        logger.debug(f"_on_changed: {e}")
                if hasattr(self.app, 'load_training_data'):
                    try:
                        self.app.load_training_data()
                    except Exception as e:
                        logger.debug(f"_on_changed: {e}")
            self._profile_manager_qt = ProfileManagerQt(pm, _on_changed)
        self._profile_manager_qt.show()
