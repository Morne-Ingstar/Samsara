"""Mocked desktop methods and real dispatch threads; no app startup/audio/models."""

import ast
import logging
from pathlib import Path
import threading
import time
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import languages as _languages
from samsara.audio_engine import wake_dispatch
from samsara.audio_engine.ring import FrameBus
from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara.audio_engine.wake_dispatch import (
    TranscriptionOwners, WakeDispatchQueue, dispatch_queue_depth,
)
from samsara.runtime import thread_registry


METHODS = {
    'process_wake_word_buffer', '_decode_wake_word_buffer', '_handle_command_mode_utterance',
    '_release_wake_consumer', 'set_wake_word_enabled',
    '_apply_wake_word_enabled', 'stop_wake_word_mode', '_start_wake_session',
    '_confirm_wake_capture', '_restart_wake_session_timer', '_expire_wake_session',
    '_end_wake_session', '_reset_wake_dictation', '_open_hands_free_capture_duck',
    '_close_hands_free_capture_duck', '_restore_hands_free_capture_duck_now',
    '_filter_dictation_language', '_cancel_pending_wake_start',
}


@pytest.fixture
def rig(monkeypatch):
    # Compile only production methods onto a mocked app, never the module prologue.
    path = Path(__file__).resolve().parents[1] / 'dictation.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in METHODS]
    assert {n.name for n in methods} == METHODS
    events, threads, timers = [], [], []
    real_spawn = thread_registry.spawn

    def spawn(*args, **kwargs):
        thread = real_spawn(*args, **kwargs)
        threads.append(thread)
        return thread

    class Timer:
        def __init__(self, name, delay, fn, args=(), **kwargs):
            self.fn, self.args, self.cancelled = fn, args, False
            timers.append(self)

        def cancel(self):
            self.cancelled = True

        def fire_even_if_cancelled(self):
            self.fn(*self.args)

    monkeypatch.setattr(wake_dispatch.thread_registry, 'spawn', spawn)
    monkeypatch.setattr(wake_dispatch.flight_recorder, 'record',
                        lambda name, **fields: events.append((name, fields)))
    clock = SimpleNamespace(now=100.0)
    namespace = dict(
        np=np, logger=logging.getLogger('wake_dispatch_test'),
        time=SimpleNamespace(monotonic=lambda: clock.now, perf_counter=lambda: clock.now,
                             time=time.time),
        thread_registry=SimpleNamespace(spawn=spawn, timer=Timer),
        flight_recorder=wake_dispatch.flight_recorder,
        audio_ducking=SimpleNamespace(SessionDucker=Mock()),
        resample_audio=lambda audio, *_: audio,
        _languages=_languages,
        _WAKE_SESSION_CHUNK_GAP_S=1.0, _WAKE_SESSION_SEND_WORDS=('send',),
        _HANDS_FREE_CAPTURE_DUCK_RESTORE_DELAY_S=.5,
    )
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), 'exec'), namespace)

    def make(config=None):
        app = SimpleNamespace(
            config=config or {}, capture_rate=16000, model_rate=16000,
            _transcription_owners=TranscriptionOwners(),
            _wake_dispatch_queue=WakeDispatchQueue(config or {}),
            _wake_session_lock=threading.RLock(), _wake_session_inactivity_timer=None,
            _wake_session_started_at=None, _wake_session_timer_token=None,
            _wake_session_deadline=None, _wake_session_expires_at=None,
            _wake_word_settings_lock=threading.Lock(), _wake_word_settings_guard=threading.Lock(),
            _wake_word_settings_generation=0, _wake_consumer_lock=threading.Lock(),
            _wake_consumer_reasons={'wake_word'}, _config_lock=threading.Lock(),
            _dictation_finalize_lock=threading.Lock(), _pending_transcriptions=0,
            _hands_free_duck_lock=threading.Lock(), _hands_free_capture_duck_owners=set(),
            _hands_free_capture_duck_owner_seq=0, _hands_free_capture_ducker=None,
            _hands_free_capture_duck_starting=False, _hands_free_capture_duck_start_generation=0,
            _hands_free_duck_restore_timer=None, _hands_free_capture_duck_restore_generation=0,
            _hands_free_capture_duck_restore_token=object(),
            _hands_free_duck_excludes=Mock(return_value=[]), _log_duck_result=Mock(),
            _bump_wake_gate_freeze=Mock(), _stop_hands_free_idle_duck=Mock(),
            _duck_audio=Mock(), _restore_audio=Mock(), _indicator_reset=Mock(),
            _release_icon_chase=Mock(), _output_dictation=Mock(),
            _update_tray_tooltip=Mock(), save_config=Mock(), start_wake_word_mode=Mock(),
            app_state='asleep', wake_word_triggered=False, wake_word_active=True,
            command_mode_active=False, ava_command_session_active=False,
            _hotkey_recording=False, _wake_detector=None, _oww_wake_detected=False,
            _wake_rearm_needs_onset=False, _vad_available=False, _vad_reset=Mock(),
            _wake_audio_is_below_gate=Mock(return_value=False),
            get_transcription_params=Mock(return_value={}), model_lock=threading.Lock(),
            model=SimpleNamespace(transcribe=Mock(return_value=([], SimpleNamespace(language='en')))),
            voice_training_window=SimpleNamespace(apply_corrections=lambda text: text),
            _log_history=Mock(), play_sound=Mock(), _maybe_finalize_dictation=Mock(),
            _log_cmd_utt_dropped=Mock(),
            _language_confidence_gate=_languages.LanguageConfidenceGate(),
        )
        app.set_app_state = lambda **fields: app.__dict__.update(fields)
        for name in METHODS:
            setattr(app, name, MethodType(namespace[name], app))
        reader = FrameBus().new_reader()
        app._wake_consumer = WakeConsumer(SimpleNamespace(register_consumer=lambda _: reader), app)
        return app

    result = SimpleNamespace(make=make, events=events, threads=threads, timers=timers,
                             clock=clock, namespace=namespace)
    yield result
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive(), thread.name


