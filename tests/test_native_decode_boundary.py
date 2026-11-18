"""1249 fallback: committed capture boundaries, without hardware or children.

These tests do not certify independent capture or native process termination.
"""
import threading
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara.audio_engine import DictationSessionConsumer, FrameBus, FRAME_SIZE
from samsara.audio_engine.frame import Frame, RING_FRAMES
from samsara.audio_engine import dictation_consumer as consumer_module
from samsara.audio_engine.capture_snapshot import CaptureJournal, spans, fade_capture
from samsara import decode_recovery as decoder, hold_recording_recovery as retention


def setup_consumer(monkeypatch, streaming):
    bus = FrameBus()
    engine = NS(register_consumer=bus.new_reader, _overflow_count=0)
    app = NS(model_rate=16000, config={})
    consumer = DictationSessionConsumer(engine, app)
    app._dictation_consumer = consumer
    monkeypatch.setattr(consumer_module.thread_registry, 'spawn', lambda *a, **k: None)
    assert (consumer.activate_streaming() if streaming else consumer.activate())
    return consumer, engine, bus


def write(bus, value, epoch=0, at=None):
    bus.write(np.full(FRAME_SIZE, value, dtype=np.int16),
              time.perf_counter() if at is None else at, epoch)


def stop(consumer, streaming):
    return consumer.stop_streaming() if streaming else consumer.drain()


@pytest.mark.parametrize('streaming', [False, True])
def test_mixed_epochs_keep_both_ranges_without_native_chunk_stitch(monkeypatch, streaming):
    consumer, engine, bus = setup_consumer(monkeypatch, streaming)
    write(bus, 100, epoch=2, at=10.)
    write(bus, 200, epoch=3, at=20.)
    audio = stop(consumer, streaming)
    np.testing.assert_allclose(audio * 32767, np.repeat([100, 200], FRAME_SIZE), atol=.001)
    snapshot = audio.capture_snapshot
    assert snapshot is consumer.capture_snapshot
    assert [frame.epoch for frame in snapshot.frames] == [2, 3]
    assert [frame.captured_at for frame in snapshot.frames] == [10., 20.]
    assert snapshot.final_sequence == 1
    assert snapshot.losses[0].reason == 'device_epoch'
    assert snapshot.losses[0].missing_samples is None
    assert spans(audio) == [(0, FRAME_SIZE), (FRAME_SIZE, 2 * FRAME_SIZE)]
    chunks = decoder.chunk_audio(audio, 16000, None)
    assert [(c.start, c.end) for c in chunks] == spans(audio)
    # Context must not cross the boundary even during retry or after batch fade.
    assert [(c.start, c.end) for c in decoder.chunk_audio(
        fade_capture(audio, 16000, 50), 16000, None, recovery=True)] == spans(audio)


@pytest.mark.parametrize('streaming', [False, True])
def test_timeout_snapshot_is_immutable_and_reader_remains_owned(monkeypatch, streaming):
    consumer, engine, bus = setup_consumer(monkeypatch, streaming)
    entered, release = threading.Event(), threading.Event()
    original = consumer._reader.read_next
    calls = 0
    def blocked_read():
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(3)
        return original()
    monkeypatch.setattr(type(consumer._reader), 'read_next', lambda self: blocked_read())
    write(bus, 100)
    write(bus, 200)
    monkeypatch.setattr(consumer_module, '_THREAD_JOIN_TIMEOUT_S', .01)
    event = consumer._streaming_stop if streaming else consumer._drain_stop
    operation = consumer._streaming_drain_loop if streaming else consumer._hold_drain_loop
    thread = threading.Thread(target=operation, args=(event,))
    if streaming:
        consumer._streaming_thread = thread
    else:
        consumer._drain_thread = thread
    thread.start()
    try:
        assert entered.wait(1)
        audio = stop(consumer, streaming)
        assert len(audio) == FRAME_SIZE
        snapshot = audio.capture_snapshot
        assert snapshot.drain_incomplete and snapshot.final_sequence == 0
        assert snapshot.losses[-1].reason == 'drain_incomplete'
        assert consumer.capture_loss
        assert not consumer.activate() and not consumer.activate_streaming()
        with pytest.raises(ValueError):
            audio.setflags(write=True)
        with pytest.raises(ValueError):
            audio[0] = .5
        with pytest.raises(FrozenInstanceError):
            snapshot.generation = 999
        saved = audio.tobytes()
        release.set()
        thread.join(1)
        assert not thread.is_alive()
        assert audio.tobytes() == saved and consumer.capture_snapshot is snapshot
        assert stop(consumer, streaming) is None  # No late republishing.
        assert len(snapshot.frames) == 1
        assert consumer.activate_streaming()
        assert consumer._capture_generation == snapshot.generation + 1
    finally:
        release.set()
        thread.join(1)


@pytest.mark.parametrize('streaming', [False, True])
def test_timeout_never_waits_behind_accumulator_lock(monkeypatch, streaming):
    consumer, engine, bus = setup_consumer(monkeypatch, streaming)
    journal = consumer._capture_journal
    frame = Frame(0, 10., np.full(FRAME_SIZE, 42, np.int16), 0)
    assert journal.append(frame, frame.pcm)
    # Simulate a drain stuck while holding its accumulator lock.
    lock = consumer._streaming_lock if streaming else consumer._frames_lock
    class Stalled:
        name = 'synthetic-stalled-drain'
        def join(self, timeout):
            pass
        def is_alive(self):
            return True
    if streaming:
        consumer._streaming_thread = Stalled()
    else:
        consumer._drain_thread = Stalled()
    lock.acquire()
    try:
        started = time.monotonic()
        audio = stop(consumer, streaming)
        assert time.monotonic() - started < .5
        assert len(audio) == FRAME_SIZE
        assert audio.capture_snapshot.drain_incomplete
    finally:
        lock.release()


