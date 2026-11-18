"""1262 fixed-operation adapter, imported only after installed_probe's guard."""
import os
import time

from receipt_probe import atomic, on_ui, perform as legacy_perform, state
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


def perform(app, home, operation):
    replay = app._receipt_replay
    if operation == 'state':
        return dict(**state(app), model_loaded=bool(getattr(app, 'model_loaded', False)),
                    desktop_ready=getattr(app, '_receipt_desktop_ready', False),
                    capture_ready=bool(getattr(getattr(app, '_ace_engine', None), '_running', False)))
    if operation == 'desktop_state':
        return on_ui(receipt_desktop.observe)
    if operation == 'mic_start':
        # Quiet phase + eight-second phrase window, through the installed ACE
        # stream/ring and the mic check's unmodified source/decoder/UI delivery.
        replay.queue(delay=1.7)
        return legacy_perform(app, home, operation)
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
        app.stop_continuous_mode()
        return dict(stopped=not app.continuous_active)
    if operation == 'reread_stage':
        from samsara.session_modes import SessionMode, UtteranceSignals
        manager = app._session_mode_manager
        if not manager.buffer_dictate_until_commit:
            raise RuntimeError('ManualDraftRequired')
        manager.force_mode(SessionMode.DICTATE)
        manager.dispatch_utterance('The receipt draft stays here.', UtteranceSignals(True, (), True))
        return dict(draft_present=bool(manager.draft_document.text))
    if operation == 'reread_state':
        def observe():
            from PySide6.QtWidgets import QApplication, QPushButton
            buttons = [b for w in QApplication.topLevelWidgets() for b in w.findChildren(QPushButton)
                       if b.accessibleName() == 'Re-read draft audio']
            return dict(reread_visible=any(b.isVisible() for b in buttons),
                        handsfree_draft=bool(app._session_mode_manager.draft_document.text))
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
    if operation in {'ticket_lookup', 'ticket', 'game_on', 'game_off', 'confirmation_policy',
                     'mic', 'mic_state', 'mic_close', 'quit'}:
        return legacy_perform(app, home, operation)
    raise ValueError('UnknownOperation')


def serve(app, home):
    import json
    channel = home / 'receipt_channel'
    channel.mkdir(exist_ok=True)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if getattr(app, 'command_executor', None) and getattr(app, 'tray_icon', None):
            break
        time.sleep(.1)
    atomic(channel / 'ready.json', dict(pid=os.getpid()))
    last = None
    while True:
        try:
            data = json.loads((channel / 'request.json').read_text(encoding='utf-8'))
            if set(data) != {'id', 'operation'} or data['id'] == last:
                time.sleep(.1)
                continue
            last = data['id']
            try:
                result = perform(app, home, data['operation'])
                reply = dict(id=last, ok=True, value=result)
            except Exception as error:
                reply = dict(id=last, ok=False, error=type(error).__name__)
            atomic(channel / 'response.json', reply)
            if data['operation'] == 'quit':
                return
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(.1)
