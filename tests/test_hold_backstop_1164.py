"""1164: fake hook/time, real stop entry, and one undrained audio take."""

import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import decode_recovery, hold_backstop as hb, hotkeys, mouse_hook as mh
from tests.test_mouse_hold_backstop_1052e import App, fire, slow


@pytest.fixture
def state(monkeypatch):
    from samsara import speech_readiness

    s = SimpleNamespace(now=1000.0, down=True, jobs=[], stops=[], samples=[])
    monkeypatch.setattr(hb, '_now', lambda: s.now)
    monkeypatch.setattr(hb, '_now_ns', lambda: round(s.now * 1e9))
    monkeypatch.setattr(mh, '_perf', lambda: s.now)
    monkeypatch.setattr(mh, '_call_next_hook_ex', lambda *args: 0)
    monkeypatch.setattr(hotkeys.flight_recorder, 'record', Mock())
    monkeypatch.setattr(speech_readiness, 'status', lambda app: '')
    monkeypatch.setattr(decode_recovery, 'supersede_final', Mock())
    monkeypatch.setattr(hotkeys, 'notice_for', lambda *args: SimpleNamespace(
        interrupted=lambda *args: None, recovered=lambda: None))

    def spawn(name, target, args=(), **kwargs):
        if name == 'hold-release-backstop':
            s.jobs.append(target)
        elif name == 'stop-rec' and hasattr(s.app, '_dictation_consumer'):
            s.stops.append(target)
        else:
            target(*args)
        return SimpleNamespace(is_alive=lambda: False)

    def sample(binding):
        s.samples.append(binding)
        if isinstance(s.down, Exception):
            raise s.down
        return s.down

    monkeypatch.setattr(hb.thread_registry, 'spawn', spawn)
    monkeypatch.setattr(hb, 'physical_down', sample)
    s.app = App()
    fire(s.app, mh.WM_XBUTTONDOWN)
    s.monitor = s.app._hold_backstop

    def advance(seconds, *, moving=False):
        end = s.now + seconds
        while s.now < end - 1e-9:
            s.now = min(s.now + hb.POLL_S, end)
            if moving:
                fire(s.app, 0x0200)  # WM_MOUSEMOVE
            s.monitor.tick()

    s.advance = advance
    return s


@pytest.fixture
def capture(state, monkeypatch):
    # The gate supplies the whole class without bootstrapping the application.
    import dictation

    s, app = state, state.app
    s.audio, s.takes, s.end_times = [], [], []
    app.model_rate = 16000
    app._ace_dictation_active = True
    app._ace_streaming_active = False
    app._capture_provenance = (object(), 'dictation_hold')
    app._hold_capture_lifecycle_lock = threading.RLock()
    app.set_app_state = lambda **kw: [setattr(app, key, value) for key, value in kw.items()]
    app._restore_audio = Mock()
    app._close_hold_capture_duck = Mock()
    app._live_surface_hold_parking_enabled = lambda: False
    app._release_icon_chase = app._update_tray_tooltip = Mock()
    app.play_sound = Mock()
    app._finish_empty_hold_capture = Mock()
    app._take_recording_ownership = lambda: dictation.DictationApp._take_recording_ownership(app)
    app._stop_recording_impl = lambda **kw: dictation.DictationApp._stop_recording_impl(app, **kw)
    app.stop_recording = Mock(side_effect=lambda: dictation.DictationApp.stop_recording(app))

    def drain(**kw):
        s.takes.append(tuple(s.audio))
        app.calls.append('stop')
        return np.asarray(s.audio, dtype=np.float32)

    app._dictation_consumer = SimpleNamespace(drain_after_release=Mock(side_effect=drain))
    monkeypatch.setattr(decode_recovery, 'capture_problem', lambda *args: False)

    def capture_end(_coordinator, _start, end):
        s.end_times.append(end)
        return True  # Exit after the real drain, before any model/paste work.

    monkeypatch.setattr(dictation, 'is_self_hear_capture', capture_end)

    def finish():
        pending = app._mouse5_release
        s.now = max(s.now, pending.at + hb.MOUSE5_RELEASE_S + 1e-9)
        while s.stops:
            s.stops.pop(0)()

    s.finish = finish
    return s


