"""Synthetic levels/PCM only; no dictation bootstrap or hardware access."""
import json
import logging
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara import hold_diagnostics as diagnostics
from samsara import mic_silence_watch as silence
from samsara.audio_engine.frame import FRAME_SIZE
from samsara.audio_engine.ring import FrameBus


def rig(**values):
    app = NS(config={}, continuous_active=True, wake_word_active=False, **values)
    events = []
    watch = silence.SilenceWatch(app, lambda *args: events.append(args))
    app._mic_silence_watch_1252 = watch
    return app, watch, events


def evidence(seconds=5., intervals=()):
    return NS(stopped=100., intervals=list(intervals))


def test_synthetic_floor_qualification():
    rng = np.random.default_rng(1252)
    dead = rng.normal(size=(50, FRAME_SIZE))
    quiet = rng.normal(size=(50, FRAME_SIZE))
    dead *= .0001 / np.sqrt(np.mean(dead * dead))
    quiet *= .0005 / np.sqrt(np.mean(quiet * quiet))
    dead_levels = np.sqrt(np.mean(dead * dead, axis=1))
    quiet_levels = np.sqrt(np.mean(quiet * quiet, axis=1))
    assert max(dead_levels) < silence.DEAD_RMS < min(quiet_levels)
    _, watch, events = rig()
    watch.hold(quiet_levels, evidence())
    assert events == []
    watch.hold(dead_levels, evidence())
    assert events == [(True, 'System default microphone')]


@pytest.mark.parametrize('level', [0., .0001, .00014])
@pytest.mark.parametrize('count', [1, 51])
def test_dead_hold_notice_immediate_and_once(level, count, caplog):
    app, watch, events = rig()
    app._ace_engine = NS(capture_name='Synthetic interface')
    with caplog.at_level(logging.INFO, logger='Samsara'):
        watch.hold([level] * count, evidence())
        watch.hold([level] * count, evidence())
    assert events == [(True, 'Synthetic interface')]
    assert caplog.text.count('[MIC_SILENT]') == 1
    assert 'max_rms=' in caplog.text and 'floor=' in caplog.text
    assert 'Synthetic interface' not in caplog.text


@pytest.mark.parametrize('levels', [[.0005] * 50, [.02] + [.0001] * 49,
                                    [.0001] * 49 + [.001], [],
                                    [float('nan')] * 50, [float('inf')] * 50])
def test_quiet_real_short_or_unknown_hold_never_warns(levels):
    _, watch, events = rig()
    watch.hold(levels, evidence())
    assert events == []


def test_ten_seconds_listening_once_then_one_recovery_and_rearm(caplog, monkeypatch):
    app, watch, events = rig()
    engine = app._ace_engine = Engine()
    clock = NS(now=0.)
    monkeypatch.setattr(silence.time, 'perf_counter', lambda: clock.now)
    watch.poll()
    with caplog.at_level(logging.INFO, logger='Samsara'):
        write(engine, 0.)
        watch.poll()
        # 1338: ten seconds of stalled callbacks, rather than quiet PCM.
        clock.now = 9.9
        watch.poll()
        assert events == []
        for stamp in (10., 20., 30.):
            clock.now = stamp
            watch.poll()
        assert len(events) == 1
        clock.now = 31.
        write(engine, clock.now, value=32)
        watch.poll()
        assert [e[0] for e in events] == [True, False]
        clock.now = 41.
        watch.poll()
    assert [e[0] for e in events] == [True, False, True]
    assert caplog.text.count('[MIC_SILENT]') == 3


@pytest.mark.parametrize('boundary', ['gap', 'epoch', 'skip', 'inactive'])
def test_listening_requires_contiguous_eligible_samples(boundary, monkeypatch):
    app, watch, events = rig()
    engine = app._ace_engine = Engine()
    clock = NS(now=0.)
    monkeypatch.setattr(silence.time, 'perf_counter', lambda: clock.now)
    watch.poll()
    feed(engine, watch, clock, 99)
    epoch = 0
    # 1338: preserve gap, endpoint, exclusion, and disabled-listening coverage
    # using the same ring path as production.
    if boundary == 'gap':
        clock.now += 5.
    elif boundary == 'epoch':
        epoch = 1
    elif boundary == 'skip':
        app.audio_coordinator = NS(is_speaking=True)
        feed(engine, watch, clock, 1)
        app.audio_coordinator.is_speaking = False
    else:
        app.continuous_active = False
        feed(engine, watch, clock, 1)
        app.continuous_active = True
    feed(engine, watch, clock, 99, epoch=epoch)
    assert events == []


@pytest.mark.parametrize('state', [dict(audio_coordinator=NS(is_speaking=True)),
                                  dict(_hands_free_capture_duck_owners={1}),
                                  dict(_playback_buffer=np.ones(10)),
                                  dict(_tts_last_speaking=99.9)])
