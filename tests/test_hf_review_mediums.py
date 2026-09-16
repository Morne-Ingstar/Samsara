"""Regression tests for the MEDIUM findings closed against
docs/reviews/hands_free_path_review.md (2026-09-10 follow-up pass).

Each test targets exactly one finding and is written to fail against the
pre-fix code shape. No dictation module is imported at module scope --
Samsara may be running live on this machine -- DictationApp methods are
called as unbound functions on lightweight fake apps, with `dictation`
imported lazily inside the handful of test functions that need it (the
same convention already used by tests/test_command_mode.py and
tests/test_dictation_app.py). DictationApp is never constructed.
"""
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.audio_engine.frame import FRAME_SIZE, PREBUFFER_FRAMES
from samsara.audio_engine.ring import FrameBus
from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara.audio_engine.dictation_consumer import DictationSessionConsumer
from samsara.audio_engine import continuous_consumer as cc_module
from samsara import constants


# ---------------------------------------------------------------------------
# wake_consumer.py -- shared harness
# ---------------------------------------------------------------------------

def _wake_app(**overrides):
    app = SimpleNamespace(
        config={}, app_state='asleep', wake_word_triggered=False,
        _oww_wake_detected=False, _wake_detector=None,
        _wake_profile_detectors={},
        is_speaking=False, silence_start=None, _vad_available=True,
        _vad_is_speech=Mock(return_value=False), _vad_reset=Mock(),
        _dictation_silence_timeout=None, _hotkey_recording=False,
        command_mode_active=False, ava_command_session_active=False,
        wake_word_active=True, _tts_last_speaking=0.0,
        _command_executed_at=None, audio_coordinator=None,
        _wake_rearm_needs_onset=False, _expire_wake_session=Mock(),
        _restart_wake_session_timer=Mock(),
        _open_hands_free_capture_duck=Mock(return_value=1),
        _close_hands_free_capture_duck=Mock(),
        process_wake_word_buffer=Mock(),
        _session_mode_manager=None,
    )
    for key, value in overrides.items():
        setattr(app, key, value)
    return app


def _wake_rig(app):
    bus = FrameBus()
    reader = bus.new_reader()
    engine = SimpleNamespace(register_consumer=lambda name: reader)
    consumer = WakeConsumer(engine, app)

    def feed(t, value=12000):
        pcm = np.full(FRAME_SIZE, value, dtype=np.int16)
        bus.write(pcm, t_capture=t, device_epoch=0)
        consumer._process_frame(reader.read_next())

    return consumer, feed


# ---------------------------------------------------------------------------
# Medium: wake_consumer.py ~663 -- Silero unavailable, RMS onset never
# restarted the wake-session inactivity timer.
#
# 2026-09-10 policy reversal (see wake_consumer.py's inline comment at the
# `if silero_onset:` restart-timer call, and the corroborating
# test_rms_fallback_does_not_count_as_silero_onset in
# test_wake_session_policy.py): an RMS edge cannot distinguish the owner
# from a TV, so the session timer is now deliberately restarted ONLY on a
# real Silero speech onset. With VAD unavailable, an RMS onset still clears
# the wake-onset rearm latch (so hands-free doesn't go deaf), but it no
# longer renews the session timer -- the session runs to its inactivity
# deadline and the user re-wakes. This inverts the two tests below as
# originally written against the MEDIUM finding; they now assert the
# current, intentionally-degraded behaviour instead.
# ---------------------------------------------------------------------------

def test_wake_session_timer_not_restarted_by_rms_onset_when_vad_unavailable():
    app = _wake_app(app_state='wake_session', _vad_available=False)
    consumer, feed = _wake_rig(app)

    feed(1.0, value=50)     # quiet: establishes an RMS "was not speech" edge
    app._restart_wake_session_timer.assert_not_called()

    feed(1.1, value=12000)  # loud: RMS onset edge, Silero structurally absent

    app._restart_wake_session_timer.assert_not_called()


def test_wake_session_timer_never_restarted_by_rms_alone():
    app = _wake_app(app_state='wake_session', _vad_available=False)
    consumer, feed = _wake_rig(app)

    feed(1.0, value=50)     # quiet: establishes the "was not speech" edge
    feed(1.1, value=12000)  # onset -- ignored under the Silero-only restart policy
    app._restart_wake_session_timer.assert_not_called()

    feed(1.2, value=12000)  # still loud -- no fresh non-speech -> speech edge either
    app._restart_wake_session_timer.assert_not_called()


