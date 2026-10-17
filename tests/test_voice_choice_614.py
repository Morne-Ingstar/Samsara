"""Chosen voices reach real UI/router/provider paths without audio or network."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QPushButton

from samsara.tts import edge_tts_engine as edge, winrt_engine as winrt
from samsara.tts.coordinator import AudioCoordinator
from samsara.tts.engine_base import SpeechHandle, VoiceInfo
from samsara.tts.exceptions import RenderError
from samsara.ui import tutorial_try_preview
from tests.test_settings_revisit_543 import window as settings_window
from tests.test_tutorial_try_389 import build
from tests.test_network_voice_fallback_542 import rig


class RecordingEngine:
    def __init__(self, provider):
        self.voices = [VoiceInfo(f'{provider}-{i}', f'Voice {i}', 'en-US', 'unknown')
                       for i in (1, 2)]
        self.calls = []

    def list_voices(self):
        return self.voices

    def speak(self, text, **kwargs):
        self.calls.append(kwargs)
        kwargs['on_done']()
        return SpeechHandle(str(len(self.calls)), 'done')

    def cancel_all(self):
        pass

    def shutdown(self):
        pass


def install_router(app, engine, provider):
    app.config['tts'].update(engine=provider, enabled=True, network_voice_accepted=True)
    app.tts_engine = engine
    router = AudioCoordinator(app, engine)
    router._on_transition = Mock()  # No capture, device or polling side effects.
    app.audio_coordinator = router
    return router


@pytest.mark.parametrize('provider', ['winrt', 'edge'])
def test_settings_test_button_reaches_engine_with_each_unsaved_voice(settings_window, provider):
    window = settings_window
    engine = RecordingEngine(provider)
    install_router(window.app, engine, provider)
    window._widgets['tts_engine'].setCurrentText(provider)
    window._refresh_tts_voices()
    before = deepcopy(window.app.config)
    button = next(b for b in window.findChildren(QPushButton) if b.text() == 'Test Voice')
    for voice in engine.voices:
        combo = window._widgets['tts_voice_combo']
        combo.setCurrentIndex(combo.findData(voice.voice_id))
        button.click()
        assert engine.calls[-1]['voice_id'] == voice.voice_id
    assert window.app.config == before
    assert len(engine.calls) == 2
    window._apply_and_close()
    assert window.app.config['tts']['voice_id'] == engine.voices[-1].voice_id


@pytest.mark.parametrize('saved,trial', [('winrt', 'winrt'), ('edge', 'edge'),
                                        ('winrt', 'edge'), ('edge', 'winrt')])
def test_tutorial_voice_picker_reaches_live_or_private_engine(build, monkeypatch, saved, trial):
    _, page, app = build(config={'tts': {'engine': saved, 'enabled': True,
                                      'feedback_level': 'brief', 'network_voice_accepted': True}})
    live = RecordingEngine(saved)
    install_router(app, live, saved)
    private = RecordingEngine(trial)
    monkeypatch.setattr(tutorial_try_preview, 'make_engine', lambda *_: private)
    page.voice_engine = None
    page.refresh()
    page.voice.setCurrentIndex(page.voice.findData(trial))
    before = deepcopy(app.config)
    target = live if saved == trial else private
    target.calls.clear()
    for voice in target.voices:
        page.voice_id.setCurrentIndex(page.voice_id.findData(voice.voice_id))
        assert target.calls[-1]['voice_id'] == voice.voice_id
    assert len(target.calls) == 2
    assert app.config == before
    page.keep('voice')
    assert app.config['tts']['engine'] == trial
    assert app.config['tts']['voice_id'] == target.voices[-1].voice_id


@pytest.fixture
def synthesizer(monkeypatch):
    voices = [SimpleNamespace(id=f'local-{i}', display_name=f'Local {i}',
                              language='en-US', gender=0) for i in (1, 2)]

    class Synth:
        all_voices = voices
        instances = []

        def __init__(self):
            self.voice = SimpleNamespace(id='default-local')
            self.options = SimpleNamespace()
            self.rendered = []
            self.closed = False
            self.instances.append(self)

        async def synthesize_text_to_stream_async(self, text):
            self.rendered.append(self.voice.id)
            return self.voice.id.encode()

        async def try_set_default_voice_async(self, voice):
            raise AssertionError('Per-utterance selection must not change the Windows default')

        def close(self):
            self.closed = True

    class Helper:
        def run_sync(self, coro):
            try:
                coro.send(None)
            except StopIteration as result:
                return result.value
            raise AssertionError('Fake synthesis must not perform async I/O')

    async def read(stream):
        return stream

    monkeypatch.setattr(winrt, '_read_stream_bytes', read)
    engine = winrt.WinRTEngine.__new__(winrt.WinRTEngine)
    engine._SpeechSynthesizer = Synth
    engine._VoiceGender = SimpleNamespace(MALE=0, FEMALE=1)
    engine._helper = Helper()
    return engine, Synth


def test_windows_synthesis_selects_fresh_instance_voice_each_time(synthesizer):
    engine, synth = synthesizer
    for voice_id in ('local-1', 'local-2', 'local-1', None):
        assert engine._synthesize('Synthetic sample', voice_id, 1.3, 1) == (
            voice_id or 'default-local').encode()
    assert all(instance.closed for instance in synth.instances)


@pytest.mark.parametrize('voice_id', [
    r'HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\Legacy',
    r'HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens\Missing',
    'removed-local-voice', 'en-US-AriaNeural',
])
def test_unselectable_voice_fails_without_rendering_default(synthesizer, voice_id):
    engine, synth = synthesizer
    with pytest.raises(RenderError):
        engine._synthesize('Synthetic sample', voice_id, 1, 1)
    assert synth.instances[-1].rendered == []
    assert synth.instances[-1].closed


@pytest.mark.parametrize('catalog', [[], None])
def test_windows_catalog_does_not_offer_unselectable_registry_tokens(synthesizer, catalog):
    engine, synth = synthesizer
    synth.all_voices = catalog
    # The old registry fallback advertised IDs that _synthesize could not select.
    engine._build_voice_list_from_registry = Mock(return_value=[
        VoiceInfo('registry-only', 'Unsupported SAPI voice', 'en-US', 'unknown')])
    assert engine._build_voice_list() == []
    engine._build_voice_list_from_registry.assert_not_called()


def test_windows_catalog_and_synthesis_use_same_ids(synthesizer):
    engine, _ = synthesizer
    assert [voice.voice_id for voice in engine._build_voice_list()] == ['local-1', 'local-2']
    for voice in engine._build_voice_list():
        assert engine._synthesize('Synthetic sample', voice.voice_id, 1, 1) == voice.voice_id.encode()


def test_rejected_voice_assignment_never_renders_default(synthesizer):
    engine, synth = synthesizer

    class RejectSelection(synth):
        @property
        def voice(self):
            return SimpleNamespace(id='default-local')

        @voice.setter
        def voice(self, value):
            pass  # Model a provider that ignores a selection.

    engine._SpeechSynthesizer = RejectSelection
    with pytest.raises(RenderError):
        engine._synthesize('Synthetic sample', 'local-2', 1, 1)
    assert synth.instances[-1].rendered == []
    assert synth.instances[-1].closed


@pytest.mark.parametrize('entry', ['settings', 'tutorial'])
@pytest.mark.parametrize('delayed', [False, True])
def test_local_failed_sample_is_visible(settings_window, build, entry, delayed):
    handle = SpeechHandle('failed-local', 'pending' if delayed else 'failed')
    if entry == 'settings':
        status = settings_window._widgets['tts_test_status']
        settings_window._watch_tts_sample(handle, status)
        if delayed:
            handle._state = 'failed'
            settings_window._tts_sample_timer.timeout.emit()
    else:
        _, page, _ = build()
        page.stage, page.preview_handle = 1, handle
        handle._state = 'failed'
        page.poll()
        status = page.result
    assert status.text() == 'The voice sample could not be spoken.'


@pytest.mark.parametrize('entry', ['settings', 'tutorial'])
def test_network_choice_and_cooldown_fallback_are_visible(rig, settings_window, build, monkeypatch, entry):
    requests = []

    def synthesize(voice_id, *args, **kwargs):
        requests.append(voice_id)
        return b'fake-mp3'

    monkeypatch.setattr(edge, '_synthesize_mp3', synthesize)
    if entry == 'settings':
        window = settings_window
        install_router(window.app, rig.engine, 'edge')
        window._widgets['tts_engine'].setCurrentText('edge')
        window._refresh_tts_voices()
        choose = window._widgets['tts_voice_combo']
        choose.setCurrentIndex(choose.findData('en-US-AriaNeural'))
        next(b for b in window.findChildren(QPushButton) if b.text() == 'Test Voice').click()
        rig.jobs.pop(0)()
        assert requests == ['en-US-AriaNeural']
        rig.engine._retry_at = float('inf')
        window._test_tts()
        rig.jobs.pop(0)()
        window._tts_sample_timer.timeout.emit()
        assert 'Windows voice' in window._widgets['tts_test_status'].text()
    else:
        _, page, app = build()
        install_router(app, RecordingEngine('winrt'), 'winrt')
        monkeypatch.setattr(tutorial_try_preview, 'make_engine', lambda *_: rig.engine)
        page.stage = 1
        page.voice.blockSignals(True)
        page.voice.setCurrentIndex(page.voice.findData('edge'))
        page.voice.blockSignals(False)
        page.load_voices(rig.engine, 'edge')
        page.voice_id.blockSignals(True)
        page.voice_id.setCurrentIndex(page.voice_id.findData('en-US-AriaNeural'))
        page.voice_id.blockSignals(False)
        page.actions['voice'].click()
        rig.jobs.pop(0)()
        assert requests[-1] == 'en-US-AriaNeural'
        page.poll()
        rig.engine._retry_at = float('inf')
        page.actions['voice'].click()
        rig.jobs.pop(0)()
        assert page.preview_handle.network_bypassed
        assert page.preview_handle.network_failed is False
        page.poll()
        assert page.result.text() == 'Network voice unavailable - using the Windows voice.'
    assert len(requests) == 1  # Cooldown must not make another Network request.
    assert rig.local.calls[-1][1].get('voice_id') is None