def audio(value=1):
    return [np.full(6400, value, dtype=np.float32)]


def drain(rig):
    # Joining a settings worker can append a dispatch worker; include those too.
    for thread in rig.threads:
        thread.join(timeout=3)
        assert not thread.is_alive(), thread.name


@pytest.mark.parametrize('value, expected', [(None, 4), (1, 1), (7, 7), (0, 4), (-2, 4),
                                            ('invalid', 4), (float('inf'), 4)])
def test_queue_depth(value, expected):
    config = {} if value is None else {'wake_word': {'session': {'dispatch_queue_depth': value}}}
    assert dispatch_queue_depth(config) == expected
    assert dispatch_queue_depth({'wake_word': 'jarvis'}) == 4


def test_two_utterances_50ms_apart_decode_in_order(rig):
    app = rig.make()
    entered, release = threading.Event(), threading.Event()
    seen = []

    def transcribe(samples, **kwargs):
        seen.append(int(samples[0]))
        if len(seen) == 1:
            entered.set()
            assert release.wait(2)
        return [], SimpleNamespace(language='en')

    app.model.transcribe.side_effect = transcribe
    try:
        app._wake_consumer._flush(audio(1))
        assert entered.wait(2)
        time.sleep(.05)
        app._wake_consumer._flush(audio(2))
        assert seen == [1]
        assert len(rig.threads) == 1
    finally:
        release.set()
    drain(rig)
    assert seen == [1, 2]
    assert app._transcription_owners.current('wake') is None
    app._log_history.assert_not_called()


