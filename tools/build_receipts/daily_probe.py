"""1262 fixed-operation adapter, imported only after installed_probe's guard."""
import json
import os

from receipt_probe import on_ui, perform as legacy_perform, serve as serve_mailbox, state
import receipt_replay
import receipt_desktop


def start(app, home):
    from PySide6.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(home / 'qt'))
    import sounddevice as sd
    replay = receipt_replay.Replay(receipt_replay.read_take(os.environ['SAMSARA_RECEIPT_TAKE']))
    receipt_replay.install(sd, replay)
    app._receipt_replay = replay
    # Register before the first UI startup; installs on the actual Qt thread.
    from samsara.ui import qt_runtime
    def guard():
        try:
            app._receipt_desktop = receipt_desktop.install()
            app._receipt_desktop_ready = True
        except Exception:
            app._receipt_desktop_ready = False
    qt_runtime.post_when_ready(guard)
    from samsara.runtime import thread_registry
    thread_registry.spawn('build-receipts-1262', lambda: serve(app, home), daemon=True)


class MicCheckReplay:
    METRICS = ('verdict', 'speech_rms', 'speech_dbfs', 'noise_dbfs', 'snr_db', 'noise_level',
               'full_scale_frames', 'duration_s', 'rms', 'rms_dbfs', 'peak', 'clipped_fraction')

    def __init__(self, window, replay, home, *, difference):
        from PySide6.QtCore import Qt
        from samsara import mic_check
        self.window, self.replay, self.difference = window, replay, difference
        self.expected = mic_check.SENTENCE.rsplit(' ', 1)[0] + ' cat.' if difference else mic_check.SENTENCE
        self.phases = []
        self.samples = 0
        self.quiet_dbfs = None
        self.path = home / 'diagnostics' / 'mic_check.json'
        try:
            self.previous_save = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self.previous_save = None
        self.original_transcriber = window.transcriber
        window.transcriber = self.transcribe
        self.connection = window.progress.connect(self.progress, Qt.ConnectionType.DirectConnection)
        replay.silence()

    def transcribe(self, audio):
        # Only the decoded wording is controlled. The installed check captures,
        # measures, saves and renders the recorded stream itself.
        self.samples = len(audio)
        return self.expected, None

    def progress(self, kind, value):
        if kind in ('quiet', 'noise', 'speak', 'repeat_pause', 'repeat_speak', 'decoding', 'result', 'error'):
            self.phases.append(kind)
        if kind == 'noise':
            self.quiet_dbfs = float(value)
        if kind in ('speak', 'repeat_speak'):
            # Run on the capture thread at the exact quiet-to-speech boundary.
            self.replay.queue()
        elif kind in ('repeat_pause', 'decoding', 'result', 'error'):
            self.replay.silence()

    def stop(self):
        self.replay.silence()
        self.window.progress.disconnect(self.connection)
        self.window.transcriber = self.original_transcriber

    def observe(self):
        from samsara import mic_check
        window = self.window
        if window.busy:
            return dict(busy=True)
        take = window.take
        if take is None:
            return dict(busy=False, phases=self.phases)
        measured = take.measurements
        metrics = {key: measured[key] for key in self.METRICS}
        saved = False
        if take.saved is True:
            try:
                recorded = json.loads(self.path.read_text(encoding='utf-8'))
                saved = (self.path.stat().st_mtime_ns != self.previous_save
                         and recorded.get('schema') == 1
                         and set(recorded) == set(metrics) | {'schema', 'microphone_name', 'time'}
                         and all(recorded[key] == value for key, value in metrics.items()))
            except (FileNotFoundError, ValueError):
                pass
        rows = [label.text() for label, _, _ in window.rows]
        transcript_visible = window.heard.isVisible() and window.heard_title.isVisible()
        if self.difference:
            transcript_ok = transcript_visible and window.heard.text() == f'“{self.expected}”'
        else:
            transcript_ok = (not window.heard.isVisible() and not window.heard_title.isVisible()
                             and not window.heard.text())
        return dict(busy=False, phases=self.phases, phrase_samples=self.samples, quiet_dbfs=self.quiet_dbfs,
                    measurements=metrics, checklist_rows=rows, transcript_visible=transcript_visible,
                    capture_verified=(self.phases[:4] == ['quiet', 'noise', 'speak', 'decoding']
                                      and self.phases[-1] == 'result' and self.quiet_dbfs is not None
                                      and self.quiet_dbfs <= -90 and metrics['speech_rms'] > 0
                                      and self.samples == mic_check.PHRASE_S * mic_check.SAMPLE_RATE
                                      and metrics['duration_s'] == mic_check.PHRASE_S),
                    metrics_saved=saved, transcript_visibility_verified=transcript_ok,
                    checklist_visible=(window.results.isVisible() and rows == list(mic_check.ROWS)
                                       and not window.capture_panel.isVisible()
                                       and window.play_button.isVisible() and window.details_button.isVisible()
                                       and all(widget.isVisible() and widget.text()
                                               for row in window.rows for widget in row)))


