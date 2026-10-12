"""533: actual tutorial navigation, immediate trials and unchanged save safety."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QWidget

from tests.test_tutorial_try_389 import build
from samsara.audio_engine.frame import FRAME_SIZE, FRAME_MS
from samsara.audio_engine.ring import EMPTY
from samsara.tts.coordinator import AudioCoordinator
from samsara.tts.engine_base import SpeechHandle
from samsara.ui import network_voice_notice, tutorial_try_preview


def test_footer_walks_every_step_in_both_directions_without_saving(build):
    window, page, app = build()
    before = deepcopy(app.config)
    for stage in range(3):
        assert page.stage == stage
        assert page.position.text() == f'{stage + 1} of 3'
        assert [key for key, frame in page.cards.items() if frame.isVisible()] == [('level', 'voice', 'pace')[stage]]
        if stage != 2:
            window._go_next()
    window._go_back()
    assert page.stage == 1
    window._go_back()
    assert page.stage == 0
    window._go_next(); window._go_next(); window._go_next()
    assert window._steps[window._step][0] == 'done'
    assert app.config == before
    app.save_config.assert_not_called()
    app.update_config_and_save.assert_not_called()
    app.set_feedback_level.assert_not_called()


@pytest.mark.parametrize('saved', ['silent', 'brief', 'full'])
def test_each_choice_replays_without_saving_and_full_is_always_spoken(build, saved):
    _, page, app = build(level=saved)
    before = deepcopy(app.config)
    for index, level in enumerate(('silent', 'brief', 'full')):
        for _ in range(2):
            page.level.buttons[index].click()
            assert page.played_level == level
            assert level.capitalize() in page.states['level'].text()
    assert app.play_sound.call_count == 2
    if saved == 'silent':
        assert len(app.sample_engines) == 1
        assert app.sample_engines[0].speak.call_count == 2
        assert all(engine.speak.called for engine in app.sample_engines)
    else:
        assert app.audio_coordinator.speak.call_count == 2
    assert app.config == before
    app.save_config.assert_not_called()
    app.set_feedback_level.assert_not_called()


def test_keep_uses_last_successful_trial_and_initial_keep_changes_nothing(build):
    _, page, app = build()
    page.keep('level')
    app.set_feedback_level.assert_not_called()
    page.level.buttons[1].click()
    app.recording = True
    page.level.setCurrentIndex(2)
    page.sample('level')
    app.recording = False
    page.keep('level')
    app.set_feedback_level.assert_called_once_with('brief')


def test_voice_opens_speaking_and_selection_and_slider_release_replay(build):
    window, page, app = build()
    before = deepcopy(app.config)
    window._go_next()
    app.audio_coordinator.speak.assert_called_once()
    page.voice_id.setCurrentIndex(page.voice_id.findData('voice-b'))
    assert app.audio_coordinator.speak.call_count == 2
    page.speed.setValue(130)
    page.volume.setValue(60)
    assert app.audio_coordinator.speak.call_count == 2
    page.speed.sliderReleased.emit()
    page.volume.sliderReleased.emit()
    assert app.audio_coordinator.speak.call_count == 4
    assert app.audio_coordinator.speak.call_args.kwargs['voice_id'] == 'voice-b'
    assert app.audio_coordinator.speak.call_args.kwargs['speed'] == 1.3
    assert app.audio_coordinator.speak.call_args.kwargs['volume'] == .6
    assert app.config == before


def test_keyboard_slider_release_also_replays_without_saving(build):
    window, page, app = build()
    window._go_next()
    before = deepcopy(app.config)
    app.audio_coordinator.speak.reset_mock()
    page.speed.setFocus()
    QTest.keyPress(page.speed, Qt.Key.Key_Right)
    app.audio_coordinator.speak.assert_not_called()
    QTest.keyRelease(page.speed, Qt.Key.Key_Right)
    app.audio_coordinator.speak.assert_called_once()
    assert app.config == before


def test_silent_skip_uses_trial_without_saving_and_back_can_change_it(build):
    window, page, app = build()
    page.level.buttons[0].click()
    window._go_next()
    assert page.stage == 1 and page.voice_skip.isVisible()
    assert not any(frame.isVisible() for frame in page.cards.values())
    app.audio_coordinator.speak.assert_not_called()
    window._go_back()
    page.level.buttons[2].click()
    window._go_next()
    assert page.cards['voice'].isVisible()
    assert app.config['tts']['feedback_level'] == 'brief'


@pytest.mark.parametrize('accepted', [False, True])
def test_network_notice_precedes_engine_creation_and_every_network_sample(build, monkeypatch, accepted):
    _, page, app = build()
    before, events = deepcopy(app.config), []
    factory = tutorial_try_preview.make_engine
    def notice(_):
        assert not app.sample_engines
        events.append('notice')
        return accepted
    def create(*args):
        assert events == ['notice'] and page.network_allowed()
        events.append('engine')
        return factory(*args)
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', notice)
    monkeypatch.setattr(tutorial_try_preview, 'make_engine', create)
    page.voice.setCurrentIndex(page.voice.findData('edge'))
    assert events == (['notice', 'engine'] if accepted else ['notice'])
    if accepted:
        assert page.preview.engine.speak.called
        assert page.preview.authorized()
        page.stop_preview(dispose=True)
        assert app.sample_engines[-1].shutdown.called
    assert app.config == before
    app.save_config.assert_not_called()


def test_leaving_during_network_notice_never_starts_sample(build, monkeypatch):
    window, page, app = build()
    before = deepcopy(app.config)
    def notice(_):
        window.hide()
        return True
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', notice)
    page.voice.setCurrentIndex(page.voice.findData('edge'))
    assert not app.sample_engines and page.preview is None
    assert app.config == before
    app.save_config.assert_not_called()


def test_private_sample_tracks_live_echo_and_disposes_without_touching_live_engine(build, monkeypatch):
    monkeypatch.setattr(AudioCoordinator, '_start_interrupt_poll', lambda self: None)
    window, page, app = build(level='silent')
    live_engine = MagicMock()
    live_engine.get_engine_state.return_value = 'idle'
    live = AudioCoordinator(app, live_engine)
    app.audio_coordinator = live
    before = deepcopy(app.config)
    try:
        page.level.buttons[2].click()
        sample = page.preview
        assert isinstance(sample, tutorial_try_preview.SampleSpeech)
        assert live.is_speaking and live.speech_windows()
        assert app.audio_coordinator is live and live.engine is live_engine
        live_engine.speak.assert_not_called()
        live_engine.shutdown.assert_not_called()
        page.stop_preview(dispose=True)
        assert not live.is_speaking and live.speech_windows()
        sample.engine.shutdown.assert_called_once()
        assert not sample.authorized() and app.config == before
    finally:
        window.close()
        live.shutdown()


def test_private_sample_completion_keeps_newer_live_speech(build, monkeypatch):
    monkeypatch.setattr(AudioCoordinator, '_start_interrupt_poll', lambda self: None)
    window, page, app = build(level='silent')
    live = AudioCoordinator(app, None)
    app.audio_coordinator = live
    page.level.buttons[2].click()
    sample = page.preview
    live._last_spoken_handle = SpeechHandle('newer-live-speech')
    live._speech_pending = 1  # A genuinely queued live reply owns SPEAKING.
    page.stop_preview()
    assert live.is_speaking
    sample.engine.shutdown.assert_not_called()
    window.close()
    live.shutdown()


def test_private_completion_does_not_close_newer_speech_before_its_handle_is_published(build, monkeypatch):
    monkeypatch.setattr(AudioCoordinator, '_start_interrupt_poll', lambda self: None)
    window, page, app = build(level='silent')
    live = AudioCoordinator(app, None)
    app.audio_coordinator = live
    page.level.buttons[2].click()
    live._speech_pending = 1  # Live speak queued its reply, before publishing _last_spoken_handle.
    page.stop_preview()
    assert live.is_speaking
    window.close()
    live.shutdown()


def test_private_completion_disposes_on_poll_and_revokes_consent_on_hide(build, monkeypatch):
    window, page, app = build()
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', lambda _: True)
    page.voice.setCurrentIndex(page.voice.findData('edge'))
    sample = page.preview
    callback = sample.engine.speak.call_args.kwargs['on_done']
    callback()
    sample.engine.shutdown.assert_not_called()  # No shutdown/join from its own worker.
    page.poll()
    sample.engine.shutdown.assert_not_called()  # Completed previews retain their engine for replay.
    assert not sample.authorized()
    page.sample('voice')
    next_sample = page.preview
    window.hide()
    assert not next_sample.authorized()
    next_sample.engine.shutdown.assert_called_once()


def test_newer_live_speech_stops_private_output_on_poll_without_cancelling_live_reply(build, monkeypatch):
    monkeypatch.setattr(AudioCoordinator, '_start_interrupt_poll', lambda self: None)
    window, page, app = build(level='silent')
    live = AudioCoordinator(app, None)
    app.audio_coordinator = live
    page.level.buttons[2].click()
    sample = page.preview
    live._last_spoken_handle = SpeechHandle('newer-live-speech')
    live._speech_pending = 1  # A genuinely queued live reply owns SPEAKING.
    page.poll()
    sample.engine.cancel_all.assert_called_once()
    sample.engine.shutdown.assert_not_called()
    assert live.is_speaking and page.preview is None
    window.close()
    live.shutdown()


def test_pace_stops_after_ending_pause_suggests_then_keeps(build):
    _, page, app = build()
    page.show_stage(2)
    before = deepcopy(app.config)
    values = [5000] + [0] * (1200 // FRAME_MS) + [5000] + [0] * (4000 // FRAME_MS)
    frames = [SimpleNamespace(pcm=np.full(FRAME_SIZE, value, dtype=np.int16)) for value in values]
    reader = SimpleNamespace(read_next=lambda: frames.pop(0) if frames else EMPTY)
    engine = SimpleNamespace(_running=True, register_consumer=MagicMock(return_value=reader), unregister_consumer=MagicMock())
    app._ace_engine = engine
    page.actions['pace'].click()
    assert page.reader is reader and 'Listening.' in page.result.text()
    for _ in range(20):
        page.poll()
        if page.reader is None:
            break
    assert page.reader is None and page.pace.currentData() == 'relaxed'
    assert 'Suggested pace: Relaxed' in page.result.text()
    assert 'seconds' in page.pace_description.text()
    assert app.config == before
    page.keeps['pace'].click()
    assert app.config['accessibility']['speech_pace'] == 'relaxed'
    engine.unregister_consumer.assert_called_once_with(reader)


def test_busy_blocks_open_voice_and_hide_stops_speech_and_measurement(build):
    window, page, app = build()
    app.recording = True
    window._go_next()
    app.audio_coordinator.speak.assert_not_called()
    page.speed.sliderReleased.emit()
    app.audio_coordinator.speak.assert_not_called()
    app.recording = False
    page.sample('voice')
    window.hide()
    app.audio_coordinator.cancel_speech.assert_called_once()
    window.show()
    page.show_stage(2)
    reader = SimpleNamespace(read_next=lambda: EMPTY)
    engine = SimpleNamespace(_running=True, register_consumer=MagicMock(return_value=reader), unregister_consumer=MagicMock())
    app._ace_engine = engine
    page.measure()
    window.hide()
    engine.unregister_consumer.assert_called_once_with(reader)
    assert page.reader is None and not page.timer.isActive()
    app.save_config.assert_not_called()


@pytest.mark.parametrize('scale', [1, 1.5])
def test_all_states_fit_real_tutorial_without_scrollbars(build, qapp, scale):
    import re
    window, page, app = build()
    if scale != 1:
        for widget in (window, *window.findChildren(QWidget)):
            widget.setStyleSheet(re.sub(r'(font-size\s*:\s*)(\d+(?:\.\d+)?)px',
                lambda m: f'{m[1]}{round(float(m[2])*scale)}px', widget.styleSheet()))
        window._step_indicator.set_ui_scale(scale)
    for stage in range(3):
        page.show_stage(stage)
        for _ in range(8):
            qapp.processEvents()
        assert page.horizontalScrollBar().maximum() == page.verticalScrollBar().maximum() == 0
        for label in page.widget().findChildren(QLabel):
            if label.isVisible() and label.text():
                needed = label.heightForWidth(label.width()) if label.hasHeightForWidth() else label.fontMetrics().height()
                assert label.height() >= needed, (stage, label.text())
        for control in page.level.buttons + page.pace.buttons:
            assert control.height() >= 44 and control.accessibleName() == control.text()
    page.suggest([1.2])
    for _ in range(8):
        qapp.processEvents()
    assert page.verticalScrollBar().maximum() == 0
    page.measure()  # Unavailable microphone offers direct choices.
    for _ in range(8):
        qapp.processEvents()
    assert page.verticalScrollBar().maximum() == 0
    reader = SimpleNamespace(read_next=lambda: EMPTY)
    app._ace_engine = SimpleNamespace(_running=True, register_consumer=lambda _: reader, unregister_consumer=lambda _: None)
    page.measure()
    for _ in range(8):
        qapp.processEvents()
    assert page.verticalScrollBar().maximum() == 0
    page.show_stage(0)
    page.level.buttons[0].setFocus()
    QTest.keyClick(page.level.buttons[0], Qt.Key.Key_Return)
    assert page.played_level == 'silent'


@pytest.mark.parametrize('finish', ['callback', 'poll', 'hide'])
def test_suppressed_live_reply_during_sample_releases_speaking(build, monkeypatch, finish):
    monkeypatch.setattr(AudioCoordinator, '_start_interrupt_poll', lambda self: None)
    window, page, app = build(level='silent')
    live = AudioCoordinator(app, None)
    app.audio_coordinator = live
    try:
        page.level.buttons[2].click()
        sample = page.preview
        callback = sample.engine.speak.call_args.kwargs['on_done']
        noop = live.speak('Synthetic reminder', category='general')
        assert live._last_spoken_handle is noop and live._speech_pending == 0
        assert live.is_speaking
        assert sample.authorized()  # Suppressed speech does not take ownership.
        if finish == 'hide':
            window.hide()
        else:
            callback()
            if finish == 'poll':
                page.poll()
        assert not live.is_speaking and not sample.authorized()
        assert live.speech_windows(), 'Retain the echo tail after completion'
        assert app.config['tts']['feedback_level'] == 'silent'
        app.save_config.assert_not_called()
    finally:
        window.close()
        live.shutdown()


def test_old_sample_callback_cannot_finish_reused_engine_trial(build):
    window, page, app = build(level='silent')
    page.level.buttons[2].click()
    sample = page.preview
    callback = sample.engine.speak.call_args.kwargs['on_done']
    page.level.buttons[2].click()
    assert page.preview is sample and len(app.sample_engines) == 1
    callback()
    assert sample.active and sample.authorized()
    window.hide()
    sample.engine.shutdown.assert_called_once()


def test_silent_skipped_focus_is_next_and_tab_can_enter_step(build, qapp):
    window, page, app = build(level='silent')
    window._next_btn.setFocus()
    window._go_next()
    qapp.processEvents()
    assert page.voice_skip.isVisible()
    assert window.focusWidget() is window._next_btn
    window._go_next()
    qapp.processEvents()
    assert window.focusWidget() is window._next_btn
    assert page.reader is None
    app.save_config.assert_not_called()


@pytest.mark.parametrize('control', ['level', 'voice', 'pace'])
def test_busy_or_hidden_focused_control_parks_on_next(build, qapp, control):
    window, page, app = build()
    if control == 'voice':
        page.show_stage(1)
        target = page.voice_id
    elif control == 'pace':
        page.show_stage(2)
        target = page.pace.buttons[0]
    else:
        target = page.level.buttons[2]
    target.setFocus()
    qapp.processEvents()
    assert window.focusWidget() is target
    if control == 'pace':
        page._pace_ready = False  # Hiding the focused choices must also park focus.
    else:
        app.recording = True
    page.refresh()
    qapp.processEvents()
    assert window.focusWidget() is window._next_btn
    assert window.focusWidget() is not window._skip_all_btn
    app.save_config.assert_not_called()


def test_repeated_space_on_next_walks_steps_without_starting_microphone(build, qapp):
    window, page, app = build()
    window._next_btn.setFocus()
    for stage in (1, 2):
        QTest.keyClick(window._next_btn, Qt.Key.Key_Space)
        qapp.processEvents()
        assert page.stage == stage and window.focusWidget() is window._next_btn
        assert page.reader is None
    QTest.keyClick(window._next_btn, Qt.Key.Key_Tab)
    qapp.processEvents()
    assert window.focusWidget() is page.actions['pace']
    app.save_config.assert_not_called()


@pytest.mark.parametrize('destination', ['done', 'display'])
def test_leaving_try_restores_width_and_poll_does_not_resize(build, qapp, destination):
    window, page, app = build()
    original = window._try_previous_width
    page.show_stage(1)
    for _ in range(8):
        qapp.processEvents()
    window.resize(max(window.minimumWidth(), window.width() - 40), window.height())
    width = window.width()
    for _ in range(20):
        page.poll()
        qapp.processEvents()
    assert window.width() == width
    page.show_stage(2 if destination == 'done' else 0)
    if destination == 'done':
        window._go_next()
    else:
        window._go_back()
    for _ in range(8):
        qapp.processEvents()
    assert window._steps[window._step][0] == destination
    assert window.width() == original
    assert window.screen().availableGeometry().contains(window.frameGeometry())


def test_private_engine_reused_and_capture_lock_free_during_construction_shutdown(build, monkeypatch):
    import threading
    window, page, app = build(level='silent')
    factory = tutorial_try_preview.make_engine
    observations = []
    def check_lock():
        def worker():
            acquired = app._hold_capture_lifecycle_lock.acquire(blocking=False)
            observations.append(acquired)
            if acquired:
                app._hold_capture_lifecycle_lock.release()
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive() and observations[-1] is True
    def create(*args):
        check_lock()
        sample = factory(*args)
        sample.shutdown.side_effect = check_lock
        return sample
    monkeypatch.setattr(tutorial_try_preview, 'make_engine', create)
    page.show_stage(1)  # Saved Silent skips automatic speech, direct sample remains possible.
    page.sample('voice')
    sample = page.preview
    page.speed.setValue(120)
    page.speed.sliderReleased.emit()
    page.volume.setValue(70)
    page.volume.sliderReleased.emit()
    assert page.preview is sample and len(app.sample_engines) == 1
    assert sample.engine.speak.call_count == 3
    sample.engine.shutdown.assert_not_called()
    window.hide()
    sample.engine.shutdown.assert_called_once()
    assert observations == [True, True]
    app.save_config.assert_not_called()


def test_disabled_saved_speech_can_be_previewed_without_enabling_it(build):
    _, page, app = build(config={'tts': {'enabled': False, 'feedback_level': 'brief', 'engine': 'winrt'}})
    before = deepcopy(app.config)
    page.sample('voice')
    assert isinstance(page.preview, tutorial_try_preview.SampleSpeech)
    page.keep('voice')
    assert app.config['tts']['enabled'] is False
    assert before['tts']['enabled'] is False


def test_default_voice_sentence_uses_lowercase_current_voice(build):
    _, page, _ = build()
    page.sample('voice')
    assert page.states['voice'].text() == 'You are hearing the current voice.'


def test_first_tab_on_initial_try_arrival_enters_the_first_choice(build, qapp):
    window, page, app = build()
    assert window.focusWidget() is window._next_btn
    QTest.keyClick(window._next_btn, Qt.Key.Key_Tab)
    qapp.processEvents()
    assert window.focusWidget() is page.level.buttons[0]
    assert page.played_level is None
    app.save_config.assert_not_called()


def test_network_slider_replays_stop_old_blocks_before_new_sample(build, monkeypatch):
    """Model Edge's delayed block checks and speak resetting a shared cancel flag."""
    _, page, app = build()
    before = deepcopy(app.config)
    engines = []

    class BlockEngine:
        def __init__(self):
            self._cancelled = False
            self.utterances = []
            self.shutdown = MagicMock(side_effect=self.cancel_all)

        def set_authorization_checker(self, checker):
            self.checker = checker

        def list_voices(self):
            return []

        def speak(self, text, **kwargs):
            assert self.checker()
            self._cancelled = False  # The live Edge engine resets ONE shared flag.
            handle = SpeechHandle(str(len(self.utterances)))
            self.utterances.append(SimpleNamespace(blocks=0, stopped=False, done=kwargs['on_done']))
            return handle

        def cancel_all(self):
            self._cancelled = True  # A worker sees this only on its next block.

        def play_block(self):
            audible = 0
            for utterance in self.utterances:
                if utterance.stopped:
                    continue
                if self._cancelled or not self.checker():
                    utterance.stopped = True
                    utterance.done()  # Late completion must not end replacement speech.
                else:
                    utterance.blocks += 1
                    audible += 1
            return audible

    def create(_app, provider):
        assert provider == 'edge' and page.network_allowed()
        engine = BlockEngine()
        engines.append(engine)
        return engine

    monkeypatch.setattr(tutorial_try_preview, 'make_engine', create)
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', lambda _: True)
    page.voice.setCurrentIndex(page.voice.findData('edge'))
    assert sum(engine.play_block() for engine in engines) == 1
    first = engines[0].utterances[0]
    for slider in (page.speed, page.volume, page.speed):
        slider.setValue(slider.value() + 1)
        slider.sliderReleased.emit()  # No worker block between cancellation and replay.
        assert sum(engine.play_block() for engine in engines) == 1
        assert page.preview.active and page.preview.authorized()
    assert len(engines) == 4
    assert first.stopped and first.blocks == 1
    for engine in engines[:-1]:
        engine.shutdown.assert_called_once()
        assert engine._cancelled and not engine.checker()
    assert app.config == before
    app.save_config.assert_not_called()
    page.stop_preview(dispose=True)
    assert sum(engine.play_block() for engine in engines) == 0