# ---------------------------------------------------------------------------
# Medium: wake_consumer.py ~960 -- one enabled wake_profiles entry disabled
# the primary OWW gate for every phrase, and a profile's own loaded
# detector was never consulted.
# ---------------------------------------------------------------------------

def test_profile_detector_with_model_participates_as_prefilter():
    profile_detector = SimpleNamespace(is_available=True, reset=Mock(), detected=Mock(return_value=True))
    app = _wake_app(
        _wake_detector=None,  # primary unavailable
        _wake_profile_detectors={'hermes': profile_detector},
        config={'wake_profiles': [{'id': 'hermes', 'enabled': True}]},
    )
    consumer, feed = _wake_rig(app)

    feed(1.0, value=12000)

    assert app._oww_wake_detected is True
    profile_detector.detected.assert_called_once()
    profile_detector.reset.assert_called_once()


def test_model_backed_wake_profile_does_not_disable_strict_oww_gate():
    primary = SimpleNamespace(is_available=True, reset=Mock(), detected=Mock(return_value=False))
    profile_detector = SimpleNamespace(is_available=True, reset=Mock())
    app = _wake_app(
        config={'wake_profiles': [{'id': 'hermes', 'enabled': True}]},
        _wake_detector=primary,
        _wake_profile_detectors={'hermes': profile_detector},
    )
    consumer = WakeConsumer.__new__(WakeConsumer)
    consumer._app = app
    consumer._hands_free_capture_duck_token = None
    consumer._is_ai_cmd_mode = lambda a: False
    consumer._is_toggle_cmd = lambda a: False

    consumer._flush([np.zeros(160, dtype=np.float32)], owner_token=3)

    # A profile with its OWN loaded model no longer buys a bypass of the
    # strict primary gate when nothing (primary or profile) actually fired.
    app.process_wake_word_buffer.assert_not_called()
    app._close_hands_free_capture_duck.assert_called_once_with(3)


def test_modelless_wake_profile_still_reaches_whisper_fallback():
    primary = SimpleNamespace(is_available=True, reset=Mock(), detected=Mock(return_value=False))
    app = _wake_app(
        config={'wake_profiles': [{'id': 'hermes', 'enabled': True}]},
        _wake_detector=primary,
        _wake_profile_detectors={'hermes': None},
    )
    consumer = WakeConsumer.__new__(WakeConsumer)
    consumer._app = app
    consumer._hands_free_capture_duck_token = None
    consumer._is_ai_cmd_mode = lambda a: False
    consumer._is_toggle_cmd = lambda a: False

    consumer._flush([np.zeros(160, dtype=np.float32)], owner_token=None)

    app.process_wake_word_buffer.assert_called_once()


# ---------------------------------------------------------------------------
# Medium: dictation.py ~9429 -- the long_dictation hard-cap flush was a
# guaranteed no-op (self.speech_buffer has had no writer since the ACE
# migration). Now routed through WakeConsumer.flush_utterance().
# ---------------------------------------------------------------------------

def test_flush_utterance_dispatches_buffered_audio():
    app = _wake_app(app_state='long_dictation')
    consumer = WakeConsumer.__new__(WakeConsumer)
    consumer._app = app
    consumer._utterance_frames = [np.zeros(160, dtype=np.float32)]
    consumer._buffer_rms_history = [0.0]
    consumer._hands_free_capture_duck_token = 5
    consumer._is_ai_cmd_mode = lambda a: False
    consumer._is_toggle_cmd = lambda a: False

    dispatched = consumer.flush_utterance()

    assert dispatched is True
    assert consumer._utterance_frames == []
    app.process_wake_word_buffer.assert_called_once()
    _, kwargs = app.process_wake_word_buffer.call_args
    assert kwargs.get('tracked') is True


def test_flush_utterance_is_noop_when_nothing_buffered():
    consumer = WakeConsumer.__new__(WakeConsumer)
    consumer._app = _wake_app()
    consumer._utterance_frames = []
    consumer._buffer_rms_history = []
    consumer._hands_free_capture_duck_token = None

    assert consumer.flush_utterance() is False


