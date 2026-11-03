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

from samsara import decode_recovery, hold_windows, speech_presence
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
    monkeypatch.setattr(speech_presence._presence, 'measure', lambda pcm, rate:
        speech_presence.Presence(True, longest_s=1. if np.any(pcm) else 0.))

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
        app.config['dictation'] = {'speech_presence_gate': False}
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
            if missing == 'empty' and len(audio) <= 30 * RATE:
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
    assert text == ' '.join(item[0] for item in timeline)
    # 1021 may trim only the digital-silence suffix before the native call.
    assert timeline[-1][2] <= calls[0][1] <= duration
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
    assert text == ' '.join(item[0] for item in timeline)
    assert len(calls) == 1
    assert not hasattr(session.app, '_hold_retry_audio')
    assert not any('[HOLD_WINDOWS] unresolved' in r.message for r in caplog.records)


def test_empty_spoken_window_warns_with_range_and_keeps_entire_audio(capture, caplog):
    worker, session, pcm, timeline, _ = capture(90, missing='empty')
    with caplog.at_level(logging.WARNING):
        _, _, text = decode_recovery.stream_final(worker, pcm, worker._final_params())
    assert text == ' '.join(item[0] for item in timeline[:3])
    retained = session.app._hold_retry_audio
    np.testing.assert_array_equal(retained.audio, pcm)
    assert retained.reason == 'untranscribed_window'
    pcm[0] = .9
    assert retained.audio[0] == 0  # Retry owns a stable copy.
    warning = next(r.message for r in caplog.records if '[HOLD_WINDOWS] unresolved' in r.message)
    assert 'start_s=24.000 end_s=90.000' in warning
    assert 'audio_retained=1' in warning
    assert not any(label in warning for label in LABELS)


def test_recovery_error_preserves_native_words_and_retains_audio(capture, caplog):
    worker, session, pcm, timeline, _ = capture(45)
    decode = session.app.model.transcribe.side_effect
    def failing(audio, **params):
        if len(audio) <= 30 * RATE:
            raise RuntimeError('synthetic window failure')
        return decode(audio, **params)
    session.app.model.transcribe.side_effect = failing
    with caplog.at_level(logging.WARNING):
        text = decode_recovery.stream_final(worker, pcm, worker._final_params())[2]
    assert text == ' '.join(item[0] for item in timeline[:3])
    np.testing.assert_array_equal(session.app._hold_retry_audio.audio, pcm)
    assert any('window_decode_failed' in r.message for r in caplog.records)


def test_context_overlap_deduplicates_recovery_only():
    original = segment('We checked the northern gate.', 0, 24)
    recovered = segment('northern gate. The carrots are ready.', 23.8, 28)
    result = [original]
    hold_windows._append(result, recovered, recovered=True)
    assert ''.join(s.text for s in result).strip() == 'We checked the northern gate. The carrots are ready.'
    repeated = segment('The carrots are ready.', 30, 33)
    hold_windows._append(result, repeated, recovered=True)
    assert len(result) == 3  # Repetition outside overlapping context is intentional.


def test_continuous_pcm_windows_cover_every_sample_and_stay_bounded():
    pcm = np.full(90 * RATE, .01, np.float32)
    windows = list(hold_windows._windows(pcm, RATE, 0, 90))
    assert windows[0][0] == 0 and windows[-1][1] == len(pcm)
    assert all(0 < end - start <= 25 * RATE for start, end in windows)
    assert all(right[0] <= left[1] for left, right in zip(windows, windows[1:]))


def test_middle_gap_context_cannot_repeat_next_native_words():
    original = segment('The golden gate is closed.', 60, 64)
    recovered = segment('Finish the garden. The golden gate', 56, 61)
    item = hold_windows._trim_following_context(recovered, [original])
    assert item.text.strip() == 'Finish the garden.'
    assert original.text.strip() == 'The golden gate is closed.'


def test_native_timestamps_in_final_padding_do_not_invent_missing_audio():
    assert hold_windows._gaps([segment('Complete recording.', 0, 91.5)], 90) == []
    assert hold_windows._gaps([segment('End of padded window.', 91, 92)], 90) == [(0, 90)]


@pytest.mark.parametrize('timing', ['missing', 'invalid_recovery'])
def test_invalid_timestamps_keep_native_text_and_recoverable_audio(capture, caplog, timing):
    worker, session, pcm, timeline, _ = capture(45)
    decode = session.app.model.transcribe.side_effect
    def bad_timing(audio, **params):
        segments, info = decode(audio, **params)
        segments = list(segments)
        if timing == 'missing':
            for s in segments:
                del s.start
        elif len(audio) <= 30 * RATE:
            for s in segments:
                s.end = float('nan')
        return iter(segments), info
    session.app.model.transcribe.side_effect = bad_timing
    with caplog.at_level(logging.WARNING):
        text = decode_recovery.stream_final(worker, pcm, worker._final_params())[2]
    assert text == ' '.join(item[0] for item in timeline[:3])
    np.testing.assert_array_equal(session.app._hold_retry_audio.audio, pcm)
    assert any('audio_retained=1' in r.message for r in caplog.records)


def test_cancel_between_windows_never_launches_another_native_decode(capture):
    worker, session, pcm, _, calls = capture(90)
    def check():
        if calls:
            raise TimeoutError('cancelled')
    returned, _ = hold_windows.transcribe(session.app, pcm, worker._final_params(),
        lambda app, audio, **params: app.model.transcribe(audio, **{k: v for k, v in params.items() if k != 'path'}),
        check=check)
    with pytest.raises(TimeoutError):
        list(returned)
    assert len(calls) == 1


@pytest.mark.parametrize('device,short,long', [('cpu', 8, 32), ('cuda', 15, 60)])
def test_default_budget_scales_for_long_holds_but_explicit_deadline_is_honored(device, short, long):
    app = NS(device_type=device, config={})
    before = time.monotonic()
    assert short - .1 < decode_recovery._deadline(app, 2) - before < short + .1
    assert long - .1 < decode_recovery._deadline(app, 90) - before < long + .1
    app.config = {'dictation': {'final_decode_timeout_s': .05}}
    assert 0 < decode_recovery._deadline(app, 90) - time.monotonic() <= .050001
