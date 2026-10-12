"""Network failures stay audible, cancellable and private, entirely offline."""
import asyncio
from copy import deepcopy
import logging
import selectors
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.tts import edge_tts_engine as edge
from samsara.tts.coordinator import AudioCoordinator, IDLE, LISTENING, SPEAKING
from samsara.tts.engine_base import SpeechHandle
from samsara.tts.exceptions import RenderError
from tests.test_tutorial_try_389 import build


@pytest.fixture
def offline_asyncio(monkeypatch):
    """Keep real asyncio timers/tasks without Windows' loopback self-pipe.

    Each fake stream runs entirely on its private worker thread. It needs no
    I/O or cross-thread wakeup; the isolated gate correctly forbids sockets.
    """
    class TimerSelector(selectors.SelectSelector):
        def register(self, *args, **kwargs):
            raise AssertionError('Offline streams must not register I/O')

        def select(self, timeout=None):
            assert timeout is not None, 'Offline stream has no scheduled wakeup'
            time.sleep(max(0.0, timeout))
            return []

    class TimerLoop(asyncio.SelectorEventLoop):
        def __init__(self):
            super().__init__(TimerSelector())

        def _make_self_pipe(self):
            pass

        def _close_self_pipe(self):
            pass

        def _write_to_self(self):
            raise AssertionError('Offline streams must stay on their loop thread')

    monkeypatch.setattr(edge.asyncio, 'new_event_loop', TimerLoop)


class WindowsVoice:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.hold = False
        self.fail = False
        self.started = threading.Event()
        self.cancelled = []
        self.volume = []
        self.stopped = False
        self.restarts = 0

    def speak(self, text, **kwargs):
        handle = SpeechHandle(str(len(self.calls)))
        self.calls.append((text, kwargs, handle))
        handle._state = 'playing' if self.hold else 'failed' if self.fail else 'done'
        self.started.set()
        if not self.hold:
            kwargs['on_done']()
        return handle

    def cancel(self, handle):
        self.cancelled.append(handle)
        handle._state = 'cancelled'

    def cancel_all(self):
        for _, _, handle in self.calls:
            self.cancel(handle)

    def get_engine_state(self):
        return 'playing' if self.hold else 'idle'

    def set_volume(self, handle, volume, **kwargs):
        self.volume.append((handle, volume, kwargs))

    def set_output_device(self, device):
        self.kwargs['output_device'] = device

    def shutdown(self):
        self.stopped = True

    def restart_stream(self):
        self.restarts += 1


@pytest.fixture
def rig(monkeypatch):
    from samsara.tts import winrt_engine
    jobs, requests, played = [], [], []
    local = WindowsVoice()
    monkeypatch.setattr(edge, '_import_edge_tts', lambda: None)
    monkeypatch.setattr(winrt_engine, 'WinRTEngine', lambda **kw: local)
    def spawn(name, worker, **kw):
        assert name == 'EdgeTTS-worker'
        jobs.append(worker)
    monkeypatch.setattr(edge.thread_registry, 'spawn', spawn)
    def fail(*args, **kwargs):
        requests.append(kwargs)
        raise RenderError('Network unavailable')
    monkeypatch.setattr(edge, '_synthesize_mp3', fail)
    monkeypatch.setattr(edge, '_mp3_to_pcm', lambda data: b'pcm')
    engine = edge.EdgeTTSEngine(output_device=17)
    monkeypatch.setattr(engine, '_play_pcm', lambda pcm, **kw: played.append(pcm))
    return SimpleNamespace(engine=engine, local=local, jobs=jobs, requests=requests,
                           played=played, fail=fail)


def app_router(rig):
    app = SimpleNamespace(config={'tts': {'enabled': True, 'engine': 'edge',
                                         'feedback_level': 'full', 'voice_id': 'network-id',
                                         'speed': 1.3, 'volume': .6}},
                          tts_engine=rig.engine, command_mode_active=False,
                          _show_outcome_chip=Mock(),
                          listening_indicator=SimpleNamespace(show_outcome=Mock()),
                          _schedule_ui=Mock(), _hold_chip_seq=7, _hold_chip_resolved_seq=6)
    router = AudioCoordinator(app, rig.engine)
    router._on_transition = Mock()  # No capture or poll workers in this fixture.
    router._emit_caption = Mock()
    app.audio_coordinator = router
    return app, router


