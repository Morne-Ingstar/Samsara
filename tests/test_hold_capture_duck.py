"""Bind production methods to fake apps; never import the desktop bootstrap."""
import ast
import collections
from pathlib import Path
import sys
import threading
import time
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.audio_engine.dictation_consumer import DictationSessionConsumer
from samsara.audio_engine.frame import FRAME_MS, FRAME_SIZE, PREBUFFER_FRAMES
from samsara.audio_engine.ring import EMPTY
from samsara.constants import HOLD_RELEASE_TAIL_SPEECH_THRESHOLD


ROOT = Path(__file__).resolve().parents[1]
METHODS = (
    'start_recording', '_start_recording_impl', '_open_hold_capture_duck',
    '_close_hold_capture_duck', 'stop_recording', 'cancel_recording',
    '_cancel_recording_impl', '_open_hands_free_capture_duck',
    '_close_hands_free_capture_duck', '_restore_hands_free_capture_duck_now',
    '_take_recording_ownership', '_stop_recording_impl',
    '_live_surface_hold_parking_enabled',
    '_resolve_parked_draft_before_quit',
)


class Timer:
    def __init__(self, name, delay, fn, args=(), **kwargs):
        self.fn, self.args = fn, args
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn(*self.args)


@pytest.fixture
def app():
    source = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in source.body if isinstance(n, ast.ClassDef)
               and n.name == 'DictationApp')
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in METHODS]
    ns = dict(time=time, logger=Mock(), flight_recorder=SimpleNamespace(record=Mock()),
              audio_ducking=SimpleNamespace(SessionDucker=None),
              thread_registry=SimpleNamespace(timer=Timer),
              _HANDS_FREE_CAPTURE_DUCK_RESTORE_DELAY_S=0.5,
              HOLD_RELEASE_TAIL_SPEECH_THRESHOLD=HOLD_RELEASE_TAIL_SPEECH_THRESHOLD,
              _RecordingOwnership=collections.namedtuple(
                  'Ownership', 'is_command is_ava command_ghost ava_ghost'))
    exec(compile(ast.Module(body=methods, type_ignores=[]), 'dictation.py', 'exec'), ns)
    a = SimpleNamespace(
        config={'mode': 'hold', 'recording_tail_ms': 0},
        _running=True, model_loaded=True, loading_model=False, recording=False,
        _stop_in_flight=False, _streaming_session=None, _wake_consumer=None,
        hotkey_pressed=False, command_mode_recording=False, ava_mode_recording=False,
        _memo_recording=False, _hotkey_recording=False,
        _ace_dictation_active=False, _ace_streaming_active=False,
        _hold_capture_lifecycle_lock=threading.RLock(),
        _hold_capture_duck_seq=0, _hold_capture_duck_token=None,
        _hold_capture_duck_confirmed_at=None,
        _hands_free_duck_lock=threading.Lock(), _hands_free_capture_ducker=None,
        _hands_free_capture_duck_owners=set(), _hands_free_capture_duck_owner_seq=0,
        _hands_free_capture_duck_starting=False, _hands_free_capture_duck_start_generation=0,
        _hands_free_duck_restore_timer=None, _hands_free_capture_duck_restore_generation=0,
        _hands_free_capture_duck_restore_token=object(), _sound_cache={},
        _hands_free_duck_excludes=lambda: set(), _bump_wake_gate_freeze=Mock(),
        _log_duck_result=Mock(), _duck_audio=Mock(), _restore_audio=Mock(),
        play_sound=Mock(), _request_icon_chase=Mock(), _release_icon_chase=Mock(),
        _update_tray_tooltip=Mock(), events=[], duckers=[], volume=1.0, ns=ns,
    )
    for name in METHODS:
        setattr(a, name, MethodType(ns[name], a))
    a.set_app_state = lambda **kw: a.__dict__.update(kw)

    class Ducker:
        def __init__(self, duck_level, exclude_pids):
            self.level = duck_level
            self.sessions_failed = 0
            self.last_error = None
            self._tracked_by_id = {'media': object()}
            self.stop_calls = 0
            a.duckers.append(self)

        def start(self):
            a.volume = self.level
            a.events.append('duck_confirmed')

        def stop(self):
            self.stop_calls += 1
            a.volume = 1.0
            a.events.append('restored')

    ns['audio_ducking'].SessionDucker = Ducker
    a._dictation_consumer = SimpleNamespace(
        activate=Mock(return_value=True), activate_streaming=Mock(return_value=True),
        cancel=Mock(), stop_streaming=Mock(), finish_capture=Mock(),
        drain_after_release=Mock(return_value=None), drain=Mock(return_value=None),
    )
    a.stop_impl = a._stop_recording_impl
    a._stop_recording_impl = lambda: a.set_app_state(recording=False)
    return a


