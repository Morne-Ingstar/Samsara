"""631: one hold-start budget, token cleanup and actual cue queue receipts."""
import ast
from pathlib import Path
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import microphone_recovery as recovery, speech_presence as presence
from samsara.audio_engine.dictation_consumer import DictationSessionConsumer
from samsara.runtime import thread_registry


class Clock:
    def __init__(self):
        self.now = 10.0
        self.sleeps = []

    def perf_counter(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


def hold_app(*, starting=False):
    app = NS(config={}, _running=True, _hold_capture_duck_seq=0,
             _hands_free_duck_lock=threading.RLock(),
             _hands_free_capture_ducker=None,
             _hands_free_capture_duck_starting=starting,
             _hands_free_capture_duck_start_generation=0,
             _hands_free_capture_duck_owners=set(),
             _hands_free_duck_restore_timer=None,
             _hands_free_duck_excludes=lambda: set(),
             _bump_wake_gate_freeze=Mock(), _log_duck_result=Mock())

    def close_owner(token):
        app._hands_free_capture_duck_owners.discard(token)

    def close_hold():
        close_owner(app._hold_capture_duck_token)
        app._hold_capture_duck_token = None
        app._hold_capture_duck_confirmed_at = None

    app._close_hands_free_capture_duck = Mock(side_effect=close_owner)
    app._close_hold_capture_duck = Mock(side_effect=close_hold)
    return app


def assert_optional_fallback(app):
    assert app._hold_capture_duck_confirmed_at is None
    assert app._hold_capture_onset.token is None
    # Fallback metadata retains the finish time; it cannot authorize a bound.
    consumer = NS(_app=app)
    assert DictationSessionConsumer._select_capture_boundary(consumer) is None


def inline_worker(monkeypatch, clock, spawn_delay=0):
    waits = []

    class Event:
        set_flag = False

        def set(self):
            self.set_flag = True

        def is_set(self):
            return self.set_flag

        def wait(self, timeout):
            waits.append(timeout)
            if not self.set_flag:
                clock.sleep(timeout)
            return self.set_flag

    monkeypatch.setattr(recovery, 'threading', NS(Event=Event))

    def spawn(name, work, **kwargs):
        assert name == 'hold-duck-open' and kwargs == {'daemon': True}
        clock.sleep(spawn_delay)
        work()

    monkeypatch.setattr(thread_registry, 'spawn', spawn)
    return waits


@pytest.mark.parametrize('spawn_delay,open_delay', [(0, 0), (.08, .04), (.19, 0)])
def test_reused_starting_owner_shares_request_deadline(monkeypatch, spawn_delay, open_delay):
    clock = Clock()
    app = hold_app(starting=True)
    waits = inline_worker(monkeypatch, clock, spawn_delay)

    def reuse(token):
        clock.sleep(open_delay)
        app._hands_free_capture_duck_owners.add(token)
        return token

    app._open_hands_free_capture_duck = reuse
    recorder = NS(record=Mock())
    recovery.open_hold_duck(app, NS(), recorder, clock)
    assert clock.now - 10 == pytest.approx(.2)
    assert waits == pytest.approx([.2 - spawn_delay - open_delay])
    assert app._hold_capture_duck_confirmed_at is None
    assert_optional_fallback(app)
    assert app._hands_free_capture_duck_owners == set()
    app._close_hold_capture_duck.assert_called_once()
    engage = recorder.record.call_args
    assert engage.args == ('hold_capture_duck.engage',)
    assert engage.kwargs['confirmed'] is False
    assert engage.kwargs['elapsed_ms'] == pytest.approx(200)


def test_reused_owner_can_confirm_within_remaining_budget_without_extra_preroll(monkeypatch):
    clock = Clock()
    app = hold_app(starting=True)
    inline_worker(monkeypatch, clock, .08)

    def reuse(token):
        app._hands_free_capture_duck_owners.add(token)
        return token

    app._open_hands_free_capture_duck = reuse
    advance = clock.sleep

    def settle(seconds):
        advance(seconds)
        if clock.now >= 10.18:
            app._hands_free_capture_duck_starting = False
            app._hands_free_capture_ducker = NS(sessions_failed=0, last_error=None, sessions_ducked=0)

    clock.sleep = settle
    recovery.open_hold_duck(app, NS(), NS(record=Mock()), clock)
    assert clock.now - 10 == pytest.approx(.18)
    assert app._hold_capture_duck_confirmed_at == pytest.approx(clock.now)
    assert app._hold_capture_onset.policy == 'strict'
    assert app._hold_capture_onset.boundary == pytest.approx(clock.now)
    app._close_hold_capture_duck.assert_not_called()


@pytest.mark.parametrize('failure', ['none', 'failed', 'exception'])
@pytest.mark.parametrize('media', [False, True])
def test_fresh_duck_preserves_550_fallback_and_onset(monkeypatch, failure, media):
    clock = Clock()
    app = hold_app()
    inline_worker(monkeypatch, clock)

    class Ducker:
        sessions_failed = int(failure == 'failed')
        sessions_ducked = int(media)
        last_error = None

        def __init__(self, **kwargs):
            pass

        def start(self):
            clock.sleep(.025)
            if failure == 'exception':
                raise OSError('synthetic volume failure')

        def stop(self):
            pass

    recovery.open_hold_duck(app, NS(SessionDucker=Ducker), NS(record=Mock()), clock)
    assert clock.now - 10 == pytest.approx(.025)
    if failure == 'none':
        assert app._hold_capture_duck_confirmed_at == pytest.approx(10.025)
        assert app._hold_capture_onset.boundary == pytest.approx(10.025 if media else 9.75)
        app._close_hold_capture_duck.assert_not_called()
    else:
        assert app._hold_capture_duck_confirmed_at is None
        assert_optional_fallback(app)
        assert app._hands_free_capture_duck_owners == set()
        app._close_hold_capture_duck.assert_called_once()


def test_slow_worker_late_completion_cleans_only_its_owner(monkeypatch):
    clock = Clock()
    app = hold_app()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_event = threading.Event
    waits = []

    class DeadlineEvent:
        def __init__(self):
            self.inner = real_event()

        def set(self):
            self.inner.set()

        def is_set(self):
            return self.inner.is_set()

        def wait(self, timeout):
            assert started.wait(1)
            waits.append(timeout)
            clock.sleep(timeout)
            return False

    events = iter([DeadlineEvent(), real_event()])
    monkeypatch.setattr(recovery, 'threading', NS(Event=lambda: next(events)))

    class SlowDucker:
        sessions_failed = 0
        sessions_ducked = 0
        last_error = None

        def __init__(self, **kwargs):
            pass

        def start(self):
            started.set()
            assert release.wait(2)

        def stop(self):
            pass

    real_spawn = thread_registry.spawn
    workers = []

    def spawn(name, work, **kwargs):
        def run():
            try:
                work()
            finally:
                finished.set()
        workers.append(real_spawn(name, run, **kwargs))

    monkeypatch.setattr(thread_registry, 'spawn', spawn)
    try:
        recovery.open_hold_duck(app, NS(SessionDucker=SlowDucker), NS(record=Mock()), clock)
        assert waits == pytest.approx([.2])
        assert clock.now - 10 == pytest.approx(.2)
        assert app._hold_capture_duck_confirmed_at is None
        assert_optional_fallback(app)
        app._hands_free_capture_duck_owners.add(-2)
        app._hold_capture_duck_token = -2
    finally:
        release.set()
        assert finished.wait(2)
        for worker in workers:
            worker.join(1)
    app._close_hands_free_capture_duck.assert_any_call(-1)
    assert app._hands_free_capture_duck_owners == {-2}
    assert app._hold_capture_duck_token == -2
    assert app._hold_capture_duck_confirmed_at is None
    assert_optional_fallback(app)


@pytest.mark.parametrize('cue', ['disabled', 'muted', 'missing', 'dropped', 'audible', 'not_requested'])
def test_only_queued_audible_start_waits(monkeypatch, cue):
    clock = Clock()
    monkeypatch.setattr(presence, 'perf_counter', clock.perf_counter)
    monkeypatch.setattr(presence, 'sleep', clock.sleep)
    sound = np.zeros(1920)
    app = NS(config={'audio_feedback': cue != 'disabled',
                     'sound_volume': 0 if cue == 'muted' else .5},
             _hotkey_recording=True, _sound_stream_sr=16000,
             _sound_cache={} if cue == 'missing' else {'start': sound})

    def play(*args, **kwargs):
        if cue == 'audible':
            clock.sleep(.03)  # Queue time includes output setup.
            presence.note_hold_earcon(app, 'start', sound)

    app.play_sound = Mock(side_effect=play)
    presence.begin_hold(app)
    presence.play_hold_start(app, cue != 'not_requested')
    context = presence.capture_context(app)
    if cue == 'audible':
        assert clock.sleeps == [.03, .15]
        assert app._last_recording_earcon_ms == 120
        assert context.earcon_end_at == pytest.approx(10.15)
        # A subsequent dropped cue must not reuse the old receipt.
        app.play_sound.side_effect = None
        presence.begin_hold(app)
        presence.play_hold_start(app, True)
        assert clock.sleeps == [.03, .15]
        assert presence.capture_context(app).earcon_end_at is None
        assert app._last_recording_earcon_ms == 0
    else:
        assert clock.sleeps == []
        assert context.earcon_end_at is None
        assert app._last_recording_earcon_ms == 0


@pytest.mark.parametrize('output_ready,rescanning', [(True, False), (False, False), (True, True)])
def test_real_playback_queue_and_output_drops(monkeypatch, output_ready, rescanning):
    # Execute only the original method definition, never the desktop bootstrap.
    source = Path(__file__).resolve().parents[1] / 'dictation.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'play_sound')
    timing = NS(feedback_started=Mock(), feedback_finished=Mock())
    scope = dict(speech_presence=presence, np=np, time=time, current_stage_timing=lambda: timing)
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<631-play>', 'exec'), scope)
    clock = Clock()
    monkeypatch.setattr(presence, 'perf_counter', clock.perf_counter)
    monkeypatch.setattr(presence, 'sleep', clock.sleep)
    monkeypatch.setattr(recovery, 'cue_output_ready', lambda app: output_ready)
    monkeypatch.setattr(recovery, 'discard_cue', Mock())
    lock = threading.Lock()
    app = NS(config={}, _hotkey_recording=True, _sound_cache={'start': np.zeros(1920)},
             _sound_stream_sr=16000, _sound_stream_device=7,
             _resolve_playback_output=Mock(return_value=7),
             _audio_refresh_lock=lock, _buffer_lock=threading.Lock())
    app.play_sound = lambda *args, **kwargs: scope['play_sound'](app, *args, **kwargs)
    presence.begin_hold(app)
    if rescanning:
        lock.acquire()
    try:
        presence.play_hold_start(app, True)
    finally:
        if rescanning:
            lock.release()
    audible = output_ready and not rescanning
    assert clock.sleeps == ([.15] if audible else [])
    assert app._last_recording_earcon_ms == (120 if audible else 0)
    assert (presence.capture_context(app).earcon_end_at is not None) == audible
    if rescanning:
        app._resolve_playback_output.assert_not_called()
