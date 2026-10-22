"""389: tutorial choices, bounded previews and passive ring measurement."""
from copy import deepcopy
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import MagicMock

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from samsara.ui import tutorial_qt, network_voice_notice, tutorial_try_preview
from samsara.tts import runtime
from samsara.tts.engine_base import VoiceInfo
from samsara.audio_engine.ring import EMPTY
from samsara.audio_engine.frame import FRAME_SIZE, FRAME_MS


@pytest.fixture
def build(qapp, monkeypatch):
    windows = []
    displays = []
    # Execute only the production setter declaration, never dictation's bootstrap.
    source_path = Path(__file__).resolve().parents[1] / 'dictation.py'
    source = ast.parse(source_path.read_text(encoding='utf-8'))
    app_class = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'DictationApp')
    setter = next(node for node in app_class.body if isinstance(node, ast.FunctionDef) and node.name == 'set_feedback_level')
    namespace = {'logger': logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[setter], type_ignores=[]), str(source_path), 'exec'), namespace)
    def create(level='brief', engine=True, config=None, display='Full'):
        cfg = {'tts': {'feedback_level': level, 'enabled': level != 'silent', 'engine': 'winrt', 'speed': 1},
               'accessibility': {'speech_pace': 'standard', 'other': 'preserved'}}
        if config:
            cfg.update(config)
        app = SimpleNamespace(config=cfg, _config_lock=threading.Lock(), recording=False,
                              _hold_capture_lifecycle_lock=threading.RLock(), history_db=MagicMock(),
                              audio_coordinator=make_coordinator() if engine else MagicMock(engine=None, is_speaking=False),
                              command_executor=SimpleNamespace(_matcher=SimpleNamespace(list_commands=lambda: [])),
                              sample_engines=[],
                              save_config=MagicMock(), play_sound=MagicMock())
        def sample_engine(_app, _provider):
            if not engine:
                raise RuntimeError('synthetic unavailable speech')
            sample = MagicMock()
            sample.list_voices.return_value = [VoiceInfo('preview-a', 'Preview A', 'en', 'unknown'), VoiceInfo('preview-b', 'Preview B', 'en', 'unknown')]
            sample.speak.return_value = SimpleNamespace(utterance_id='synthetic-private-sample')
            app.sample_engines.append(sample)
            return sample
        monkeypatch.setattr(tutorial_try_preview, 'make_engine', sample_engine)
        app.set_feedback_level = MagicMock(side_effect=lambda value: namespace['set_feedback_level'](app, value))
        app.update_config_and_save = MagicMock(side_effect=lambda changes: app.config.update(changes))
        from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
        from samsara.ui.listening_indicator import ListeningIndicator
        app.config.setdefault('ui', {})['live_surface'] = {'enabled': display == 'Full'}
        if display == 'Full':
            controller = LiveSurfaceController(app)
            app.listening_indicator = LiveSurfaceIndicatorAdapter(controller)
            app.flush_display = controller.drain
            app.display_text = lambda: controller.view().notice.message
            displays.append(controller.close)
        else:
            indicator = ListeningIndicator()
            indicator.set_idle_animation(False)
            app.listening_indicator = indicator
            app.flush_display = lambda: None
            app.display_text = lambda: indicator._chip_label
            displays.append(lambda: (indicator.destroy(), indicator.deleteLater()))
        app._schedule_ui = lambda fn, *args: fn(*args)
        window = tutorial_qt.TutorialWindow(app)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window._step = next(i for i, (key, _) in enumerate(window._steps) if key == 'try')
        window._show_step()
        window.show()
        qapp.processEvents()
        windows.append(window)
        return window, window._try_page, app
    def make_coordinator():
        engine = MagicMock()
        engine.list_voices.return_value = [VoiceInfo('voice-a', 'Voice A', 'en', 'unknown'), VoiceInfo('voice-b', 'Voice B', 'en', 'unknown')]
        coordinator = MagicMock(engine=engine, is_speaking=False, _speech_pending=0)
        def speak(*args, **kwargs):
            handle = SimpleNamespace(utterance_id='synthetic-preview')
            coordinator._last_spoken_handle = handle
            coordinator.is_speaking = True
            return handle
        coordinator.speak.side_effect = speak
        coordinator.cancel_speech.side_effect = lambda: setattr(coordinator, 'is_speaking', False)
        return coordinator
    def rebuild(app, cfg):
        app.audio_coordinator = make_coordinator()
        if not cfg.get('enabled'):
            app.audio_coordinator.engine = None
        app.tts_engine = app.audio_coordinator.engine
    monkeypatch.setattr(runtime, 'rebuild_tts_runtime', rebuild)
    yield create
    for window in windows:
        window.close()
        window.deleteLater()
    for close in displays:
        close()
    qapp.processEvents()