def start(app, **kw):
    app.start_recording(play_earcon=False, **kw)


def flush(app):
    timer = app._hands_free_duck_restore_timer
    if timer:
        timer.fire()


def test_hold_opens_distinct_owner_before_activation_and_closes_on_stop(app):
    def activate():
        assert app.events == ['duck_confirmed']
        assert app._hold_capture_duck_confirmed_at is not None
        assert app._hands_free_capture_duck_owners == {-1}
        return True
    app._dictation_consumer.activate.side_effect = activate
    start(app)
    assert app.recording
    assert app.duckers[0].level == 0.15
    app._duck_audio.assert_not_called()
    app.stop_recording()
    assert app._hands_free_capture_duck_owners == set()
    app._dictation_consumer.finish_capture.assert_called_once()
    flush(app)
    assert app.volume == 1.0


@pytest.mark.parametrize('failure', ['stop', 'restore', 'cancel', 'activate', 'earcon'])
def test_exception_always_releases_hold_owner(app, failure):
    error = RuntimeError('injected')
    if failure in ('activate', 'earcon'):
        target = app._dictation_consumer.activate if failure == 'activate' else app.play_sound
        target.side_effect = error
        with pytest.raises(RuntimeError, match='injected'):
            app.start_recording(play_earcon=failure == 'earcon')
        assert not app.recording
    else:
        start(app)
        if failure == 'stop':
            app._stop_recording_impl = Mock(side_effect=error)
        elif failure == 'restore':
            app._restore_audio.side_effect = error
        else:
            app._dictation_consumer.cancel.side_effect = error
        with pytest.raises(RuntimeError, match='injected'):
            (app.cancel_recording if failure == 'cancel' else app.stop_recording)()
    assert app._hands_free_capture_duck_owners == set()
    flush(app)
    assert app.volume == 1.0


@pytest.mark.parametrize('first', ['hold', 'hands_free'])
def test_nesting_restores_only_when_both_owners_close(app, first):
    hf = app._open_hands_free_capture_duck()
    start(app)
    assert app._hands_free_capture_duck_owners == {hf, -1}
    assert len(app.duckers) == 1
    if first == 'hold':
        app.stop_recording()
    else:
        app._close_hands_free_capture_duck(hf)
    flush(app)
    assert app.volume == 0.15
    assert app.duckers[0].stop_calls == 0
    if first == 'hold':
        app._close_hands_free_capture_duck(hf)
    else:
        app.stop_recording()
    flush(app)
    assert app.volume == 1.0
    assert app.duckers[0].stop_calls == 1


def test_config_disabled_avoids_all_hold_ducking(app):
    app.config['capture_duck_hold_enabled'] = False
    # Even a legacy opt-in must not create a second hold duck.
    app.config['ducking'] = {'enabled': True, 'level': 0.1}
    start(app)
    assert app.recording
    assert app._hold_capture_duck_confirmed_at is None
    app.stop_recording()
    assert app.duckers == []
    app._duck_audio.assert_not_called()


def test_reuses_existing_level_key(app):
    app.config['ducking'] = {'hands_free_level': 0.08}
    start(app)
    assert app.duckers[0].level == 0.08
    app.stop_recording()


def test_rapid_twenty_holds_leave_no_owners_and_ignore_stale_closes(app):
    for number in range(1, 21):
        start(app)
        assert app._hands_free_capture_duck_owners == {-number}
        app._close_hands_free_capture_duck(-(number - 1))
        assert app._hands_free_capture_duck_owners == {-number}
        app.stop_recording()
        assert app._hands_free_capture_duck_owners == set()
    flush(app)
    assert app.volume == 1.0


@pytest.mark.parametrize('end', ['stop_recording', 'cancel_recording'])
def test_release_or_escape_racing_slow_start_cannot_orphan_owner(app, end):
    entered, release, ending = threading.Event(), threading.Event(), threading.Event()
    original = app.ns['audio_ducking'].SessionDucker.start
    def blocked(ducker):
        entered.set()
        assert release.wait(2)
        original(ducker)
    app.ns['audio_ducking'].SessionDucker.start = blocked
    starter = threading.Thread(target=start, args=(app,))
    stopper = threading.Thread(target=lambda: (ending.set(), getattr(app, end)()))
    starter.start()
    assert entered.wait(2)
    stopper.start()
    assert ending.wait(2)
    try:
        assert not app._dictation_consumer.activate.called
    finally:
        release.set()
        starter.join(2)
        stopper.join(2)
    assert not starter.is_alive() and not stopper.is_alive()
    assert not app.recording
    assert app._hands_free_capture_duck_owners == set()
    flush(app)
    assert app.volume == 1.0


