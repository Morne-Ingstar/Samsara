"""1043: deterministic skipped native windows through real hold callers.

Tone bursts mark time ranges, not recognition quality. The decoder fixture
reproduces a first-window-only or missing-middle result; no model is loaded.
"""
import logging
import threading
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara import decode_recovery, speech_presence
from tests.test_silent_hold_387 import make_worker

RATE = 16000
LABELS = ('Amber garden ready.', 'Blue fence measured.', 'Copper tools arranged.',
          'Distant carrots planted.', 'Emerald leaves watered.', 'Fresh soil checked.',
          'Golden gate closed.', 'Hidden bench painted.', 'Indigo seeds stored.')


def segment(text, start, end):
    return NS(text=' ' + text, start=start, end=end, avg_logprob=-.2,
              no_speech_prob=.1, compression_ratio=1.1, temperature=0.,
              words=[NS(word=' ' + text, start=start, end=end, probability=.9)])


@pytest.fixture
def capture(make_worker, monkeypatch):
    def measure(pcm, rate):
        scores = tuple(.9 if np.any(pcm[i:i+512]) else 0. for i in range(0,len(pcm),512))
        return speech_presence.Presence(True, max_probability=max(scores, default=0),
            probabilities=scores, frames=len(scores), samples=len(pcm))
    monkeypatch.setattr(speech_presence._presence, 'measure', measure)

    def build(duration, *, missing='tail', silent_after=None):
        pcm = np.zeros(duration * RATE, np.float32)
        timeline = []
        for index, start in enumerate(range(1, duration - 3, 10)):
            if silent_after is not None and start >= silent_after:
                break
            pcm[start * RATE:(start + 3) * RATE] = .03
            timeline.append((LABELS[index], start, start + 3))
        worker, session = make_worker([])
        app = session.app
        app.device_type = 'cpu'
        app.config['dictation'] = {'speech_presence_gate': True}  # 1052d chunk evidence
        app._filter_dictation_language = lambda text, info: True
        session._stop_capture.return_value = pcm
        app._dictation_consumer.snapshot_streaming_audio.return_value = pcm
        calls = []

        def decode(audio, **params):
            offset = (audio.ctypes.data - pcm.ctypes.data) // pcm.itemsize / RATE
            end = offset + len(audio) / RATE
            calls.append((offset, end, dict(params)))
            selected = [(text, a, b) for text, a, b in timeline if a >= offset and b <= end]
            if len(audio) > 30 * RATE:
                selected = [(text, a, b) for text, a, b in selected
                            if (a < 25 if missing in ('tail', 'empty') else not 30 <= a < 60)]
            if missing == 'empty' and offset >= 24:
                selected = []
            return iter(segment(text, a - offset, b - offset) for text, a, b in selected), NS(language='en')

        app.model.transcribe.side_effect = decode
        return worker, session, pcm, timeline, calls
    return build


@pytest.mark.parametrize('duration', [45, 90])
@pytest.mark.parametrize('path', ['partial', 'stream_final', 'batch'])
def test_all_windows_survive_real_hold_callers(capture, duration, path):
    worker, session, pcm, timeline, calls = capture(duration)
    # Pin the injected native failure before exercising its production caller.
    raw, _ = session.app.model.transcribe(pcm)
    assert len(list(raw)) < len(timeline)
    calls.clear()
    if path == 'partial':
        text = worker._transcribe_partial()
    elif path == 'stream_final':
        worker._final_pass()
        text = session.on_final.call_args.args[0]
    else:
        result = decode_recovery.decode_hold(session.app, pcm, worker._final_params(), duration)
        text = result.text
    # 1052d: a first late preview shows only its trailing window; finals cover all.
    expected = timeline if path != 'partial' else [item for item in timeline if item[1] >= calls[0][0]]
    assert text == ' '.join(item[0] for item in expected)
    # 1021 may trim only the digital-silence suffix before the native call.
    assert all(end - start <= 25.001 for start, end, _ in calls)  # 1052d bounded from first call
    assert all(end - start <= 25.001 for start, end, _ in calls[1:])
    assert all(not params['without_timestamps'] for _, _, params in calls)
    assert all(not params['condition_on_previous_text'] for _, _, params in calls)
    assert not hasattr(session.app, '_hold_retry_audio')
    assert not session.app.model_lock.locked()