def select(box, value):
    # Explicit sample tests set a voice without also triggering the automatic sample.
    quiet = box.accessibleName() == 'Voice'
    if quiet:
        box.blockSignals(True)
    box.setCurrentIndex(box.findData(value))
    if quiet:
        box.blockSignals(False)


def test_step_navigation_and_skip_does_not_write(build):
    window, page, app = build()
    before = deepcopy(app.config)
    assert [key for key, _ in window._steps] == ['welcome', 'dictation', 'command', 'ways', 'display', 'try', 'done']
    assert window._next_btn.isEnabled()
    for _ in range(3):
        window._go_next()
    assert window._steps[window._step][0] == 'done'
    assert app.config == before and not page.kept
    app.save_config.assert_not_called()
    app.set_feedback_level.assert_not_called()
    app.update_config_and_save.assert_not_called()


def test_choices_samples_acceptance_and_back_do_not_save(build, monkeypatch):
    window, page, app = build()
    before = deepcopy(app.config)
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', lambda _: True)
    select(page.level, 'full')
    page.sample('level')
    select(page.voice, 'edge')
    page.speed.setValue(125)
    page.sample('voice')
    select(page.pace, 'relaxed')
    window._go_back()
    assert app.config == before
    assert page.preview is None
    app.save_config.assert_not_called()
    app.set_feedback_level.assert_not_called()
    app.update_config_and_save.assert_not_called()


@pytest.mark.parametrize('in_use', ['silent', 'brief', 'full'])
@pytest.mark.parametrize('level', ['silent', 'brief', 'full'])
def test_sample_routes_real_feedback_policy_without_setting_write(build, level, in_use):
    _, page, app = build(level=in_use)
    before = deepcopy(app.config)
    select(page.level, level)
    page.sample('level')
    assert app.play_sound.called == (level == 'brief')
    if level == 'brief':
        app.play_sound.assert_called_once_with('action_complete')
    speech = level == 'full'
    live_speech = speech and in_use != 'silent'
    assert app.audio_coordinator.speak.called == live_speech
    assert (page.preview is not None) == speech
    if live_speech:
        assert page.preview is app.audio_coordinator
        app.audio_coordinator.speak.assert_called_once_with('Example complete.', voice_id=None, speed=1, volume=.8, category='readback')
    elif speech:
        app.sample_engines[-1].speak.assert_called_once()
    else:
        assert page.result.text() == 'Example complete.'
    assert 'Keep this to hear it spoken.' not in page.states['level'].text()
    assert app.config == before


@pytest.mark.parametrize('level', ['silent', 'brief', 'full'])
def test_level_keep_uses_named_setter_and_only_its_keys(build, level):
    window, page, app = build()
    select(page.level, level)
    page.sample('level')
    page.keep('level')
    app.set_feedback_level.assert_called_once_with(level)
    app.save_config.assert_called_once()
    app.update_config_and_save.assert_not_called()
    assert app.config['tts']['engine'] == 'winrt'
    window._go_next()
    assert page.cards['voice'].isVisible() == (level != 'silent')
    window._go_next()
    window._go_next()
    assert page.kept['level'] in window._try_summary.text()
    assert 'Voice kept' not in window._try_summary.text()