def perform(app, home, operation):
    replay = app._receipt_replay
    if operation == 'state':
        game = getattr(app, '_game_mode', None)
        return dict(**state(app), model_loaded=bool(getattr(app, 'model_loaded', False)),
                    desktop_ready=getattr(app, '_receipt_desktop_ready', False),
                    capture_ready=bool(getattr(getattr(app, '_ace_engine', None), '_running', False)),
                    manual_game_fixture=bool(game and not game.automatic
                                             and app.config.get('game_mode', {}).get('detect') == 'manual'))
    if operation == 'desktop_state':
        return on_ui(receipt_desktop.observe)
    if operation in {'mic_match', 'mic_difference'}:
        def start_check():
            window = app._mic_check_window
            previous = getattr(app, '_receipt_mic_case', None)
            if previous:
                previous.stop()
            app._receipt_mic_case = MicCheckReplay(window, replay, home, difference=operation == 'mic_difference')
            window.start_button.click()
            return dict(started=window.busy)
        return on_ui(start_check)
    if operation == 'mic_check_state':
        return on_ui(lambda: app._receipt_mic_case.observe())
    if operation == 'mic_check_close':
        def close_check():
            window = getattr(app, '_mic_check_window', None)
            if window is not None:
                window.close()
            case = getattr(app, '_receipt_mic_case', None)
            if case is not None:
                case.stop()
                app._receipt_mic_case = None
            return dict(closed=True)
        return on_ui(close_check)
    if operation in {'pace_set', 'pace_read'}:
        on_ui(app.open_settings)
        def pace():
            from PySide6.QtWidgets import QSlider
            window = app._settings_qt._window
            window.show_tab('Dictation')
            sliders = [s for s in window.findChildren(QSlider)
                       if s.accessibleName() == 'How long you can pause']
            if len(sliders) != 1:
                raise RuntimeError('PaceControlUnavailable')
            slider = sliders[0]
            if operation == 'pace_set':
                slider.setValue(500)
                changed = slider.value() == 500
                window._apply_button.click()
                return dict(slider_changed=changed)
            return dict(slider_reopened_at_five_seconds=slider.value() == 500,
                        persisted_after_normal_restart=state(app)['pause'] == 5)
        return on_ui(pace)
    if operation == 'silence_start':
        replay.silence()
        app.start_continuous_mode()
        return dict(listening=bool(app.continuous_active))
    if operation == 'silence_state':
        return on_ui(lambda: dict(notice_visible=bool(
            getattr(app, '_mic_silence_notice_1252', None)
            and app._mic_silence_notice_1252.isVisible()),
            notice_title_correct=bool(getattr(app, '_mic_silence_notice_1252', None)
            and app._mic_silence_notice_1252.windowTitle() == 'No sound from your microphone')))
    if operation == 'continuous_stop':
        try:
            app.stop_continuous_mode()
        finally:
            if getattr(app, '_receipt_reread_manager', None) is not None:
                def clear_draft():
                    manager = app._receipt_reread_manager
                    manager.reset()
                    app.listening_indicator.set_session_mode(None)
                    app.live_surface.sync_draft(manager)
                    app._receipt_reread_manager = None
                on_ui(clear_draft)
        return dict(stopped=not app.continuous_active)
    if operation == 'reread_stage':
        from samsara.session_modes import SessionMode, UtteranceSignals

        def stage():
            manager = app._ensure_session_mode_manager()
            send_mode = (app.config.get('hands_free') or {}).get('send_mode', 'staged')
            if not manager.buffer_dictate_until_commit or send_mode != 'staged':
                raise RuntimeError('ManualDraftRequired')
            if manager.draft_document.text or manager.retained_draft:
                raise RuntimeError('EmptyDraftRequired')
            # Toggle entry uses this same reset. The silence fixture already
            # owns capture, so do not start a second microphone consumer.
            app._receipt_reread_manager = manager
            manager.reset(initial_mode=SessionMode.DICTATE)
            app.listening_indicator.set_session_mode('HANDS FREE')
            # Skip foreground-based verbatim detection for this fixture. The
            # short fragment also bypasses the single-line editor probe.
            previous_verbatim = app.config.get('verbatim')
            app.config['verbatim'] = {'enabled': False}
            try:
                outcome = manager.dispatch_utterance('Synthetic receipt draft', UtteranceSignals(True, (), True))
            finally:
                if previous_verbatim is None:
                    app.config.pop('verbatim')
                else:
                    app.config['verbatim'] = previous_verbatim
            app.live_surface.sync_draft(manager)
            return dict(draft_present=outcome.kind == 'dictate_staged' and bool(manager.draft_document.text))

        return on_ui(stage)
    if operation == 'reread_state':
        def observe():
            from PySide6.QtWidgets import QPushButton
            widget = app.live_surface.widget
            buttons = widget.findChildren(QPushButton) if widget is not None else ()
            manager = getattr(app, '_receipt_reread_manager', None)
            return dict(reread_visible=any(b.accessibleName() == 'Re-read draft audio' and b.isVisible()
                                           for b in buttons),
                        handsfree_draft=bool(manager and manager.draft_document.text))
        return on_ui(observe)
    if operation == 'mouse_down':
        replay.silence()
        # Behaviour below the global hook boundary. No SendInput: it would also
        # reach the owner's live hook. Independent of the helper module/class.
        app._on_main_hotkey_mouse(True)
        return dict(recording=bool(app.recording))
    if operation == 'mouse_up':
        app._on_main_hotkey_mouse(False)
        return dict(release_dispatched=True)
    if operation == 'mouse_state':
        return dict(recording=bool(app.recording), stopping=bool(getattr(app, '_stop_in_flight', False)))
    if operation == 'help':
        # Observe the real coordinator call, forward unchanged, return booleans
        # only. This is text-command admission; not recognition/audible proof.
        coordinator = app.audio_coordinator
        original = coordinator.speak
        observed = []
        def speak(text, *args, **kwargs):
            observed.append(text == 'A phrase that tells Samsara to listen for a command without holding a key.')
            return original(text, *args, **kwargs)
        coordinator.speak = speak
        try:
            result = app.command_executor.process_text('What is a wake word?', app, force_commands=True)
            return dict(command_completed=result.state.value == 'completed',
                        correct_answer_to_speech=observed == [True])
        finally:
            coordinator.speak = original
    if operation in {'ticket_lookup', 'ticket_lookup_diagnostic', 'ticket', 'game_on', 'game_off',
                     'confirmation_policy', 'mic', 'quit'}:
        return legacy_perform(app, home, operation)
    raise ValueError('UnknownOperation')


def serve(app, home):
    serve_mailbox(app, home, handler=perform)