def test_hold_tts_and_samsara_muted_windows_excluded(state, monkeypatch):
    monkeypatch.setattr(silence.time, 'monotonic', lambda: 100.)
    _, watch, events = rig(**state)
    watch.hold([.0001] * 50, evidence())
    assert events == []


def test_hold_cue_duck_intervals_excluded_but_normal_output_duck_not_whole_take():
    _, watch, events = rig(_hold_capture_duck_token=-1,
                           _hands_free_capture_duck_owners={-1})
    watch.hold([.0001] * 50, evidence(intervals=[(95., 100.)]))
    assert events == []
    watch.hold([.0001] * 50, evidence(intervals=[(95., 95.2)]))
    assert len(events) == 1  # Remaining dead samples independently prove the input.


@pytest.mark.parametrize('loss', [dict(capture_loss=True), dict(dropped_frames=1)])
def test_incomplete_hold_cannot_prove_whole_take_dead(loss):
    app, _, events = rig()
    silence.observe_hold(NS(_app=app, **loss), [.0001] * 50, evidence())
    assert events == []


class Engine:
    def __init__(self, name='Synthetic interface'):
        self.bus = FrameBus()
        self._running = True
        self.capture_name = name
        self.registered, self.closed = [], []

    def register_consumer(self, name):
        reader = self.bus.new_reader(name)
        self.registered.append(reader)
        return reader

    def unregister_consumer(self, reader):
        self.closed.append(reader)
        reader.invalidate()


def write(engine, stamp, value=3, epoch=0):
    engine.bus.write(np.full(FRAME_SIZE, value, np.int16), stamp, epoch)


def feed(engine, watch, clock, count, *, value=3, epoch=0):
    start = clock.now
    for index in range(1, count + 1):
        clock.now = start + index / 10
        write(engine, clock.now, value=value, epoch=epoch)
        if index % 5 == 0 or index == count:
            watch.poll()


@pytest.mark.parametrize('mode', ['continuous_active', 'wake_word_active'])
def test_real_ring_seam_no_missing_samples_as_silence_and_recovery(mode, monkeypatch):
    app, watch, events = rig()
    app.continuous_active = False
    setattr(app, mode, True)
    engine = app._ace_engine = Engine()
    clock = NS(now=0.)
    monkeypatch.setattr(silence.time, 'perf_counter', lambda: clock.now)
    silence.poll(app)
    # Empty ring, however much time passes, never means dead samples.
    clock.now = 20.
    silence.poll(app)
    assert events == []
    feed(engine, watch, clock, 100)
    # 1338: fresh low-level samples can be ordinary quiet. Keep testing that
    # the real reader can clear a warning once audio returns after a hold.
    assert events == []
    watch.hold([.0001], evidence())
    assert events == [(True, 'Synthetic interface')]
    clock.now += .1
    write(engine, clock.now, 32)
    silence.poll(app)
    assert [x[0] for x in events] == [True, False]
    silence.close(app)
    assert engine.closed == engine.registered


@pytest.mark.parametrize('state', [dict(recording=True), dict(_hotkey_recording=True),
                                  dict(_hands_free_capture_duck_owners={1}),
                                  dict(audio_coordinator=NS(is_speaking=True))])
def test_ring_holds_duck_and_tts_windows_do_not_warn(state, monkeypatch):
    app, watch, events = rig(**state)
    engine = app._ace_engine = Engine()
    clock = NS(now=0.)
    monkeypatch.setattr(silence.time, 'perf_counter', lambda: clock.now)
    watch.poll()
    for i in range(110):
        clock.now = (i + 1) * .1
        write(engine, clock.now)
        watch.poll()
    assert events == []


def test_stale_ring_backlog_and_overrun_cannot_warn(monkeypatch):
    app, watch, events = rig()
    engine = app._ace_engine = Engine()
    monkeypatch.setattr(silence.time, 'perf_counter', lambda: 100.)
    watch.poll()
    for i in range(700):
        write(engine, i * .1)
    for _ in range(10):
        watch.poll()
    assert events == [] and watch.last_frame_at is None  # 1338: stale frames cannot arm liveness.


def test_watcher_poll_exception_cannot_change_endpoint_recovery_or_cleanup(monkeypatch):
    import sys
    from unittest.mock import Mock
    from samsara import microphone_recovery
    calls = []
    changes = NS(notify=Mock(), prime=lambda: calls.append('prime'),
                 tick=lambda: calls.append('tick'), warn_once=Mock())
    waits = iter([False, True])
    app = NS(_output_watcher_stop=NS(wait=lambda _: next(waits)),
             _microphone_changes=changes)
    monkeypatch.setitem(sys.modules, 'comtypes', NS(
        CoInitializeEx=lambda: calls.append('com-init'),
        CoUninitialize=lambda: calls.append('com-close')))
    monkeypatch.setitem(sys.modules, 'comtypes.client', NS(PumpEvents=lambda _: None))
    monkeypatch.setattr(microphone_recovery, 'register_notifications',
                        lambda _: lambda: calls.append('endpoint-close'))
    def poll_failure(_):
        calls.append('observe')
        raise RuntimeError('synthetic optional observation failure')
    monkeypatch.setattr(silence, 'poll', poll_failure)
    monkeypatch.setattr(silence, 'close', lambda _: calls.append('observer-close'))
    microphone_recovery.watch(app, lambda: 'output')
    assert calls == ['com-init', 'prime', 'observe', 'tick',
                     'observer-close', 'endpoint-close', 'com-close']