def test_silent_skip_uses_played_choice_then_saved_level(build):
    window, page, app = build(level='silent')
    page.show_stage(1)
    assert page.voice_skip.isVisible() and not page.cards['voice'].isVisible()
    page.show_stage(0)
    page.level.buttons[2].click()
    window._go_next()
    assert page.cards['voice'].isVisible()
    assert app.config['tts']['feedback_level'] == 'silent'


def test_voice_keep_calls_shared_runtime_with_only_voice_updates(build, monkeypatch):
    _, page, app = build()
    rebuild = MagicMock()
    monkeypatch.setattr(runtime, 'rebuild_tts_runtime', rebuild)
    page.speed.setValue(125)
    before = deepcopy(app.config)
    page.keep('voice')
    expected = deepcopy(before)
    expected['tts'].update(engine='winrt', speed=1.25, volume=.8)
    assert app.config == expected
    rebuild.assert_not_called()
    app.save_config.assert_called_once()
    app.set_feedback_level.assert_not_called()
    app.update_config_and_save.assert_not_called()


def test_pace_keep_uses_named_config_setter_and_preserves_other_keys(build):
    _, page, app = build()
    before = deepcopy(app.config)
    select(page.pace, 'unhurried')
    page.keep('pace')
    app.update_config_and_save.assert_called_once_with({'accessibility': {'speech_pace': 'unhurried', 'other': 'preserved'}})
    assert app.config['tts'] == before['tts']
    app.save_config.assert_not_called()


def test_network_rejected_never_reaches_engine_or_saves(build, monkeypatch):
    window, page, app = build()
    before = deepcopy(app.config)
    notice = MagicMock(return_value=False)
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', notice)
    select(page.voice, 'edge')
    notice.assert_called_once_with(window)
    assert page.voice.currentData() == 'winrt'
    # Defensive check at the sample/keep seam, even if signals were bypassed.
    page.voice.blockSignals(True)
    select(page.voice, 'edge')
    page.sample('voice')
    assert page.preview is None and app.config == before
    app.save_config.assert_not_called()
    # Declining resets the chooser to Local; force unaccepted Network again to test Keep independently.
    select(page.voice, 'edge')
    page.keep('voice')
    assert page.preview is None and app.config == before
    app.save_config.assert_not_called()


def test_network_acceptance_stays_unsaved_until_voice_keep(build, monkeypatch):
    _, page, app = build()
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', lambda _: True)
    select(page.voice, 'edge')
    assert 'network_voice_accepted' not in app.config['tts']
    page.sample('voice')
    assert isinstance(page.preview, tutorial_try_preview.SampleSpeech)
    assert page.actions['voice'].isEnabled()
    assert len(app.sample_engines) == 2
    assert all(engine.speak.call_count == 1 for engine in app.sample_engines)
    app.sample_engines[0].shutdown.assert_called_once()  # Active Network trials cannot share cancellation.
    app.audio_coordinator.speak.assert_not_called()
    assert 'network_voice_accepted' not in app.config['tts']
    page.keep('voice')
    assert app.config['tts']['engine'] == 'edge'
    assert app.config['tts']['network_voice_accepted'] is True
    app.save_config.assert_called_once()


@pytest.mark.parametrize('flag', ['recording', 'command_mode_recording', 'ava_mode_recording', '_hotkey_recording', '_memo_recording', '_stop_in_flight', '_ace_dictation_active', '_ace_streaming_active', '_hold_park_finalizing', 'continuous_active', 'command_mode_active', 'ava_mode_active'])
def test_busy_app_disables_and_refuses_samples(build, flag):
    _, page, app = build()
    setattr(app, flag, True)
    page.poll()
    assert all(not control.isEnabled() for control in page.level.buttons)
    assert not page.actions['voice'].isEnabled()
    assert page.actions['voice'].text() == 'Samsara is busy'
    for name in ('level', 'voice'):
        page.sample(name)
    assert page.preview is None
    app.play_sound.assert_not_called()


