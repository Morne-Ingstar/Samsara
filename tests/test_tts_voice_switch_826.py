"""Utterance-wide voice selection and bounded short-reply reuse, all offline."""
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.tts import edge_tts_engine as edge
from samsara.tts import winrt_engine
from samsara.tts.coordinator import AudioCoordinator
from samsara.tts.engine_base import SpeechHandle
from samsara.tts.exceptions import RenderError
from samsara.tts.runtime import rebuild_tts_runtime, tts_runtime_lock


class WindowsVoice:
    def __init__(self):
        self.calls = []
        self.hold = False
        self.started = threading.Event()

    def speak(self, text, **kwargs):
        handle = SpeechHandle('local', _state='playing' if self.hold else 'done')
        self.calls.append((text, kwargs, handle))
        self.started.set()
        if not self.hold:
            kwargs['on_done']()
        return handle

    def cancel(self, handle):
        handle._state = 'cancelled'

    def cancel_all(self):
        for _, _, handle in self.calls:
            self.cancel(handle)

    def shutdown(self):
        self.cancel_all()


@pytest.fixture
def rig(monkeypatch):
    jobs, synthesis, decoded, played, built = [], [], [], [], []
    local = WindowsVoice()
    monkeypatch.setattr(edge, '_import_edge_tts', lambda: None)
    monkeypatch.setattr(edge.thread_registry, 'spawn',
                        lambda name, worker, **kw: jobs.append(worker))

    def synth(*args, **kwargs):
        synthesis.append((args, kwargs))
        return b'mp3'

    def decode(data):
        decoded.append(data)
        return b'\0\0' * 100

    def play(pcm, *, authorize, on_start):
        assert authorize()
        on_start()
        played.append(pcm)

    def make_local(**kwargs):
        built.append(kwargs)
        return local

    monkeypatch.setattr(edge, '_synthesize_mp3', synth)
    monkeypatch.setattr(edge, '_mp3_to_pcm', decode)
    monkeypatch.setattr(winrt_engine, 'WinRTEngine', make_local)
    engine = edge.EdgeTTSEngine(fallback_voice_id='chosen-windows-id')
    monkeypatch.setattr(engine, '_play_pcm', play)

    def run(text='Synthetic confirmation', **kwargs):
        handle = engine.speak(text, **kwargs)
        jobs.pop(0)()
        return handle

    yield SimpleNamespace(engine=engine, local=local, jobs=jobs, synthesis=synthesis,
                          decoded=decoded, played=played, built=built, run=run)
    engine.shutdown()