def test_middle_window_loss_is_repaired_without_erasing_later_native_text(capture):
    worker, session, pcm, timeline, _ = capture(90, missing='middle')
    segments, _, text = decode_recovery.stream_final(worker, pcm, worker._final_params())
    assert text == ' '.join(item[0] for item in timeline)
    assert [s.start for s in segments] == [a for _, a, _ in timeline]
    assert [s.words[0].start for s in segments] == [a for _, a, _ in timeline]


@pytest.mark.parametrize('path', ['partial', 'stream_final', 'batch'])
def test_silent_second_half_adds_nothing_and_does_not_retry(capture, caplog, path):
    worker, session, pcm, timeline, calls = capture(90, silent_after=30)
    with caplog.at_level(logging.WARNING):
        if path == 'partial':
            text = worker._transcribe_partial()
        elif path == 'batch':
            text = decode_recovery.decode_hold(session.app, pcm, worker._final_params(), 90).text
        else:
            text = decode_recovery.stream_final(worker, pcm, worker._final_params())[2]
    expected = timeline if path != 'partial' else []  # 1052d first late preview has no frozen prefix
    assert text == ' '.join(item[0] for item in expected)
    assert all(start < 30 or path == 'partial' for start, _, _ in calls)  # 1052d: no spoken tail is lost
    assert not hasattr(session.app, '_hold_retry_audio')
    assert not any('[HOLD_FINAL] failed' in r.message for r in caplog.records)


def test_empty_spoken_window_warns_with_range_and_keeps_entire_audio(capture, caplog):
    worker, session, pcm, timeline, _ = capture(90, missing='empty')
    with caplog.at_level(logging.WARNING):
        _, _, text = decode_recovery.stream_final(worker, pcm, worker._final_params())
    assert text == ' '.join(item[0] for item in timeline[:3])
    retained = session.app._hold_retry_audio
    np.testing.assert_array_equal(retained.audio, pcm)
    assert retained.reason == 'empty_spoken_chunk'  # 1052d explicit chunk failure
    pcm[0] = .9
    assert retained.audio[0] == 0  # Retry owns a stable copy.
    warning = next(r.message for r in caplog.records if '[HOLD_FINAL] failed' in r.message)
    start, end, _ = session._hold_failed_ranges[0]
    assert f'start_s={start:.3f} end_s={end:.3f}' in warning  # 1052d source-time core
    assert 'audio_retained=1' in warning
    assert not any(label in warning for label in LABELS)


def test_recovery_error_preserves_native_words_and_retains_audio(capture, caplog):
    worker, session, pcm, timeline, _ = capture(45)
    decode = session.app.model.transcribe.side_effect
    def failing(audio, **params):
        if (audio.ctypes.data - pcm.ctypes.data) // pcm.itemsize >= 24 * RATE:
            raise RuntimeError('synthetic window failure')
        return decode(audio, **params)
    session.app.model.transcribe.side_effect = failing
    with caplog.at_level(logging.WARNING):
        text = decode_recovery.stream_final(worker, pcm, worker._final_params())[2]
    assert text == ' '.join(item[0] for item in timeline[:3])
    np.testing.assert_array_equal(session.app._hold_retry_audio.audio, pcm)
    assert any('chunk_decode_failed' in r.message for r in caplog.records)