def test_busy_mid_preview_uses_live_cancellation_without_runtime_shutdown(build):
    _, page, app = build(level='full')
    live = app.audio_coordinator
    page.sample('level')
    assert page.preview is live
    page.poll()
    live.cancel_speech.assert_not_called()  # Our own speech does not cancel itself.
    app.recording = True
    page.poll()
    assert page.preview is None and app.audio_coordinator is live
    live.cancel_speech.assert_called_once()
    live.shutdown.assert_not_called()
    live.engine.shutdown.assert_not_called()


def test_page_exit_preserves_unrelated_live_speech(build):
    window, page, app = build()
    page.sample('voice')
    app.audio_coordinator._last_spoken_handle = object()  # A later application reply owns playback.
    window._go_back()
    app.audio_coordinator.cancel_speech.assert_not_called()
    app.audio_coordinator.shutdown.assert_not_called()


def test_transcription_owner_disables_samples(build):
    _, page, app = build()
    app._transcription_owners = SimpleNamespace(current=lambda lane: lane == 'hold')
    page.poll()
    assert all(not control.isEnabled() for control in page.level.buttons)
    assert not page.actions['voice'].isEnabled()
    page.sample('voice')
    app.audio_coordinator.speak.assert_not_called()


def test_no_engine_offers_no_voice_actions(build):
    _, page, app = build(engine=False)
    assert page.states['voice'].text() == 'Speech is unavailable.'
    assert not page.actions['voice'].isEnabled()
    assert all(not control.isEnabled() for control in (page.voice, page.voice_id, page.speed, page.volume))
    page.sample('voice')
    assert page.states['voice'].text() == 'Speech is unavailable.'
    assert page.preview is None
    assert not app.sample_engines
    page.keep('voice')
    app.save_config.assert_not_called()


@pytest.mark.parametrize('pauses,answer', [([.4], 'standard'), ([1.2], 'relaxed'), ([2.6], 'unhurried')])
def test_fed_pause_list_preselects_real_recommendation(build, pauses, answer):
    _, page, app = build()
    before = deepcopy(app.config)
    page.suggest(pauses)
    assert page.pace.currentData() == answer
    assert app.config == before


def test_real_ring_measurement_never_opens_stream_and_drops_audio(build, monkeypatch):
    import sounddevice
    stream = MagicMock(side_effect=AssertionError('No second input stream'))
    monkeypatch.setattr(sounddevice, 'InputStream', stream)
    monkeypatch.setattr(sounddevice, 'RawInputStream', stream)
    _, page, app = build()
    frames = [SimpleNamespace(pcm=np.full(FRAME_SIZE, value, dtype=np.int16)) for value in [0, 5000] + [0]*12 + [5000]]
    reader = SimpleNamespace(read_next=lambda: frames.pop(0) if frames else EMPTY)
    engine = SimpleNamespace(_running=True, register_consumer=MagicMock(return_value=reader), unregister_consumer=MagicMock())
    app._ace_engine = engine
    page.measure()
    page.poll()
    page.finish_measure()
    assert page.pace.currentData() == 'relaxed'
    assert page.levels == [] and page.reader is None and page.capture is None
    engine.register_consumer.assert_called_once_with('tutorial-try-389')
    engine.unregister_consumer.assert_called_once_with(reader)
    stream.assert_not_called()
    assert app.config['accessibility']['speech_pace'] == 'standard'


def test_unavailable_ring_only_offers_manual_pace(build):
    _, page, _ = build()
    page.measure()
    assert page.reader is None
    assert 'microphone is unavailable' in page.result.text()
    assert page.pace.isEnabled()