def test_hardcap_flush_routes_through_wake_consumer_not_dead_buffer():
    from dictation import DictationApp

    assert not hasattr(DictationApp, '_flush_speech_buffer_to_transcription')
    assert not hasattr(DictationApp, '_process_wake_word_buffer_tracked')

    consumer = Mock()
    app = SimpleNamespace(
        app_state='long_dictation',
        _dictation_finalize_lock=threading.Lock(),
        _dictation_finalize_requested=False,
        _wake_consumer=consumer,
        _maybe_finalize_dictation=Mock(),
    )

    DictationApp._finalize_dictation_hardcap(app)

    consumer.flush_utterance.assert_called_once_with()
    app._maybe_finalize_dictation.assert_called_once_with()
    assert not hasattr(app, 'speech_buffer')


# ---------------------------------------------------------------------------
# Medium: dictation_consumer.py ~163 -- a hotkey press within 0.5s of TTS
# ending skips the prebuffer rewind, so drain_after_release() was treating
# live captured speech as if it were ambient pre-press silence.
# ---------------------------------------------------------------------------

def test_release_tail_ignores_prebuffer_when_rewind_was_skipped(caplog):
    from samsara.audio_engine import dictation_consumer as dc_module

    class FakeReader:
        def __init__(self):
            self.rewound = []

        def snap_to_head(self):
            pass

        def rewind(self, n):
            self.rewound.append(n)

        def read_next(self):
            from samsara.audio_engine.ring import EMPTY
            return EMPTY

    reader = FakeReader()
    app = SimpleNamespace(audio_coordinator=None, _tts_last_speaking=time.monotonic())
    engine = SimpleNamespace(register_consumer=lambda name: reader)
    consumer = DictationSessionConsumer(engine, app)

    consumer.activate()
    try:
        assert reader.rewound == []  # TTS-ended-too-recently guard skipped it
        assert consumer._prebuffer_rewound is False
    finally:
        consumer._drain_stop.set()
        consumer._drain_thread.join(timeout=2)

    # Stand in for what a real capture's first PREBUFFER_FRAMES would hold
    # here: live captured speech, since there was no real pre-press window.
    loud = np.full(160, 12000, dtype=np.int16)
    consumer._frames = [(i, 0, loud) for i in range(PREBUFFER_FRAMES)]

    caplog.set_level(logging.DEBUG, logger=dc_module.logger.name)
    consumer.drain_after_release(silence_ms=10, max_tail_ms=10)

    tail_logs = [r for r in caplog.records if '[TAIL]' in r.message]
    assert tail_logs, "expected a [TAIL] debug log from drain_after_release"
    assert 'noise_floor=0.0000' in tail_logs[-1].message


def test_release_tail_uses_real_prebuffer_when_rewound(caplog):
    from samsara.audio_engine import dictation_consumer as dc_module

    class FakeReader:
        def __init__(self):
            self.rewound = []

        def snap_to_head(self):
            pass

        def rewind(self, n):
            self.rewound.append(n)

        def read_next(self):
            from samsara.audio_engine.ring import EMPTY
            return EMPTY

    reader = FakeReader()
    app = SimpleNamespace(audio_coordinator=None, _tts_last_speaking=0.0)
    engine = SimpleNamespace(register_consumer=lambda name: reader)
    consumer = DictationSessionConsumer(engine, app)

    consumer.activate()
    try:
        assert reader.rewound == [PREBUFFER_FRAMES]
        assert consumer._prebuffer_rewound is True
    finally:
        consumer._drain_stop.set()
        consumer._drain_thread.join(timeout=2)

    quiet = np.full(160, 50, dtype=np.int16)
    consumer._frames = [(i, 0, quiet) for i in range(PREBUFFER_FRAMES)]

    caplog.set_level(logging.DEBUG, logger=dc_module.logger.name)
    consumer.drain_after_release(silence_ms=10, max_tail_ms=10)

    tail_logs = [r for r in caplog.records if '[TAIL]' in r.message]
    assert tail_logs
    assert 'noise_floor=0.0000' not in tail_logs[-1].message


# ---------------------------------------------------------------------------
# Medium: continuous_consumer.py ~179 -- is_speaking/silence_start read and
# written outside _frames_lock, racing commit_now() on the keyboard thread.
# ---------------------------------------------------------------------------