def test_idle_network_engine_is_reused(build, monkeypatch):
    _, page, app = build()
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', lambda _: True)
    page.voice.setCurrentIndex(page.voice.findData('edge'))
    sample = page.preview
    sample.engine.speak.call_args.kwargs['on_done']()
    page.poll()
    page.speed.sliderReleased.emit()
    assert page.preview is sample and len(app.sample_engines) == 1
    assert sample.engine.speak.call_count == 2
    sample.engine.shutdown.assert_not_called()
    app.save_config.assert_not_called()


@pytest.mark.parametrize('key', ['level', 'voice', 'pace'])
@pytest.mark.parametrize('active', [False, True])
def test_keep_disposes_private_engine_for_every_step(build, key, active):
    _, page, app = build(level='silent')
    page.level.buttons[2].click()
    sample = page.preview
    if not active:
        sample.engine.speak.call_args.kwargs['on_done']()
        page.poll()
    if key == 'pace':
        page.suggest([.4])
    page.keep(key)
    assert key in page.kept
    assert sample.closed and not sample.authorized()
    sample.engine.shutdown.assert_called_once()
    assert page.sample_engine is page.sample_key is page.preview is None


def test_initial_page_with_no_try_does_not_restore_a_try_width(build):
    window, page, app = build()
    window._stack_layout.removeWidget(page)
    page.hide()
    window._current_page = window._try_page = None
    window._step = 0
    width = window.width()
    window._try_previous_width = width + 77
    window._show_step()
    assert window._current_page is window._pages[0]
    assert window.width() == width
    app.save_config.assert_not_called()