def test_fifth_waiting_utterance_drops_oldest_and_finishes_its_owners(rig, caplog):
    app = rig.make()
    entered, release = threading.Event(), threading.Event()
    seen = []

    def decode(samples, *args, **kwargs):
        seen.append(int(samples[0][0]))
        if seen == [0]:
            entered.set()
            assert release.wait(2)

    app._decode_wake_word_buffer = decode
    app._close_hands_free_capture_duck = Mock()
    try:
        app.process_wake_word_buffer(audio(0), tracked=True, owner_token=100)
        assert entered.wait(2)
        for i in range(1, 6):
            app.process_wake_word_buffer(audio(i), tracked=True, owner_token=100+i)
        # 525689c deliberately releases capture ducking before queued decode,
        # so every completed capture has already returned media to normal.
        assert sorted(c.args[0] for c in app._close_hands_free_capture_duck.call_args_list) == list(range(100, 106))
        assert app._pending_transcriptions == 5  # One decoding plus four waiting.
        assert any(r.levelno == logging.WARNING and 'dropping oldest' in r.message
                   for r in caplog.records)
        assert ('wake.dispatch_dropped', {'reason': 'overflow', 'queue_depth': 4,
                                          'policy': 'oldest'}) in rig.events
    finally:
        release.set()
    drain(rig)
    assert seen == [0, 2, 3, 4, 5]
    assert app._pending_transcriptions == 0
    assert sorted(c.args[0] for c in app._close_hands_free_capture_duck.call_args_list) == list(range(100, 106))