@pytest.mark.parametrize('stall_ms', [60, 90, 140])
def test_short_hook_stall_and_false_up_never_stop_held_button(state, stall_ms):
    s = state
    s.monitor.tick()
    s.app._mouse_hook._callback_started_at = s.now
    s.down = False
    s.advance(stall_ms / 1000)
    assert s.app.recording and s.app.calls == ['start']
    s.app._mouse_hook._callback_started_at = None
    s.advance(30, moving=True)
    assert s.app.recording and s.app.calls == ['start']


@pytest.mark.parametrize('gap_ms', [29, 30, 41, 46, 57, 69, 108, 110, 149])
def test_chatter_resumes_one_take_without_losing_gap_audio(capture, caplog, gap_ms):
    s, app = capture, capture.app
    s.audio.extend(range(100))
    provenance = app._capture_provenance
    with caplog.at_level(logging.INFO, logger=hb.logger.name):
        fire(app)
        assert not app.recording and not app._hold_down_mouse and app._stop_in_flight
        s.now += gap_ms / 1000
        s.audio.extend(range(100, 200))
        fire(app, mh.WM_XBUTTONDOWN)
        assert app.recording and app._hold_down_mouse and not app._stop_in_flight
        s.stops.pop(0)()
        assert app.calls == ['start'] and not s.takes
        assert app._capture_provenance is provenance
        app._restore_audio.assert_not_called()
        s.audio.extend(range(200, 300))
        fire(app)
        release_ns = round(s.now * 1e9)
        s.finish()
    assert app.calls == ['start', 'stop'] and not app.recording
    assert s.takes == [tuple(range(300))] and s.end_times == [release_ns]
    app._restore_audio.assert_called_once()
    assert [r.getMessage() for r in caplog.records if 'chatter merged' in r.getMessage()] == [
        f'[MOUSE] chatter merged gap_ms={gap_ms}']


@pytest.mark.parametrize('stalled', [False, True])
def test_real_release_ends_hold_immediately_and_retains_original_stop_time(capture, stalled):
    s, app = capture, capture.app
    if stalled:
        slow(app)
    s.audio.extend(range(100))
    fire(app)
    pending = app._mouse5_release
    assert not app.recording and not app._hold_down_mouse
    assert app._main_hotkey_source == 'key' and len(s.stops) == 1
    s.now += .149
    s.audio.extend(range(100, 150))
    assert not app.recording and not s.takes
    s.finish()
    assert app.stop_recording.call_count == 1
    assert s.takes == [tuple(range(150))] and s.end_times == [pending.at_ns]
    assert not app._stop_in_flight


def test_repeated_release_edges_cannot_extend_stop_deadline(capture):
    s = capture
    fire(s.app)
    pending = s.app._mouse5_release
    s.now += .100
    fire(s.app)
    assert s.app._mouse5_release is pending and len(s.stops) == 1
    s.finish()
    assert s.app.calls == ['start', 'stop']


def test_repress_wakes_an_already_waiting_stop_entry(capture, monkeypatch):
    s, app = capture, capture.app
    fire(app)
    pending = app._mouse5_release
    waits = []

    def wait(timeout):
        waits.append(timeout)
        s.now += .060
        fire(app, mh.WM_XBUTTONDOWN)
        return True

    monkeypatch.setattr(pending.changed, 'wait', wait)
    s.stops.pop(0)()
    assert waits == [pytest.approx(.150)]
    assert app.recording and app.calls == ['start'] and not s.takes


def test_uninterrupted_release_waits_only_remaining_window(capture, monkeypatch):
    s = capture
    fire(s.app)
    pending = s.app._mouse5_release
    s.now += .100
    waits = []

    def wait(timeout):
        waits.append(timeout)
        s.now += timeout + 1e-9
        return False

    monkeypatch.setattr(pending.changed, 'wait', wait)
    s.stops.pop(0)()
    assert waits == [pytest.approx(.050)]
    assert s.takes == [()] and s.end_times == [pending.at_ns]