def test_incomplete_snapshot_flows_into_existing_retry_retention(monkeypatch):
    consumer, engine, bus = setup_consumer(monkeypatch, True)
    frame = Frame(0, 10., np.full(FRAME_SIZE, 42, np.int16), 0)
    journal = consumer._capture_journal
    assert journal.append(frame, frame.pcm)
    audio = journal.read(seal=True, incomplete=True).audio()
    session = NS(app=consumer._app, cancel_event=threading.Event(),
                 _worker=NS(_final_params=lambda: {'language': 'en'}))
    assert retention.capture_stopped(session, audio) is audio
    slot = session.app._hold_retry_audio
    assert slot.reason == 'drain_incomplete'
    assert slot.audio.capture_snapshot is audio.capture_snapshot
    assert session._capture_snapshot_1249.drain_incomplete
    np.testing.assert_array_equal(slot.audio, audio)


@pytest.mark.parametrize('streaming', [False, True])
def test_overrun_and_overflow_receipts_keep_known_and_unknown_loss_separate(monkeypatch, streaming):
    consumer, engine, bus = setup_consumer(monkeypatch, streaming)
    for index in range(RING_FRAMES + 7):
        write(bus, index)
    engine._overflow_count += 1
    audio = stop(consumer, streaming)
    assert len(audio) == RING_FRAMES * FRAME_SIZE
    losses = {loss.reason: loss for loss in audio.capture_snapshot.losses}
    assert losses['sequence_gap'].missing_samples == 7 * FRAME_SIZE
    assert losses['input_overflow'].missing_samples is None
    assert consumer.capture_snapshot.receipt()['frames'][0]['seq'] == 7


def test_first_timestamp_keeps_unknown_start_coverage_and_source_times():
    journal = CaptureJournal(1, 5.)
    for seq, at in enumerate([10., 10.1, 20.]):
        frame = Frame(seq, at, np.zeros(FRAME_SIZE, np.int16), 2)
        assert journal.append(frame, frame.pcm)
    snapshot = journal.read(seal=True)
    assert snapshot.start_coverage == 'unknown'
    assert [f.captured_at for f in snapshot.frames] == [10., 10.1, 20.]
    # Callback-time jumps lack measured backend cadence/ADC acknowledgement;
    # preserve evidence, never invent a missing sample count from wall time.
    assert not snapshot.losses


def test_retry_copies_and_source_slices_preserve_partition_coordinates(monkeypatch):
    consumer, engine, bus = setup_consumer(monkeypatch, True)
    for index, epoch in enumerate([0, 0, 1, 1]):
        write(bus, index + 1, epoch=epoch)
    audio = stop(consumer, True)
    decoder.retain(consumer._app, audio, {}, 'device_epoch')
    copied = consumer._app._hold_retry_audio.audio
    part = copied[FRAME_SIZE:3 * FRAME_SIZE]
    assert spans(part) == [(0, FRAME_SIZE), (FRAME_SIZE, 2 * FRAME_SIZE)]
    assert copied.capture_snapshot is audio.capture_snapshot
    assert [(c.start, c.end) for c in decoder.chunk_audio(part, 16000, None)] == spans(part)


def test_sealing_empty_journal_is_stable_after_late_append():
    journal = CaptureJournal(3, None)
    snapshot = journal.read(seal=True, incomplete=True)
    frame = Frame(1, 10., np.ones(FRAME_SIZE, np.int16), 0)
    assert not journal.append(frame, frame.pcm)
    assert snapshot.audio() is None and snapshot.final_sequence is None
    assert journal.read(seal=True) is snapshot


def test_partial_native_input_starts_after_last_discontinuity(monkeypatch):
    consumer, engine, bus = setup_consumer(monkeypatch, True)
    write(bus, 100, epoch=0)
    write(bus, 200, epoch=1)
    audio = stop(consumer, True)
    calls = []
    def native(app, pcm, **params):
        calls.append(pcm)
        return iter([NS(start=0., end=.1, text=' safe', words=None)]), NS()
    monkeypatch.setattr(decoder, 'transcribe_with_vocabulary', native)
    worker = NS(_session=NS(app=consumer._app), _decoder_guard=lambda *a, **k: False)
    segments, _ = decoder.partial_decode(worker, audio, {})
    assert len(calls) == 1 and len(calls[0]) == FRAME_SIZE
    np.testing.assert_allclose(calls[0] * 32767, 200, atol=.001)
    assert segments[0].start == .1


def test_batch_incomplete_is_retained_before_any_decode_gate(monkeypatch):
    consumer, engine, bus = setup_consumer(monkeypatch, False)
    frame = Frame(0, 10., np.full(FRAME_SIZE, 42, np.int16), 0)
    assert consumer._capture_journal.append(frame, frame.pcm)
    audio = consumer._capture_journal.read(seal=True, incomplete=True).audio()
    assert not decoder.capture_problem(consumer._app, audio)
    slot = consumer._app._hold_retry_audio
    assert slot.reason == 'drain_incomplete'
    assert slot.audio.capture_snapshot is audio.capture_snapshot