def test_toggle_completion_cannot_clear_decoding_wake_token(rig):
    app = rig.make()
    entered, release = threading.Event(), threading.Event()

    def transcribe(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return [], SimpleNamespace(language='en')

    app.model.transcribe.side_effect = transcribe
    try:
        app.process_wake_word_buffer(audio())
        assert entered.wait(2)
        token = app._transcription_owners.current('wake')
        assert token is not None
        # A short toggle utterance returns through the real worker's finally.
        app._handle_command_mode_utterance([np.ones(100, dtype=np.float32)], 16000)
        assert app._transcription_owners.current('toggle') is None
        assert app._transcription_owners.current('wake') is token
    finally:
        release.set()
    drain(rig)


def test_stale_completion_cannot_clear_new_token_in_same_lane():
    owners = TranscriptionOwners()
    first, second = owners.claim('wake'), owners.claim('wake')
    owners.release('wake', first)
    assert owners.current('wake') is second


def test_tray_untick_never_joins_or_decodes_on_caller(rig, caplog):
    app = rig.make()
    caller = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    stop_idents = []
    app.wake_word_triggered = True

    def stop():
        stop_idents.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return audio()

    app._wake_consumer = SimpleNamespace(_running=True, stop=stop)
    app._decode_wake_word_buffer = Mock()
    caplog.set_level(logging.INFO)
    try:
        app.set_wake_word_enabled(False)
        assert entered.wait(2)  # GUI call returned while the join is still blocked.
        assert stop_idents == [rig.threads[0].ident]
        assert stop_idents[0] != caller
    finally:
        release.set()
    drain(rig)
    # Explicit disable discards the final buffer, as documented by the log/event.
    app._decode_wake_word_buffer.assert_not_called()
    assert ('wake.dispatch_dropped', {'reason': 'listener_stopped'}) in rig.events
    assert any('discarding buffered utterance' in r.message for r in caplog.records)
    app.save_config.assert_called_once()


def test_triggered_release_queues_final_decode_on_different_thread(rig):
    app = rig.make()
    caller = threading.get_ident()
    app.wake_word_triggered = True
    app._wake_consumer = SimpleNamespace(_running=True, stop=lambda: audio())
    app._decode_wake_word_buffer = Mock(side_effect=lambda *a, **k: threading.get_ident())
    seen = []
    app._decode_wake_word_buffer.side_effect = lambda *a, **k: seen.append(threading.get_ident())
    app._release_wake_consumer('wake_word')
    drain(rig)
    app._decode_wake_word_buffer.assert_called_once()
    assert seen[0] != caller


def test_timer_concurrent_onsets_and_cancelled_expiry_leave_one_live_timer(rig):
    app = rig.make()
    app._start_wake_session()
    old = rig.timers[-1]
    rig.clock.now = 105.0
    barrier = threading.Barrier(9)

    def replace():
        barrier.wait(timeout=2)
        app._restart_wake_session_timer(speech_onset=True)

    def expire():
        barrier.wait(timeout=2)
        old.fire_even_if_cancelled()

    for i in range(8):
        thread_registry.spawn('timer-race', replace if i % 2 else expire)
    barrier.wait(timeout=2)
    drain(rig)
    live = [timer for timer in rig.timers if not timer.cancelled]
    assert live == [app._wake_session_inactivity_timer]
    assert app._wake_session_expires_at == 115.0
    # Even after the new deadline, a cancelled callback cannot expire its replacement.
    rig.clock.now = 115.0
    old.fire_even_if_cancelled()
    assert app.app_state == 'wake_session'
    live[0].fire_even_if_cancelled()
    assert app.app_state == 'asleep'
    assert app._wake_session_inactivity_timer is None
    assert not [timer for timer in rig.timers if not timer.cancelled]


@pytest.mark.parametrize('stale', [None, 999])
@pytest.mark.parametrize('enabled', [True, False])
def test_stale_duck_close_does_not_change_owners_or_restore(rig, caplog, stale, enabled):
    app = rig.make({'ducking': {'hands_free_enabled': enabled}})
    app._hands_free_capture_duck_owners = {1, 2}
    app._hands_free_capture_ducker = Mock()
    pending = Mock()
    app._hands_free_duck_restore_timer = pending
    caplog.set_level(logging.DEBUG)
    app._close_hands_free_capture_duck(stale)
    assert app._hands_free_capture_duck_owners == {1, 2}
    assert app._hands_free_duck_restore_timer is pending
    pending.cancel.assert_not_called()
    assert any('Ignoring stale capture duck close' in r.message for r in caplog.records)


def test_duck_closes_release_only_live_owner_then_restore(rig):
    app = rig.make()
    ducker = Mock()
    rig.namespace['audio_ducking'].SessionDucker.return_value = ducker
    first, second = app._open_hands_free_capture_duck(), app._open_hands_free_capture_duck()
    assert first != second
    ducker.start.assert_called_once()
    app._close_hands_free_capture_duck(first)
    assert app._hands_free_capture_duck_owners == {second}
    assert not rig.timers
    app._close_hands_free_capture_duck(second)
    assert not app._hands_free_capture_duck_owners
    assert len(rig.timers) == 1
    rig.timers[0].fire_even_if_cancelled()
    ducker.stop.assert_called_once()


def test_lost_duck_generation_releases_only_losing_owner(rig):
    app = rig.make()
    entered, release = threading.Event(), threading.Event()
    losing = Mock()

    def start():
        entered.set()
        assert release.wait(2)

    losing.start.side_effect = start
    rig.namespace['audio_ducking'].SessionDucker.return_value = losing
    results = []
    try:
        thread_registry.spawn('duck-race', lambda: results.append(app._open_hands_free_capture_duck()))
        assert entered.wait(2)
        with app._hands_free_duck_lock:
            app._hands_free_capture_duck_start_generation += 1
            app._hands_free_capture_duck_starting = True
            app._hands_free_capture_duck_owners.add(999)
    finally:
        release.set()
    drain(rig)
    assert results == [None]
    assert app._hands_free_capture_duck_owners == {999}
    assert app._hands_free_capture_duck_starting is True
    losing.stop.assert_called_once()


def test_passive_dispatch_does_not_leave_capture_token(rig):
    app = rig.make()
    app._open_hands_free_capture_duck = Mock(return_value=17)
    app._close_hands_free_capture_duck = Mock()
    app._decode_wake_word_buffer = Mock()
    app._wake_consumer._flush(audio())
    assert app._wake_consumer._hands_free_capture_duck_token is None
    WakeConsumer._close_hands_free_duck_safe(app, None)  # A subsequent short utterance.
    drain(rig)
    app._close_hands_free_capture_duck.assert_called_once_with(17)


def test_capture_token_transfers_to_dispatch_and_exception_closes_once(rig):
    app = rig.make()
    app._wake_consumer._hands_free_capture_duck_token = 42
    app._close_hands_free_capture_duck = Mock()
    app._decode_wake_word_buffer = Mock(side_effect=RuntimeError('decoder failed'))
    app._wake_consumer._flush(audio(), 42)
    assert app._wake_consumer._hands_free_capture_duck_token is None
    app._wake_consumer.abort_utterance()
    drain(rig)
    app._close_hands_free_capture_duck.assert_called_once_with(42)


def test_worker_spawn_failure_finishes_dropped_job(rig, monkeypatch):
    app = rig.make()
    app._close_hands_free_capture_duck = Mock()
    monkeypatch.setattr(wake_dispatch.thread_registry, 'spawn', Mock(side_effect=RuntimeError('spawn')))
    app.process_wake_word_buffer(audio(), tracked=True, owner_token=12)
    assert app._pending_transcriptions == 0
    app._close_hands_free_capture_duck.assert_called_once_with(12)
    assert not app._wake_dispatch_queue._active


def test_lazy_whisper_decode_keeps_model_lock_until_iteration_finishes(rig):
    app = rig.make()
    locked = []

    def segments():
        locked.append(app.model_lock.locked())
        yield SimpleNamespace(text='')

    app.model.transcribe.return_value = (segments(), SimpleNamespace(language='en'))
    app.process_wake_word_buffer(audio())
    drain(rig)
    assert locked == [True]


def test_decode_failure_does_not_strand_next_utterance_or_future_worker(rig):
    app = rig.make()
    entered, release = threading.Event(), threading.Event()
    seen = []

    def decode(samples, *args, **kwargs):
        number = int(samples[0][0])
        seen.append(number)
        if number == 1:
            entered.set()
            assert release.wait(2)
            raise RuntimeError('first decode failed')

    app._decode_wake_word_buffer = decode
    try:
        app.process_wake_word_buffer(audio(1))
        assert entered.wait(2)
        app.process_wake_word_buffer(audio(2))
    finally:
        release.set()
    drain(rig)
    assert seen == [1, 2]
    app.process_wake_word_buffer(audio(3))
    drain(rig)
    assert seen == [1, 2, 3]


def test_disabled_listener_discards_queued_audio_and_releases_owner(rig):
    app = rig.make()
    app.wake_word_active = False
    app._decode_wake_word_buffer = Mock()
    app._close_hands_free_capture_duck = Mock()
    app.process_wake_word_buffer(audio(), owner_token=5)
    drain(rig)
    app._decode_wake_word_buffer.assert_not_called()
    app._close_hands_free_capture_duck.assert_called_once_with(5)
    assert ('wake.dispatch_dropped', {'reason': 'listener_stopped'}) in rig.events


def test_queued_audio_from_expired_session_is_discarded_with_cleanup(rig):
    app = rig.make()
    entered, release = threading.Event(), threading.Event()
    app._start_wake_session()
    app._close_hands_free_capture_duck = Mock()

    def decode(*args, **kwargs):
        entered.set()
        assert release.wait(2)

    app._decode_wake_word_buffer = Mock(side_effect=decode)
    try:
        app.process_wake_word_buffer(audio(1), owner_token=1)
        assert entered.wait(2)
        app.process_wake_word_buffer(audio(2), owner_token=2)
        app._end_wake_session()
    finally:
        release.set()
    drain(rig)
    app._decode_wake_word_buffer.assert_called_once()
    assert app._close_hands_free_capture_duck.call_count == 2
    assert ('wake.dispatch_dropped', {'reason': 'session_ended'}) in rig.events
