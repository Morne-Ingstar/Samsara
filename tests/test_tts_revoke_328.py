"""Settings must revoke a live network TTS engine on Apply."""
import sys
from types import ModuleType
from threading import RLock
from unittest.mock import MagicMock

import pytest

from samsara.tts.coordinator import AudioCoordinator, SUPPRESSED_TTS_ID
from samsara.tts.engine_base import SpeechHandle
from samsara.ui.settings.services import SettingsServices


class RecordingEngine:
    def __init__(self, **_kwargs):
        self.calls = []
        self.cancelled = 0
        self.stopped = False

    def speak(self, text, **_kwargs):
        self.calls.append(text)
        return SpeechHandle(utterance_id='recorded')

    def cancel_all(self):
        self.cancelled += 1

    def shutdown(self):
        self.stopped = True

    def get_engine_state(self):
        return 'idle'

    def is_speaking(self):
        return False


def _app(engine=None):
    app = MagicMock()
    app.config = {'tts': {'enabled': True, 'engine': 'edge'}, 'audio_coordinator': {}}
    app._config_lock = RLock()
    app.command_mode_active = False
    app.tts_engine = engine or RecordingEngine()
    app.audio_coordinator = AudioCoordinator(app, app.tts_engine)
    return app


class _Settings(SettingsServices):
    def __init__(self, app, requested):
        self.app = app
        self._save_fns = [lambda merged: {'tts': requested}]
        self.closed = False

    def close(self):
        self.closed = True


def test_disable_revokes_engine_and_stale_coordinator():
    old_engine = RecordingEngine()
    app = _app(old_engine)
    old_coordinator = app.audio_coordinator
    _Settings(app, {'enabled': False, 'engine': 'edge'})._apply_and_close()

    assert old_engine.cancelled >= 1 and old_engine.stopped
    assert app.tts_engine is None and app.audio_coordinator is None
    assert old_coordinator.speak('later').utterance_id == SUPPRESSED_TTS_ID
    assert old_engine.calls == []


def test_provider_switch_replaces_edge_before_next_speech(monkeypatch):
    import samsara.tts as tts

    monkeypatch.setattr(tts, 'WinRTEngine', RecordingEngine)
    old_engine = RecordingEngine()
    app = _app(old_engine)
    old_coordinator = app.audio_coordinator
    _Settings(app, {'enabled': True, 'engine': 'winrt'})._apply_and_close()

    assert old_engine.cancelled >= 1 and old_engine.stopped
    assert isinstance(app.tts_engine, RecordingEngine)
    assert app.tts_engine is not old_engine
    assert old_coordinator.speak('old network path').utterance_id == SUPPRESSED_TTS_ID
    app.audio_coordinator.speak('new local path')
    assert old_engine.calls == []
    assert app.tts_engine.calls == ['new local path']


def test_edge_worker_rechecks_authorization_before_network(monkeypatch):
    from samsara.tts import edge_tts_engine as edge

    queued = []
    monkeypatch.setattr(edge, '_import_edge_tts', lambda: None)
    monkeypatch.setattr(edge.thread_registry, 'spawn',
                        lambda name, worker, **_kw: queued.append(worker)
                        if name == 'EdgeTTS-worker' else None)
    monkeypatch.setattr(edge, '_synthesize_mp3',
                        lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(AssertionError('network reached')))
    engine = edge.EdgeTTSEngine()
    app = _app(engine)
    app.audio_coordinator.speak('queued')
    assert len(queued) == 1

    app.config['tts']['enabled'] = False
    engine.cancel_all()
    queued.pop()()
    assert engine.get_engine_state() == 'idle'
    assert engine.speak('direct old reference')._state == 'done'
    assert queued == []

    # Re-enabling Edge creates a new engine; a retained old reference stays revoked.
    app.config['tts']['enabled'] = True
    app.tts_engine = RecordingEngine()
    assert engine.speak('retired reference')._state == 'done'
    assert queued == []


def test_edge_short_stream_cannot_retry_after_revoke(monkeypatch):
    from samsara.tts import edge_tts_engine as edge

    allowed = [True]
    requests = []
    fake_module = ModuleType('edge_tts')

    class Communicate:
        def __init__(self, *_args, **_kwargs):
            requests.append(1)

        async def stream(self):
            yield {'type': 'audio', 'data': b'x'}
            allowed[0] = False

    fake_module.Communicate = Communicate
    monkeypatch.setitem(sys.modules, 'edge_tts', fake_module)
    with pytest.raises(edge._SynthesisRevoked):
        edge._synthesize_mp3('voice', 'short stream', '+0%', '+0%',
                             authorize=lambda: allowed[0])
    assert len(requests) == 1