def test_old_resumed_worker_cannot_retire_a_new_release(capture):
    s, app = capture, capture.app
    fire(app)
    old_stop = s.stops.pop(0)
    s.now += .040
    fire(app, mh.WM_XBUTTONDOWN)
    fire(app)
    latest = app._mouse5_release
    old_stop()
    assert app._stop_in_flight and app._mouse5_release is latest
    assert not s.takes
    s.finish()
    assert app.calls == ['start', 'stop'] and not app._stop_in_flight


def test_late_repress_does_not_resume_or_swallow_distinct_hold(capture):
    s, app = capture, capture.app
    fire(app)
    s.now += .160
    fire(app, mh.WM_XBUTTONDOWN)
    assert app._release_tail_repress == ('mouse', None)
    assert not app._mouse5_release.resumed and not app.recording
    s.finish()
    assert app.calls == ['start', 'stop', 'start'] and app.recording


@pytest.mark.parametrize('full_display', [False, True])
@pytest.mark.parametrize('gap_ms', [None, 30, 110])
def test_streaming_release_and_resume_keep_the_same_session(capture, full_display, gap_ms):
    from samsara.streaming import StreamingSession

    s, app = capture, capture.app
    session = StreamingSession.__new__(StreamingSession)
    session.app = app
    session._state = session.STATE_STREAMING
    session._state_lock = threading.Lock()
    session.stop_event, session.cancel_event = threading.Event(), threading.Event()
    app._streaming_session = session
    app._ace_dictation_active, app._ace_streaming_active = False, True
    app.config['recording_tail_ms'] = 0
    app._live_surface_hold_parking_enabled = lambda: full_display
    app.live_surface = object()
    consumer = app._dictation_consumer
    consumer.wait_for_streaming_release_tail = Mock(return_value={})
    consumer.finish_capture = Mock()
    consumer.stop_streaming = Mock()
    fire(app)
    assert not app.recording and not app._hold_down_mouse
    if gap_ms is not None:
        s.now += gap_ms / 1000
        fire(app, mh.WM_XBUTTONDOWN)
        s.stops.pop(0)()
        assert app.recording and app._streaming_session is session
        assert session._state == session.STATE_STREAMING and not session.stop_event.is_set()
        consumer.finish_capture.assert_not_called()
        consumer.wait_for_streaming_release_tail.assert_not_called()
        fire(app)
    s.finish()
    assert not app.recording and not app._stop_in_flight
    assert app._streaming_session is session and session.stop_event.is_set()
    assert session._state == session.STATE_FINALIZING
    assert consumer.finish_capture.call_count == int(full_display)
    # Only the existing streaming worker may drain its authoritative PCM.
    consumer.stop_streaming.assert_not_called()
    assert app.calls == ['start']


def test_true_lost_hook_recovers_only_after_silence_and_sustained_up(state):
    s = state
    s.monitor.tick()
    s.app._mouse_hook._begin_recovery('synthetic lost hook')
    s.down = False
    s.advance(1.99)
    assert s.app.recording and s.app._main_hotkey_source == 'mouse'
    s.advance(.06)
    assert s.app.calls == ['start', 'stop'] and s.monitor.stop.is_set()
    assert getattr(s.app, '_mouse5_release', None) is None


def test_late_up_still_requires_300ms_and_multiple_samples(state):
    s = state
    s.monitor.tick()
    slow(s.app)
    s.advance(2.1)
    assert s.app.recording
    s.down = False
    s.advance(.299)
    assert s.app.recording
    s.advance(.06)
    assert s.app.calls == ['start', 'stop']


