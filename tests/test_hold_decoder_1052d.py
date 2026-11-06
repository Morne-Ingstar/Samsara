"""One original capture, bounded chunks and honest release decisions (1052d)."""
import logging
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import decode_recovery as decoder, speech_presence
from samsara.hold_final_policy import preserve_preview_tail
from samsara.streaming import StreamingSession
from samsara.transcript_gates import LOOP_WARNING
from tests.test_silent_hold_387 import make_worker

RATE = 16000


def evidence(seconds, spans=(), *, level=.9):
    samples = round(seconds * RATE)
    scores = np.zeros((samples + 511) // 512)
    for start, end in spans:
        scores[round(start * RATE / 512):round(end * RATE / 512)] = level
    return speech_presence.Presence(True, max_probability=float(max(scores, default=0)),
        frames=len(scores), probabilities=tuple(scores), samples=samples)


def segment(text, start=0., end=1., **changes):
    return NS(**(dict(text=' ' + text, start=start, end=end, avg_logprob=-.2,
        no_speech_prob=.1, compression_ratio=1.1, temperature=0., words=None) | changes))


def configure(make_worker, monkeypatch, duration=45, spans=None):
    worker, session = make_worker([])
    audio = np.full(round(duration * RATE), .03, np.float32)
    measured = evidence(duration, [(0, duration)] if spans is None else spans)
    monkeypatch.setattr(speech_presence, 'measure', lambda *a: measured)
    session.app.device_type = 'cpu'
    session.app._filter_dictation_language = lambda *a: True
    session.app._show_outcome_chip = Mock()
    session._stop_capture.return_value = audio
    return worker, session, audio


@pytest.mark.parametrize('duration', [0, .064, 1, 15, 20])
def test_short_hold_is_one_unmodified_slice(duration):
    audio = np.arange(round(duration * RATE), dtype=np.float32)
    chunks = decoder.chunk_audio(audio, RATE, evidence(duration))
    assert len(chunks) == bool(len(audio))
    if chunks:
        assert (chunks[0].start, chunks[0].end) == (0, len(audio))


@pytest.mark.parametrize('duration', [20.01, 45, 90, 300])
def test_chunks_cover_original_samples_without_compacting_or_crossing_30s(duration):
    audio = np.arange(round(duration * RATE), dtype=np.float32)
    chunks = decoder.chunk_audio(audio, RATE, evidence(duration, [(0, duration)]))
    assert chunks[0].core_start == 0 and chunks[-1].core_end == len(audio)
    assert all(a.core_end == b.core_start for a, b in zip(chunks, chunks[1:]))
    assert all(0 < c.end - c.start <= 25 * RATE for c in chunks)
    assert all(np.shares_memory(audio[c.start:c.end], audio) for c in chunks)
    np.testing.assert_array_equal(np.concatenate([audio[c.core_start:c.core_end] for c in chunks]), audio)


def test_chunk_seam_prefers_confirmed_thinking_pause_and_has_padding():
    audio = np.zeros(45 * RATE, np.float32)
    measured = evidence(45, [(0, 10), (11, 21), (21.4, 45)])
    first = decoder.chunk_audio(audio, RATE, measured)[0]
    assert 21 * RATE <= first.core_end <= 21.4 * RATE  # Last confirmed pause.
    assert first.end - first.core_end == RATE // 4
    # .1 is not strictly below the pause threshold.
    measured = evidence(45, [(0, 45)], level=.1)
    assert decoder.chunk_audio(audio, RATE, measured)[0].core_end == round(24.5 * RATE)


@pytest.mark.parametrize('device,ladder', [('cpu', (0., .2)), ('cuda', (0., .2, .4, .6, .8, 1.))])
@pytest.mark.parametrize('profile', [0., (0., .2), None])
def test_device_controls_ladder_even_for_fast_profile(make_worker, device, ladder, profile):
    worker, session = make_worker([], params={'temperature': profile})
    session.app.device_type = device
    effective = decoder._params(session.app, worker._final_params(), 12)
    assert effective['temperature'] == ladder
    assert effective['condition_on_previous_text'] is False
    assert effective['vad_filter'] is False and effective['without_timestamps'] is False


@pytest.mark.parametrize('path', ['stream', 'batch'])
def test_one_loop_retry_at_point_four_keeps_prompt_and_original_chunk(make_worker, monkeypatch, path):
    worker, session, audio = configure(make_worker, monkeypatch, 2)
    calls = []
    def transcribe(pcm, **params):
        calls.append((pcm, params))
        return iter([segment('beep beep beep beep' if len(calls) == 1 else 'The spoken sentence.')]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    params = dict(initial_prompt='Explicit prose hint.', hotwords='quilter')
    text = decoder.stream_final(worker, audio, params)[2] if path == 'stream' else decoder.decode_hold(session.app, audio, params, 2).text
    assert text == 'The spoken sentence.' and len(calls) == 2
    assert all(np.shares_memory(pcm, audio) for pcm, _ in calls)
    assert calls[0][1]['temperature'] == (0., .2)
    assert calls[1][1]['temperature'] == .4
    assert all(p['initial_prompt'] == 'Explicit prose hint.' for _, p in calls)


def test_persistent_loop_loses_only_its_chunk_warns_and_retains_complete_pcm(make_worker, monkeypatch, caplog):
    worker, session, audio = configure(make_worker, monkeypatch)
    calls = []
    def transcribe(pcm, **params):
        offset = (pcm.ctypes.data - audio.ctypes.data) // audio.itemsize / RATE
        calls.append((offset, params))
        return iter([segment('beep beep beep beep' if offset < 24.25 else 'Good later words remain.')]), NS(language='en')  # 1165: reject the same PCM in smaller pieces
    session.app.model.transcribe.side_effect = transcribe
    session._last_partial = 'An unrelated hallucinated preview.'
    with caplog.at_level(logging.WARNING):
        text = decoder.stream_final(worker, audio, {})[2]
    assert text == 'Good later words remain.' and len(calls) > 3  # 1165: failed long chunk is subdivided
    np.testing.assert_array_equal(session.app._hold_retry_audio.audio, audio)
    assert session._hold_failed_ranges == ((0., 24.5, 'repetition_loop'),)
    assert not session._hold_last_chunk_failed
    assert 'start_s=0.000 end_s=24.500' in caplog.text
    assert 'beep' not in caplog.text
    session._decode_failure = None
    assert not decoder.deliver_failure(session, 45)
    session.app._show_outcome_chip.assert_called_once_with(LOOP_WARNING, 'info', 8000)  # 1125: temporary information


def test_native_ladder_failure_has_no_five_second_recovery_or_preview_replacement(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 17.4)
    session.app.model.transcribe.side_effect = lambda *a, **kw: (iter([segment('Untrusted.', compression_ratio=13.5)]), NS(language='en'))
    session._last_partial = 'Previously shown but unconfirmed words.'
    assert decoder.stream_final(worker, audio, {})[2] == ''
    assert session.app.model.transcribe.call_count == 5  # 1165: original plus four bounded pieces
    assert session._decode_failure == decoder.UNCLEAR  # 1125: nothing typed
    np.testing.assert_array_equal(session.app._hold_retry_audio.audio, audio)


def test_cuda_to_cpu_fallback_is_judged_against_the_actual_cpu_ladder(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 2)
    session.app.device_type = 'cuda'
    def transcribe(*a, **kw):
        session.app.device_type = 'cpu'
        return iter([segment('Untrusted fallback.', temperature=.2, avg_logprob=-2.)]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    assert not decoder.stream_final(worker, audio, {})[2]
    assert session._hold_failed_ranges[0][2] == 'fallback_exhausted'
    assert session.app.model.transcribe.call_count == 1


def test_successful_shorter_final_wins_over_longer_preview(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 2)
    session._last_partial = 'Keep the project notes including invented words.'
    session.app.model.transcribe.side_effect = lambda *a, **kw: (iter([segment('Keep the project notes.')]), NS(language='en'))
    assert decoder.stream_final(worker, audio, {})[2] == 'Keep the project notes.'


@pytest.mark.parametrize('tail,expected', [('including the quiet words.', True), ('beep beep beep beep', False)])
def test_preview_tail_only_fills_failed_last_chunk_and_must_pass_loop_gate(tail, expected):
    session = NS(_last_partial='Keep the complete project notes ' + tail, _hold_last_chunk_failed=True)
    text = preserve_preview_tail(session, 'Keep the complete project notes.')
    assert text.endswith(tail) is expected
    session._hold_last_chunk_failed = False
    assert preserve_preview_tail(session, 'Keep the complete project notes.') == 'Keep the complete project notes.'


def test_partials_freeze_accepted_prefix_at_pause_and_decode_trailing_window(make_worker, monkeypatch):
    worker, session = make_worker([])
    audio = np.full(45 * RATE, .03, np.float32)
    current = [12]
    calls = []
    session.app._dictation_consumer.snapshot_streaming_audio.side_effect = lambda: audio[:current[0] * RATE]
    def measure(pcm, rate):
        origin = (pcm.ctypes.data - audio.ctypes.data) // audio.itemsize / RATE
        duration = len(pcm) / RATE
        return evidence(duration, [(max(0, a-origin), min(duration, b-origin))
                                  for a, b in [(0, 10), (12, 22), (24, 45)] if a < origin + duration and b > origin])
    monkeypatch.setattr(speech_presence, 'measure', measure)
    calls_for_gate = speech_presence.Presence(False)
    monkeypatch.setattr(speech_presence._presence, 'measure', lambda *a: calls_for_gate)
    def transcribe(pcm, **params):
        offset = (pcm.ctypes.data - audio.ctypes.data) // audio.itemsize / RATE
        calls.append((offset, len(pcm) / RATE, params))
        selected = [(text, a, b) for text, a, b in [('First sentence.', 0, 10), ('Second sentence.', 12, 22), ('Third sentence.', 24, 44)]
                    if offset <= a and b <= current[0]]
        return iter([segment(t, a-offset, b-offset) for t, a, b in selected]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    assert worker._transcribe_partial() == 'First sentence.'
    current[0] = 24
    assert worker._transcribe_partial() == 'First sentence. Second sentence.'
    current[0] = 45
    assert worker._transcribe_partial() == 'First sentence. Second sentence. Third sentence.'
    assert calls[1][0] > 10 and calls[2][0] > 22
    assert all(length <= 25 for _, length, _ in calls)
    assert all(not params['without_timestamps'] for _, _, params in calls)


def test_empty_final_delivery_cannot_resurrect_rejected_preview(monkeypatch):
    from samsara import hold_recording_recovery, tutorial_capture
    session = StreamingSession.__new__(StreamingSession)
    session.app = NS()
    session._state_lock = threading.Lock()
    session._hold_failed_ranges = ((0., 2., 'repetition_loop'),)
    session._decode_failure = LOOP_WARNING
    session._hold_quality_warning = False
    session._last_partial = 'beep beep beep beep'
    session._overlay = NS(show_empty_result=Mock(), flash_done_and_fade=Mock())
    session._mark_done = Mock()
    monkeypatch.setattr(tutorial_capture, 'final_current', lambda *a, **k: True)
    prepare = Mock(side_effect=AssertionError('must not restore preview'))
    monkeypatch.setattr(hold_recording_recovery, 'prepare_final', prepare)
    session._deliver_final(None, None, 2, 0)
    prepare.assert_not_called()
    session._overlay.show_empty_result.assert_called_once_with(LOOP_WARNING)


def test_lazy_deadline_and_lock_ownership_remain_bounded(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 2)
    entered, release = threading.Event(), threading.Event()
    def transcribe(*a, **kw):
        entered.set()
        def lazy():
            release.wait(2)
            yield segment('Late words.')
        return lazy(), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    session.app.config['dictation'] = {'final_decode_timeout_s': .06}
    try:
        started = time.monotonic()
        assert decoder.stream_final(worker, audio, {})[2] == ''
        assert entered.is_set() and time.monotonic() - started < .4
        assert session.app.model_lock.locked()
        assert decoder.stream_final(worker, audio, {})[2] == ''
        assert session.app.model.transcribe.call_count == 1
    finally:
        release.set()
        assert session.app._hold_decode_job.done.wait(2)
    assert not session.app.model_lock.locked()
