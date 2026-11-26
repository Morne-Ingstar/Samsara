"""Hold faults use the capture timeline through the real consumer and notice."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from samsara import hold_diagnostics, listening_display, mic_silence_watch, speech_presence
from samsara.audio_engine.dictation_consumer import DictationSessionConsumer, thread_registry
from samsara.audio_engine.frame import FRAME_SIZE
from samsara.live_surface import feedback
from tests.test_mic_silence_watch_1252 import Engine


@pytest.fixture
def hold(monkeypatch):
    qt = QApplication.instance() or QApplication([])
    app = NS(config={'mode': 'hold'}, recording=False)
    engine = app._ace_engine = Engine()
    consumer = DictationSessionConsumer(engine, app)
    clock = NS(now=100.)
    # Use distinct origins for the ACE/TTS clock and the legacy audit clock.
    monkeypatch.setattr(hold_diagnostics.time, 'perf_counter', lambda: clock.now)
    monkeypatch.setattr(speech_presence, 'perf_counter', lambda: clock.now)
    monkeypatch.setattr(hold_diagnostics.time, 'monotonic', lambda: clock.now + 700.)
    monkeypatch.setattr(hold_diagnostics, '_active', None)
    # Keep the ring unread until the real release drain, with no scheduling race.
    monkeypatch.setattr(thread_registry, 'spawn', lambda *args, **kwargs: None)
    monkeypatch.setattr(feedback, 'schedule_feedback', Mock())
    app._schedule_ui = lambda fn, *args: fn(*args)
    try:
        yield app, consumer, engine.bus, clock
    finally:
        consumer.deactivate()
        window = getattr(app, '_mic_silence_notice_1252', None)
        if window is not None:
            window.close()
        qt.processEvents()


def queue_cue(app, duration):
    app._playback_buffer = np.ones(16, np.float32)
    app._audio_output_idle_after = hold_diagnostics.time.monotonic() + duration + .25
    hold_diagnostics.cue(app, duration)


def assert_visible_notice(app):
    window = getattr(app, '_mic_silence_notice_1252', None)
    assert window is not None and window.isVisible()
    assert window.windowTitle() == 'No sound from your microphone'
    assert window.message.text().splitlines()[0] == 'No sound from your microphone'


@pytest.mark.parametrize('output', ['release-cue', 'overlapping-cue', 'finished-tts', 'late-tts'])
def test_full_streaming_release_uses_admitted_pcm_overlap(hold, output):
    app, consumer, bus, clock = hold
    app.config['ui'] = {'listening_display': 'full', 'live_surface': {'enabled': True}}
    assert listening_display.run_display(app).full
    hold_diagnostics.begin(app)
    speech_presence.begin_hold(app)
    assert consumer.activate_streaming()
    app.recording = True
    clock.now = 100.25
    if output == 'overlapping-cue':
        queue_cue(app, .4)
    if output == 'finished-tts':
        app.audio_coordinator = NS(is_speaking=False, speech_windows=lambda: (
            NS(start_ns=100_200_000_000, end_ns=100_450_000_000),))
    zero = np.zeros(FRAME_SIZE, dtype=np.int16)
    bus.write(zero, 100.375, 0)
    clock.now = 100.5
    app.recording = False
    consumer.wait_for_streaming_release_tail(silence_ms=0, max_tail_ms=0)
    consumer.finish_capture(force=True)
    # Full queues the release sound before its worker can drain the consumer.
    queue_cue(app, .4)
    if output == 'late-tts':
        app.audio_coordinator = NS(is_speaking=True, speech_windows=lambda: (
            NS(start_ns=100_500_000_000, end_ns=101_000_000_000),))
    clock.now = 100.625
    bus.write(np.full_like(zero, 1000), clock.now, 0)
    audio = consumer.stop_streaming()
    assert len(audio) == FRAME_SIZE and not audio.any()
    assert audio.capture_snapshot.final_sequence == 0
    assert mic_silence_watch.excluded(app, hold=True)  # Drain still sees output busy.
    if output in {'overlapping-cue', 'finished-tts'}:
        assert not hasattr(app, '_mic_silence_notice_1252')
    else:
        assert_visible_notice(app)


def test_compact_batch_hold_shows_the_same_notice(hold, monkeypatch):
    app, consumer, bus, clock = hold
    app.config['ui'] = {'listening_display': 'compact', 'live_surface': {'enabled': False}}
    assert not listening_display.run_display(app).full
    hold_diagnostics.begin(app)
    speech_presence.begin_hold(app)
    assert consumer.activate()
    clock.now = 100.25
    bus.write(np.zeros(FRAME_SIZE, dtype=np.int16), clock.now, 0)
    clock.now = 100.5

    def advance_tail_clock(seconds):
        clock.now += seconds

    monkeypatch.setattr(hold_diagnostics.time, 'sleep', advance_tail_clock)
    audio = consumer.drain_after_release(max_tail_ms=100)
    assert len(audio) == FRAME_SIZE and not audio.any()
    assert_visible_notice(app)
