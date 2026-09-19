"""TTS page of the Samsara settings window.

Moved verbatim out of samsara/ui/settings_qt.py (queue 21, no behaviour change).
TTSPage is mixed into settings_qt._SettingsWindow, so every method still runs
with the window as self (self._widgets, self._save_fns, self._setting_row ...).
Shared names are imported back from samsara.ui.settings_qt; see
samsara/ui/settings/__init__.py for why that import is cycle-safe.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from samsara.ui import theme

from samsara.ui.settings_qt import _CONTENT_MAX_WIDTH, logger


class TTSPage:
    """Methods of the TTS settings page (moved from _SettingsWindow)."""

    def _build_tts_tab(self):
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

        cfg    = self.app.config.get('tts', {}) or {}
        ac_cfg = self.app.config.get('audio_coordinator', {}) or {}

        # ---- Master toggle --------------------------------------------------
        layout.addWidget(self._section_title("Text-to-Speech"))
        layout.addSpacing(4)

        tts_enabled = QCheckBox("Enable text-to-speech")
        tts_enabled.setChecked(bool(cfg.get('enabled', False)))
        self._widgets['tts_enabled'] = tts_enabled
        layout.addWidget(tts_enabled)

        restart_note = QLabel("Restart Samsara to apply enable/disable changes.")
        restart_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(restart_note)
        layout.addSpacing(16)

        # ---- Engine & Voice -------------------------------------------------
        layout.addWidget(self._section_title("Voice"))
        layout.addSpacing(4)

        engine_combo = QComboBox()
        engine_combo.addItems(['winrt', 'edge'])
        engine_combo.setCurrentText(cfg.get('engine', 'edge'))
        self._widgets['tts_engine'] = engine_combo
        layout.addLayout(self._setting_row(
            "Engine",
            "winrt: built-in Windows voices.  edge: higher-quality online voices (internet required). Restart required.",
            engine_combo,
        ))
        edge_note = QLabel("Edge uses Microsoft’s online voices; WinRT is the offline option.")
        edge_note.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(edge_note)
        layout.addSpacing(8)

        # Populate voice list from current engine instance
        voice_labels: list[str] = []
        label_to_id: dict[str, str] = {}
        id_to_label: dict[str, str] = {}
        engine_obj = getattr(self.app, 'tts_engine', None)
        if engine_obj is not None:
            try:
                for v in engine_obj.list_voices():
                    lbl = f"{v.display_name} ({v.language})"
                    voice_labels.append(lbl)
                    label_to_id[lbl] = v.voice_id
                    id_to_label[v.voice_id] = lbl
            except Exception as e:
                logger.debug(f"_build_tts_tab: {e}")

        current_voice_id = cfg.get('voice_id')
        if voice_labels:
            initial_lbl = id_to_label.get(current_voice_id, voice_labels[0])
        else:
            initial_lbl = current_voice_id or "No voices — enable TTS and restart"

        voice_combo = QComboBox()
        voice_combo.addItems(voice_labels or [initial_lbl])
        voice_combo.setCurrentText(initial_lbl)
        voice_combo.setEnabled(bool(voice_labels))
        self._widgets['tts_voice_combo']        = voice_combo
        self._widgets['tts_voice_label_to_id']  = label_to_id
        layout.addLayout(self._setting_row(
            "Voice",
            "Voice used for Ava's spoken responses",
            voice_combo,
        ))
        layout.addSpacing(16)

        # ---- Voice tuning ---------------------------------------------------
        layout.addWidget(self._section_title("Voice Tuning"))
        layout.addSpacing(4)

        speed_spin = QDoubleSpinBox()
        speed_spin.setRange(0.5, 2.0)
        speed_spin.setSingleStep(0.1)
        speed_spin.setDecimals(2)
        speed_spin.setValue(float(cfg.get('speed', 1.0)))
        self._widgets['tts_speed'] = speed_spin
        layout.addLayout(self._setting_row(
            "Speed",
            "1.0 = normal, 0.5 = half-speed, 2.0 = double-speed",
            speed_spin,
        ))
        layout.addSpacing(8)

        pitch_spin = QDoubleSpinBox()
        pitch_spin.setRange(0.5, 2.0)
        pitch_spin.setSingleStep(0.1)
        pitch_spin.setDecimals(2)
        pitch_spin.setValue(float(cfg.get('pitch', 1.0)))
        self._widgets['tts_pitch'] = pitch_spin
        layout.addLayout(self._setting_row(
            "Pitch",
            "1.0 = normal pitch, lower values deepen, higher values raise",
            pitch_spin,
        ))
        layout.addSpacing(8)

        raw_vol = float(cfg.get('volume', 0.8))
        vol_slider = QSlider(Qt.Orientation.Horizontal)
        vol_slider.setRange(0, 100)
        vol_slider.setValue(int(raw_vol * 100))
        vol_slider.setFixedWidth(200)
        vol_slider.setMinimumHeight(theme.HIT_TARGET_MIN)
        vol_slider.setStyleSheet(
            f"QSlider::groove:horizontal{{height:4px;background:{theme.wash(0.12)};border-radius:2px;}}"
            f"QSlider::handle:horizontal{{width:16px;height:16px;margin:-6px 0;border-radius:8px;background:{theme.ACCENT};}}"
            f"QSlider::sub-page:horizontal{{background:{theme.ACCENT};border-radius:2px;}}"
        )
        vol_pct = QLabel(f"{int(raw_vol * 100)}%")
        vol_pct.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px;")
        vol_pct.setFixedWidth(40)
        vol_slider.valueChanged.connect(lambda v: vol_pct.setText(f"{v}%"))
        self._widgets['tts_volume_slider'] = vol_slider

        vol_container = QWidget()
        vol_h = QHBoxLayout(vol_container)
        vol_h.setContentsMargins(0, 0, 0, 0)
        vol_h.setSpacing(8)
        vol_h.addWidget(vol_slider)
        vol_h.addWidget(vol_pct)
        layout.addLayout(self._setting_row(
            "Volume",
            "TTS speech volume (0 = silent, 100 = full)",
            vol_container,
        ))
        layout.addSpacing(16)

        # ---- Audio ducking --------------------------------------------------
        layout.addWidget(self._section_title("Audio Ducking"))
        layout.addSpacing(4)

        duck_desc = QLabel(
            "Reduce background audio while Ava is speaking so her voice is clearly audible."
        )
        duck_desc.setWordWrap(True)
        duck_desc.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(duck_desc)
        layout.addSpacing(6)

        duck_enabled = QCheckBox("Duck audio while Ava is speaking")
        duck_enabled.setChecked(bool(ac_cfg.get('enabled', True)))
        self._widgets['tts_duck_enabled'] = duck_enabled
        layout.addWidget(duck_enabled)
        layout.addSpacing(8)

        raw_duck = float(ac_cfg.get('duck_factor', 0.7))
        duck_slider = QSlider(Qt.Orientation.Horizontal)
        duck_slider.setRange(0, 100)
        duck_slider.setValue(int(raw_duck * 100))
        duck_slider.setFixedWidth(200)
        duck_slider.setMinimumHeight(theme.HIT_TARGET_MIN)
        duck_slider.setStyleSheet(
            f"QSlider::groove:horizontal{{height:4px;background:{theme.wash(0.12)};border-radius:2px;}}"
            f"QSlider::handle:horizontal{{width:16px;height:16px;margin:-6px 0;border-radius:8px;background:{theme.ACCENT};}}"
            f"QSlider::sub-page:horizontal{{background:{theme.ACCENT};border-radius:2px;}}"
        )
        duck_pct = QLabel(f"{int(raw_duck * 100)}%")
        duck_pct.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px;")
        duck_pct.setFixedWidth(40)
        duck_slider.valueChanged.connect(lambda v: duck_pct.setText(f"{v}%"))
        self._widgets['tts_duck_slider'] = duck_slider

        duck_container = QWidget()
        duck_h = QHBoxLayout(duck_container)
        duck_h.setContentsMargins(0, 0, 0, 0)
        duck_h.setSpacing(8)
        duck_h.addWidget(duck_slider)
        duck_h.addWidget(duck_pct)
        layout.addLayout(self._setting_row(
            "Duck level",
            "How much to reduce other audio (100% = silent others, 0% = no reduction)",
            duck_container,
        ))
        layout.addSpacing(16)

        # ---- Test -----------------------------------------------------------
        test_row = QHBoxLayout()
        test_row.setSpacing(12)
        test_btn = QPushButton("Test Voice")
        test_btn.setMinimumWidth(120)
        test_btn.setMinimumHeight(theme.HIT_TARGET_MIN)
        test_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        test_btn.clicked.connect(self._test_tts)
        test_status = QLabel("")
        test_status.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        self._widgets['tts_test_status'] = test_status
        test_row.addWidget(test_btn)
        test_row.addWidget(test_status)
        test_row.addStretch()
        layout.addLayout(test_row)
        layout.addSpacing(16)

        # ---- When to speak (collapsible) ------------------------------------
        when_toggle = QPushButton("When should Samsara speak?  ▶")
        when_toggle.setMinimumHeight(theme.HIT_TARGET_MIN)
        when_toggle.setStyleSheet(
            f"QPushButton{{background:transparent;color:{theme.ICON_IDLE};border:none;"
            f"font-size:{theme.TYPE_BODY}px;text-align:left;padding:0;}}"
            f"QPushButton:hover{{color:{theme.TEXT_PRIMARY};}}"
        )
        layout.addWidget(when_toggle)

        when_widget = QWidget()
        when_widget.setVisible(False)
        when_layout = QVBoxLayout(when_widget)
        when_layout.setContentsMargins(0, 8, 0, 0)
        when_layout.setSpacing(6)

        phase_note = QLabel(
            "Some of these categories aren't wired up yet — saved now, applied when they are."
        )
        phase_note.setWordWrap(True)
        phase_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        when_layout.addWidget(phase_note)

        _WHEN_TOGGLES = [
            ('tts_use_agent',    "Speak agent responses",           'use_for_agent_responses',    True),
            ('tts_use_confirm',  "Speak confirmations",             'use_for_confirmations',      True),
            ('tts_use_warnings', "Speak warnings",                  'use_for_warnings',           True),
            ('tts_use_status',   "Speak status updates (Thinking)", 'use_for_status_updates',     True),
            ('tts_use_readback', "Speak dictation readback",        'use_for_dictation_readback', False),
            ('tts_use_errors',   "Speak errors",                    'use_for_errors',             True),
        ]
        for wkey, label_text, cfg_key, default in _WHEN_TOGGLES:
            cb = QCheckBox(label_text)
            cb.setChecked(bool(cfg.get(cfg_key, default)))
            self._widgets[wkey] = cb
            when_layout.addWidget(cb)

        layout.addWidget(when_widget)

        def _toggle_when():
            vis = not when_widget.isVisible()
            when_widget.setVisible(vis)
            when_toggle.setText(
                "When should Samsara speak?  " + ("▼" if vis else "▶")
            )
        when_toggle.clicked.connect(_toggle_when)

        def _save(_acc):
            updates = {}
            if 'tts_enabled' in self._widgets:
                tts_cfg = dict(self.app.config.get('tts', {}) or {})
                tts_cfg['enabled'] = self._widgets['tts_enabled'].isChecked()
                tts_cfg['engine']  = self._widgets['tts_engine'].currentText()
                voice_label = self._widgets['tts_voice_combo'].currentText()
                voice_id    = self._widgets.get('tts_voice_label_to_id', {}).get(voice_label)
                if voice_id:
                    tts_cfg['voice_id'] = voice_id
                tts_cfg['speed']  = self._widgets['tts_speed'].value()
                tts_cfg['pitch']  = self._widgets['tts_pitch'].value()
                tts_cfg['volume'] = self._widgets['tts_volume_slider'].value() / 100.0
                for wkey, _label, cfg_key, _default in _WHEN_TOGGLES:
                    if wkey in self._widgets:
                        tts_cfg[cfg_key] = self._widgets[wkey].isChecked()
                updates['tts'] = tts_cfg
                # Audio coordinator duck settings
                ac_cfg = dict(self.app.config.get('audio_coordinator', {}) or {})
                ac_cfg['enabled']     = self._widgets['tts_duck_enabled'].isChecked()
                ac_cfg['duck_factor'] = self._widgets['tts_duck_slider'].value() / 100.0
                updates['audio_coordinator'] = ac_cfg
            return updates
        self._save_fns.append(_save)

        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def _test_tts(self) -> None:
        coordinator = getattr(self.app, 'audio_coordinator', None)
        status = self._widgets.get('tts_test_status')
        if coordinator is None:
            if status:
                status.setText("TTS not initialized — restart with TTS enabled.")
            return
        voice_combo = self._widgets.get('tts_voice_combo')
        voice_label = voice_combo.currentText() if voice_combo else None
        voice_id = self._widgets.get('tts_voice_label_to_id', {}).get(voice_label)
        speed  = self._widgets['tts_speed'].value() if 'tts_speed' in self._widgets else 1.0
        volume = (self._widgets['tts_volume_slider'].value() / 100.0
                  if 'tts_volume_slider' in self._widgets else 0.8)
        _PHRASE = "Note saved. Your reminder will be in the brain dump."
        try:
            coordinator.speak(
                _PHRASE, voice_id=voice_id, speed=speed, volume=volume, category="general"
            )
            if status:
                status.setText(f'Speaking: "{_PHRASE}"')
        except Exception as exc:
            if status:
                status.setText(f"Error: {exc}")