def test_pace_seconds_follow_real_profile_and_current_baseline(build):
    _, page, _ = build(config={'silence_threshold': 3})
    select(page.pace, 'relaxed')
    assert page.pace_description.text() == ('Continuous dictation waits 7.5 seconds after speech.'
                                            ' Commands need a clear gap of 1 second.')
    select(page.pace, 'unhurried')
    assert page.pace_description.text() == ('Continuous dictation waits 12 seconds after speech.'
                                            ' Commands need a clear gap of 1 second.')


@pytest.mark.parametrize('config,expected', [
    ({'silence_threshold': 1}, 'Continuous dictation waits 1 second after speech.'
                              ' Commands need a clear gap of 1 second.'),
    ({'command_mode': {'dictate_utterance_silence_s': 1.5}},
     'Continuous dictation waits 2 seconds after speech. Commands need a clear gap of 2.25 seconds.'),
])
def test_pace_description_formats_singular_and_fractional_seconds(build, config, expected):
    _, page, _ = build(config=config)
    assert page.pace_description.text() == expected


@pytest.mark.parametrize('scale', [1, 1.5])
def test_keyboard_names_and_layout_at_actual_text_scale(build, qapp, scale):
    import re
    window, page, _ = build()
    if scale != 1:
        for widget in [window, *window.findChildren(QWidget)]:
            widget.setStyleSheet(re.sub(r'(font-size\s*:\s*)(\d+(?:\.\d+)?)px',
                                       lambda m: f'{m[1]}{round(float(m[2])*scale)}px', widget.styleSheet()))
        window._step_indicator.set_ui_scale(scale)
    qapp.processEvents()
    for button in page.findChildren(QPushButton):
        assert button.accessibleName() == button.text()
        assert QAccessible.queryAccessibleInterface(button) is not None
        assert button.height() >= 44
    button = page.level.buttons[1]
    button.setFocus()
    QTest.keyClick(button, Qt.Key.Key_Return)
    assert page.result.text() == 'Example complete.'
    for _ in range(8):
        qapp.processEvents()
    assert page.horizontalScrollBar().maximum() == 0
    for label in page.widget().findChildren(QLabel):
        if label.isVisible() and label.text():
            needed = label.heightForWidth(label.width()) if label.hasHeightForWidth() else label.fontMetrics().height()
            assert label.height() >= needed, label.text()
            assert max(label.fontMetrics().horizontalAdvance(word) for word in label.text().split()) <= label.width()


def test_provider_change_clears_incompatible_voice_only_on_keep(build, monkeypatch):
    cfg = {'tts': {'feedback_level': 'brief', 'enabled': True, 'engine': 'winrt', 'speed': 1,
                   'voice_id': 'windows-only-voice', 'volume': .75}}
    _, page, app = build(config=cfg)
    before = deepcopy(app.config)
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', lambda _: True)
    select(page.voice, 'edge')
    page.sample('voice')
    assert isinstance(page.preview, tutorial_try_preview.SampleSpeech)
    assert len(app.sample_engines) == 2
    assert all(engine.speak.call_count == 1 for engine in app.sample_engines)
    app.sample_engines[0].shutdown.assert_called_once()  # Active Network trials cannot share cancellation.
    app.audio_coordinator.speak.assert_not_called()
    assert app.config == before
    page.keep('voice')
    assert app.config['tts'] == dict(feedback_level='brief', enabled=True, engine='edge', speed=1,
                                     volume=.75, network_voice_accepted=True)


def test_done_lists_each_kept_card_once(build):
    window, page, _ = build()
    page.sample('level')
    page.suggest([.4])
    for key in ('level', 'voice', 'pace'):
        page.keep(key)
    page.stage = 2
    window._go_next()
    assert window._try_summary.text().splitlines() == list(page.kept.values())
    assert len(page.kept) == 3


