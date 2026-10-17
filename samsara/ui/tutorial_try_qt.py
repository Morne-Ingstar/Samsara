"""Tutorial trials: three unsaved steps, passive capture and bounded speech."""
from copy import deepcopy
from contextlib import nullcontext
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QLayout, QScrollArea

from samsara.log import get_logger
from samsara.speech_pace import profile
from samsara.tts import runtime as tts_runtime
from samsara.ui.tutorial_try_steps import build_steps
from samsara.ui.tutorial_try_preview import SampleSpeech

logger = get_logger(__name__)


class _TryPage(QScrollArea):
    """Unsaved choices and bounded previews; owns no microphone stream."""
    def __init__(self, window):
        super().__init__()
        self.app, self.window, window._try_page = window._app, window, self
        self.kept, self.accepted = {}, False
        self.reader = self.capture = self.preview = self.preview_handle = None
        self.voice_engine = None
        self.sample_engine = self.sample_key = None
        self._described_pace = None
        self.levels = []
        self.stage, self.played_level, self._voice_provider = 0, None, None
        self._pace_ready = False
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidgetResizable(True)
        content, layout = window._padded()
        layout.setContentsMargins(28, 8, 28, 8)
        layout.setSpacing(8)
        self.cards, self.states, self.actions, self.keeps = {}, {}, {}, {}
        build_steps(self, layout)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.setWidget(content)
        self.level.tried.connect(lambda: self.sample('level'))
        self.voice.currentIndexChanged.connect(self.network_choice)
        self.voice_id.currentIndexChanged.connect(lambda: self.sample('voice'))
        self.speed.sliderReleased.connect(lambda: self.sample('voice'))
        self.volume.sliderReleased.connect(lambda: self.sample('voice'))
        self.pace.currentIndexChanged.connect(self.pace_choice)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)
        self.refresh()

    def show_stage(self, index):
        self.finish_measure(cancel=True)
        self.stop_preview()
        self.stage = index
        self.result.setText('')
        self.refresh()
        if index == 1 and not self.voice_skipped():
            self.sample('voice')
        elif index == 2:
            from samsara.audio_engine.guide_capture import running_engine
            if running_engine(self.app) is None:
                self._pace_ready = True
                self.result.setText('The microphone is unavailable. You can still choose a pace.')
        self.refresh()
        self.focus_next()
        QTimer.singleShot(0, self, self.window._fit_done_page_height)

    def advance(self, delta):
        destination = self.stage + delta
        if not 0 <= destination < 3:
            return False
        self.show_stage(destination)
        return True

    def voice_skipped(self):
        return (self.played_level or (self.app.config.get('tts', {}) or {}).get('feedback_level', 'brief')) == 'silent'

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
    def focus_next(self):
        focused = self.window.focusWidget()
        reason = Qt.FocusReason.TabFocusReason if focused is not None and focused.property('keyboardFocus') else Qt.FocusReason.OtherFocusReason
        self.window._next_btn.setFocus(reason)
        if not (self.stage == 1 and self.voice_skipped()):
            first = self.level.buttons[0] if self.stage == 0 else (self.voice_id if self.stage == 1 else self.actions['pace'])
            self.setTabOrder(self.window._next_btn, first)

    def park_focus(self, control):
        focused = self.window.focusWidget()
        if focused is control or (focused is not None and control.isAncestorOf(focused)):
            self.focus_next()

    def enable(self, control, enabled):
        if not enabled:
            self.park_focus(control)
        control.setEnabled(enabled)

    def visible(self, control, visible):
        if not visible:
            self.park_focus(control)
        control.setVisible(visible)

    def refresh(self):
        cfg = self.app.config.get('tts', {}) or {}
        self.position.setText(f'{self.stage + 1} of 3')
        for index, key in enumerate(('level', 'voice', 'pace')):
            self.visible(self.cards[key], self.stage == index and not (key == 'voice' and self.voice_skipped()))
        self.voice_skip.setVisible(self.stage == 1 and self.voice_skipped())
        self.result.setWordWrap(self.result.text() != 'Example complete.')
        self.result.setVisible(bool(self.result.text()) and not self.result.text().startswith(('Speaking:', 'Suggested pace:')))
        if not self.states['level'].text():
            self.states['level'].setText('In use: ' + str(cfg.get('feedback_level', 'brief')).capitalize())
        self.states['pace'].setText(self.result.text() if self.stage == 2 and self.result.text().startswith('Suggested pace:')
                                   else 'In use: ' + profile(self.app.config).capitalize())
        engine = getattr(getattr(self.app, 'audio_coordinator', None), 'engine', None)
        provider = self.voice.currentData()
        if engine is not self.voice_engine or self._voice_provider is None:
            self.voice_engine = engine
            if provider == cfg.get('engine', 'winrt'):
                self.load_voices(engine, provider, cfg.get('voice_id'))
        own_speech = self.owns_preview()
        busy = self.busy(include_speech=not own_speech) or self.reader is not None
        voice_available = engine is not None or not cfg.get('enabled', True)
        if not voice_available:
            self.states['voice'].setText('Speech is unavailable.')
        for key in ('voice',):
            action = self.actions[key]
            action.setText('Samsara is busy' if busy else 'Hear it')
            action.setAccessibleName(action.text())
            self.enable(action, not busy and voice_available)
        for control in self.level.buttons:
            self.enable(control, not busy)
        for control in (self.voice, self.voice_id, self.speed, self.volume):
            self.enable(control, not busy and voice_available)
        self.enable(self.keeps['level'], self.played_level is not None and not busy)
        self.enable(self.keeps['voice'], voice_available and not busy and self.states['voice'].text() != 'Speech is unavailable.')
        pace_busy = self.busy(include_speech=False)
        action = self.actions['pace']
        self.enable(action, not pace_busy)
        action.setText('Samsara is busy' if pace_busy else ('Finish measuring' if self.reader is not None else 'Start reading'))
        action.setAccessibleName(action.text())
        self.visible(self.pace, self._pace_ready and self.reader is None)
        self.pace_description.setVisible(self._pace_ready and self.reader is None)
        self.pace_instruction.setVisible(not self._pace_ready or self.reader is not None)
        self.pace_sentence.setVisible(not self._pace_ready or self.reader is not None)
        self.enable(self.keeps['pace'], self._pace_ready and self.reader is None and not pace_busy)
        if self._described_pace != self.pace.currentData():
            self.describe_pace()

    def load_voices(self, engine, provider, current=None):
        self.voice_id.blockSignals(True)
        try:
            self.voice_id.clear()
            self.voice_id.addItem('Current voice', current)
            for voice in engine.list_voices() if engine else ():
                self.voice_id.addItem(voice.display_name, voice.voice_id)
            self._voice_provider = provider
        except Exception:
            logger.debug('Tutorial voice list unavailable', exc_info=True)
        finally:
            self.voice_id.blockSignals(False)

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
    def pace_choice(self):
        self._pace_ready = True
        self.describe_pace()
        self.refresh()
    def network_choice(self):
        self.stop_preview()
        self.result.setText('')
        if not self.consent(self.voice.currentData()):
            self.voice.blockSignals(True)
            self.voice.setCurrentIndex(self.voice.findData('winrt'))
            self.voice.blockSignals(False)
            self._voice_provider = None
            self.refresh()
            return
        self._voice_provider = None
        if self.voice.currentData() != (self.app.config.get('tts', {}) or {}).get('engine', 'winrt'):
            self.load_voices(None, self.voice.currentData())
        self.refresh()
        self.sample('voice')

    def network_allowed(self):
        from samsara.ui.network_voice_notice import ACCEPTANCE_KEY
        return self.accepted or bool((self.app.config.get('tts', {}) or {}).get(ACCEPTANCE_KEY.split('.')[-1]))

    def consent(self, provider):
        if provider == 'edge' and not self.network_allowed():
            from samsara.ui.network_voice_notice import request_acceptance
            self.accepted = request_acceptance(self.window)
            return self.accepted
        return True

    def sample(self, key):
        from samsara.tts.coordinator import _feedback_decision
        self.stop_preview()
        if self.busy():
            self.result.setText('Samsara is busy. Try again when dictation has finished.')
            return
        cfg = dict(self.app.config.get('tts', {}) or {})
        needs_speech = key == 'voice' or (key == 'level' and self.level.currentData() == 'full')
        provider = self.voice.currentData() if key == 'voice' else cfg.get('engine', 'winrt')
        # The notice must be outside the capture lock, followed by a fresh busy check.
        if needs_speech and not self.consent(provider):
            self.result.setText('Choose Network and read the notice before trying it.')
            return
        if needs_speech and not self.isVisible():
            return  # The page may have been left while the notice was open.
        if key == 'voice':
            cfg.update(engine=provider, voice_id=self.voice_id.currentData(), speed=self.speed.value()/100, volume=self.volume.value()/100)
        # Construction/shutdown may enumerate voices or join audio workers: no capture lock.
        if needs_speech:
            try:
                self.prepare_speech(cfg)
            except Exception:
                self.result.setText('Speech is unavailable.')
                self.states['voice'].setText('Speech is unavailable.')
                self.refresh()
                return
        with getattr(self.app, '_hold_capture_lifecycle_lock', None) or nullcontext():
            if self.busy() or (self.reader is not None and key != 'pace'):
                self.result.setText('Samsara is busy. Try again when dictation has finished.')
                return
            if key == 'pace':
                self.measure()
                return
            if key == 'level':
                text = 'Example complete.'
                self.result.setText(text)
                from samsara.live_surface.feedback import schedule_feedback
                if getattr(self.app, 'listening_indicator', None) is not None:
                    schedule_feedback(self.app, text, 'success', 1800, source='tutorial_sample')
                value = self.level.currentData()
                decision = _feedback_decision(value, 'general')
                if decision == 'action_complete':
                    self.app.play_sound(decision)
                if decision != 'speak':
                    self.played_level = value
                    self.describe_level()
                    self.refresh()
                    return
            else:
                text = 'This is how Samsara sounds at this speed and volume.'
            if provider == 'edge' and not self.network_allowed():
                return
            if self.speak(text, cfg):
                if key == 'level':
                    self.played_level = self.level.currentData()
                    self.describe_level()
                else:
                    name = self.voice_id.currentText()
                    if name == 'Current voice':
                        self.states['voice'].setText('You are hearing the current voice.')  # STAND-IN COPY (533)
                    else:
                        self.states['voice'].setText('You are hearing ' + name + '.')
            else:
                self.result.show()
            self.refresh()

    def describe_level(self):
        descriptions = {
            'silent': 'That was Silent: the result appeared on the listening display.',
            'brief': 'That was Brief: the result appeared on the listening display, with a short sound.',
            'full': 'That was Full: the result appeared on the listening display and was spoken aloud.',
        }
        self.states['level'].setText(descriptions[self.played_level])

    def use_live_speech(self, cfg):
        coordinator = getattr(self.app, 'audio_coordinator', None)
        saved = self.app.config.get('tts', {}) or {}
        return (coordinator is not None and coordinator.engine is not None and saved.get('enabled', True)
                and saved.get('feedback_level', 'brief') != 'silent'
                and cfg.get('engine', 'winrt') == saved.get('engine', 'winrt'))

    def prepare_speech(self, cfg):
        if self.use_live_speech(cfg):
            return
        key = (cfg.get('engine', 'winrt'), cfg.get('voice_id'))
        if (self.sample_engine is None or self.sample_key != key or self.sample_engine.closed
                or self.sample_engine.router is not getattr(self.app, 'audio_coordinator', None)):
            self.stop_preview(dispose=True)
            self.sample_engine = SampleSpeech(self, key[0])
            self.sample_key = key
        if self._voice_provider != key[0] or self.voice_id.count() == 1:
            self.load_voices(self.sample_engine.engine, key[0], key[1])

    def speak(self, text, cfg):
        coordinator = getattr(self.app, 'audio_coordinator', None)
        try:
            if self.busy():
                return False
            if self.use_live_speech(cfg):
                self.preview = coordinator
                self.preview_handle = coordinator.speak(text, voice_id=cfg.get('voice_id'), speed=cfg.get('speed', 1),
                                                        volume=cfg.get('volume', .8), category='readback')
            else:
                self.preview = self.sample_engine
                self.preview_handle = self.preview.speak(text, cfg)
                if self.preview_handle is None:
                    self.stop_preview()
                    return False
            self.result.setText('Speaking: ' + text)
            return True
        except Exception:
            logger.debug('Tutorial sample unavailable', exc_info=True)
            self.stop_preview()
            self.result.setText('Speech is unavailable.')
            self.states['voice'].setText('Speech is unavailable.')
            return False

    def keep(self, key):
        if key == 'level' and self.played_level is None:
            return
        if key == 'pace' and not self._pace_ready:
            return
        if key == 'voice' and ((self.voice.currentData() == 'edge' and not self.network_allowed())
                               or self.states['voice'].text() == 'Speech is unavailable.'):
            return
        self.stop_preview(dispose=True)
        if self.busy() or self.reader is not None:
            self.result.setText('Samsara is busy. Try again when dictation has finished.')
            return
        if key == 'level':
            self.app.set_feedback_level(self.played_level)
            summary = 'Kept: ' + self.played_level.capitalize()
        elif key == 'voice':
            with tts_runtime.tts_runtime_lock(self.app):
                cfg = self.app.config.setdefault('tts', {})
                replace_tts = cfg.get('engine', 'winrt') != self.voice.currentData()
                if cfg.get('engine', 'winrt') != self.voice.currentData():
                    cfg.pop('voice_id', None)
                if self.voice_id.currentData():
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
            self._pace_ready = True
            self.result.setText('The microphone is unavailable. You can still choose a pace.')
            self.refresh()
            return
        try:
            self.reader = self.capture.register_consumer('tutorial-try-389')
        except Exception:
            self.capture = None
            self._pace_ready = True
            self.result.setText('The microphone is unavailable. You can still choose a pace.')
            self.refresh()
            return
        self.levels = []
        self.deadline = time.monotonic() + 30
        self.actions['pace'].setText('Finish measuring')
        self.actions['pace'].setAccessibleName('Finish measuring')
        self.result.setText('Listening. Read the sentence; Samsara will stop when you finish.')
        self.refresh()
    def poll(self):
        from samsara.audio_engine.guide_capture import running_engine, block_rms
        from samsara.audio_engine.ring import EMPTY
        handle = self.preview_handle
        if self.stage == 1 and handle is not None:
            if (getattr(handle, '_state', None) == 'failed'
                    or getattr(handle, 'voice_result', None) == 'failed'):
                self.result.setText('The voice sample could not be spoken.')  # STAND-IN COPY (614)
                self.states['voice'].setText(self.result.text())
            elif getattr(handle, 'network_failed', False) is True:
                self.result.setText('The Network voice did not answer.')  # STAND-IN COPY (542)
                self.states['voice'].setText(self.result.text())
            elif (getattr(handle, 'network_bypassed', False) is True
                  or getattr(handle, 'voice_result', None) == 'windows'):
                self.result.setText('Network voice unavailable - using the Windows voice.')  # STAND-IN COPY (614; reused 542)
                self.states['voice'].setText(self.result.text())
        if (self.busy(include_speech=False) or
                (isinstance(self.preview, SampleSpeech) and not self.preview.authorized())):
            self.stop_preview()
        elif self.preview is not None and not self.preview.is_speaking:
            self.stop_preview()
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
            from samsara.audio_engine.frame import FRAME_MS
            voiced = next((i for i, value in enumerate(reversed(self.levels)) if value >= .02), None)
            # A four-second ending pause includes the slowest profile's interword gaps.
            if (voiced is not None and voiced * FRAME_MS >= 4000) or time.monotonic() >= self.deadline:
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
        self.actions['pace'].setText('Start reading')
        self.actions['pace'].setAccessibleName('Start reading')
        if cancel:
            return
        levels = levels[next((i for i, value in enumerate(levels) if value >= .02), len(levels)):]
        if not levels:
            self._pace_ready = True
            self.result.setText('No speech was measured. Try again or choose a pace.')
            self.refresh()
            return
        self.suggest(measured_interword_pauses(levels, 1000 // FRAME_MS))
    def suggest(self, pauses):
        from samsara.speech_pace import recommend_profile, pace_recommendation
        self._pace_ready = True
        self.pace.setCurrentIndex(self.pace.findData(recommend_profile(pauses)))
        self.result.setText(pace_recommendation(pauses))
        self.describe_pace()
        self.refresh()
    def owns_preview(self):
        return (self.preview is not None and self.preview_handle is not None
                and ((isinstance(self.preview, SampleSpeech) and
                      self.preview.authorized()) or
                     getattr(self.preview, '_last_spoken_handle', None) is self.preview_handle))

    def stop_preview(self, dispose=False):
        coordinator, handle = self.preview, self.preview_handle
        self.preview = self.preview_handle = None
        if isinstance(coordinator, SampleSpeech):
            # Edge cancellation is engine-wide: replay must not reset the flag
            # while an older utterance is still checking it between audio blocks.
            dispose = dispose or (coordinator.provider == 'edge' and coordinator.active)
            coordinator.cancel_speech()
        elif coordinator is not None and handle is not None and getattr(coordinator, '_last_spoken_handle', None) is handle:
            coordinator.cancel_speech()
        if dispose and self.sample_engine is not None:
            sample, self.sample_engine = self.sample_engine, None
            self.sample_key = None
            sample.close()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()
        if self.stage == 1 and not self.voice_skipped():
            self.sample('voice')
        self.focus_next()

    def hideEvent(self, event):
        self.timer.stop()
        self.finish_measure(cancel=True)
        self.stop_preview(dispose=True)
        super().hideEvent(event)