def test_slow_edge_never_starts_another_voice(rig, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return b'mp3'

    monkeypatch.setattr(edge, '_synthesize_mp3', slow)
    done = Mock()
    handle = rig.engine.speak('Synthetic full utterance', on_done=done)
    worker = threading.Thread(target=rig.jobs.pop(0))
    worker.start()
    try:
        assert entered.wait(1)
        assert rig.engine.get_engine_state() == 'synthesizing'
        assert not rig.local.calls and not rig.built and not rig.played
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert handle.voice_result == 'network'
    assert len(rig.played) == 1 and not rig.local.calls
    done.assert_called_once()


def test_cache_hit_skips_synthesis_and_decode(rig):
    cold = rig.run(voice_id='en-US-AvaNeural')
    warm = rig.run(voice_id='en-US-AvaNeural')
    assert not cold.audio_cache_hit and warm.audio_cache_hit
    assert len(rig.synthesis) == len(rig.decoded) == 1
    assert len(rig.played) == 2 and rig.played[0] == rig.played[1]
    assert not rig.local.calls


@pytest.mark.parametrize('change', [
    {'voice_id': 'en-US-EmmaNeural'}, {'speed': 1.2}, {'pitch': 1.2},
    {'volume': .5}, {'text': 'Different synthetic confirmation'},
])
def test_changed_text_or_voice_settings_cannot_reuse_audio(rig, change):
    rig.run()
    assert not rig.run(**change).audio_cache_hit
    assert len(rig.synthesis) == 2


def test_implicit_and_explicit_ava_share_the_effective_voice_key(rig):
    rig.run()
    assert rig.run(voice_id='en-US-AvaNeural').audio_cache_hit


def test_queued_identical_confirmations_share_one_synthesis(rig):
    first = rig.engine.speak('Synthetic confirmation')
    second = rig.engine.speak('Synthetic confirmation')
    rig.jobs.pop(0)()
    rig.jobs.pop(0)()
    assert first.voice_result == second.voice_result == 'network'
    assert second.audio_cache_hit and len(rig.synthesis) == 1


def test_cache_survives_output_device_change_without_reusing_a_stream(rig):
    rig.run()
    rig.engine.set_output_device(27)
    rig.engine.restart_stream()
    assert rig.run().audio_cache_hit
    assert rig.engine._playback_device() == 27 and len(rig.played) == 2


def test_lru_evicts_oldest_and_refreshes_a_hit(rig, monkeypatch):
    monkeypatch.setattr(edge, '_CACHE_ENTRIES', 2)
    rig.run('Synthetic A')
    rig.run('Synthetic B')
    assert rig.run('Synthetic A').audio_cache_hit
    rig.run('Synthetic C')
    assert not rig.run('Synthetic B').audio_cache_hit
    assert len(rig.engine._short_reply_cache) == 2


def test_total_cache_bytes_are_bounded(rig, monkeypatch):
    monkeypatch.setattr(edge, '_CACHE_BYTES', 300)
    rig.run('Synthetic A')
    rig.run('Synthetic B')
    assert rig.engine._cache_bytes == 200
    assert not rig.run('Synthetic A').audio_cache_hit


@pytest.mark.parametrize('oversized', ['text', 'audio'])
def test_long_replies_and_large_audio_are_not_cached(rig, monkeypatch, oversized):
    if oversized == 'audio':
        monkeypatch.setattr(edge, '_CACHE_ITEM_BYTES', 100)
    text = 'x' * (edge._SHORT_REPLY_CHARS + 1) if oversized == 'text' else 'Synthetic'
    rig.run(text)
    assert not rig.run(text).audio_cache_hit
    assert not rig.engine._short_reply_cache and len(rig.synthesis) == 2


def test_exhausted_partial_synthesis_is_not_cached(rig, monkeypatch):
    calls = Mock(return_value=edge._PartialAudio(b'partial'))
    monkeypatch.setattr(edge, '_synthesize_mp3', calls)
    rig.run()
    assert not rig.run().audio_cache_hit
    assert calls.call_count == 2 and not rig.engine._short_reply_cache


def test_failed_synthesis_falls_back_with_whole_line_and_selected_voice(rig, monkeypatch):
    monkeypatch.setattr(edge, '_synthesize_mp3', Mock(side_effect=RenderError('offline')))
    handle = rig.run('Synthetic full utterance', voice_id='en-US-AvaNeural',
                     speed=1.2, volume=.6, category='confirmation')
    text, kwargs, _ = rig.local.calls[0]
    assert text == 'Synthetic full utterance'
    assert kwargs['voice_id'] == 'chosen-windows-id'
    assert kwargs['speed'] == 1.2 and kwargs['volume'] == .6
    assert kwargs['category'] == 'confirmation'
    assert handle.voice_result == 'windows' and handle.network_failed
    assert not rig.played and not rig.engine._short_reply_cache


def test_no_selected_windows_voice_never_uses_installed_default(rig, monkeypatch):
    rig.engine._fallback_voice_id = None
    monkeypatch.setattr(edge, '_synthesize_mp3', Mock(side_effect=RenderError('offline')))
    assert rig.run().voice_result == 'failed'
    assert not rig.local.calls and not rig.built and not rig.played


def test_cached_edge_voice_can_play_during_cooldown_without_resetting_it(rig):
    rig.run()
    rig.engine._failure_count = 2
    rig.engine._retry_at = time.monotonic() + 600
    retry_at = rig.engine._retry_at
    handle = rig.run()
    assert handle.audio_cache_hit and handle.voice_result == 'network'
    assert not handle.network_bypassed and not rig.local.calls
    assert rig.engine._retry_at == retry_at and rig.engine._failure_count == 2


def test_playback_failure_never_splices_in_windows_or_caches_audio(rig, monkeypatch):
    def fail(pcm, **kwargs):
        kwargs['on_start']()
        raise RuntimeError('Synthetic output failure after first block')
    monkeypatch.setattr(rig.engine, '_play_pcm', fail)
    assert rig.run().voice_result == 'failed'
    assert not rig.local.calls and not rig.engine._short_reply_cache


def test_cancelled_cache_hit_does_not_play_or_synthesize_again(rig):
    rig.run()
    handle = rig.engine.speak('Synthetic confirmation')
    rig.engine.cancel_all()
    rig.jobs.pop(0)()
    assert handle.voice_result == 'cancelled'
    assert len(rig.played) == len(rig.synthesis) == 1
    assert rig.run().audio_cache_hit  # a new utterance can use the existing cache


def test_revocation_during_synthesis_prevents_playback_fallback_and_cache(rig, monkeypatch):
    allowed = [True]
    rig.engine.set_authorization_checker(lambda: allowed[0])
    def synth(*args, **kwargs):
        allowed[0] = False
        return b'mp3'
    monkeypatch.setattr(edge, '_synthesize_mp3', synth)
    assert rig.run().voice_result == 'cancelled'
    assert not rig.played and not rig.local.calls and not rig.engine._short_reply_cache


def test_shutdown_discards_pcm_and_cancels_queued_hits(rig):
    rig.run()
    pending = rig.engine.speak('Synthetic confirmation')
    rig.engine.shutdown()
    rig.jobs.pop(0)()
    assert pending.voice_result == 'cancelled'
    assert not rig.engine._short_reply_cache and rig.engine._cache_bytes == 0
    assert len(rig.played) == 1


def _app(engine=None):
    return SimpleNamespace(config={'tts': {'enabled': True, 'engine': 'winrt',
                                          'voice_id': 'chosen-windows-id',
                                          'feedback_level': 'full'}},
                           _config_lock=threading.Lock(), audio_coordinator=None,
                           tts_engine=engine, command_mode_active=False)


def test_runtime_retains_explicit_local_choice_across_provider_rebuild(rig, monkeypatch):
    import samsara.tts as tts
    app = _app()
    monkeypatch.setattr(tts, 'EdgeTTSEngine', lambda **kwargs: rig.engine)
    monkeypatch.setattr(edge, '_synthesize_mp3', Mock(side_effect=RenderError('offline')))
    with tts_runtime_lock(app):
        app.config['tts'].update(engine='edge', voice_id='en-US-AvaNeural')
        rebuild_tts_runtime(app, app.config['tts'])
    try:
        # The router's callback reads the remembered Windows ID, never Ava's ID.
        assert rig.run().voice_result == 'windows'
        assert rig.local.calls[0][1]['voice_id'] == 'chosen-windows-id'
        assert app.config['tts']['voice_id'] == 'en-US-AvaNeural'
    finally:
        app.audio_coordinator.shutdown()


def test_new_edge_session_has_no_unrecorded_local_voice(rig, monkeypatch):
    app = _app(rig.engine)
    app.config['tts'].update(engine='edge', voice_id='en-US-AvaNeural')
    router = AudioCoordinator(app, rig.engine)
    monkeypatch.setattr(edge, '_synthesize_mp3', Mock(side_effect=RenderError('offline')))
    try:
        assert rig.run().voice_result == 'failed'
        assert not rig.local.calls and not rig.built
    finally:
        router.shutdown()


def test_default_windows_selection_clears_previous_explicit_choice(rig, monkeypatch):
    app = _app(rig.engine)
    app._tts_windows_voice_id = 'previous-windows-id'
    app.config['tts']['voice_id'] = None
    with tts_runtime_lock(app):
        app.config['tts'].update(engine='edge', voice_id='en-US-AvaNeural')
    router = AudioCoordinator(app, rig.engine)
    monkeypatch.setattr(edge, '_synthesize_mp3', Mock(side_effect=RenderError('offline')))
    try:
        assert app._tts_windows_voice_id is None
        assert rig.run().voice_result == 'failed'
        assert not rig.local.calls
    finally:
        router.shutdown()


def test_fallback_finishes_before_next_edge_utterance(rig, monkeypatch):
    waiting = threading.Event()
    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()
        def __enter__(self):
            if self.lock.locked():
                waiting.set()
            self.lock.acquire()
        def __exit__(self, *args):
            self.lock.release()
    rig.engine._worker_lock = ObservedLock()
    rig.local.hold = True
    calls = []
    def synth(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise RenderError('offline')
        return b'mp3'
    monkeypatch.setattr(edge, '_synthesize_mp3', synth)
    first = rig.engine.speak('Synthetic first utterance')
    second = rig.engine.speak('Synthetic second utterance')
    workers = [threading.Thread(target=rig.jobs.pop(0)) for _ in range(2)]
    workers[0].start()
    second_started = False
    released = False
    try:
        assert rig.local.started.wait(1)
        workers[1].start()
        second_started = True
        assert waiting.wait(1)
        assert len(calls) == 1 and not rig.played
        text, kwargs, local_handle = rig.local.calls[0]
        assert text == 'Synthetic first utterance'
        local_handle._state = 'done'
        kwargs['on_done']()
        released = True
    finally:
        if not released:
            rig.local.cancel_all()
        workers[0].join(2)
        if second_started:
            workers[1].join(2)
    assert not any(worker.is_alive() for worker in workers)
    assert first.voice_result == 'windows' and second.voice_result == 'network'
    assert len(rig.local.calls) == len(rig.played) == 1


def test_actual_pcm_writer_records_first_block_once(rig, monkeypatch):
    writes = []
    class Stream:
        def __init__(self, **kwargs):
            self.device = kwargs['device']
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def write(self, block):
            writes.append((self.device, len(block)))
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(OutputStream=Stream))
    monkeypatch.setattr(edge, 'output_sample_rate', lambda *args, **kwargs: 44100)
    start = Mock()
    edge.EdgeTTSEngine._play_pcm(rig.engine, b'\0\0' * 3000,
                                authorize=lambda: True, on_start=start)
    assert [length for _, length in writes] == [2048, 952]
    start.assert_called_once()


def test_synthetic_latency_before_and_after_cache(rig, monkeypatch):
    synth, decode = edge._synthesize_mp3, edge._mp3_to_pcm
    def delayed_synth(*args, **kwargs):
        time.sleep(.12)
        return synth(*args, **kwargs)
    def delayed_decode(data):
        time.sleep(.03)
        return decode(data)
    monkeypatch.setattr(edge, '_synthesize_mp3', delayed_synth)
    monkeypatch.setattr(edge, '_mp3_to_pcm', delayed_decode)
    cold = rig.run()
    warm = rig.run()
    print(f'Synthetic first-write latency: cold={cold.audio_started_ms:.2f}ms, '
          f'cache={warm.audio_started_ms:.2f}ms; fake synth=120ms, decode=30ms; '
          'no network or audio hardware measured')
    assert warm.audio_cache_hit and len(rig.synthesis) == len(rig.decoded) == 1
    assert warm.audio_started_ms < cold.audio_started_ms
