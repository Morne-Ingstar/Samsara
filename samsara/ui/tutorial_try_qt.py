"""Tutorial settings trials using only the app's live speech and microphone feed."""
from copy import deepcopy
from contextlib import nullcontext
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QPushButton, QScrollArea, QSlider

from samsara.log import get_logger
from samsara.speech_pace import profile
from samsara.tts import runtime as tts_runtime
from samsara.ui import theme
from samsara.ui.settings.calm_layout import stylesheet as settings_control_style
from samsara.ui.settings.audio_page import BASE as audio_rows

logger = get_logger(__name__)


class _TryButton(QPushButton):
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


class _TryPage(QScrollArea):
    """Unsaved choices and bounded previews; owns no microphone stream."""
    def __init__(self, window):
        super().__init__()
        self.app, self.window, window._try_page = window._app, window, self
        self.kept, self.accepted = {}, False
        self.reader = self.capture = self.preview = self.preview_handle = None
        self.voice_engine = None
        self._described_pace = None
        self.levels = []
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidgetResizable(True)
        content, layout = window._padded()
        layout.addWidget(self.label('Nothing is saved until you press Keep this.'))
        self.cards, self.states, self.actions, self.keeps = {}, {}, {}, {}
        cfg = self.app.config.get('tts', {}) or {}
        self.level = self.choices('How much Samsara talks', [('Silent', 'silent'), ('Brief', 'brief'), ('Full', 'full')], cfg.get('feedback_level', 'brief'))
        self.voice = self.choices(next(row[0] for row in audio_rows if row[2] == 'tts_engine'), [('Local', 'winrt'), ('Network', 'edge')], cfg.get('engine', 'winrt'))
        self.pace = self.choices('Your pace', [('Standard', 'standard'), ('Relaxed', 'relaxed'), ('Unhurried', 'unhurried')], profile(self.app.config))
        for key, title, description, choice, action in (
            ('level', 'How much Samsara talks', 'Try an on-screen result, a short sound or a spoken result.', self.level, 'Play an example'),
            ('voice', 'The voice', 'Try a voice, speaking speed and volume.', self.voice, 'Hear it'),
            ('pace', 'Your pace', 'Read this sentence aloud at your normal pace.', self.pace, 'Measure'),
        ):
            frame = window._instruction_box(title)
            frame.setAccessibleName(title)
            title_label = frame.layout().itemAt(0).widget()
            title_label.setStyleSheet(f'color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;font-weight:600;')
            frame.layout().addWidget(self.label(description))
            if key == 'voice':
                self.voice_id = self.choices('Voice', [], None)
                tuning_grid = QGridLayout()
                tuning_grid.setHorizontalSpacing(theme.SPACE_3)
                tuning_grid.setVerticalSpacing(theme.SPACE_2)
                self.voice_labels = {}
                for line, (name, control) in enumerate((('Voice', self.voice_id),
                                                       (self.voice.accessibleName(), self.voice))):
                    label = self.label(name)
                    label.setBuddy(control)
                    self.voice_labels[name] = label
                    tuning_grid.addWidget(label, line, 0)
                    tuning_grid.addWidget(control, line, 1, Qt.AlignmentFlag.AlignLeft)
                tuning_grid.setColumnStretch(3, 1)
                frame.layout().addLayout(tuning_grid)
                for name, title, low, high, value in (('speed', 'Speaking speed', 50, 200, cfg.get('speed', 1)*100),
                                                       ('volume', 'Volume', 0, 100, cfg.get('volume', .8)*100)):
                    label, slider = self.label(title), QSlider(Qt.Orientation.Horizontal)
                    slider.setAccessibleName(title)
                    slider.setRange(low, high)
                    slider.setValue(round(float(value)))
                    slider.setFixedWidth(140)
                    slider.setFixedHeight(theme.HIT_TARGET_MIN)
                    slider.setStyleSheet(settings_control_style())
                    value_label = self.label('')
                    format_value = (lambda v: f'{v/100:g}×') if name == 'speed' else (lambda v: f'{v}%')
                    slider.valueChanged.connect(lambda v, l=value_label, f=format_value: l.setText(f(v)))
                    value_label.setText(format_value(slider.value()))
                    label.setBuddy(slider)
                    self.voice_labels[title] = label
                    line = 2 if name == 'speed' else 3
                    tuning_grid.addWidget(label, line, 0)
                    tuning_grid.addWidget(slider, line, 1, Qt.AlignmentFlag.AlignLeft)
                    tuning_grid.addWidget(value_label, line, 2)
                    setattr(self, name, slider)
            if key == 'pace':
                frame.layout().addWidget(self.label('I would like enough time to finish what I am saying.'))
                self.pace_description = self.label('')
                frame.layout().addWidget(self.pace_description)
            row = QHBoxLayout()
            if key != 'voice':
                row.addWidget(choice)
            button = self.button(action, lambda checked=False, name=key: self.sample(name))
            self.actions[key] = button
            row.addWidget(button)
            self.keeps[key] = self.button('Keep this', lambda checked=False, name=key: self.keep(name))
            row.addWidget(self.keeps[key])
            row.addStretch()
            frame.layout().addLayout(row)
            state = self.label('')
            frame.layout().addWidget(state)
            self.states[key] = state
            self.cards[key] = frame
            layout.addWidget(frame)
        self.result = self.label('')
        layout.addWidget(self.result)
        layout.addStretch()
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.setWidget(content)
        self.level.currentIndexChanged.connect(self.refresh)
        self.voice.currentIndexChanged.connect(self.network_choice)
        self.pace.currentIndexChanged.connect(self.describe_pace)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)
        self.refresh()
    @staticmethod
    def label(text):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setStyleSheet(f'color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;')
        return label
    @staticmethod
    def button(text, callback):
        button = _TryButton(text)
        button.setAccessibleName(text)
        theme.make_secondary(button)
        button.setFixedHeight(theme.HIT_TARGET_MIN)
        button.setStyleSheet(button.styleSheet() + f'QPushButton{{min-height:{theme.HIT_TARGET_MIN - 2}px;padding:0 {theme.SPACE_2}px;}}')
        button.clicked.connect(callback)
        return button
    @staticmethod
    def choices(name, rows, current):
        box = QComboBox()
        box.setFixedHeight(theme.HIT_TARGET_MIN)
        box.setStyleSheet(f'QComboBox{{min-height:{theme.HIT_TARGET_MIN - 2}px;padding:0 {theme.SPACE_2}px;}}')
        box.setAccessibleName(name)
        for label, value in rows:
            box.addItem(label, value)
        box.setCurrentIndex(max(0, box.findData(current)))
        return box
    def busy(self, include_speech=True):
        app = self.app
        flags = ('recording', 'command_mode_recording', 'ava_mode_recording', '_hotkey_recording',
                 '_memo_recording', '_stop_in_flight', '_ace_dictation_active', '_ace_streaming_active',
                 '_hold_park_finalizing', 'continuous_active', 'command_mode_active', 'ava_mode_active')
        owners = getattr(app, '_transcription_owners', None)
        return (any(bool(getattr(app, flag, False)) for flag in flags)
                or (include_speech and (bool(getattr(app, 'is_speaking', False))
                    or bool(getattr(getattr(app, 'audio_coordinator', None), 'is_speaking', False))))
                or (owners is not None and any(owners.current(lane) for lane in ('hold', 'wake', 'toggle', 'ava'))))
    def refresh(self):
        cfg = self.app.config.get('tts', {}) or {}
        level = cfg.get('feedback_level', 'brief')
        self.states['level'].setText('In use: ' + str(level).capitalize())
        if self.level.currentData() == 'full' and level != 'full':
            self.states['level'].setText(self.states['level'].text() + '\nKeep this to hear it spoken.')
        self.states['voice'].setText(f'In use: {"Network" if cfg.get("engine") == "edge" else "Local"}, {cfg.get("speed", 1):g}\u00d7, {round(cfg.get("volume", .8)*100)}%')
        self.states['pace'].setText('In use: ' + profile(self.app.config).capitalize())
        self.cards['voice'].setVisible(level != 'silent')
        engine = getattr(getattr(self.app, 'audio_coordinator', None), 'engine', None)
        if engine is not self.voice_engine:
            self.voice_engine = engine
            self.voice_id.clear()
            self.voice_id.addItem('Current voice', cfg.get('voice_id'))
            try:
                for voice in engine.list_voices() if engine else ():
                    self.voice_id.addItem(voice.display_name, voice.voice_id)
            except Exception:
                logger.debug('Tutorial voice list unavailable', exc_info=True)
        same_provider = self.voice.currentData() == cfg.get('engine', 'winrt')
        for control in (self.voice, self.speed, self.volume, self.keeps['voice']):
            control.setEnabled(engine is not None)
        self.voice_id.setEnabled(engine is not None and same_provider)
        if engine is None:
            self.states['voice'].setText('Speech is unavailable.')
        elif not same_provider:
            self.states['voice'].setText(self.states['voice'].text() + '\nKeep this to hear the other voice.')
        for key in ('level', 'voice'):
            own_speech = (self.preview is getattr(self.app, 'audio_coordinator', None) and self.preview_handle is not None
                          and getattr(self.preview, '_last_spoken_handle', None) is self.preview_handle)
            button, busy = self.actions[key], self.busy(include_speech=not own_speech) or self.reader is not None
            button.setText('Samsara is busy' if busy else ('Play an example' if key == 'level' else 'Hear it'))
            button.setAccessibleName(button.text())
            button.setEnabled(not busy and (key != 'voice' or (engine is not None and same_provider)))
        self.actions['pace'].setEnabled(not self.busy(include_speech=False))
        self.actions['pace'].setText('Samsara is busy' if self.busy(include_speech=False) else ('Finish measuring' if self.reader is not None else 'Measure'))
        self.actions['pace'].setAccessibleName(self.actions['pace'].text())
        if self._described_pace != self.pace.currentData():
            self.describe_pace()
    def describe_pace(self):
        from samsara.constants import DEFAULT_SILENCE_TIMEOUT
        from samsara.speech_pace import effective_value, clear_gap_s
        with getattr(self.app, '_config_lock', None) or nullcontext():
            cfg = deepcopy(self.app.config)
        self._described_pace = self.pace.currentData()
        cfg.setdefault('accessibility', {})['speech_pace'] = self.pace.currentData()
        silence = effective_value(cfg, 'silence_threshold', DEFAULT_SILENCE_TIMEOUT)
        gap = clear_gap_s(cfg)
        self.pace_description.setText(f'Continuous dictation waits {silence:g} seconds after speech.' +
                                     (f' Commands need a clear gap of {gap:g} seconds.' if gap else ''))
    def network_choice(self):
        self.stop_preview()
        self.result.setText('')
        from samsara.ui.network_voice_notice import request_acceptance
        if self.voice.currentData() == 'edge' and not self.network_allowed():
            self.accepted = request_acceptance(self.window)
            if not self.accepted:
                self.voice.setCurrentIndex(self.voice.findData('winrt'))
        self.refresh()
    def network_allowed(self):
        from samsara.ui.network_voice_notice import ACCEPTANCE_KEY
        return self.accepted or bool((self.app.config.get('tts', {}) or {}).get(ACCEPTANCE_KEY.split('.')[-1]))
    def sample(self, key):
        from samsara.tts.coordinator import _feedback_decision
        self.stop_preview()
        if self.busy():
            self.result.setText('Samsara is busy. Try again when dictation has finished.')
            return
        cfg = self.app.config.get('tts', {}) or {}
        needs_speech = key == 'voice' or (key == 'level' and self.level.currentData() == cfg.get('feedback_level') == 'full')
        if needs_speech and cfg.get('engine') == 'edge' and not self.network_allowed():
            self.network_choice()
            if not self.network_allowed():
                self.result.setText('Choose Network and read the notice before trying it.')
                return
        with getattr(self.app, '_hold_capture_lifecycle_lock', None) or nullcontext():
            if self.busy() or (self.reader is not None and key != 'pace'):
                self.result.setText('Samsara is busy. Try again when dictation has finished.')
                return
            if key == 'pace':
                self.measure()
                return
            self.stop_preview()
            if key == 'level':
                text = 'Example complete.'
                self.result.setText(text)
                indicator = getattr(self.app, 'listening_indicator', None)
                if indicator is not None:
                    from samsara.live_surface.feedback import schedule_feedback
                    schedule_feedback(self.app, text, 'success', 1800, source='tutorial_sample')
                decision = _feedback_decision(self.level.currentData(), 'general')
                if decision == 'skip':
                    return
                if decision == 'action_complete':
                    self.app.play_sound(decision)
                    return
            else:
                if getattr(getattr(self.app, 'audio_coordinator', None), 'engine', None) is None:
                    self.result.setText('Speech is unavailable.')
                    return
                text = 'This is how Samsara sounds at this speed and volume.'
            cfg = dict(self.app.config.get('tts', {}) or {})
            if key == 'level' and cfg.get('feedback_level', 'brief') != 'full':
                self.refresh()
                return
            if key == 'voice':
                if self.voice.currentData() != cfg.get('engine', 'winrt'):
                    self.refresh()
                    return
                cfg.update(voice_id=self.voice_id.currentData(), speed=self.speed.value()/100, volume=self.volume.value()/100)
            if cfg.get('engine') == 'edge' and not self.network_allowed():
                return  # Consent may change while waiting for capture; never open a modal under its lock.
            self.speak(text, cfg)
    def speak(self, text, cfg):
        coordinator = getattr(self.app, 'audio_coordinator', None)
        if coordinator is None or coordinator.engine is None:
            self.result.setText('Speech is unavailable.')
            return
        try:
            if not self.busy():
                self.preview = coordinator
                self.preview_handle = coordinator.speak(text, voice_id=cfg.get('voice_id'), speed=cfg.get('speed', 1),
                                                       volume=cfg.get('volume', .8), category='readback')
                self.result.setText('Speaking: ' + text)
        except Exception:
            self.stop_preview()
            self.result.setText('Speech is unavailable.')
    def keep(self, key):
        if key == 'voice' and (getattr(getattr(self.app, 'audio_coordinator', None), 'engine', None) is None
                               or (self.voice.currentData() == 'edge' and not self.network_allowed())):
            return
        self.stop_preview()
        if self.busy() or self.reader is not None:
            self.result.setText('Samsara is busy. Try again when dictation has finished.')
            return
        if key == 'level':
            self.app.set_feedback_level(self.level.currentData())
            summary = 'Feedback kept: ' + self.level.currentText()
        elif key == 'voice':
            with tts_runtime.tts_runtime_lock(self.app):
                cfg = self.app.config.setdefault('tts', {})
                replace_tts = cfg.get('engine', 'winrt') != self.voice.currentData()
                if cfg.get('engine', 'winrt') != self.voice.currentData():
                    cfg.pop('voice_id', None)
                elif self.voice_id.currentData():
                    cfg['voice_id'] = self.voice_id.currentData()
                cfg.update(engine=self.voice.currentData(), speed=self.speed.value()/100, volume=self.volume.value()/100)
                if cfg['engine'] == 'edge':
                    cfg['network_voice_accepted'] = True
                if replace_tts:
                    tts_runtime.rebuild_tts_runtime(self.app, cfg)
                self.app.save_config()
            summary = f'Voice kept: {self.voice.currentText()}, {self.speed.value()/100:g}\u00d7, {self.volume.value()}%'
        else:
            self.app.update_config_and_save({'accessibility': dict(self.app.config.get('accessibility', {}) or {}, speech_pace=self.pace.currentData())})
            summary = 'Pace kept: ' + self.pace.currentText()
        self.kept[key] = summary
        self.result.setText(summary)
        self.refresh()
    def measure(self):
        from samsara.audio_engine.guide_capture import running_engine
        if self.reader is not None:
            self.finish_measure()
            return
        self.stop_preview()
        self.capture = running_engine(self.app)
        if self.capture is None:
            self.result.setText('The microphone is unavailable. You can still choose a pace.')
            return
        try:
            self.reader = self.capture.register_consumer('tutorial-try-389')
        except Exception:
            self.capture = None
            self.result.setText('The microphone is unavailable. You can still choose a pace.')
            return
        self.levels = []
        self.deadline = time.monotonic() + 30
        self.actions['pace'].setText('Finish measuring')
        self.actions['pace'].setAccessibleName('Finish measuring')
        self.result.setText('Listening. Read the sentence, then press Finish measuring.')
    def poll(self):
        from samsara.audio_engine.guide_capture import running_engine, block_rms
        from samsara.audio_engine.ring import EMPTY
        if self.busy(include_speech=False):
            self.stop_preview()
        elif self.preview is not None and not self.preview.is_speaking:
            self.preview = self.preview_handle = None
        self.refresh()
        if self.reader is None:
            return
        if self.busy(include_speech=False) or running_engine(self.app) is not self.capture:
            self.finish_measure(cancel=True)
            self.result.setText('Measurement stopped. Try again when the microphone is ready.')
            return
        try:
            for _ in range(20):
                frame = self.reader.read_next()
                if frame is EMPTY:
                    break
                self.levels.append(block_rms(frame.pcm.astype('float32') / 32767))
            if time.monotonic() >= self.deadline:
                self.finish_measure()
        except Exception:
            self.finish_measure(cancel=True)
            self.result.setText('The microphone is unavailable. You can still choose a pace.')
    def finish_measure(self, cancel=False):
        from samsara.audio_engine.frame import FRAME_MS
        from samsara.speech_pace import measured_interword_pauses
        reader, self.reader = self.reader, None
        if reader is not None:
            try:
                self.capture.unregister_consumer(reader)
            except Exception:
                logger.debug('Tutorial pace reader was invalidated', exc_info=True)
        self.capture = None
        levels, self.levels = self.levels, []
        self.actions['pace'].setText('Measure')
        self.actions['pace'].setAccessibleName('Measure')
        if cancel:
            return
        levels = levels[next((i for i, value in enumerate(levels) if value >= .02), len(levels)):]
        if not levels:
            self.result.setText('No speech was measured. Try again or choose a pace.')
            return
        self.suggest(measured_interword_pauses(levels, 1000 // FRAME_MS))
    def suggest(self, pauses):
        from samsara.speech_pace import recommend_profile
        self.pace.setCurrentIndex(self.pace.findData(recommend_profile(pauses)))
        self.result.setText('Suggested pace: ' + self.pace.currentText() + '. Nothing has been saved.')
    def stop_preview(self):
        coordinator, handle = self.preview, self.preview_handle
        self.preview = self.preview_handle = None
        if coordinator is not None and handle is not None and getattr(coordinator, '_last_spoken_handle', None) is handle:
            coordinator.cancel_speech()
    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()
    def hideEvent(self, event):
        self.timer.stop()
        self.finish_measure(cancel=True)
        self.stop_preview()
        super().hideEvent(event)