def test_initial_network_choice_requires_notice_on_hear(build, monkeypatch):
    _, page, app = build(config={'tts': {'feedback_level': 'brief', 'enabled': True, 'engine': 'edge', 'speed': 1}})
    before = deepcopy(app.config)
    notice = MagicMock(return_value=False)
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', notice)
    page.sample('voice')
    notice.assert_called_once()
    assert page.preview is None and app.config == before


def test_page_exit_unregisters_reader_and_discards_levels(build):
    window, page, app = build()
    reader = SimpleNamespace(read_next=lambda: EMPTY)
    engine = SimpleNamespace(_running=True, register_consumer=MagicMock(return_value=reader), unregister_consumer=MagicMock())
    app._ace_engine = engine
    page.measure()
    page.levels = [.2, 0, .3]
    window._go_back()
    engine.unregister_consumer.assert_called_once_with(reader)
    assert page.reader is None and page.levels == [] and not page.timer.isActive()


def test_switching_provider_cancels_only_owned_live_preview(build, monkeypatch):
    _, page, app = build()
    before = deepcopy(app.config)
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', lambda _: True)
    page.sample('voice')
    assert page.preview is app.audio_coordinator
    select(page.voice, 'edge')
    assert isinstance(page.preview, tutorial_try_preview.SampleSpeech)
    app.audio_coordinator.cancel_speech.assert_called_once()
    app.audio_coordinator.shutdown.assert_not_called()
    assert app.config == before


def test_live_voice_speed_and_volume_preview_then_keep(build, monkeypatch):
    _, page, app = build()
    before = deepcopy(app.config)
    select(page.voice_id, 'voice-b')
    page.speed.setValue(125)
    page.volume.setValue(65)
    rebuild = MagicMock()
    monkeypatch.setattr(runtime, 'rebuild_tts_runtime', rebuild)
    page.sample('voice')
    app.audio_coordinator.speak.assert_called_once_with('This is how Samsara sounds at this speed and volume.',
        voice_id='voice-b', speed=1.25, volume=.65, category='readback')
    assert app.config == before
    rebuild.assert_not_called()
    page.keep('voice')
    assert app.config['tts'] == dict(before['tts'], voice_id='voice-b', speed=1.25, volume=.65)
    rebuild.assert_not_called()
    app.audio_coordinator.cancel_speech.assert_called_once()
    app.save_config.assert_called_once()


def test_other_provider_is_heard_without_replacing_live_runtime(build, monkeypatch):
    _, page, app = build()
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', lambda _: True)
    live, before = app.audio_coordinator, deepcopy(app.config)
    select(page.voice, 'edge')
    assert page.actions['voice'].isEnabled() and page.voice_id.isEnabled()
    assert isinstance(page.preview, tutorial_try_preview.SampleSpeech)
    app.sample_engines[-1].speak.assert_called_once()
    live.speak.assert_not_called()
    assert app.audio_coordinator is live and app.config == before
    page.keep('voice')
    assert app.config['tts']['engine'] == 'edge'
    assert app.audio_coordinator is not live


def test_preview_speech_is_in_live_echo_tracking(build, monkeypatch):
    """Release safety: the capture filter must see requested sample speech."""
    from samsara.tts.coordinator import AudioCoordinator
    from samsara.tts.engine_base import SpeechHandle

    class SilentEngine:
        def speak(self, *_args, **_kwargs):
            return SpeechHandle('synthetic-sample')
        def get_engine_state(self):
            return 'playing'
        def cancel_all(self):
            pass
        def shutdown(self):
            pass

    monkeypatch.setattr(AudioCoordinator, '_start_interrupt_poll', lambda self: None)
    window, page, app = build(level='full')
    app.audio_coordinator = AudioCoordinator(app, SilentEngine())

    try:
        select(page.level, 'full')
        page.sample('level')
        assert page.preview is app.audio_coordinator
        assert app.audio_coordinator.speech_windows(), 'The live echo filter must track tutorial speech'
        app.recording = True
        page.poll()
        assert not app.audio_coordinator.is_speaking
        assert app.audio_coordinator.speech_windows(), 'Cancellation must retain the echo tail'
    finally:
        window.close()
        app.audio_coordinator.shutdown()


