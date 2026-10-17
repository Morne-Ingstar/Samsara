"""TTS page of the Samsara settings window.

Moved verbatim out of samsara/ui/settings_qt.py (queue 21, no behaviour change).
TTSPage is mixed into settings_qt._SettingsWindow, so every method still runs
with the window as self (self._widgets, self._save_fns, self._setting_row ...).
Shared names are imported back from samsara.ui.settings_qt; see
samsara/ui/settings/__init__.py for why that import is cycle-safe.
"""

from PySide6.QtCore import Qt, QEvent, QSignalBlocker, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
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
from samsara.ui import network_voice_notice
from samsara.ui.settings.presentation import CleanComboBox as QComboBox

from samsara.ui.settings_qt import _CONTENT_MAX_WIDTH, logger
from samsara.config_schema import (
    FEEDBACK_LEVEL_CHOICES, FEEDBACK_LEVEL_FRESH_DEFAULT, apply_feedback_level,
)


class _FeedbackLevelCombo(QComboBox):
    """Refresh destination help after assembly has reparented the control."""

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_help()


class _VoiceDetailsLink(QLabel):
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.linkActivated.emit('notice')
            event.accept()
        else:
            super().keyPressEvent(event)


class _VoiceNoticeArea(QWidget):
    """Move legacy-user consent above Audio rows after page assembly."""

    def eventFilter(self, owner, event):
        if event.type() == QEvent.Type.Show and hasattr(owner, '_pages'):
            if self.legacy_panel.parentWidget() is self:
                owner._pages['Audio'].base.insertWidget(0, self.legacy_panel)
                owner._pages['Audio'].base.insertSpacing(1, theme.SPACE_4)
        if event.type() == QEvent.Type.Hide and hasattr(owner, 'app'):
            cfg = owner.app.config.get('tts', {}) or {}
            owner._network_voice_accepted = bool(cfg.get('network_voice_accepted', False))
            with QSignalBlocker(owner._widgets['tts_engine']):
                owner._widgets['tts_engine'].setCurrentText(cfg.get('engine', 'winrt'))
            adapter = owner._widgets['tts_engine'].parentWidget()
            if getattr(adapter, '_canonical_control', None) is owner._widgets['tts_engine']:
                adapter.refresh()
            owner._refresh_voice_notice()
        return False


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

        # Queue 359 owns assembly.py, whose conditions still read this mirror.
        # Keep it out of every layout; the combo is the only visible control.
        tts_enabled = QCheckBox()
        self._widgets['tts_enabled'] = tts_enabled
        feedback_combo = _FeedbackLevelCombo()
        for level, (label, help_text) in FEEDBACK_LEVEL_CHOICES.items():
            feedback_combo.addItem(label, level)
            feedback_combo.setItemData(feedback_combo.count() - 1, help_text, Qt.ItemDataRole.ToolTipRole)
        level = cfg.get('feedback_level', FEEDBACK_LEVEL_FRESH_DEFAULT)
        index = feedback_combo.findData(level)
        feedback_combo.setCurrentIndex(index if index >= 0 else feedback_combo.findData(FEEDBACK_LEVEL_FRESH_DEFAULT))
        self._widgets['tts_feedback_level'] = feedback_combo

        def sync_feedback():
            selected = feedback_combo.currentData()
            tts_enabled.setChecked(selected != 'silent')
            help_text = FEEDBACK_LEVEL_CHOICES[selected][1]
            feedback_combo.setAccessibleDescription(help_text)
            row = getattr(self, '_rows_by_widget', {}).get('tts_feedback_level')
            if row is not None:
                row.detail.setText(help_text)
                row.description = help_text

        feedback_combo.refresh_help = sync_feedback
        feedback_combo.currentIndexChanged.connect(sync_feedback)
        sync_feedback()
        layout.addWidget(feedback_combo)

        restart_note = QLabel("Enable, disable, and engine changes take effect on Apply.")
        restart_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(restart_note)
        layout.addSpacing(16)

        # ---- Engine & Voice -------------------------------------------------
        layout.addWidget(self._section_title("Voice"))
        layout.addSpacing(4)

        engine_combo = QComboBox()
        engine_combo.addItems(['winrt', 'edge'])
        engine_combo.setCurrentText(cfg.get('engine', 'winrt'))
        self._widgets['tts_engine'] = engine_combo
        self._network_voice_accepted = bool(cfg.get('network_voice_accepted', False))
        self._build_network_voice_notice(engine_combo, cfg)
        layout.addLayout(self._setting_row(
            "Engine",
            "winrt: built-in Windows voices.  edge: higher-quality online voices (internet required).",
            engine_combo,
        ))
        edge_note = QLabel("Edge uses Microsoft’s online voices; WinRT is the offline option.")
        edge_note.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(edge_note)
        layout.addSpacing(8)

        voice_combo = QComboBox()
        self._widgets['tts_voice_combo'] = voice_combo
        self._refresh_tts_voices(cfg.get('voice_id'))
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
        duck_desc.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(duck_desc)
        layout.addSpacing(6)

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
            "Remaining Ava speech volume during earcons (100% = full volume, 0% = silent).",
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
        test_status.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px;")
        self._widgets['tts_test_status'] = test_status
        test_row.addWidget(test_btn)
        test_row.addWidget(test_status)
        test_row.addStretch()
        layout.addLayout(test_row)
        layout.addSpacing(16)

        def _save(_acc):
            updates = {}
            if 'tts_feedback_level' in self._widgets:
                tts_cfg = dict(self.app.config.get('tts', {}) or {})
                apply_feedback_level({'tts': tts_cfg}, self._widgets['tts_feedback_level'].currentData())
                tts_cfg['engine']  = self._widgets['tts_engine'].currentText()
                if self._network_voice_accepted:
                    tts_cfg['network_voice_accepted'] = True
                voice_id = self._widgets['tts_voice_combo'].currentData()
                # An unavailable catalog cannot establish that a saved ID is bad.
                if self._tts_catalog_ids:
                    tts_cfg['voice_id'] = voice_id
                elif getattr(self, '_tts_voices_pending', False) and tts_cfg['engine'] != (self.app.config.get('tts', {}) or {}).get('engine', 'winrt'):
                    tts_cfg['voice_id'] = None
                tts_cfg['speed']  = self._widgets['tts_speed'].value()
                tts_cfg['volume'] = self._widgets['tts_volume_slider'].value() / 100.0
                updates['tts'] = tts_cfg
                # Audio coordinator duck settings
                ac_cfg = dict(self.app.config.get('audio_coordinator', {}) or {})
                ac_cfg['duck_factor'] = self._widgets['tts_duck_slider'].value() / 100.0
                updates['audio_coordinator'] = ac_cfg
            return updates
        self._save_fns.append(_save)

        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def _tts_voices_for(self, provider, accepted):
        """Enumerate local catalogs, disposing private engines without speaking."""
        from samsara.ui.tutorial_try_preview import make_engine
        live_provider = (self.app.config.get('tts', {}) or {}).get('engine', 'winrt')
        live_engine = getattr(self.app, 'tts_engine', None)
        coordinator = getattr(self.app, 'audio_coordinator', None)
        live_provider = getattr(coordinator, '_engine_provider', None) or live_provider
        catalog = getattr(self, '_tts_live_catalog', None)
        if catalog is not None and catalog[0] is live_engine:
            live_provider = catalog[1]
        engine = live_engine if provider == live_provider else None
        if engine is not None:
            # Config may change before its runtime; an existing engine does
            # not become another provider merely because the config changed.
            self._tts_live_catalog = (engine, provider)
        private = engine is None
        try:
            if private:
                engine = make_engine(self.app, provider)
            return list(engine.list_voices())
        except Exception as exc:
            logger.debug('Settings voice enumeration failed: %s', type(exc).__name__)
            return []
        finally:
            if private and engine is not None:
                try:
                    engine.shutdown()
                except Exception as exc:
                    logger.debug('Settings voice engine disposal failed: %s', type(exc).__name__)

    def _validate_tts_voice(self, requested):
        """The merge can retain an outside engine change; validate its final pair."""
        requested = dict(requested)
        voice_id = requested.get('voice_id')
        if voice_id:
            provider = requested.get('engine', 'winrt')
            if provider == self._widgets['tts_engine'].currentText():
                combo = self._widgets['tts_voice_combo']
                known = [combo.itemData(i) for i in range(combo.count())]
            else:
                known = getattr(self, '_tts_provider_catalogs', {}).get(provider, ())
                if not known:
                    # An outside engine change cannot inherit this dialog's
                    # old-provider voice. Trust a complete outside pair only
                    # when its voice also changed since this visit opened.
                    live = self.app.config.get('tts', {}) or {}
                    opened = getattr(self, '_config_at_open', {}).get('tts', {}) or {}
                    outside_pair = (live.get('engine') == provider
                                    and live.get('voice_id') == voice_id
                                    and voice_id != opened.get('voice_id'))
                    if not outside_pair:
                        requested['voice_id'] = None
                    return requested
            if any(known) and voice_id not in known:
                requested['voice_id'] = None
        return requested

    def _refresh_tts_voices(self, preferred_id=None):
        """Show the selected provider's voices and its explicit default."""
        provider = self._widgets['tts_engine'].currentText()
        combo = self._widgets['tts_voice_combo']
        from samsara.runtime import thread_registry
        self._tts_voice_request = getattr(self, '_tts_voice_request', 0) + 1
        request = self._tts_voice_request
        self._tts_catalog_ids = set()
        self._tts_voices_pending = True
        with QSignalBlocker(combo):
            combo.clear()
            combo.addItem('checking...', preferred_id)  # Stand-in copy (619).
        combo.setEnabled(False)
        self._widgets['tts_voice_label_to_id'] = {}
        accepted = self._network_voice_accepted
        def ready(voices):
            if request != self._tts_voice_request:
                return
            self._tts_voices_pending = False
            if not hasattr(self, '_tts_provider_catalogs'):
                self._tts_provider_catalogs = {}
            self._tts_provider_catalogs[provider] = {voice.voice_id for voice in voices}
            self._populate_tts_voices(combo, voices, preferred_id)
        deliver = self._visit_callback(ready, combo)
        def load():
            voices = self._tts_voices_for(provider, accepted)
            self._post_visit_result(deliver, (voices,))
        thread_registry.spawn('settings-voice-catalog', load, daemon=True)

    def _populate_tts_voices(self, combo, voices, preferred_id):
        self._tts_catalog_ids = {voice.voice_id for voice in voices}
        labels = {}
        with QSignalBlocker(combo):
            combo.clear()
            combo.addItem('Default voice', None)  # Stand-in copy (543).
            for voice in voices:
                label = f'{voice.display_name} ({voice.language})'
                combo.addItem(label, voice.voice_id)
                labels[label] = voice.voice_id
            index = combo.findData(preferred_id) if preferred_id else 0
            combo.setCurrentIndex(max(0, index))
        self._widgets['tts_voice_label_to_id'] = labels
        # An explicit default remains available when enumeration fails.
        combo.setEnabled(True)
        self._fit_catalog_combo(combo)
        adapter = combo.parentWidget()
        if getattr(adapter, '_canonical_control', None) is combo:
            adapter.refresh()

    def _test_tts(self) -> None:
        provider = self._widgets['tts_engine'].currentText()
        live_cfg = self.app.config.get('tts', {}) or {}
        live_provider = live_cfg.get('engine', 'winrt')
        status = self._widgets.get('tts_test_status')
        if provider != live_provider:
            if status is not None:
                status.setText('Apply the engine change, then reopen Settings to test this voice.')  # Stand-in copy (543).
            return
        if provider == 'edge' and not self._ensure_network_voice_accepted():
            return
        voice_id = self._widgets['tts_voice_combo'].currentData()
        # The live coordinator interprets None as "use the saved voice".
        # An unapplied default must not silently test that previous voice.
        if voice_id is None and live_cfg.get('voice_id'):
            if status is not None:
                status.setText('Apply the voice change, then reopen Settings to test this voice.')  # Stand-in copy (543).
            return
        coordinator = getattr(self.app, 'audio_coordinator', None)
        if coordinator is None or coordinator.engine is None:
            if status is not None:
                status.setText("TTS is off. Enable it and Apply settings.")
            return
        speed  = self._widgets['tts_speed'].value() if 'tts_speed' in self._widgets else 1.0
        volume = (self._widgets['tts_volume_slider'].value() / 100.0
                  if 'tts_volume_slider' in self._widgets else 0.8)
        _PHRASE = "Note saved. Your reminder will be in the brain dump."
        try:
            handle = coordinator.speak(
                _PHRASE, voice_id=voice_id, speed=speed, volume=volume, category="readback"
            )
            if status:
                status.setText(f'Speaking: "{_PHRASE}"')
            self._watch_tts_sample(handle, status)
        except Exception as exc:
            if status:
                status.setText(f"Error: {exc}")

    def _watch_tts_sample(self, handle, status):
        """Read engine outcomes on the Qt thread, for this visit/sample."""
        previous = getattr(self, '_tts_sample_timer', None)
        if previous is not None:
            previous.stop()
            previous.deleteLater()
        self._tts_sample_timer = None

        def outcome():
            return getattr(handle, 'voice_result', None) or getattr(handle, '_state', None)

        active = ('pending', 'synthesizing', 'playing')
        if status is None or outcome() not in (
                *active, 'done', 'network', 'windows', 'failed', 'cancelled'):
            return
        timer = QTimer(status)
        self._tts_sample_timer = timer
        timer.setInterval(100)

        def poll():
            if self._tts_sample_timer is not timer:
                return
            result = outcome()
            if result == 'windows':
                status.setText('Spoken with the Windows voice because the Network voice did not answer.')
            elif result == 'failed':
                status.setText('The voice sample could not be spoken.')
            elif result == 'cancelled':
                status.setText('Voice sample cancelled.')
            elif result == 'pending' and getattr(handle, 'network_failed', False):
                status.setText('The Network voice did not answer. Trying the Windows voice.')
            if result not in active:
                timer.stop()

        timer.timeout.connect(self._visit_callback(poll, status))
        poll()
        if outcome() in active:
            timer.start()

    def _voice_notice_dialog(self, read_only=False):
        dialog = network_voice_notice.NetworkVoiceNotice(self)
        buttons = dialog.findChildren(QPushButton)
        for button in buttons:
            button.setAutoDefault(False)
            button.setDefault(False)
        safe = buttons[0]
        safe.setDefault(True)
        safe.setFocus()
        QWidget.setTabOrder(safe, buttons[1])
        if read_only:
            buttons[1].hide()
        return dialog

    def _ensure_network_voice_accepted(self):
        if self._network_voice_accepted:
            return True
        dialog = self._voice_notice_dialog()
        accepted = dialog.exec() == dialog.DialogCode.Accepted
        dialog.deleteLater()
        if accepted:
            self._network_voice_accepted = True
            self._refresh_voice_notice()
            if 'tts_voice_combo' in self._widgets:
                self._refresh_tts_voices(self._widgets['tts_voice_combo'].currentData())
        return accepted

    def _show_voice_notice(self):
        dialog = self._voice_notice_dialog(read_only=True)
        dialog.exec()
        dialog.deleteLater()

    def _build_network_voice_notice(self, combo, cfg):
        area = _VoiceNoticeArea()
        column = QVBoxLayout(area)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(theme.SPACE_2)
        quiet = QWidget()
        quiet_layout = QVBoxLayout(quiet)
        quiet_layout.setContentsMargins(0, 0, 0, 0)
        fact = QLabel(network_voice_notice.notice_facts()[0])
        fact.setWordWrap(True)
        fact.setProperty('role', 'description')
        quiet_layout.addWidget(fact)
        # PLACEHOLDER COPY (377); native link survives the shared button treatment.
        link = _VoiceDetailsLink(f'<a href="notice" style="color:{theme.ACCENT}; text-decoration:none;">What is sent</a>')
        link.setAccessibleName('What is sent')
        link.setOpenExternalLinks(False)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse
                                     | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        link.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        link.setMinimumHeight(theme.HIT_TARGET_MIN)
        link.setStyleSheet('QLabel { background:transparent; border:1px solid transparent; text-decoration:none; }'
                          f'QLabel:focus {{ border:1px solid {theme.ACCENT}; }}')
        link.linkActivated.connect(lambda _url: self._show_voice_notice())
        quiet_layout.addWidget(link, alignment=Qt.AlignmentFlag.AlignLeft)
        column.addWidget(quiet)
        panel = QFrame()
        theme.style_card(panel)
        panel.setStyleSheet(f'QFrame[class="card"] {{ background:{theme.BG1}; '
                           f'border:1px solid {theme.BORDER}; border-radius:8px; }}')
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(theme.SPACE_4, theme.SPACE_4, theme.SPACE_4, theme.SPACE_4)
        panel_layout.setSpacing(theme.SPACE_2)
        heading = QLabel(network_voice_notice.NOTICE_HEADING)
        heading.setStyleSheet(f'font-size:{theme.TYPE_HEADING}px;font-weight:600;')
        panel_layout.addWidget(heading)
        for text in network_voice_notice.notice_facts():
            label = QLabel(text)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            panel_layout.addWidget(label)
        actions = QHBoxLayout()
        actions.addStretch()
        for title, local in (('Use the local voice', True), ('Keep the network voice', False)):
            button = QPushButton(title)  # PLACEHOLDER COPY (377)
            button.setMinimumHeight(theme.HIT_TARGET_MIN)
            theme.make_secondary(button)
            button.setAutoDefault(False)
            button.clicked.connect(lambda checked=False, local=local: self._resolve_existing_voice(local))
            actions.addWidget(button)
        panel_layout.addLayout(actions)
        column.addWidget(panel)
        area.legacy_panel = panel
        self._widgets['tts_engine_warn'] = area
        self._widgets['network_voice_quiet'] = quiet
        self._widgets['network_voice_existing'] = panel
        self.installEventFilter(area)
        self._refresh_voice_notice()
        combo.currentTextChanged.connect(self._network_voice_changed)

    def _resolve_existing_voice(self, local):
        if local:
            self._widgets['tts_engine'].setCurrentText('winrt')
        else:
            self._network_voice_accepted = True
            self._refresh_tts_voices((self.app.config.get('tts', {}) or {}).get('voice_id'))
        self._refresh_voice_notice()

    def _network_voice_changed(self, engine):
        if engine == 'edge' and not self._ensure_network_voice_accepted():
            self._widgets['tts_engine'].setCurrentText('winrt')
        self._refresh_voice_notice()
        saved = self.app.config.get('tts', {}) or {}
        preferred = saved.get('voice_id') if self._widgets['tts_engine'].currentText() == saved.get('engine', 'winrt') else None
        self._refresh_tts_voices(preferred)

    def _refresh_voice_notice(self):
        network = self._widgets['tts_engine'].currentText() == 'edge'
        self._widgets['network_voice_quiet'].setVisible(network and self._network_voice_accepted)
        self._widgets['network_voice_existing'].setVisible(network and not self._network_voice_accepted)