def test_same_turn_same_words_settings_and_completion(rig):
    app, router = app_router(rig)
    before, done = deepcopy(app.config), Mock()
    handle = router.speak('Example speech', category='readback', on_done=done)
    assert router.get_state() == SPEAKING
    rig.jobs.pop(0)()
    text, args, _ = rig.local.calls[0]
    assert text == 'Example speech'
    assert args['speed'] == 1.3 and args['volume'] == .6
    assert args['category'] == 'readback' and args['queue_mode'] == 'append'
    assert 'voice_id' not in args
    assert handle.network_failed and handle.voice_result == 'windows'
    assert router.get_state() == IDLE and handle._state == 'done'
    done.assert_called_once()
    assert app.config == before
    app._schedule_ui.assert_called_once_with(app.listening_indicator.show_outcome,
        'Network voice unavailable - using the Windows voice.', 'warning', 10000)
    app._show_outcome_chip.assert_not_called()
    assert app._hold_chip_resolved_seq == 6
    assert app._outcome_ring[-1].source == 'network_voice_fallback'
    router.speak('Another example', category='readback')
    rig.jobs.pop(0)()
    AudioCoordinator(app, rig.engine)._on_network_fallback()
    assert app._schedule_ui.call_count == 1 and len(app._outcome_ring) == 1