@pytest.mark.parametrize('scale', [1, 1.5])
def test_calm_rows_share_surfaces_heights_and_columns(build, qapp, scale):
    import re
    from PySide6.QtCore import QPoint
    from samsara.ui import theme
    from samsara.ui.settings.calm_layout import stylesheet
    from samsara.ui.focus_ring import keyboard_stylesheet
    from samsara.ui.settings.audio_page import BASE

    window, page, _ = build()
    page.show_stage(1)
    for slider in (page.speed, page.volume):
        assert slider.styleSheet() == keyboard_stylesheet(stylesheet())
    if scale != 1:
        for widget in [window, *window.findChildren(QWidget)]:
            widget.setStyleSheet(re.sub(r'(font-size\s*:\s*)(\d+(?:\.\d+)?)px',
                lambda match: f'{match[1]}{round(float(match[2])*scale)}px', widget.styleSheet()))
        window._step_indicator.set_ui_scale(scale)
    for _ in range(8):
        qapp.processEvents()
    provider_label = next(row[0] for row in BASE if row[2] == 'tts_engine')
    assert page.voice.accessibleName() == provider_label
    labels = [page.voice_labels[name] for name in ('Voice', provider_label, 'Speaking speed', 'Volume')]
    controls = [page.voice_id, page.voice, page.speed, page.volume]
    # Two columns keep the voice step within the tutorial at large text sizes.
    for column in (0, 1):
        assert labels[column].x() == labels[column + 2].x()
        assert controls[column].x() == controls[column + 2].mapTo(page.cards['voice'], QPoint()).x()
        assert labels[column].y() < labels[column + 2].y()
    for key, choice in (('level', page.level), ('pace', page.pace)):
        assert all(button.height() == theme.HIT_TARGET_MIN for button in choice.buttons)
        assert page.keeps[key].height() == theme.HIT_TARGET_MIN
    assert page.voice.height() == page.voice_id.height() == page.actions['voice'].height() == page.keeps['voice'].height()
    page.show_stage(1)
    card = page.cards['voice'].grab().toImage()
    for slider in (page.speed, page.volume):
        pixel = card.pixelColor(slider.x() + 4, slider.y() + 4)
        assert pixel == card.pixelColor(4, slider.y() + 4)
        assert pixel.name() != '#000000'


@pytest.mark.parametrize('display', ['Full', 'Compact'])
@pytest.mark.parametrize('level', ['silent', 'brief', 'full'])
def test_samples_show_on_both_displays_without_history_or_any_writes(build, display, level):
    _, page, app = build(level=level, display=display)
    before = deepcopy(app.config)
    for _ in range(4):
        page.sample('level')
        app.flush_display()
        assert app.display_text() == 'Example complete.'
    assert app.config == before
    app.history_db.add.assert_not_called()
    app.save_config.assert_not_called()
    app.set_feedback_level.assert_not_called()
    app.update_config_and_save.assert_not_called()
    # The exemption is scoped: real results still reach History on both paths.
    from samsara.live_surface.feedback import schedule_feedback
    schedule_feedback(app, 'Synthetic ordinary result', 'success', 1800)
    app.flush_display()
    app.history_db.add.assert_called_once()


@pytest.mark.parametrize('record_during_notice', [False, True])
def test_network_notice_leaves_capture_lock_free_and_rechecks_busy(build, monkeypatch, record_during_notice):
    _, page, app = build(level='full', config={'tts': {'engine': 'edge', 'enabled': True, 'feedback_level': 'full'}})
    observed = []
    def notice(_):
        def try_capture_lock():
            acquired = app._hold_capture_lifecycle_lock.acquire(blocking=False)
            observed.append(acquired)
            if acquired:
                app._hold_capture_lifecycle_lock.release()
        worker = threading.Thread(target=try_capture_lock)
        worker.start(); worker.join(timeout=2)
        assert not worker.is_alive() and observed == [True]
        app.recording = record_during_notice
        return True
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', notice)
    page.sample('level')
    assert observed == [True]
    assert app.audio_coordinator.speak.called == (not record_during_notice)
    app.save_config.assert_not_called()


