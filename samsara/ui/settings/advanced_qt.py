"""Advanced page of the Samsara settings window.

Moved verbatim out of samsara/ui/settings_qt.py (queue 21, no behaviour change).
AdvancedPage is mixed into settings_qt._SettingsWindow, so every method still runs
with the window as self (self._widgets, self._save_fns, self._setting_row ...).
Shared names are imported back from samsara.ui.settings_qt; see
samsara/ui/settings/__init__.py for why that import is cycle-safe.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from samsara.ui import theme

from samsara.ui.settings_qt import _CONTENT_MAX_WIDTH


class AdvancedPage:
    """Methods of the Advanced settings page (moved from _SettingsWindow)."""

    def _build_advanced_tab(self):
        try:
            from samsara.cuda_detect import is_cuda_available, cuda_status_message
            _cuda_ok  = is_cuda_available()
            _cuda_msg = cuda_status_message()
        except Exception:
            _cuda_ok  = False
            _cuda_msg = "CUDA detection unavailable."

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
        layout.setSpacing(8)

        cfg = self.app.config
        aec_cfg = cfg.get('echo_cancellation', {}) or {}
        ducking_cfg = cfg.get('ducking', {}) or {}

        # ---- Section: Hardware Acceleration --------------------------------
        layout.addWidget(self._section_title("Hardware Acceleration"))
        layout.addSpacing(4)

        device_display_to_value = {'CPU': 'cpu'}
        if _cuda_ok:
            device_display_to_value['CUDA (NVIDIA GPU)'] = 'cuda'
        device_value_to_display = {v: k for k, v in device_display_to_value.items()}

        current_device = cfg.get('device', 'cpu')
        if current_device == 'cuda' and not _cuda_ok:
            current_device = 'cpu'
        current_device_display = device_value_to_display.get(current_device, 'CPU')

        device_combo = QComboBox()
        device_combo.addItems(list(device_display_to_value.keys()))
        device_combo.setCurrentText(current_device_display)
        self._widgets['adv_device'] = device_combo
        self._widgets['adv_device_map'] = device_display_to_value
        layout.addLayout(self._setting_row(
            "Compute device",
            _cuda_msg + "  Device changes require restart.",
            device_combo,
        ))
        layout.addSpacing(8)

        compute_options = ['float16', 'int8', 'float32']
        current_compute = cfg.get('compute_type', 'float16')
        compute_combo = QComboBox()
        compute_combo.addItems(compute_options)
        if current_compute in compute_options:
            compute_combo.setCurrentText(current_compute)
        self._widgets['adv_compute_type'] = compute_combo
        layout.addLayout(self._setting_row(
            "Compute type",
            "float16: fastest on GPU.  int8: low-memory CPUs.  float32: fallback.",
            compute_combo,
        ))
        layout.addSpacing(20)

        # ---- Section: Performance ------------------------------------------
        layout.addWidget(self._section_title("Performance"))
        layout.addSpacing(4)

        perf_options = ['fast', 'balanced', 'accurate']
        current_perf = cfg.get('performance_mode', 'balanced')
        perf_combo = QComboBox()
        perf_combo.addItems(perf_options)
        if current_perf in perf_options:
            perf_combo.setCurrentText(current_perf)
        self._widgets['adv_perf_mode'] = perf_combo
        layout.addLayout(self._setting_row(
            "Performance mode",
            "fast: lowest latency.  balanced: good tradeoff.  accurate: best quality.",
            perf_combo,
        ))
        layout.addSpacing(20)

        # ---- Section: Continuous Mode --------------------------------------
        layout.addWidget(self._section_title("Continuous Mode"))
        layout.addSpacing(4)

        silence_spin = QDoubleSpinBox()
        silence_spin.setRange(0.5, 10.0)
        silence_spin.setSingleStep(0.5)
        silence_spin.setDecimals(1)
        silence_spin.setSuffix(" s")
        silence_spin.setValue(float(cfg.get('silence_threshold', 2.0)))
        self._widgets['adv_silence'] = silence_spin
        layout.addLayout(self._setting_row(
            "Silence threshold",
            "Seconds of silence before continuous mode auto-transcribes",
            silence_spin,
        ))
        layout.addSpacing(8)

        min_speech_spin = QDoubleSpinBox()
        min_speech_spin.setRange(0.1, 2.0)
        min_speech_spin.setSingleStep(0.1)
        min_speech_spin.setDecimals(1)
        min_speech_spin.setSuffix(" s")
        min_speech_spin.setValue(float(cfg.get('min_speech_duration', 0.3)))
        self._widgets['adv_min_speech'] = min_speech_spin
        layout.addLayout(self._setting_row(
            "Min speech duration",
            "Recordings shorter than this are discarded as noise",
            min_speech_spin,
        ))
        layout.addSpacing(20)

        # ---- Section: Speech Threshold -------------------------------------
        layout.addWidget(self._section_title("Speech Threshold"))
        layout.addSpacing(4)

        thresh_mode_combo = QComboBox()
        thresh_mode_combo.addItems(['auto', 'manual'])
        thresh_mode_combo.setCurrentText(cfg.get('threshold_mode', 'auto'))
        self._widgets['adv_threshold_mode'] = thresh_mode_combo
        layout.addLayout(self._setting_row(
            "Calibration mode",
            "auto: calibrate on startup (recommended).  manual: use a fixed threshold below.",
            thresh_mode_combo,
        ))
        layout.addSpacing(8)

        cal_spin = QDoubleSpinBox()
        cal_spin.setRange(1.0, 10.0)
        cal_spin.setSingleStep(0.1)
        cal_spin.setDecimals(1)
        cal_spin.setValue(float(cfg.get('cal_multiplier', 3.0)))
        self._widgets['adv_cal_multiplier'] = cal_spin
        layout.addLayout(self._setting_row(
            "Calibration multiplier",
            "Auto mode: signal must be this many times louder than ambient to count as speech",
            cal_spin,
        ))
        layout.addSpacing(8)

        # Manual threshold row — visible only when mode is 'manual'
        manual_row_widget = QWidget()
        manual_row_widget.setVisible(thresh_mode_combo.currentText() == 'manual')
        manual_row_layout = QVBoxLayout(manual_row_widget)
        manual_row_layout.setContentsMargins(0, 0, 0, 0)

        current_thresh = (
            cfg.get('wake_word_config', {}).get('audio', {}).get('speech_threshold', 0.03)
        )
        manual_spin = QDoubleSpinBox()
        manual_spin.setRange(0.005, 0.20)
        manual_spin.setSingleStep(0.005)
        manual_spin.setDecimals(4)
        manual_spin.setValue(float(current_thresh))
        self._widgets['adv_manual_threshold'] = manual_spin
        manual_row_layout.addLayout(self._setting_row(
            "Manual threshold",
            "Raw RMS amplitude level required to count as speech (0.005 – 0.20)",
            manual_spin,
        ))
        layout.addWidget(manual_row_widget)

        thresh_mode_combo.currentTextChanged.connect(
            lambda t: manual_row_widget.setVisible(t == 'manual')
        )
        layout.addSpacing(20)

        # ---- Section: Echo Cancellation ------------------------------------
        layout.addWidget(self._section_title("Experimental Echo Cancellation"))
        layout.addSpacing(4)

        aec_cb = QCheckBox("Enable experimental echo cancellation (not recommended)")
        aec_cb.setChecked(bool(aec_cfg.get('enabled', False)))
        self._widgets['adv_aec_enabled'] = aec_cb
        layout.addWidget(aec_cb)

        aec_note = QLabel(
            "Measured only 3–8% echo reduction and may distort audio. Leave off unless "
            "evaluating. Restart required; Windows only."
        )
        aec_note.setWordWrap(True)
        aec_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px; margin-left: 26px;")
        layout.addWidget(aec_note)
        layout.addSpacing(8)

        aec_latency_spin = QDoubleSpinBox()
        aec_latency_spin.setRange(0.0, 500.0)
        aec_latency_spin.setSingleStep(5.0)
        aec_latency_spin.setDecimals(0)
        aec_latency_spin.setSuffix(" ms")
        aec_latency_spin.setValue(float(aec_cfg.get('latency_ms', 30.0)))
        self._widgets['adv_aec_latency'] = aec_latency_spin
        layout.addLayout(self._setting_row(
            "Latency compensation",
            "How far back to search system audio for the mic's echo. Default is likely wrong "
            "— don't change without measuring.",
            aec_latency_spin,
        ))
        layout.addSpacing(20)

        # ---- Section: Audio Ducking ------------------------------------------
        layout.addWidget(self._section_title("Playback Reduction While Dictating"))
        layout.addSpacing(4)

        ducking_cb = QCheckBox("Lower other apps' volume while dictating")
        ducking_cb.setChecked(bool(ducking_cfg.get('enabled', False)))
        self._widgets['adv_ducking_enabled'] = ducking_cb
        layout.addWidget(ducking_cb)

        ducking_note = QLabel(
            "Turns down other apps' audio while you dictate so the mic hears less of it; "
            "restores it after."
        )
        ducking_note.setWordWrap(True)
        ducking_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px; margin-left: 26px;")
        layout.addWidget(ducking_note)
        layout.addSpacing(8)

        ducking_level_spin = QDoubleSpinBox()
        ducking_level_spin.setRange(0.0, 1.0)
        ducking_level_spin.setSingleStep(0.05)
        ducking_level_spin.setDecimals(2)
        ducking_level_spin.setValue(float(ducking_cfg.get('level', 0.2)))
        self._widgets['adv_ducking_level'] = ducking_level_spin
        layout.addLayout(self._setting_row(
            "Ducked volume",
            "Absolute volume level for other apps during dictation (0 = silent, 1 = full volume).",
            ducking_level_spin,
        ))
        layout.addSpacing(12)

        hands_free_ducking_cb = QCheckBox(
            "Reduce other apps while hands-free is listening"
        )
        hands_free_ducking_cb.setChecked(
            bool(ducking_cfg.get('hands_free_enabled', True))
        )
        self._widgets['adv_hands_free_ducking_enabled'] = hands_free_ducking_cb
        layout.addWidget(hands_free_ducking_cb)

        hands_free_capture_spin = QDoubleSpinBox()
        hands_free_capture_spin.setRange(0.0, 1.0)
        hands_free_capture_spin.setSingleStep(0.05)
        hands_free_capture_spin.setDecimals(2)
        hands_free_capture_spin.setValue(
            float(ducking_cfg.get('hands_free_level', 0.15))
        )
        self._widgets['adv_hands_free_ducking_level'] = hands_free_capture_spin
        layout.addLayout(self._setting_row(
            "Other apps' volume while hands-free captures",
            "Temporary reduction while you are speaking. 0 = silent; 1 = no reduction.",
            hands_free_capture_spin,
        ))

        hands_free_idle_spin = QDoubleSpinBox()
        hands_free_idle_spin.setRange(0.0, 1.0)
        hands_free_idle_spin.setSingleStep(0.05)
        hands_free_idle_spin.setDecimals(2)
        hands_free_idle_spin.setValue(
            float(ducking_cfg.get('hands_free_idle_level', 0.8))
        )
        self._widgets['adv_hands_free_idle_level'] = hands_free_idle_spin
        layout.addLayout(self._setting_row(
            "Other apps' volume while hands-free is on",
            "Other apps stay at this volume for as long as hands-free is on. "
            "1 = no reduction.",
            hands_free_idle_spin,
        ))
        layout.addSpacing(20)

        # ---- Section: Listening Indicator ----------------------------------
        layout.addWidget(self._section_title("Listening Indicator"))
        layout.addSpacing(4)

        indicator_desc = QLabel(
            "An always-on-top pill that shows your current mode and pulses while recording."
        )
        indicator_desc.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(indicator_desc)
        layout.addSpacing(6)

        ind_cb = QCheckBox("Show listening indicator overlay")
        ind_cb.setChecked(bool(cfg.get('listening_indicator_enabled', False)))
        self._widgets['adv_indicator_enabled'] = ind_cb
        layout.addWidget(ind_cb)
        layout.addSpacing(8)

        pos_options = [
            'top-left', 'top-center', 'top-right',
            'bottom-left', 'bottom-center', 'bottom-right',
        ]
        current_pos = cfg.get('listening_indicator_position', 'bottom-center')
        # "custom" (set by dragging the indicator via the tray's "Move
        # listening indicator..." action) is only offered as a combo item
        # when it's the active value -- it isn't a preset a user can pick
        # here since there are no coordinates to assign to it. Choosing any
        # preset below discards/supersedes the custom placement (see _save).
        combo_items = list(pos_options)
        if current_pos == 'custom':
            combo_items.append('custom')
        pos_combo = QComboBox()
        pos_combo.addItems(combo_items)
        if current_pos in combo_items:
            pos_combo.setCurrentText(current_pos)
        self._widgets['adv_indicator_pos'] = pos_combo
        layout.addLayout(self._setting_row(
            "Indicator position",
            "Screen edge where the indicator pill is anchored",
            pos_combo,
        ))

        if current_pos == 'custom':
            custom_note = QLabel(
                "Position set by dragging the indicator. Pick a preset "
                "above to replace it."
            )
            custom_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px; margin-left: 26px;")
            layout.addWidget(custom_note)

        layout.addSpacing(20)

        # ---- Section: Gesture Lane -----------------------------------------
        layout.addWidget(self._section_title("Gesture Lane"))
        layout.addSpacing(4)

        gesture_note = QLabel(
            "Maps deliberate webcam hand poses to commands. Camera runs at low resolution "
            "and is fully released when off."
        )
        gesture_note.setWordWrap(True)
        gesture_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(gesture_note)
        layout.addSpacing(6)

        gesture_cb = QCheckBox("Enable gesture lane (webcam hand poses)")
        gesture_cb.setChecked(bool(self.app.config.get('gesture', {}).get('enabled', False)))
        self._widgets['adv_gesture_enabled'] = gesture_cb
        layout.addWidget(gesture_cb)
        layout.addSpacing(20)

        # ---- Section: Components -------------------------------------------
        layout.addWidget(self._section_title("Components"))
        layout.addSpacing(4)
        components_note = QLabel(
            "Optional downloads are verified before installation. Installed, missing, "
            "and coming-soon components are shown here."
        )
        components_note.setWordWrap(True)
        components_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(components_note)

        from samsara.ui.first_run_qt import ComponentsPage  # noqa: PLC0415
        self._components_page = ComponentsPage(
            container, on_later=lambda: None, include_installed=True, show_later=False,
        )
        layout.addWidget(self._components_page.widget)
        self._components_page.refresh_async()

        def _offer_gesture_install(checked):
            if not checked:
                return
            gesture_cb.blockSignals(True)
            gesture_cb.setChecked(False)
            gesture_cb.blockSignals(False)
            row = self._components_page._rows.get("gesture-control")
            message = "Gesture control is not installed. Add it now from Components?"
            if row is None or not row["get"].isEnabled():
                QMessageBox.information(self, "Gesture control", message)
                return
            choice = QMessageBox.question(
                self, "Gesture control", message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if choice == QMessageBox.StandardButton.Yes:
                self._components_page.start_fetch("gesture-control")

        gesture_cb.toggled.connect(_offer_gesture_install)
        layout.addSpacing(20)

        # ---- Section: Smart Corrections -------------------------------------
        layout.addWidget(self._section_title("Smart Corrections"))
        layout.addSpacing(4)

        sc_cfg = cfg.get('smart_corrections', {}) or {}

        sc_note = QLabel(
            "Optional AI cleanup pass — fixes homophones and misheard words without "
            "rephrasing you. Off by default."
        )
        sc_note.setWordWrap(True)
        sc_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(sc_note)
        layout.addSpacing(6)

        sc_enabled_cb = QCheckBox("Enable Smart Corrections")
        sc_enabled_cb.setChecked(bool(sc_cfg.get('enabled', False)))
        self._widgets['sc_enabled'] = sc_enabled_cb
        layout.addWidget(sc_enabled_cb)
        layout.addSpacing(6)

        sc_status_label = QLabel("Active backend: --")
        sc_status_label.setWordWrap(True)
        sc_status_label.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px; margin-left: 4px;")
        self._widgets['sc_status_label'] = sc_status_label
        layout.addWidget(sc_status_label)
        layout.addSpacing(8)
        self._refresh_sc_status()

        sc_backend_options = ['auto', 'ollama', 'cloud']
        sc_backend_combo = QComboBox()
        sc_backend_combo.addItems(sc_backend_options)
        current_sc_backend = sc_cfg.get('backend', 'auto')
        if current_sc_backend in sc_backend_options:
            sc_backend_combo.setCurrentText(current_sc_backend)
        self._widgets['sc_backend'] = sc_backend_combo
        layout.addLayout(self._setting_row(
            "Backend",
            "auto prefers local Ollama; falls back to Cloud AI if configured",
            sc_backend_combo,
        ))
        layout.addSpacing(4)

        sc_cloud_hint = QLabel("Cloud AI status")
        sc_cloud_hint.setWordWrap(True)
        sc_cloud_hint.setStyleSheet(f"color: {theme.WARNING}; font-size: {theme.TYPE_MIN}px; margin-left: 4px;")

        self._widgets['sc_cloud_hint'] = sc_cloud_hint

        def _update_sc_cloud_hint(_text=None):
            self._refresh_sc_cloud_hint()

        sc_backend_combo.currentTextChanged.connect(_update_sc_cloud_hint)
        layout.addWidget(sc_cloud_hint)
        _update_sc_cloud_hint()
        layout.addSpacing(8)

        sc_fallback_cb = QCheckBox(
            "Allow cloud fallback when local AI is unavailable (sends "
            "dictated text to your cloud provider)"
        )
        sc_fallback_cb.setChecked(bool(sc_cfg.get('allow_cloud_fallback', False)))
        self._widgets['sc_allow_cloud_fallback'] = sc_fallback_cb
        layout.addWidget(sc_fallback_cb)
        layout.addSpacing(8)

        sc_model_edit = QLineEdit()
        sc_model_edit.setText(sc_cfg.get('ollama_model', 'qwen2.5:3b'))
        self._widgets['sc_model'] = sc_model_edit
        layout.addLayout(self._setting_row(
            "Ollama model",
            "Local model used for the correction pass (must already be pulled)",
            sc_model_edit,
        ))
        layout.addSpacing(8)

        sc_modes_cfg = sc_cfg.get('modes', {}) or {}

        sc_mode_hotkey_cb = QCheckBox("Hold-to-dictate")
        sc_mode_hotkey_cb.setChecked(bool(sc_modes_cfg.get('hotkey', True)))
        self._widgets['sc_mode_hotkey'] = sc_mode_hotkey_cb
        layout.addWidget(sc_mode_hotkey_cb)

        sc_mode_wake_cb = QCheckBox("Wake dictation")
        sc_mode_wake_cb.setChecked(bool(sc_modes_cfg.get('wake', True)))
        self._widgets['sc_mode_wake'] = sc_mode_wake_cb
        layout.addWidget(sc_mode_wake_cb)

        sc_mode_streaming_cb = QCheckBox("Streaming")
        sc_mode_streaming_cb.setChecked(bool(sc_modes_cfg.get('streaming', False)))
        self._widgets['sc_mode_streaming'] = sc_mode_streaming_cb
        layout.addWidget(sc_mode_streaming_cb)
        layout.addSpacing(8)

        sc_repair_disfluencies_cb = QCheckBox(
            "Remove filler words and self-corrections "
            "('I totally understand, misunderstood' -> 'I totally misunderstood')"
        )
        sc_repair_disfluencies_cb.setChecked(bool(sc_cfg.get('repair_disfluencies', False)))
        self._widgets['sc_repair_disfluencies'] = sc_repair_disfluencies_cb
        layout.addWidget(sc_repair_disfluencies_cb)
        layout.addSpacing(20)

        # ---- Section: Benchmark ---------------------------------------------
        layout.addWidget(self._section_title("Benchmark"))
        layout.addSpacing(4)

        bench_cfg = cfg.get('benchmark', {}) or {}

        bench_note = QLabel(
            "Saves your dictation audio and transcripts locally so you can review accuracy. "
            "Nothing leaves this machine."
        )
        bench_note.setWordWrap(True)
        bench_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(bench_note)
        layout.addSpacing(6)

        bench_cb = QCheckBox("Collect benchmark samples")
        bench_cb.setChecked(bool(bench_cfg.get('collect_samples', False)))
        self._widgets['adv_bench_collect'] = bench_cb
        layout.addWidget(bench_cb)
        layout.addSpacing(20)

        # ---- Section: Command word (93) --------------------------------------
        layout.addWidget(self._section_title("Command word"))
        layout.addSpacing(4)

        intent_note = QLabel(
            "Samsara types a single word rather than running it as a command, so you can "
            "dictate \"copy\", \"yes\" or \"Claude\" without anything happening. Set a word "
            "here and saying it first runs the rest as a command instead -- say it and then "
            "\"copy\" to actually copy. Leave it empty to turn this off."
        )
        intent_note.setWordWrap(True)
        intent_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(intent_note)
        layout.addSpacing(6)

        intent_prefix_edit = QLineEdit()
        intent_prefix_edit.setText(str((cfg.get('intent', {}) or {}).get('command_prefix', '') or ''))
        intent_prefix_edit.setPlaceholderText("empty -- off")
        self._widgets['adv_intent_command_prefix'] = intent_prefix_edit
        layout.addLayout(self._setting_row(
            "Command word",
            "Spoken before a command to run it even when it is one word",
            intent_prefix_edit,
        ))

        def _save(acc):
            updates = {}
            if 'adv_device' in self._widgets:
                device_map  = self._widgets['adv_device_map']
                device_disp = self._widgets['adv_device'].currentText()
                updates['device']            = device_map.get(device_disp, 'cpu')
                updates['compute_type']      = self._widgets['adv_compute_type'].currentText()
                updates['performance_mode']  = self._widgets['adv_perf_mode'].currentText()
                updates['silence_threshold'] = self._widgets['adv_silence'].value()
                updates['min_speech_duration'] = self._widgets['adv_min_speech'].value()
                updates['threshold_mode']    = self._widgets['adv_threshold_mode'].currentText()
                updates['cal_multiplier']    = self._widgets['adv_cal_multiplier'].value()
                updates['echo_cancellation'] = {
                    'enabled':    self._widgets['adv_aec_enabled'].isChecked(),
                    'latency_ms': self._widgets['adv_aec_latency'].value(),
                }
                ducking_out = dict(self.app.config.get('ducking', {}) or {})
                ducking_out.update({
                    'enabled': self._widgets['adv_ducking_enabled'].isChecked(),
                    'level':   self._widgets['adv_ducking_level'].value(),
                    'hands_free_enabled': self._widgets[
                        'adv_hands_free_ducking_enabled'
                    ].isChecked(),
                    'hands_free_level': self._widgets[
                        'adv_hands_free_ducking_level'
                    ].value(),
                    'hands_free_idle_level': self._widgets[
                        'adv_hands_free_idle_level'
                    ].value(),
                })
                updates['ducking'] = ducking_out
                updates['listening_indicator_enabled']  = (
                    self._widgets['adv_indicator_enabled'].isChecked()
                )
                selected_pos = self._widgets['adv_indicator_pos'].currentText()
                updates['listening_indicator_position'] = selected_pos
                if selected_pos != 'custom' and self.app.config.get(
                        'listening_indicator_custom_position'):
                    # A preset was chosen over the previously dragged
                    # placement -- discard it so it can't resurrect on a
                    # future switch back to "custom".
                    updates['listening_indicator_custom_position'] = None
                if 'adv_gesture_enabled' in self._widgets:
                    gesture_cfg = dict(self.app.config.get('gesture', {}) or {})
                    gesture_cfg['enabled'] = self._widgets['adv_gesture_enabled'].isChecked()
                    updates['gesture'] = gesture_cfg
                if 'sc_enabled' in self._widgets:
                    sc_cfg_out = dict(self.app.config.get('smart_corrections', {}) or {})
                    sc_cfg_out['enabled'] = self._widgets['sc_enabled'].isChecked()
                    sc_cfg_out['backend'] = self._widgets['sc_backend'].currentText()
                    sc_cfg_out['ollama_model'] = (
                        self._widgets['sc_model'].text().strip() or 'qwen2.5:3b'
                    )
                    sc_cfg_out['allow_cloud_fallback'] = (
                        self._widgets['sc_allow_cloud_fallback'].isChecked()
                    )
                    sc_cfg_out['modes'] = {
                        'hotkey':    self._widgets['sc_mode_hotkey'].isChecked(),
                        'wake':      self._widgets['sc_mode_wake'].isChecked(),
                        'streaming': self._widgets['sc_mode_streaming'].isChecked(),
                    }
                    sc_cfg_out['repair_disfluencies'] = (
                        self._widgets['sc_repair_disfluencies'].isChecked()
                    )
                    updates['smart_corrections'] = sc_cfg_out
                if 'adv_bench_collect' in self._widgets:
                    bench_cfg_out = dict(self.app.config.get('benchmark', {}) or {})
                    bench_cfg_out['collect_samples'] = self._widgets['adv_bench_collect'].isChecked()
                    updates['benchmark'] = bench_cfg_out
                if 'adv_intent_command_prefix' in self._widgets:
                    # Merge onto the whole intent section: shadow_enabled is
                    # config-file-only and must survive a Settings save.
                    intent_out = dict(self.app.config.get('intent', {}) or {})
                    intent_out['command_prefix'] = (
                        self._widgets['adv_intent_command_prefix'].text().strip()
                    )
                    updates['intent'] = intent_out
                # Apply manual threshold to wake_word_config if in manual mode --
                # merge onto whatever the Modes tab already wrote (read from
                # acc), not self.app.config, so that write isn't clobbered.
                if self._widgets['adv_threshold_mode'].currentText() == 'manual':
                    ww_cfg = dict(self.app.config.get('wake_word_config', {}) or {})
                    ww_audio = dict(ww_cfg.get('audio', {}) or {})
                    val = self._widgets['adv_manual_threshold'].value()
                    val = max(0.005, min(0.20, val))
                    ww_audio['speech_threshold'] = val
                    ww_cfg['audio'] = ww_audio
                    existing_ww = acc.get('wake_word_config', ww_cfg)
                    existing_ww_audio = dict(existing_ww.get('audio', {}) or {})
                    existing_ww_audio['speech_threshold'] = val
                    existing_ww['audio'] = existing_ww_audio
                    updates['wake_word_config'] = existing_ww
            return updates
        self._save_fns.append(_save)

        layout.addStretch()
        scroll.setWidget(container)
        return scroll