@pytest.mark.parametrize('mode', ['streaming', 'batch'])
def test_finished_hold_outputs_byte_identical_with_absent_working_or_broken_observer(mode, monkeypatch, caplog):
    from samsara.audio_engine import hold_capture
    from samsara.audio_engine.dictation_consumer import DictationSessionConsumer
    # 1252: the pair gate enables DEBUG; exercise every required audit field.
    caplog.set_level(logging.DEBUG, logger='Samsara.samsara.audio_engine.dictation_consumer')
    monkeypatch.setattr(diagnostics.time, 'monotonic', lambda: 100.)
    app, watch, events = rig()
    results = []
    observer = silence.observe_hold
    def broken(*args):
        raise RuntimeError('synthetic observer failure')
    for observe in (None, observer, broken):
        if observe is None:
            monkeypatch.delattr(silence, 'observe_hold')
        else:
            monkeypatch.setattr(silence, 'observe_hold', observe, raising=False)
        engine = Engine()
        consumer = DictationSessionConsumer(engine, app)
        consumer._hold_evidence_1121 = diagnostics.Evidence(hold_seq_id='fixed-1252')
        # 1252 / 1249: use the real ring and publication setup, not private
        # accumulator lists that bypass the generation-owned capture receipt.
        consumer._speech_presence_capture = None
        hold_capture.begin(consumer)
        frames = [np.full(FRAME_SIZE, 3, np.int16) for _ in range(50)]
        original = b''.join(x.tobytes() for x in frames)
        before = b''.join((x.astype(np.float32) / 32767.).tobytes() for x in frames)
        for i, pcm in enumerate(frames):
            engine.bus.write(pcm, 95. + (i + 1) * .1, 0)
        consumer._streaming_tail_audit = dict(release_frame=49, threshold=.005,
                                              released_at=99.9, reason='silence', frames=1)
        if mode == 'batch':
            audio = consumer.drain()
        else:
            consumer._streaming_capturing = True
            audio = consumer.stop_streaming()
        receipt = audio.capture_snapshot.receipt()
        assert receipt['sample_count'] == 50 * FRAME_SIZE
        assert receipt['final_sequence'] == 49 and len(receipt['frames']) == 50
        assert not receipt['drain_incomplete'] and receipt['losses'] == []
        results.append((audio.tobytes(), json.dumps(
            diagnostics.get(consumer).snapshot(), sort_keys=True).encode(),
            json.dumps(receipt, sort_keys=True).encode()))
        assert audio.tobytes() == before
        assert b''.join(x.tobytes() for x in frames) == original
        consumer.deactivate()
    assert results[0] == results[1] == results[2]
    assert len(events) == 1  # Working observer was actually reached.
    assert caplog.text.count('[TAIL_CAPTURE]') == 3


def test_actual_notice_action_dismiss_recovery_and_stale_dispatch(monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QPushButton
    from samsara.live_surface import feedback
    from samsara.ui import mic_check_qt
    qt = QApplication.instance() or QApplication([])
    app, watch, _ = rig()
    app._schedule_ui = lambda fn, *args: fn(*args)
    shown, cleared, checked = [], [], []
    monkeypatch.setattr(feedback, 'schedule_feedback', lambda *a, **kw: shown.append((a, kw)))
    monkeypatch.setattr(feedback, 'schedule_owner_clear', lambda *a: cleared.append(a))
    monkeypatch.setattr(mic_check_qt, 'open_mic_check', lambda owner: checked.append(owner))
    watch.active = True
    silence.present(app, True, 'Synthetic interface')
    window = app._mic_silence_notice_1252
    try:
        assert window.isVisible() and not window.isModal()
        assert window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert silence.TITLE in window.message.text()
        assert 'Synthetic interface' in window.message.text()
        for word in ('cable', 'interface', 'mute', 'Windows input level', 'another microphone'):
            assert word in window.message.text()
        buttons = {b.text(): b for b in window.findChildren(QPushButton)}
        buttons['Check my mic'].click()
        assert checked == [app]
        buttons['Dismiss'].click()
        assert not window.isVisible() and watch.active
        watch.active = False
        silence.present(app, False, 'Synthetic interface')
        assert shown[-1][0][1] == silence.RECOVERED
        assert cleared[-1][1] == 'capture_mic_silent'
        count = len(shown)
        silence.present(app, True, 'Synthetic interface')
        assert len(shown) == count and not window.isVisible()
    finally:
        window.close()
        qt.processEvents()