def test_continuous_silence_check_holds_frames_lock():
    app = SimpleNamespace(config={}, echo_canceller=None, continuous_active=True,
                          transcribe_continuous_buffer=Mock())
    engine = SimpleNamespace(register_consumer=lambda name: SimpleNamespace())
    consumer = cc_module.ContinuousConsumer(engine, app)

    consumer._speech_frames = [np.zeros(4, dtype=np.float32)]
    consumer._is_speaking = True
    consumer._silence_start = time.time() - 100  # already past the silence threshold

    lock_held_during_check = []
    real_time = time.time

    def probing_time():
        # Simulates the exact moment commit_now() (on another thread) would
        # try to acquire the same lock to reset _silence_start concurrently.
        acquired = consumer._frames_lock.acquire(blocking=False)
        lock_held_during_check.append(not acquired)
        if acquired:
            consumer._frames_lock.release()
        return real_time()

    import types
    monkeypatched = types.SimpleNamespace(time=probing_time)
    orig_time = cc_module.time
    cc_module.time = monkeypatched
    try:
        frame = SimpleNamespace(pcm=np.zeros(FRAME_SIZE, dtype=np.int16))
        consumer._process_frame(frame)
    finally:
        cc_module.time = orig_time

    assert lock_held_during_check, "time.time() was never called for the silence check"
    assert lock_held_during_check[-1] is True


# ---------------------------------------------------------------------------
# Low: streaming.py ~1110 -- self._finalized handed to the overlay by
# reference could be mutated in place while a preview tick was mid-render.
# ---------------------------------------------------------------------------

def test_finalized_transcript_snapshot_is_independent_copy():
    from samsara.streaming import DictatePreviewSession

    session = DictatePreviewSession.__new__(DictatePreviewSession)
    session._closed = False
    session._finalized = ["hello"]
    session._generation = 0
    session.app = SimpleNamespace()
    captured = {}
    session._overlay = SimpleNamespace(
        set_transcript=lambda finalized, partial: captured.update(finalized=finalized, partial=partial)
    )

    session.on_utterance_final("world", scratch_success=False, dictate_committed=False)

    snapshot = captured['finalized']
    session._finalized.append("mutated-after")

    assert snapshot == ["hello", "world"]


# ---------------------------------------------------------------------------
# Medium: dictation.py ~6524 -- _dispatch_session_transition's unlocked
# getattr-then-assign let two near-simultaneous hotkey presses both spawn a
# worker instead of the second being dropped.
# ---------------------------------------------------------------------------

def test_dispatch_session_transition_drops_concurrent_racers(monkeypatch):
    from dictation import DictationApp
    from samsara.runtime import thread_registry

    app = SimpleNamespace(_session_transition_lock=threading.Lock())
    started = threading.Event()
    finish = threading.Event()
    call_count = {'n': 0}
    threads = []

    def slow_action():
        call_count['n'] += 1
        started.set()
        finish.wait(timeout=2)

    def fake_spawn(name, target, args=(), kwargs=None, daemon=True):
        t = threading.Thread(target=target, args=args, kwargs=kwargs or {}, daemon=True)
        threads.append(t)
        t.start()
        return t

    monkeypatch.setattr(thread_registry, 'spawn', fake_spawn)

    DictationApp._dispatch_session_transition(app, slow_action)
    assert started.wait(timeout=2)

    # A second press while the first transition is still running -- the
    # docstring's own contract: "extra presses during a transition are
    # dropped".
    DictationApp._dispatch_session_transition(app, slow_action)

    finish.set()
    for t in threads:
        t.join(timeout=2)

    assert call_count['n'] == 1
    assert len(threads) == 1


# ---------------------------------------------------------------------------
# Medium: dictation.py ~8996 -- "pause" spoken during long_dictation wrote
# self.silence_start = None from the decode-completion thread, racing the
# WakeConsumer poll thread that otherwise owns it.
# ---------------------------------------------------------------------------

def _pause_word_app(is_speaking):
    segment = SimpleNamespace(text="pause")
    return SimpleNamespace(
        capture_rate=16000, model_rate=16000, config={},
        _transcription_owners=SimpleNamespace(claim=Mock(return_value=1), release=Mock()),
        model_lock=threading.Lock(),
        model=SimpleNamespace(transcribe=Mock(return_value=([segment], SimpleNamespace(language='en')))),
        _wake_audio_is_below_gate=Mock(return_value=False),
        get_transcription_params=Mock(return_value={}),
        _vad_available=True, _vad_reset=Mock(),
        _filter_dictation_language=Mock(side_effect=lambda text, info: text),
        voice_training_window=SimpleNamespace(apply_corrections=lambda text: text),
        _emit_wake_trace=Mock(), _log_history=Mock(),
        app_state='long_dictation', _dictation_paused=False,
        wake_dictation_buffer=[], play_sound=Mock(),
        is_speaking=is_speaking, silence_start=12345.0,
    )