def test_waits_for_hands_free_start_in_progress(app):
    entered, release = threading.Event(), threading.Event()
    original = app.ns['audio_ducking'].SessionDucker.start
    def blocked(ducker):
        entered.set()
        assert release.wait(2)
        original(ducker)
    app.ns['audio_ducking'].SessionDucker.start = blocked
    hf = threading.Thread(target=app._open_hands_free_capture_duck)
    hf.start()
    assert entered.wait(2)
    hold = threading.Thread(target=start, args=(app,))
    hold.start()
    try:
        # Token reservation is not confirmation; no audio activation yet.
        time.sleep(0.02)
        assert not app._dictation_consumer.activate.called
    finally:
        release.set()
        hf.join(2)
        hold.join(2)
    assert not hf.is_alive() and not hold.is_alive()
    assert app.recording
    app.stop_recording()
    app._close_hands_free_capture_duck(1)
    flush(app)
    assert app.volume == 1.0


@pytest.mark.parametrize('failure', ['raise', 'reported', 'refused', 'missing_consumer'])
def test_failed_start_aborts_without_orphaning_duck(app, failure):
    original = app.ns['audio_ducking'].SessionDucker.start
    def fail(ducker):
        if failure == 'raise':
            raise RuntimeError('backend failure')
        original(ducker)
        ducker.sessions_failed = 1
    if failure in ('raise', 'reported'):
        app.ns['audio_ducking'].SessionDucker.start = fail
        with pytest.raises(RuntimeError, match='Hold capture duck'):
            start(app)
        app._dictation_consumer.activate.assert_not_called()
    else:
        if failure == 'refused':
            app._dictation_consumer.activate.return_value = False
        else:
            app._dictation_consumer = None
        start(app)
    assert not app.recording
    assert app._hold_capture_duck_token is None
    assert app._hands_free_capture_duck_owners == set()
    flush(app)
    assert app.volume == 1.0


def test_real_stop_empty_audio_return_releases_owner(app):
    start(app)
    app._stop_recording_impl = app.stop_impl
    app.stop_recording()
    assert not app.recording
    assert app._hands_free_capture_duck_owners == set()
    flush(app)
    assert app.volume == 1.0


def test_streaming_start_stop_confirms_duck_and_bounds_async_finalization(app, monkeypatch):
    session = SimpleNamespace(start=Mock(), finalize=Mock(), cancel=Mock())
    monkeypatch.setitem(sys.modules, 'samsara.streaming',
                        SimpleNamespace(StreamingSession=lambda a: session))
    app._dictation_consumer.activate_streaming.side_effect = lambda: (
        app._hold_capture_duck_confirmed_at is not None)
    start(app, streaming=True)
    session.start.assert_called_once()
    assert app._hands_free_capture_duck_owners == {-1}
    app._stop_recording_impl = app.stop_impl
    app.stop_recording()
    session.finalize.assert_called_once()
    app._dictation_consumer.finish_capture.assert_called_once()
    assert app._hands_free_capture_duck_owners == set()
    flush(app)
    assert app.volume == 1.0


def shutdown_prefix(app):
    """Run the real quit prefix, excluding unrelated UI teardown and os._exit."""
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    quit_fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'quit_app')
    end = next(i for i, n in enumerate(quit_fn.body)
               if isinstance(n, ast.Try) and '_close_hold_capture_duck' in ast.unparse(n))
    quit_fn.body = quit_fn.body[:end + 1]
    exec(compile(ast.Module(body=[quit_fn], type_ignores=[]), 'dictation.py', 'exec'), app.ns)
    app.ns['quit_app'](app)


@pytest.mark.parametrize('cancel_raises', [False, True])
def test_shutdown_mid_hold_flushes_duck_synchronously(app, cancel_raises):
    start(app)
    if cancel_raises:
        app._dictation_consumer.cancel.side_effect = RuntimeError('cancel failed')
    shutdown_prefix(app)
    assert not app._running
    assert app._hands_free_capture_duck_owners == set()
    assert app.volume == 1.0  # No timer needed before os._exit.
    start(app)
    assert len(app.duckers) == 1


