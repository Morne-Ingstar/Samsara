"""618: real ring/consumer concurrency and recorded decoder signal fakes.

No audio device, model download, app bootstrap, owner data or child process.
"""
import logging
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import decode_recovery as recovery, speech_presence
from samsara.audio_engine import DictationSessionConsumer, FrameBus, FRAME_SIZE
from samsara.audio_engine.frame import RING_FRAMES, RING_SECONDS
from samsara.hotkeys import HotkeyCluster
from samsara.streaming import StreamingSession, StreamingWorker


class Engine:
    _overflow_count = 0

    def __init__(self):
        self.bus = FrameBus()

    def register_consumer(self, name):
        return self.bus.new_reader(name)

    def write(self, value, at=None, epoch=0):
        self.bus.write(np.full(FRAME_SIZE, value, np.int16),
                       time.perf_counter() if at is None else at, epoch)


def consumer(app=None):
    engine = Engine()
    return DictationSessionConsumer(engine, app or NS()), engine


def activate_without_consumer(c, monkeypatch, streaming=True):
    """Deterministically starve just the reader while capture keeps writing."""
    from samsara.audio_engine import dictation_consumer
    monkeypatch.setattr(dictation_consumer.thread_registry, 'spawn', lambda *a, **kw: None)
    assert (c.activate_streaming() if streaming else c.activate())


@pytest.mark.parametrize('streaming', [False, True])
def test_release_flushes_all_unread_capture_frames(monkeypatch, streaming):
    c, engine = consumer()
    activate_without_consumer(c, monkeypatch, streaming)
    for i in range(183):
        engine.write(i)
    audio = c.stop_streaming() if streaming else c.drain()
    assert audio.size == 183 * FRAME_SIZE
    np.testing.assert_allclose(audio[::FRAME_SIZE] * 32767, np.arange(183), atol=.001)
    assert not c.capture_loss
    if streaming:
        engine.write(999)
        assert c.stop_streaming() is None  # idempotent: no new audio stolen


def test_five_second_main_thread_cpu_hog_does_not_drop_frames():
    c, engine = consumer()
    assert RING_SECONDS >= 30
    assert c.activate_streaming()
    done = threading.Event()

    def writer():
        try:
            for i in range(50):
                engine.write(i)
                time.sleep(.1)
        finally:
            done.set()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end:
            sum(range(1000))
        assert done.wait(2)
        audio = c.stop_streaming()
        assert len(audio) == 50 * FRAME_SIZE
        assert not c.capture_loss
    finally:
        thread.join(2)
        c.stop_streaming()


@pytest.mark.parametrize('streaming', [False, True])
def test_overrun_counts_actual_missing_frames_and_preserves_available_history(monkeypatch, caplog, streaming):
    c, engine = consumer()
    activate_without_consumer(c, monkeypatch, streaming)
    for i in range(RING_FRAMES + 50):
        engine.write(i)
    with caplog.at_level(logging.WARNING):
        audio = c.stop_streaming() if streaming else c.drain()
    assert len(audio) == RING_FRAMES * FRAME_SIZE
    assert c.capture_loss and c.dropped_frames == 50
    assert '[CAP] dropped 50 frames' in caplog.text


@pytest.mark.parametrize('problem', ['overflow', 'epoch'])
def test_streaming_capture_reports_device_discontinuity(monkeypatch, problem):
    c, engine = consumer()
    activate_without_consumer(c, monkeypatch)
    engine.write(1)
    if problem == 'overflow':
        engine._overflow_count += 1
    engine.write(2, epoch=1 if problem == 'epoch' else 0)
    assert len(c.stop_streaming()) == FRAME_SIZE * 2
    assert c.capture_loss
    c._app._dictation_consumer = c
    c._app._show_outcome_chip = Mock()
    assert recovery.capture_problem(c._app, None)
    c._app._show_outcome_chip.assert_called_once_with(recovery.LOST, 'error')


