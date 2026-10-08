"""Streaming hold release tail, without importing the desktop app."""
import threading
import time

import numpy as np

from samsara.audio_engine import DictationSessionConsumer, FrameBus, FRAME_SIZE


class _Engine:
    def __init__(self, bus):
        self.bus = bus

    def register_consumer(self, name):
        return self.bus.new_reader(name)


class _App:
    audio_coordinator = None
    _tts_last_speaking = 0.0


def test_streaming_release_keeps_speech_then_quiet_and_preserves_accumulator():
    bus = FrameBus()
    consumer = DictationSessionConsumer(_Engine(bus), _App())
    assert consumer.activate_streaming()
    for _ in range(5):
        bus.write(np.full(FRAME_SIZE, 6000, dtype=np.int16), time.monotonic(), device_epoch=0)
    time.sleep(0.05)
    before = consumer.snapshot_streaming_audio()

    def write_tail():
        for value in (6000, 6000, 6000, 6000, 0, 0, 0):
            time.sleep(0.1)
            bus.write(np.full(FRAME_SIZE, value, dtype=np.int16), time.monotonic(), device_epoch=0)

    writer = threading.Thread(target=write_tail)
    writer.start()
    result = consumer.wait_for_streaming_release_tail(silence_ms=300, max_tail_ms=1200)
    writer.join()
    after = consumer.snapshot_streaming_audio()
    assert result['reason'] == 'silence'
    assert consumer._streaming_thread.is_alive()
    assert len(after) >= len(before) + 7 * FRAME_SIZE
    assert len(consumer.stop_streaming()) == len(after)


def test_streaming_release_quiet_uses_hold_speech_level():
    bus = FrameBus()
    consumer = DictationSessionConsumer(_Engine(bus), _App())
    assert consumer.activate_streaming()
    for _ in range(5):
        bus.write(np.full(FRAME_SIZE, 10000, dtype=np.int16), time.monotonic(), device_epoch=0)
    time.sleep(0.05)

    def background():
        # Roughly 25 dB below speech, but above the fixed 0.008 floor.
        for _ in range(3):
            time.sleep(0.1)
            bus.write(np.full(FRAME_SIZE, 560, dtype=np.int16), time.monotonic(), device_epoch=0)

    writer = threading.Thread(target=background)
    writer.start()
    result = consumer.wait_for_streaming_release_tail(silence_ms=300, max_tail_ms=900)
    writer.join()
    assert result['reason'] == 'silence'
    assert result['threshold'] > 0.008
    assert result['elapsed_ms'] < 450
    consumer.stop_streaming()


def test_streaming_release_loud_caps_and_abort_is_prompt():
    bus = FrameBus()
    consumer = DictationSessionConsumer(_Engine(bus), _App())
    assert consumer.activate_streaming()
    for _ in range(3):
        bus.write(np.full(FRAME_SIZE, 6000, dtype=np.int16), time.monotonic(), device_epoch=0)
    time.sleep(0.05)
    stop = threading.Event()

    def loud():
        while not stop.is_set():
            bus.write(np.full(FRAME_SIZE, 6000, dtype=np.int16), time.monotonic(), device_epoch=0)
            time.sleep(0.1)

    writer = threading.Thread(target=loud)
    writer.start()
    result = consumer.wait_for_streaming_release_tail(silence_ms=200, max_tail_ms=350)
    assert result['reason'] == 'max'
    abort = threading.Event()
    timer = threading.Timer(0.05, abort.set)
    timer.start()
    result = consumer.wait_for_streaming_release_tail(
        silence_ms=200, max_tail_ms=500, should_abort=abort.is_set)
    timer.join()
    stop.set()
    writer.join()
    assert result['reason'] == 'abort'
    assert result['elapsed_ms'] < 100
    consumer.stop_streaming()

import pytest
from types import SimpleNamespace
from unittest.mock import Mock
from tests.test_hold_capture_duck import app


