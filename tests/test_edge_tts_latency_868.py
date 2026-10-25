"""Bounded Edge prefetch, ordered playback and cancellation, without devices/network."""
import logging
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.tts import edge_tts_engine as edge
from samsara.tts.coordinator import AudioCoordinator, IDLE
from tests.test_network_voice_fallback_542 import offline_asyncio


@pytest.fixture
def rig(monkeypatch):
    workers, fetched, played = [], [], []
    monkeypatch.setattr(edge, '_import_edge_tts', lambda: None)
    engine = edge.EdgeTTSEngine()
    first_playing, release, second_fetched = [threading.Event() for _ in range(3)]

    def spawn(name, target, **kwargs):
        thread = threading.Thread(target=target, daemon=True)
        workers.append(thread)
        thread.start()
        return thread

    def synth(voice, text, rate, volume, **kwargs):
        fetched.append(text)
        if len(fetched) == 2:
            second_fetched.set()
        return text.encode()

    def play(pcm, *, authorize, on_start):
        assert authorize()
        played.append(pcm.decode())
        on_start()
        if len(played) == 1:
            first_playing.set()
            assert release.wait(2)

    monkeypatch.setattr(edge.thread_registry, 'spawn', spawn)
    monkeypatch.setattr(edge, '_synthesize_mp3', synth)
    monkeypatch.setattr(edge, '_mp3_to_pcm', lambda data: data)
    monkeypatch.setattr(engine, '_play_pcm', play)
    yield SimpleNamespace(engine=engine, workers=workers, fetched=fetched, played=played,
                          playing=first_playing, release=release, fetched_next=second_fetched)
    release.set()
    engine.shutdown()
    for thread in workers:
        thread.join(2)
        assert not thread.is_alive()


def test_next_fetch_overlaps_playback_but_never_overlaps_audio(rig):
    first = rig.engine.speak('First synthetic reply')
    assert rig.playing.wait(1)
    second = rig.engine.speak('Second synthetic reply')
    assert rig.fetched_next.wait(1), 'second fetch waited for first playback'
    assert rig.played == ['First synthetic reply']
    assert rig.engine.get_engine_state() == 'playing'
    rig.release.set()
    for thread in rig.workers:
        thread.join(2)
    assert rig.played == rig.fetched == ['First synthetic reply', 'Second synthetic reply']
    assert first.voice_result == second.voice_result == 'network'
    assert second.timings_ms['synthesis_complete'] < second.timings_ms['playback_lock_acquired']


def test_burst_has_bounded_jobs_threads_and_exact_completion(rig):
    done = Mock()
    first = rig.engine.speak('First synthetic reply', on_done=done)
    assert rig.playing.wait(1)
    second = rig.engine.speak('Second synthetic reply', on_done=done)
    assert rig.fetched_next.wait(1)
    rejected = [rig.engine.speak('Excess synthetic reply', on_done=done) for _ in range(1000)]
    assert len(rig.engine._jobs) == len(rig.workers) == edge._MAX_JOBS == 2
    assert all(h.voice_result == 'cancelled' and h._state == 'done' for h in rejected)
    assert done.call_count == 1000
    rig.release.set()
    for thread in rig.workers:
        thread.join(2)
    assert done.call_count == 1002 and not rig.engine._jobs
    assert first.voice_result == second.voice_result == 'network'


@pytest.mark.parametrize('action', ['cancel', 'cancel_all', 'shutdown', 'revoke'])
def test_prefetched_audio_cannot_play_after_cancel_or_revocation(rig, action):
    allowed = [True]
    rig.engine.set_authorization_checker(lambda: allowed[0])
    rig.engine.speak('First synthetic reply')
    assert rig.playing.wait(1)
    done = Mock()
    second = rig.engine.speak('Second synthetic reply', on_done=done)
    assert rig.fetched_next.wait(1)
    if action == 'cancel':
        rig.engine.cancel(second)
    elif action == 'revoke':
        allowed[0] = False
    else:
        getattr(rig.engine, action)()
    rig.release.set()
    for thread in rig.workers:
        thread.join(2)
    assert second.voice_result == 'cancelled'
    assert rig.played == ['First synthetic reply']
    done.assert_called_once()
    assert not rig.engine._jobs


def test_append_order_survives_workers_starting_in_reverse(rig, monkeypatch):
    queued = []
    monkeypatch.setattr(edge.thread_registry, 'spawn', lambda name, target, **kw: queued.append(target))
    first = rig.engine.speak('First synthetic reply')
    second = rig.engine.speak('Second synthetic reply')
    rig.release.set()
    later = threading.Thread(target=queued[1], daemon=True)
    earlier = threading.Thread(target=queued[0], daemon=True)
    rig.workers.extend([later, earlier])
    later.start()
    earlier.start()
    for thread in rig.workers:
        thread.join(2)
    assert rig.played == ['First synthetic reply', 'Second synthetic reply']
    assert first.voice_result == second.voice_result == 'network'


def test_queue_full_completes_coordinator_pending_count(rig, monkeypatch):
    app = SimpleNamespace(config={'tts': {'enabled': True, 'engine': 'edge',
                                         'feedback_level': 'full'}}, tts_engine=rig.engine,
                          command_mode_active=False)
    router = AudioCoordinator(app, rig.engine)
    monkeypatch.setattr(router, '_on_transition', lambda *a, **kw: None)
    monkeypatch.setattr(router, '_emit_caption', lambda *a, **kw: None)
    done = Mock()
    try:
        router.speak('First synthetic reply', on_done=done)
        assert rig.playing.wait(1)
        router.speak('Second synthetic reply', on_done=done)
        rejected = router.speak('Excess synthetic reply', on_done=done)
        assert rejected.voice_result == 'cancelled' and router._speech_pending == 2
        rig.release.set()
        for thread in rig.workers:
            thread.join(2)
        assert router._speech_pending == 0 and router.get_state() == IDLE
        assert done.call_count == 3
    finally:
        router.shutdown()


def test_stage_records_keep_only_id_category_and_lengths(rig, caplog):
    caplog.set_level(logging.DEBUG, logger=edge.__name__)
    rig.release.set()
    handle = rig.engine.speak('Synthetic privacy sample', category='confirmation')
    for thread in rig.workers:
        thread.join(2)
    assert {'enqueue', 'dequeue', 'synthesis_lock_acquired', 'synthesis_complete',
            'decode_complete', 'playback_lock_acquired', 'first_write'} <= handle.timings_ms.keys()
    records = [r.getMessage() for r in caplog.records if r.name == edge.__name__]
    assert all('Synthetic privacy sample' not in message for message in records)
    assert any('category=confirmation chars=24 stage=enqueue' in message for message in records)


def test_real_synthesis_loop_marks_first_chunk_and_closes_stream(monkeypatch, offline_asyncio):
    stages, closed = [], []
    class Communicate:
        def __init__(self, *args, **kwargs):
            pass

        async def stream(self):
            try:
                yield {'type': 'audio', 'data': b'x' * 4096}
                yield {'type': 'audio', 'data': b'x' * 4096}
            finally:
                closed.append(True)

    def spawn(name, target, **kwargs):
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread
    monkeypatch.setattr(edge.thread_registry, 'spawn', spawn)
    monkeypatch.setitem(sys.modules, 'edge_tts', SimpleNamespace(Communicate=Communicate))
    audio = edge._synthesize_mp3('en-US-AvaNeural', 'Synthetic', '+0%', '+0%', timing=stages.append)
    assert len(audio) == 8192 and closed == [True]
    assert stages == ['websocket_request', 'first_chunk']