def test_context_overlap_deduplicates_recovery_only():
    original = segment('We checked the northern gate.', 0, 24)
    recovered = segment('northern gate. The carrots are ready.', 23.8, 28)
    result = [original]
    decode_recovery._append(result, [recovered])
    assert ''.join(s.text for s in result).strip() == 'We checked the northern gate. The carrots are ready.'
    repeated = segment('The carrots are ready.', 30, 33)
    decode_recovery._append(result, [repeated])
    assert len(result) == 3


def test_continuous_pcm_windows_cover_every_sample_and_stay_bounded():
    pcm = np.full(90*RATE, .01, np.float32)
    chunks = decode_recovery.chunk_audio(pcm, RATE, speech_presence.Presence(False))
    assert chunks[0].start == 0 and chunks[-1].end == len(pcm)
    assert all(0 < c.end-c.start <= 25*RATE for c in chunks)
    assert all(right.core_start == left.core_end for left,right in zip(chunks,chunks[1:]))


def test_middle_gap_context_cannot_repeat_next_native_words():
    # 1052d has ordered chunks, no out-of-order recovery context.
    result = [segment('Finish the garden. The golden gate', 56, 61)]
    original = segment('The golden gate is closed.', 60, 64)
    decode_recovery._append(result, [original])
    assert ''.join(s.text for s in result).strip() == 'Finish the garden. The golden gate is closed.'
    assert original.text.strip() == 'The golden gate is closed.'


def test_native_timestamps_in_final_padding_do_not_invent_missing_audio():
    clipped = decode_recovery._clip_timing([segment('Complete recording.', 0, 91.5)], 90)
    assert clipped[0].end == 90
    assert clipped[0].start == 0


@pytest.mark.parametrize('timing', ['missing', 'invalid_recovery'])
def test_invalid_timestamps_keep_native_text_and_recoverable_audio(capture, caplog, timing):
    worker, session, pcm, timeline, _ = capture(45)
    decode = session.app.model.transcribe.side_effect
    def bad_timing(audio, **params):
        segments, info = decode(audio, **params)
        segments = list(segments)
        if timing == 'missing' and (audio.ctypes.data - pcm.ctypes.data) // pcm.itemsize >= 24 * RATE:
            for s in segments:
                del s.start
        elif (audio.ctypes.data - pcm.ctypes.data) // pcm.itemsize >= 24 * RATE:
            for s in segments:
                s.end = float('nan')
        return iter(segments), info
    session.app.model.transcribe.side_effect = bad_timing
    with caplog.at_level(logging.WARNING):
        text = decode_recovery.stream_final(worker, pcm, worker._final_params())[2]
    expected = timeline if timing == 'missing' else timeline[:3]
    assert text == ' '.join(item[0] for item in expected)  # 1052d missing timing warns without erasing recognized words
    np.testing.assert_array_equal(session.app._hold_retry_audio.audio, pcm)
    assert any('audio_retained=1' in r.message for r in caplog.records)


def test_cancel_between_windows_never_launches_another_native_decode(capture):
    worker, session, pcm, _, calls = capture(90)
    def decode(audio, **params):
        calls.append(1)
        session.cancel_event.set()
        return iter([segment('Must not deliver.', 0, 1)]), NS(language='en')
    session.app.model.transcribe.side_effect = decode
    assert not decode_recovery.stream_final(worker, pcm, worker._final_params())[2]
    assert len(calls) == 1
    assert session.app._hold_decode_job.done.wait(1)


@pytest.mark.parametrize('device,short,long', [('cpu', 8, 32), ('cuda', 15, 60)])
def test_default_budget_scales_for_long_holds_but_explicit_deadline_is_honored(device, short, long):
    app = NS(device_type=device, config={})
    before = time.monotonic()
    assert short - .1 < decode_recovery._deadline(app, 2) - before < short + .1
    assert long - .1 < decode_recovery._deadline(app, 90) - before < long + .1
    app.config = {'dictation': {'final_decode_timeout_s': .05}}
    assert 0 < decode_recovery._deadline(app, 90) - time.monotonic() <= .050001