@pytest.mark.parametrize('reset', [True, None, OSError('unavailable')])
def test_down_unknown_or_access_error_restarts_up_confirmation(state, reset):
    s = state
    s.monitor.tick()
    slow(s.app)
    s.advance(2.1)
    s.down = False
    s.advance(.25)
    s.down = reset
    s.advance(.05)
    s.down = False
    s.advance(.299)
    assert s.app.recording
    s.advance(.06)
    assert s.app.calls == ['start', 'stop']


def test_scheduler_gap_cannot_count_as_continuous_up_confirmation(state):
    s = state
    s.monitor.tick()
    slow(s.app)
    s.down = False
    s.advance(.25)
    s.now += 3
    s.monitor.tick()
    assert s.app.recording and s.monitor.up == 1
    s.advance(.299)
    assert s.app.recording
    s.advance(.06)
    assert s.app.calls == ['start', 'stop']


def test_button_that_never_reports_down_is_never_trusted(state):
    s = state
    s.down = False
    slow(s.app)
    s.advance(30)
    assert not s.monitor.down_seen and s.app.recording
    fire(s.app)
    assert s.app.calls == ['start', 'stop']


def test_later_hook_release_disqualifies_poll_across_holds_and_reinstalls(state, caplog):
    s = state
    s.monitor.tick()
    slow(s.app)
    s.down = False
    s.advance(1.5)
    with caplog.at_level(logging.WARNING, logger=hb.logger.name):
        fire(s.app)
    assert s.app._mouse5_poll_unreliable and 'poll disagreed with hook' in caplog.text
    s.now += .2
    s.down = True
    fire(s.app, mh.WM_XBUTTONDOWN)
    s.monitor = s.app._hold_backstop
    s.monitor.tick()
    s.app._mouse_hook._begin_recovery('synthetic reinstall')
    s.down = False
    s.advance(30)
    assert s.app.recording and s.app.calls == ['start', 'stop', 'start']
    fire(s.app)
    assert s.app.calls == ['start', 'stop', 'start', 'stop']


def test_hook_release_after_poll_stop_still_disqualifies_that_button(state):
    s = state
    s.monitor.tick()
    slow(s.app)
    s.down = False
    s.advance(2.1)
    assert not s.app.recording and not getattr(s.app, '_mouse5_poll_unreliable', False)
    s.now += 1.5
    fire(s.app)
    assert s.app._mouse5_poll_unreliable and s.app.calls == ['start', 'stop']


@pytest.mark.parametrize('change', ['shutdown', 'hook', 'mode', 'binding'])
def test_reconfigured_released_capture_is_finalized_without_resume(capture, change):
    s, app = capture, capture.app
    fire(app)
    if change == 'shutdown':
        app._running = False
    elif change == 'hook':
        app._mouse_hook = SimpleNamespace()
    elif change == 'mode':
        app.config['mode'] = 'toggle'
        app.config['recording_tail_ms'] = 0
        app._dictation_consumer.drain = app._dictation_consumer.drain_after_release
    else:
        app.config['hotkey'] = 'mouse4'
    assert not hb.debounce_mouse5(app, True)
    s.finish()
    assert app.calls == ['start', 'stop'] and not app.recording


@pytest.mark.parametrize('change', ['consumer', 'session', 'cancel', 'provenance'])
def test_old_stop_cannot_drain_replaced_or_cancelled_capture(capture, change):
    s, app = capture, capture.app
    fire(app)
    if change == 'consumer':
        app._dictation_consumer = object()
    elif change == 'session':
        app._streaming_session = object()
    elif change == 'cancel':
        app._ace_dictation_active = False
    else:
        app._capture_provenance = object()
    s.finish()
    assert not s.takes and not app._stop_in_flight


def test_only_worker_polls_and_release_never_queries_native_state(state):
    assert not state.samples
    fire(state.app)
    assert not state.samples and state.app.calls == ['start', 'stop']


def test_parked_release_remains_immediate(state):
    state.app._consume_parked_hold_keyup = True
    fire(state.app)
    assert not state.app._consume_parked_hold_keyup and not state.app._hold_down_mouse
    assert not getattr(state.app, '_mouse5_release', None)
    assert state.app.calls == ['start']