@pytest.mark.parametrize('preroll,expected_frames', [(0, 0), (300, 3), (400, 4), (500, 5)])
def test_hold_preroll_is_configurable_and_supersedes_strict_duck(monkeypatch, preroll, expected_frames):
    at = time.perf_counter()
    app = NS(config={'dictation': {'hold_preroll_ms': preroll}},
             _speech_presence_capture=speech_presence.HoldEvidence(at),
             _hold_capture_duck_confirmed_at=at + .18)
    c, engine = consumer(app)
    for i in range(10):
        engine.write(i, at=at - (9-i)*.1 + .00001)
    activate_without_consumer(c, monkeypatch)
    audio = c.stop_streaming()
    assert (0 if audio is None else len(audio)//FRAME_SIZE) == expected_frames
    if expected_frames:
        assert app._speech_presence_capture.first_kept_block_start >= at - preroll / 1000


def test_previous_hold_cannot_be_rewound_into_next(monkeypatch):
    app = NS(config={}, _speech_presence_capture=speech_presence.HoldEvidence(time.perf_counter()))
    c, engine = consumer(app)
    activate_without_consumer(c, monkeypatch)
    engine.write(123)
    assert c.stop_streaming() is not None
    app._speech_presence_capture = speech_presence.HoldEvidence(time.perf_counter())
    assert c.activate_streaming()
    engine.write(456, at=time.perf_counter()+.11)
    audio = c.stop_streaming()
    assert len(audio) == FRAME_SIZE
    np.testing.assert_allclose(audio * 32767, 456, atol=.001)


@pytest.mark.parametrize('strong_speech', [False, True])
def test_retained_preroll_keeps_544_press_grace_and_real_onset_speech(monkeypatch, strong_speech):
    at = time.perf_counter()
    evidence = speech_presence.HoldEvidence(at, at + .12)
    app = NS(config={}, model_rate=16000, _speech_presence_capture=evidence)
    c, engine = consumer(app)
    for i in range(10):
        engine.write(i, at=at - (9-i)*.1 + .00001)
    activate_without_consumer(c, monkeypatch)
    for i in range(10):
        engine.write(i, at=at + (i+1)*.1)
    audio = c.stop_streaming()
    grace, press, _ = evidence.offsets()
    assert 300 < press <= 400 and grace > press
    detector = speech_presence.SpeechPresence()

    def score(pcm):
        scores = np.zeros(len(pcm)//512, np.float32)
        # Mechanical/start sound stays inside the timestamp-based grace.
        first = int(press / 32)
        scores[first:first+4] = .18
        if strong_speech:
            scores[0] = .6  # genuine onset speech in the retained pre-roll
        return scores

    detector._model = score
    monkeypatch.setattr(speech_presence, '_presence', detector)
    assert speech_presence.skip_decode(audio, app, path='streaming') is (not strong_speech)


def segment(**changes):
    fields = dict(text='A complete ordinary sentence.', avg_logprob=-.3,
                  no_speech_prob=.1, compression_ratio=1.2, temperature=0.0,
                  start=0.0, end=1.0)
    return NS(**(fields | changes))


@pytest.fixture
def build_worker():
    def build(responses, duration=2.6, partial=''):
        audio = np.ones(int(16000 * duration), np.float32) * .1
        calls = []
        responses = iter(responses)

        def transcribe(pcm, **params):
            calls.append((len(pcm), params))
            response = next(responses)
            if callable(response):
                response = response()
            return iter(response), NS(language='en')

        app = NS(config={'dictation': {'final_decode_timeout_s': 1}}, model_rate=16000,
                 device_type='cpu', model_lock=threading.Lock(), model=NS(transcribe=transcribe),
                 _filter_dictation_language=lambda text, info: text)
        session = NS(app=app, stop_event=threading.Event(), cancel_event=threading.Event(),
                     _last_partial=partial, _release_tail_signals={'speech_level': .1})
        worker = StreamingWorker(session)
        return worker, session, audio, calls
    return build


@pytest.mark.parametrize('ratio', [37.25, 30.4, 23.1, 25.9, 13.5, 9.4])
def test_logged_compression_rejection_recovers_with_one_clean_slate_retry(build_worker, ratio):
    worker, session, audio, calls = build_worker([[segment(compression_ratio=ratio)], [segment()]])
    _, _, text = recovery.stream_final(worker, audio, {'temperature': (0, .2, .4, .6)})
    assert text == segment().text
    assert len(calls) == 2
    assert calls[0][1]['temperature'] == 0.0
    assert calls[1][1]['temperature'] == .2
    assert all(call[1]['condition_on_previous_text'] is False for call in calls)
    assert calls[1][1]['initial_prompt'] is None


def test_backstop_logprob_rejection_recovers_from_already_shown_partial(build_worker):
    # 1.6 s / avg_logprob -.758 backstop: simulated repeated final text.
    worker, session, audio, calls = build_worker(
        [[segment(text='loop loop loop loop', avg_logprob=-.758)]], duration=1.6,
        partial='The accepted partial sentence.')
    _, _, text = recovery.stream_final(worker, audio, {})
    assert text == session._last_partial
    assert len(calls) == 1


def test_long_recovery_is_chunked_once_without_temperature_ladders(build_worker):
    worker, session, audio, calls = build_worker(
        [[segment(compression_ratio=37.25)]] +
        [[segment(text=t)] for t in ('Opening sentence.', 'Middle sentence.', 'Next thought.', 'Final words.')],
        duration=17.4)
    _, _, text = recovery.stream_final(worker, audio, {})
    assert text.endswith('Final words.')
    assert len(calls) == 5 and max(n for n, _ in calls[1:]) <= 5*16000
    assert all(p['temperature'] == .2 for _, p in calls[1:])


def test_failed_retry_keeps_audio_and_both_displays_get_truthful_feedback(build_worker):
    worker, session, audio, calls = build_worker([[segment(compression_ratio=13.5)], [segment(compression_ratio=9.4)]])
    _, _, text = recovery.stream_final(worker, audio, {})
    assert not text and session._decode_failure == recovery.UNCLEAR
    np.testing.assert_array_equal(session.app._hold_retry_audio.audio, audio)
    for full in (True, False):
        overlay = NS(flash_done_and_fade=Mock())
        if full:
            overlay.show_empty_result = Mock()
        else:
            overlay.update_text = Mock()
        session._overlay = overlay
        session._mark_done = Mock()
        assert recovery.deliver_failure(session, 2.6)
        shown = overlay.show_empty_result if full else overlay.update_text
        assert shown.call_args.args[0] == recovery.UNCLEAR
        assert 'no speech' not in shown.call_args.args[0].lower()
        session._mark_done.assert_called_once()


def test_try_again_redecodes_retained_audio_instead_of_the_retry_command(build_worker):
    worker, session, audio, calls = build_worker([[segment(text='Try again.')], [segment()]])
    old = np.ones(16000, np.float32) * .05
    recovery.retain(session.app, old, {}, 'failed')
    _, _, text = recovery.stream_final(worker, audio, {})
    assert text == segment().text
    assert calls[0][0] == len(audio) and calls[1][0] == len(old)


def test_stalled_native_decoder_has_bounded_wait_and_no_job_accumulation(build_worker):
    entered, release = threading.Event(), threading.Event()

    def stuck():
        entered.set()
        release.wait(2)
        return [segment()]

    worker, session, audio, calls = build_worker([stuck])
    session.app.config['dictation']['final_decode_timeout_s'] = .08
    started = time.monotonic()
    try:
        assert not recovery.stream_final(worker, audio, {})[2]
        assert entered.is_set() and time.monotonic() - started < .4
        assert session.app.model_lock.locked()  # native job still owns it
        assert not recovery.stream_final(worker, audio, {})[2]
        assert len(calls) == 1
    finally:
        release.set()
        assert session.app._hold_decode_job.done.wait(2)
    assert not session.app.model_lock.locked()


def test_compact_cpu_has_no_unbounded_partial_decode_before_final(build_worker):
    worker, session, _, _ = build_worker([])
    session._cpu_final_only = False  # Compact used to miss the CPU policy.
    session.stop_event.set()
    worker._loop_partials = Mock()
    worker._final_pass = Mock()
    worker.run()
    worker._loop_partials.assert_not_called()
    worker._final_pass.assert_called_once()


def test_model_lock_wait_counts_against_final_deadline(build_worker):
    worker, session, audio, calls = build_worker([[segment()]])
    session.app.config['dictation']['final_decode_timeout_s'] = .05
    session.app.model_lock.acquire()
    try:
        started = time.monotonic()
        assert not recovery.stream_final(worker, audio, {})[2]
        assert time.monotonic() - started < .3 and not calls
    finally:
        session.app.model_lock.release()
        session.app._hold_decode_job.done.wait(2)


def finalizing_session():
    session = StreamingSession.__new__(StreamingSession)
    app = NS(recording=False, _stop_in_flight=True, config={'streaming_mode': True},
             _capslock_lifecycle_lock=threading.Lock())
    session.app = app
    app._streaming_session = session
    session._state = session.STATE_FINALIZING
    session._state_lock = threading.Lock()
    session._capture_cleanup_lock = threading.Lock()
    session._capture_cleaned = True
    session._finished_notified = False
    session._parking_enabled = True
    session.stop_event, session.cancel_event = threading.Event(), threading.Event()
    session._direct_paste = False
    session._last_pasted = False
    session._overlay = Mock()
    session._decode_audio = np.ones(16000, np.float32)
    session._decode_params = {}

    def finished(owner):
        if app._streaming_session is owner:
            app._streaming_session = None
    app._on_streaming_session_finished = finished
    return session, app


def test_new_capslock_hold_cancels_old_final_and_cannot_publish_late_result():
    session, app = finalizing_session()
    app.start_recording = Mock()
    HotkeyCluster._capslock_start_streaming(app)
    app.start_recording.assert_called_once_with(streaming=True)
    assert session.cancel_event.is_set() and app._stop_in_flight is False
    assert app._hold_retry_audio.reason == 'superseded'
    app._schedule_ui = Mock()
    session.on_final('A stale native result')
    app._schedule_ui.assert_not_called()


def test_capture_cannot_be_stolen_before_final_snapshot_finishes():
    session, app = finalizing_session()
    session._decode_audio = None
    recovery.supersede_final(app)
    assert app._streaming_session is session and not session.cancel_event.is_set()


def test_batch_guard_failure_has_recovery_and_failure_is_not_empty_speech(build_worker):
    worker, session, audio, calls = build_worker([[segment(compression_ratio=30.4)], [segment()]])
    result = recovery.decode_hold(session.app, audio, {}, len(audio)/16000)
    assert result.text == segment().text and result.diag_path == 'hold_618'
    worker, session, audio, calls = build_worker([[segment(compression_ratio=13.5)], [segment(compression_ratio=9.4)]])
    session.app._show_outcome_chip = Mock()
    result = recovery.decode_hold(session.app, audio, {}, len(audio)/16000)
    assert not result.text and result.diag_path == 'failed_618'
    assert result.language_rejected  # legacy missing-data retry must not run a third time
    session.app._show_outcome_chip.assert_called_once_with(recovery.UNCLEAR, 'warning')