def test_two_failures_cooldown_one_probe_and_recovery(rig, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(edge.time, 'monotonic', lambda: now[0])
    def run():
        handle = rig.engine.speak('Example')
        rig.jobs.pop(0)()
        return handle
    run(); run()
    assert len(rig.requests) == 2 and rig.engine._retry_at == 700
    now[0] = 699
    bypass = run()
    assert bypass.voice_result == 'windows'
    assert bypass.network_bypassed and not bypass.network_failed
    assert len(rig.requests) == 2
    now[0] = 700
    run()
    assert len(rig.requests) == 3 and rig.engine._retry_at == 1300
    now[0] = 1300
    monkeypatch.setattr(edge, '_synthesize_mp3', lambda *a, **kw: b'mp3')
    assert run().voice_result == 'network'
    assert rig.engine._failure_count == 0 and rig.engine._retry_at == 0
    assert rig.played == [b'pcm']
    assert [request['max_attempts'] for request in rig.requests] == [3, 3, 1]


@pytest.mark.parametrize('failure', ['refused', 'offline', 'timeout', 'empty', 'decode'])
def test_real_fake_stream_reaches_windows(rig, monkeypatch, failure, caplog, offline_asyncio):
    module = ModuleType('edge_tts')
    class Refused(Exception):
        status = 403
    class Communicate:
        def __init__(self, *args, **kwargs):
            pass
        async def stream(self):
            if failure == 'refused':
                raise Refused('wss://example.invalid/private')
            if failure == 'offline':
                raise ConnectionError('offline')
            if failure == 'timeout':
                await asyncio.sleep(.02)
            if failure == 'decode':
                yield {'type': 'audio', 'data': b'x' * 4096}
    module.Communicate = Communicate
    monkeypatch.setitem(sys.modules, 'edge_tts', module)
    real_synthesize = _ORIGINAL_SYNTHESIZE
    def synth(*args, **kwargs):
        return real_synthesize(*args, **kwargs, timeout=.001 if failure == 'timeout' else 1)
    monkeypatch.setattr(edge, '_synthesize_mp3', synth)
    # Run the real synthesis helper's isolated async loop on a real test thread.
    def spawn(name, worker, **kw):
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread
    monkeypatch.setattr(edge.thread_registry, 'spawn', spawn)
    if failure == 'decode':
        monkeypatch.setattr(edge, '_mp3_to_pcm', lambda _: b'')
    done = threading.Event()
    with caplog.at_level(logging.WARNING):
        handle = rig.engine.speak('Example', on_done=done.set)
        assert done.wait(2)
    assert handle.voice_result == 'windows' and len(rig.local.calls) == 1
    assert 'wss://' not in caplog.text
    if failure == 'refused':
        assert 'status=403' in caplog.text and 'attempt 1' in caplog.text


_ORIGINAL_SYNTHESIZE = edge._synthesize_mp3


def test_cancel_before_fallback_does_not_revive_with_later_speech(rig):
    done = Mock()
    old = rig.engine.speak('Old example', on_done=done)
    rig.engine.cancel_all()
    new = rig.engine.speak('New example')
    rig.jobs.pop(0)()
    assert old.voice_result == 'cancelled' and rig.local.calls == []
    rig.jobs.pop(0)()
    assert new.voice_result == 'windows' and len(rig.local.calls) == 1
    done.assert_called_once()


@pytest.mark.parametrize('action', ['cancel', 'barge', 'silent', 'disable', 'provider', 'shutdown'])
def test_active_windows_obeys_cancellation(rig, action):
    app, router = app_router(rig)
    rig.local.hold = True
    done = Mock()
    handle = router.speak('Example', category='readback', on_done=done)
    thread = threading.Thread(target=rig.jobs.pop(0), daemon=True)
    thread.start()
    try:
        assert rig.local.started.wait(1)
        assert rig.engine.get_engine_state() == 'playing'
        rig.engine.set_volume(handle, .4, fade_ms=7)
        assert rig.local.volume[0][1:] == (.4, {'fade_ms': 7})
        if action == 'barge':
            router._on_user_speech_interrupt()
        elif action == 'shutdown':
            router.shutdown()
        elif action in ('silent', 'disable'):
            app.config['tts']['enabled'] = False
            router.cancel_speech()
        elif action == 'provider':
            app.config['tts']['engine'] = 'winrt'
        else:
            router.cancel_speech()
        thread.join(2)
        assert not thread.is_alive()
        assert handle.voice_result == 'cancelled'
        assert rig.local.cancelled
        assert router.get_state() == (LISTENING if action == 'barge' else IDLE)
        done.assert_called_once()
    finally:
        rig.engine.cancel_all()
        thread.join(2)


def test_silent_never_reaches_network_or_windows(rig):
    app, router = app_router(rig)
    app.config['tts']['feedback_level'] = 'silent'
    assert router.speak('Example', category='readback').utterance_id == 'noop-silent'
    assert rig.jobs == [] and rig.local.calls == []


@pytest.mark.parametrize('failure', ['construct', 'speak', 'async'])
def test_windows_failure_still_completes_coordinator(rig, monkeypatch, failure):
    from samsara.tts import winrt_engine
    app, router = app_router(rig)
    def fail(*a, **kw):
        raise RenderError('Windows unavailable')
    if failure == 'construct':
        monkeypatch.setattr(winrt_engine, 'WinRTEngine', fail)
    elif failure == 'speak':
        monkeypatch.setattr(rig.local, 'speak', fail)
    else:
        rig.local.fail = True
    done = Mock()
    handle = router.speak('Example', on_done=done)
    rig.jobs.pop(0)()
    assert handle.voice_result == 'failed' and router.get_state() == IDLE
    app._schedule_ui.assert_not_called()
    assert not getattr(app, '_network_voice_fallback_notified', False)
    done.assert_called_once()


def test_synchronous_engine_failure_and_duplicate_done_leave_idle(rig, monkeypatch):
    _, router = app_router(rig)
    done = Mock()
    def fail(*a, **kw):
        kw['on_done']()
        raise RenderError('Cannot start speech')
    monkeypatch.setattr(rig.engine, 'speak', fail)
    with pytest.raises(RenderError):
        router.speak('Example', on_done=done)
    assert router.get_state() == IDLE and router._speech_pending == 0
    done.assert_called_once()


@pytest.mark.parametrize('private', [False, True])
def test_tutorial_reports_network_failure_before_releasing_preview(build, private):
    _, page, app = build()
    page.stage = 1
    before = deepcopy(app.config)
    handle = SpeechHandle('sample')
    handle.network_failed = True
    handle.voice_result = 'windows'
    page.preview_handle = handle
    if private:
        from samsara.ui.tutorial_try_preview import SampleSpeech
        sample = object.__new__(SampleSpeech)
        sample.active, sample.closed = False, False
        sample.page, sample.router, sample.marker = page, app.audio_coordinator, object()
        sample.provider, sample.engine = 'edge', Mock()
        page.preview = sample
    else:
        page.preview = app.audio_coordinator
        page.preview.is_speaking = False
    page.poll()
    assert page.result.text() == 'The Network voice did not answer.'
    assert page.states['voice'].text() == page.result.text()
    assert app.config == before
    app.save_config.assert_not_called()


def test_tutorial_cooldown_does_not_claim_a_network_request_failed(build):
    _, page, app = build()
    page.stage = 1
    handle = SpeechHandle('sample')
    handle.network_failed, handle.network_bypassed = False, True
    page.preview_handle, page.preview = handle, app.audio_coordinator
    page.preview.is_speaking = False
    page.result.setText('Speaking: Example')
    page.poll()
    assert page.result.text() != 'The Network voice did not answer.'


def fake_stream(monkeypatch, factory):
    module = ModuleType('edge_tts')
    class Communicate:
        def __init__(self, *args, **kwargs):
            pass
        def stream(self):
            return factory()
    module.Communicate = Communicate
    monkeypatch.setitem(sys.modules, 'edge_tts', module)
    def spawn(name, worker, **kwargs):
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread
    monkeypatch.setattr(edge.thread_registry, 'spawn', spawn)


def test_steady_thirty_second_stream_succeeds_without_outage(rig, monkeypatch, offline_asyncio):
    attempts = []
    async def stream():
        attempts.append(1)
        for _ in range(30):
            await asyncio.sleep(1)
            yield {'type': 'audio', 'data': b'x' * 4096}
    fake_stream(monkeypatch, stream)
    monkeypatch.setattr(edge, '_synthesize_mp3', _ORIGINAL_SYNTHESIZE)
    done = threading.Event()
    started = time.monotonic()
    handle = rig.engine.speak('Example', on_done=done.set)
    assert done.wait(40)
    assert time.monotonic() - started >= 30
    assert handle.voice_result == 'network' and attempts == [1]
    assert not handle.network_failed and rig.engine._failure_count == 0
    assert rig.engine._retry_at == 0 and rig.local.calls == []


@pytest.mark.parametrize('refusal', [False, True])
def test_healthy_short_stream_retries_but_handshake_refusal_does_not(rig, monkeypatch, refusal, offline_asyncio):
    attempts = []
    class Refused(Exception):
        status = 403
    async def stream():
        attempts.append(1)
        if refusal:
            raise Refused('refused')
        yield {'type': 'audio', 'data': b'x' * (100 if len(attempts) == 1 else 4096)}
    fake_stream(monkeypatch, stream)
    monkeypatch.setattr(edge, '_synthesize_mp3', _ORIGINAL_SYNTHESIZE)
    done = threading.Event()
    handle = rig.engine.speak('Example', on_done=done.set)
    assert done.wait(2)
    assert len(attempts) == (1 if refusal else 2)
    assert handle.voice_result == ('windows' if refusal else 'network')
    assert rig.engine._failure_count == (1 if refusal else 0)


def test_mid_stream_disconnect_retains_partial_without_windows_replay(rig, monkeypatch, offline_asyncio):
    attempts = []
    async def stream():
        attempts.append(1)
        yield {'type': 'audio', 'data': b'x' * 100}
        raise ConnectionError('disconnected')
    fake_stream(monkeypatch, stream)
    monkeypatch.setattr(edge, '_synthesize_mp3', _ORIGINAL_SYNTHESIZE)
    done = threading.Event()
    handle = rig.engine.speak('Example', on_done=done.set)
    assert done.wait(2)
    assert attempts == [1, 1, 1] and handle.voice_result == 'network'
    assert not handle.network_failed and rig.local.calls == []


@pytest.mark.parametrize('first_audio', [False, True])
def test_idle_timeout_cancels_stream_and_metadata_cannot_extend_it(monkeypatch, first_audio, offline_asyncio):
    closed = []
    async def stream():
        try:
            if first_audio:
                yield {'type': 'audio', 'data': b'x' * 100}
            while True:
                await asyncio.sleep(.002)
                yield {'type': 'WordBoundary', 'data': b''}
        finally:
            closed.append(True)
    fake_stream(monkeypatch, stream)
    if first_audio:
        assert _ORIGINAL_SYNTHESIZE('voice', 'Example', '+0%', '+0%',
                                    max_attempts=1, timeout=.02) == b'x' * 100
    else:
        with pytest.raises(RenderError):
            _ORIGINAL_SYNTHESIZE('voice', 'Example', '+0%', '+0%',
                                 max_attempts=1, timeout=.02)
    assert closed == [True]


def test_default_device_restart_reaches_fallback_and_next_network_uses_resolver(rig):
    rig.engine.restart_stream()  # No fallback yet; must not build one.
    assert rig.engine._fallback_engine is None and rig.local.restarts == 0
    handle = rig.engine.speak('Example')
    rig.jobs.pop(0)()
    assert handle.voice_result == 'windows'
    rig.engine.restart_stream()
    assert rig.local.restarts == 1
    device = [None]
    rig.engine._output_device_resolver = lambda: device[0]
    assert rig.engine._playback_device() is None
    device[0] = 23
    assert rig.engine._playback_device() == 23


@pytest.mark.parametrize('action', ['cancel', 'shutdown', 'device'])
def test_windows_construction_does_not_block_cancel_or_state(rig, monkeypatch, action):
    from samsara.tts import winrt_engine
    building, release, inspected = threading.Event(), threading.Event(), threading.Event()
    def construct(**kwargs):
        building.set()
        assert release.wait(2)
        return rig.local
    monkeypatch.setattr(winrt_engine, 'WinRTEngine', construct)
    done = Mock()
    handle = rig.engine.speak('Example', on_done=done)
    worker = threading.Thread(target=rig.jobs.pop(0), daemon=True)
    worker.start()
    assert building.wait(1)
    def inspect():
        assert rig.engine.get_engine_state() == 'synthesizing'
        if action == 'shutdown':
            rig.engine.shutdown()
        elif action == 'device':
            rig.engine.set_output_device(29)
        else:
            rig.engine.cancel_all()
        inspected.set()
    probe = threading.Thread(target=inspect, daemon=True)
    probe.start()
    try:
        assert inspected.wait(.5), 'Network lock blocked behind Windows construction'
    finally:
        release.set()
        probe.join(2)
        worker.join(2)
    assert not worker.is_alive()
    if action == 'device':
        assert handle.voice_result == 'windows'
        assert rig.local.kwargs['output_device'] == 29
    else:
        assert handle.voice_result == 'cancelled' and rig.local.calls == []
        assert rig.local.stopped == (action == 'shutdown')
    done.assert_called_once()


def test_notification_waits_for_actual_windows_playback(rig, monkeypatch):
    app, router = app_router(rig)
    pending = SpeechHandle('local')
    def speak(*args, **kwargs):
        rig.local.calls.append((args[0], kwargs, pending))
        rig.local.started.set()
        return pending
    monkeypatch.setattr(rig.local, 'speak', speak)
    done = Mock()
    handle = router.speak('Example', on_done=done)
    worker = threading.Thread(target=rig.jobs.pop(0), daemon=True)
    worker.start()
    try:
        assert rig.local.started.wait(1)
        app._schedule_ui.assert_not_called()
        assert not getattr(app, '_network_voice_fallback_notified', False)
        pending._state = 'playing'
        deadline = time.monotonic() + 1
        while app._schedule_ui.call_count == 0 and time.monotonic() < deadline:
            time.sleep(.01)
        assert app._schedule_ui.call_count == 1 and app._hold_chip_resolved_seq == 6
        pending._state = 'done'
        worker.join(2)
        assert handle.voice_result == 'windows'
        done.assert_called_once()
    finally:
        rig.engine.cancel_all()
        worker.join(2)