def test_shutdown_racing_start_cannot_activate_after_exit(app):
    entered, release = threading.Event(), threading.Event()
    original = app.ns['audio_ducking'].SessionDucker.start
    errors = []
    def blocked(ducker):
        entered.set()
        assert release.wait(2)
        original(ducker)
    def start_worker():
        try:
            start(app)
        except RuntimeError as exc:
            errors.append(str(exc))
    app.ns['audio_ducking'].SessionDucker.start = blocked
    starter = threading.Thread(target=start_worker)
    starter.start()
    assert entered.wait(2)
    shutdown = threading.Thread(target=shutdown_prefix, args=(app,))
    shutdown.start()
    try:
        deadline = time.monotonic() + 2
        while app._running and time.monotonic() < deadline:
            time.sleep(0.001)
        assert not app._running
    finally:
        release.set()
        starter.join(2)
        shutdown.join(2)
    assert not starter.is_alive() and not shutdown.is_alive()
    assert errors == ['Hold capture duck lost ownership']
    app._dictation_consumer.activate.assert_not_called()
    assert app._hands_free_capture_duck_owners == set()
    assert app.volume == 1.0


def consumer(app, frames):
    pending = iter(frames)
    reader = SimpleNamespace(snap_to_head=Mock(), rewind=Mock(),
                             read_next=lambda: next(pending, EMPTY))
    engine = SimpleNamespace(register_consumer=lambda name: reader)
    return DictationSessionConsumer(engine, app)


def frame(seq, end):
    return SimpleNamespace(seq=seq, t_capture=end, device_epoch=0,
                           pcm=np.full(FRAME_SIZE, seq + 1, dtype=np.int16))


@pytest.mark.parametrize('streaming', [False, True])
def test_prebuffer_and_boundary_frame_discarded_before_accumulation(app, monkeypatch, streaming):
    import samsara.audio_engine.dictation_consumer as module
    start(app)
    boundary = app._hold_capture_duck_confirmed_at
    old, crossing, clean = frame(0, boundary - 0.2), frame(1, boundary + 0.05), frame(2, boundary + 0.2)
    c = consumer(app, [old, crossing, clean])
    jobs = []
    monkeypatch.setattr(module.thread_registry, 'spawn', lambda *a, **kw: jobs.append((a, kw)))
    (c.activate_streaming if streaming else c.activate)()
    # Rewind remains intact; admission is enforced per frame, including a
    # block arriving after confirmation that still contains earlier samples.
    c._reader.rewind.assert_called_once_with(PREBUFFER_FRAMES)
    stop = c._streaming_stop if streaming else c._drain_stop
    monkeypatch.setattr(module.time, 'sleep', lambda duration: stop.set())
    (c._streaming_drain_loop if streaming else c._hold_drain_loop)(stop)
    if streaming:
        np.testing.assert_allclose(c.snapshot_streaming_audio(), clean.pcm / 32767.0)
    else:
        assert len(c._frames) == 1
        assert c._frames[0][0] == clean.seq
    app.stop_recording()


def test_final_synchronous_drain_also_filters_preconfirmation_audio(app):
    app._hold_capture_duck_confirmed_at = 10.0
    c = consumer(app, [frame(0, 9.9), frame(1, 10.05), frame(2, 10.2)])
    c._capture_not_before = 10.0
    c._log_seam_diagnostics = Mock()
    np.testing.assert_allclose(c.drain(), np.full(FRAME_SIZE, 3 / 32767.0))


def test_disabled_duck_keeps_prebuffer_and_streaming_release_excludes_late_audio(app):
    c = consumer(app, [])
    assert c._frame_inside_capture_duck(frame(0, 1.0))
    c._capture_not_before = 1.0
    c.finish_capture()
    end = c._capture_not_after
    assert c._frame_inside_capture_duck(frame(1, end))
    assert not c._frame_inside_capture_duck(frame(2, end + FRAME_MS / 1000.0))


def test_ducked_opening_speech_is_not_used_as_release_tail_noise_floor(app, monkeypatch):
    import samsara.audio_engine.dictation_consumer as module
    c = consumer(app, [])
    c._capture_not_before = 1.0
    c._frames = [(i, 0, np.full(FRAME_SIZE, 16000, dtype=np.int16))
                 for i in range(PREBUFFER_FRAMES)]
    # A release frame at RMS .02 is speech above the configured .008 floor;
    # using the opening voice (RMS .49) as room noise would end immediately.
    ticks = iter([10.0, 10.01, 10.02, 10.3, 10.3])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(ticks))
    def new_speech(delay):
        c._frames.append((len(c._frames), 0, np.full(FRAME_SIZE, 655, dtype=np.int16)))
    monkeypatch.setattr(module.time, 'sleep', new_speech)
    c.drain = Mock(return_value=None)
    log = Mock()
    monkeypatch.setattr(module, 'logger', log)
    c.drain_after_release(silence_ms=100, max_tail_ms=200, speech_threshold=0.008)
    assert log.debug.call_args.args[1] == 'max'
    assert log.debug.call_args.args[-2:] == (0.008, 0.0)