@pytest.mark.parametrize('lane', ['hold', 'command', 'ava', 'memo', 'capslock'])
def test_streaming_stop_tail_order_and_fixed_lanes(app, lane):
    events = []
    app.recording = True
    app._ace_streaming_active = True
    app.live_surface = object()
    app.config['ui'] = {'live_surface': {'enabled': True}}
    app.config['recording_tail_ms'] = 250
    app.command_mode_recording = lane == 'command'
    app.ava_mode_recording = lane == 'ava'
    app._memo_recording = lane == 'memo'
    if lane == 'capslock':
        app.live_surface = None
    app._hotkey_recording = True
    def tail(**kwargs):
        assert app._hotkey_recording
        events.append('tail')
        return dict(reason='silence', elapsed_ms=300, frames=3,
                    threshold=.008, speech_level=.08, last_frame_rms=0.)
    app._dictation_consumer.wait_for_streaming_release_tail = tail
    app._dictation_consumer.finish_capture = lambda **kw: events.append(('cutoff', kw))
    app.play_sound = lambda sound: events.append(('sound', sound, app._hotkey_recording))
    app._streaming_session = SimpleNamespace(finalize=lambda: events.append('finalize'))
    app.ns['time'] = SimpleNamespace(perf_counter_ns=time.perf_counter_ns,
                                    sleep=lambda seconds: events.append(('sleep', seconds)))
    app.stop_impl()
    if lane == 'hold':
        assert events == ['tail', ('cutoff', {'force': True}), ('sound', 'stop', False), 'finalize']
    else:
        assert events == [('sound', 'stop', False), ('sleep', .25), 'finalize']

@pytest.mark.parametrize('ducked', [True, False])
def test_capture_cutoff_is_not_extended_by_cleanup(ducked):
    c = DictationSessionConsumer(_Engine(FrameBus()), _App())
    c._capture_not_before = 1. if ducked else None
    c.finish_capture(force=True)
    cutoff = c._capture_not_after
    c.finish_capture()
    assert c._capture_not_after == cutoff
    assert not c._frame_inside_capture_duck(SimpleNamespace(t_capture=cutoff + .1))

@pytest.mark.parametrize('held_count', [0, 2])
def test_streaming_immediate_quiet_uses_floor_with_fewer_than_three_frames(held_count):
    c = DictationSessionConsumer(_Engine(FrameBus()), _App())
    c._streaming_frames = [np.full(FRAME_SIZE, .5, dtype=np.float32) for _ in range(held_count)]
    def quiet():
        for _ in range(3):
            time.sleep(.1)
            with c._streaming_lock:
                c._streaming_frames.append(np.zeros(FRAME_SIZE, dtype=np.float32))
    writer = threading.Thread(target=quiet)
    writer.start()
    result = c.wait_for_streaming_release_tail()
    writer.join()
    assert result['reason'] == 'silence'
    assert result['speech_level'] == 0
    assert result['threshold'] == .008
    assert result['elapsed_ms'] < 450


def test_batch_tail_log_retains_real_elapsed_and_frame_count(monkeypatch):
    import samsara.audio_engine.dictation_consumer as module
    c = DictationSessionConsumer(_Engine(FrameBus()), _App())
    log = Mock()
    monkeypatch.setattr(module, 'logger', log)
    c.drain = Mock(return_value=None)
    def quiet():
        for seq in range(3):
            time.sleep(.1)
            with c._frames_lock:
                c._frames.append((seq, 0, np.zeros(FRAME_SIZE, dtype=np.int16)))
    writer = threading.Thread(target=quiet)
    writer.start()
    c.drain_after_release()
    writer.join()
    args = log.debug.call_args.args
    assert args[0].startswith('[TAIL] release tail reason=')
    assert args[1] == 'silence'
    assert 200 <= args[2] < 450
    assert args[3] == 3