@pytest.mark.parametrize('key', ['level', 'voice', 'pace'])
def test_keep_stops_own_sample_without_busy_message(build, key):
    _, page, app = build(level='full')
    page.sample('voice')
    page.refresh()
    assert page.actions['voice'].text() == 'Hear it'
    page.played_level = 'full'
    page._pace_ready = True
    page.keep(key)
    app.audio_coordinator.cancel_speech.assert_called_once()
    assert key in page.kept and 'busy' not in page.result.text()


def test_keep_does_not_cancel_unrelated_speech(build):
    _, page, app = build()
    page.sample('voice')
    app.audio_coordinator._last_spoken_handle = object()
    page.keep('voice')
    app.audio_coordinator.cancel_speech.assert_not_called()
    app.save_config.assert_not_called()
    assert 'busy' in page.result.text()


def test_pace_description_changes_once_and_copies_under_config_lock(build, monkeypatch):
    from samsara.ui import tutorial_try_qt
    _, page, app = build()
    real_copy = tutorial_try_qt.deepcopy
    def locked_copy(cfg):
        assert app._config_lock.locked()
        return real_copy(cfg)
    copy = MagicMock(side_effect=locked_copy)
    monkeypatch.setattr(tutorial_try_qt, 'deepcopy', copy)
    for _ in range(20):
        page.poll()
    copy.assert_not_called()
    old = page.pace_description.text()
    select(page.pace, 'relaxed')
    assert old != page.pace_description.text()
    for _ in range(20):
        page.poll()
    copy.assert_called_once_with(app.config)


@pytest.mark.parametrize('failure', ['raise', 'plain_widget'])
def test_broken_or_stubbed_try_page_keeps_tutorial_and_done_available(build, monkeypatch, failure):
    window, _, _ = build()
    window.close()
    def broken(owner):
        if failure == 'raise':
            owner._try_page = SimpleNamespace()  # A partially constructed page.
            raise ValueError('synthetic constructor failure')
        return QWidget()
    monkeypatch.setattr(tutorial_qt, '_TryPage', broken)
    other = tutorial_qt.TutorialWindow(SimpleNamespace(config={}))
    try:
        other.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        other.show()
        expected = ['welcome', 'dictation', 'command', 'ways', 'display', 'done']
        if failure == 'plain_widget':
            expected.insert(-1, 'try')
        assert [key for key, _ in other._steps] == expected
        assert other._step_indicator.count() == len(expected)
        for index in range(len(expected)):
            other._step = index
            other._show_step()
        assert other._try_summary.text() == ''
        assert other.isVisible() and other._steps[other._step][0] == 'done'
    finally:
        other.close(); other.deleteLater()


def test_voice_keep_rebuilds_only_on_provider_change(build, monkeypatch):
    _, page, app = build()
    rebuild = MagicMock()
    monkeypatch.setattr(runtime, 'rebuild_tts_runtime', rebuild)
    monkeypatch.setattr(network_voice_notice, 'request_acceptance', lambda _: True)
    page.speed.setValue(120)
    page.volume.setValue(60)
    select(page.voice_id, 'voice-b')
    for _ in range(3):
        page.keep('voice')
    rebuild.assert_not_called()
    assert app.config['tts']['enabled'] is True
    page.sample('voice')
    app.audio_coordinator.speak.assert_called_once_with('This is how Samsara sounds at this speed and volume.',
        voice_id='voice-b', speed=1.2, volume=.6, category='readback')
    select(page.voice, 'edge')
    page.keep('voice')
    rebuild.assert_called_once_with(app, app.config['tts'])
    page.keep('voice')
    assert rebuild.call_count == 1