def test_pause_word_does_not_clear_silence_start_while_new_utterance_is_live():
    from dictation import DictationApp

    speaking_app = _pause_word_app(is_speaking=True)
    DictationApp._decode_wake_word_buffer(
        speaking_app, [np.zeros(1600, dtype=np.float32)], src_rate=16000,
    )

    assert speaking_app._dictation_paused is True
    # Owned by WakeConsumer's poll thread while a new utterance is in
    # progress -- this decode-completion thread must not touch it.
    assert speaking_app.silence_start == 12345.0


def test_pause_word_clears_silence_start_when_idle():
    from dictation import DictationApp

    idle_app = _pause_word_app(is_speaking=False)
    DictationApp._decode_wake_word_buffer(
        idle_app, [np.zeros(1600, dtype=np.float32)], src_rate=16000,
    )

    assert idle_app.silence_start is None


# ---------------------------------------------------------------------------
# Medium: dictation.py ~8403 -- duplicated VAD/RMS gate constants across the
# hold and hands-free paths (0.5 vs _GATE_VAD_PROB=0.45; 0.008 hold tail
# threshold and the 1.5x adaptive-floor ratio each declared twice).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Low: dictation_consumer.py ~155 -- drain_after_release()/stop_streaming()
# called before any activate*() used to raise AttributeError on
# _frames_lock/_streaming_stop. Both are now set in __init__.
# ---------------------------------------------------------------------------

def test_release_and_stop_streaming_survive_before_any_activate():
    from samsara.audio_engine.ring import EMPTY

    class FakeReader:
        def snap_to_head(self):
            pass

        def rewind(self, n):
            pass

        def read_next(self):
            return EMPTY

    app = SimpleNamespace(audio_coordinator=None, _tts_last_speaking=0.0)
    engine = SimpleNamespace(register_consumer=lambda name: FakeReader())
    consumer = DictationSessionConsumer(engine, app)

    # Neither call may raise AttributeError for _frames_lock/_streaming_stop
    # -- both attributes must already exist pre-activate().
    assert consumer.drain_after_release(silence_ms=10, max_tail_ms=10) is None
    assert consumer.stop_streaming() is None


# ---------------------------------------------------------------------------
# Medium: streaming.py ~1199 -- DictatePreviewSession's partial-decode tick
# blocked on model_lock, so an up-to-8s partial decode could queue ahead of
# the authoritative utterance decode. Now a non-blocking acquire skips the
# tick entirely instead of waiting.
# ---------------------------------------------------------------------------

class _PartialFakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return [SimpleNamespace(text="hello")], SimpleNamespace(language='en')


def test_preview_partial_skips_tick_instead_of_blocking_model_lock():
    from samsara.streaming import DictatePreviewSession

    app = SimpleNamespace(
        model=_PartialFakeModel(), model_lock=threading.Lock(), model_rate=16000,
        _wake_consumer=SimpleNamespace(
            snapshot_dictate_preview_audio=lambda: np.ones(16000, dtype=np.float32)),
        get_transcription_params=lambda include_vocabulary=True: {},
        _filter_dictation_language=lambda text, info, **kwargs: text,
    )
    session = DictatePreviewSession.__new__(DictatePreviewSession)
    session.app = app

    app.model_lock.acquire()  # simulate a final/hotkey decode already in flight
    try:
        result = session._transcribe_partial()
    finally:
        app.model_lock.release()

    assert result is None
    assert app.model.calls == [], "must skip the tick, never queue behind the authoritative decode"


def test_gate_constants_share_one_declaration():
    import dictation
    from samsara.audio_engine import dictation_consumer as dc_module
    import inspect

    assert dictation._GATE_VAD_PROB == constants.CONTIGUOUS_VAD_PROB_THRESHOLD
    assert dictation._SPEECH_FLOOR_RATIO == constants.ADAPTIVE_SPEECH_FLOOR_RATIO
    assert dc_module.ADAPTIVE_SPEECH_FLOOR_RATIO is constants.ADAPTIVE_SPEECH_FLOOR_RATIO
    assert dc_module.HOLD_RELEASE_TAIL_SPEECH_THRESHOLD is constants.HOLD_RELEASE_TAIL_SPEECH_THRESHOLD

    sig = inspect.signature(dc_module.DictationSessionConsumer.drain_after_release)
    assert sig.parameters['speech_threshold'].default == constants.HOLD_RELEASE_TAIL_SPEECH_THRESHOLD
