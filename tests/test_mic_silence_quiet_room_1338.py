"""Exercise real capture-ring observation without opening a microphone."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import mic_silence_watch as silence, microphone_recovery
from samsara.audio_engine.frame import FRAME_SIZE
from tests.test_mic_silence_watch_1252 import Engine, evidence, feed, rig, write


@pytest.fixture
def stream(monkeypatch):
    app, watch, events = rig()
    engine = app._ace_engine = Engine()
    clock = NS(now=100.)
    monkeypatch.setattr(silence.time, 'perf_counter', lambda: clock.now)
    watch.poll()
    return app, watch, events, engine, clock


@pytest.mark.parametrize('mode', ['continuous_active', 'wake_word_active'])
@pytest.mark.parametrize('quiet_peak', [0, 3], ids=['digital-zeros', 'below-floor-noise'])
def test_ten_minutes_quiet_keeps_normal_listening_feedback(mode, quiet_peak, stream):
    app, watch, _, engine, clock = stream
    app.continuous_active = False
    setattr(app, mode, True)
    app.config['notifications_level'] = 'all'
    app.tray_icon = Mock()
    app.listening_indicator = Mock()
    app._schedule_ui = Mock()
    watch.deliver = None  # Production feedback path for Compact and Full.
    feed(engine, watch, clock, 5, value=1000)
    quiet = np.random.default_rng(1338).integers(
        -quiet_peak, quiet_peak + 1, size=FRAME_SIZE, dtype=np.int16)
    feed(engine, watch, clock, 6000, value=quiet)
    assert not watch.active
    assert not hasattr(app, '_mic_silence_notice_1252')
    app._schedule_ui.assert_not_called()
    assert app.listening_indicator.mock_calls == app.tray_icon.mock_calls == []


@pytest.mark.parametrize('mode', ['continuous_active', 'wake_word_active'])
def test_callbacks_that_stop_warn_at_ten_seconds_even_after_zeros(mode, stream):
    app, watch, events, engine, clock = stream
    app.continuous_active = False
    setattr(app, mode, True)
    feed(engine, watch, clock, 10, value=0)
    for _ in range(19):
        clock.now += .5
        watch.poll()
    assert events == []
    clock.now += .5
    watch.poll()
    assert events == [(True, engine.capture_name)]
    feed(engine, watch, clock, 10, value=0)
    assert events == [(True, engine.capture_name), (False, engine.capture_name)]


def test_open_stream_that_never_delivers_frames_warns_after_grace(stream):
    _, watch, events, engine, clock = stream
    engine.rescan_stream = object()
    watch.poll()
    clock.now += 9.5
    watch.poll()
    assert events == []
    clock.now += .5
    watch.poll()
    assert events == [(True, engine.capture_name)]


def test_windows_removal_warns_immediately_even_when_frames_still_arrive(stream):
    app, watch, events, engine, clock = stream
    app._no_microphone = False
    selected = ('test-id', engine.capture_name)
    other = ('other-id', 'Other microphone')
    endpoints = {selected, other}
    changes = microphone_recovery.DeviceChanges(app, endpoints=lambda: endpoints)
    changes._observe()
    endpoints = {selected}
    changes._observe()
    feed(engine, watch, clock, 1, value=0)
    assert events == []

    endpoints = {other}
    changes._observe()
    feed(engine, watch, clock, 1, value=1000)
    assert events == [(True, engine.capture_name)]
    feed(engine, watch, clock, 10, value=1000)
    assert len(events) == 1  # Frames from the removed endpoint cannot clear the warning.

    endpoints = {selected, other}
    changes._observe()
    feed(engine, watch, clock, 1, value=1000, epoch=1)
    assert events[-1] == (False, engine.capture_name)


def test_native_stream_loss_warns_without_waiting_for_the_stall_deadline(stream):
    _, watch, events, engine, clock = stream
    feed(engine, watch, clock, 1)
    engine.device_lost = True
    watch.poll()
    assert events == [(True, engine.capture_name)]


def test_silence_after_speech_warns_only_on_a_zero_audio_hold(stream):
    app, watch, events, engine, clock = stream
    feed(engine, watch, clock, 5, value=1000)
    feed(engine, watch, clock, 150, value=0)
    assert events == []
    silence.observe_hold(NS(_app=app), [0.] * 5, evidence())
    assert events == [(True, engine.capture_name)]


@pytest.mark.parametrize('fault', ['hold', 'stall'])
def test_dismiss_clears_outcome_and_stays_dismissed_until_recovery(fault, stream, monkeypatch):
    from PySide6.QtWidgets import QApplication, QPushButton
    from samsara.live_surface import feedback

    qt = QApplication.instance() or QApplication([])
    app, watch, _, engine, clock = stream
    app._schedule_ui = lambda fn, *args: fn(*args)
    show, clear = Mock(), Mock()
    monkeypatch.setattr(feedback, 'schedule_feedback', show)
    monkeypatch.setattr(feedback, 'schedule_owner_clear', clear)
    watch.deliver = None
    feed(engine, watch, clock, 5, value=1000)

    def fail_input():
        if fault == 'hold':
            feed(engine, watch, clock, 1, value=0)
            watch.hold([0.] * 5, evidence())
        else:
            clock.now += 10.
            watch.poll()

    fail_input()
    window = app._mic_silence_notice_1252
    try:
        assert window.isVisible()
        next(b for b in window.findChildren(QPushButton) if b.text() == 'Dismiss').click()
        assert not window.isVisible()
        clear.assert_called_once_with(app, 'capture_mic_silent')
        for _ in range(3):
            fail_input()
            assert not window.isVisible()
        show.assert_called_once()

        feed(engine, watch, clock, 5, value=1000)
        assert not window.isVisible()
        assert show.call_args.args[1] == silence.RECOVERED
        fail_input()
        assert window.isVisible()
        assert show.call_count == 3
    finally:
        window.close()
        qt.processEvents()


def test_stale_backlog_cannot_trigger_a_stall_while_fresh_callbacks_continue(stream):
    _, watch, events, engine, clock = stream
    feed(engine, watch, clock, 1)
    start = clock.now
    for index in range(1, 301):
        clock.now = start + index / 10
        write(engine, clock.now, value=0)
    for _ in range(16):
        watch.poll()
    assert events == []
    assert watch.last_frame_at == clock.now
