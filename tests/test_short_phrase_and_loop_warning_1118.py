"""1118: recover quiet speech once; report only omissions in the final."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import decode_recovery as decoder, speech_presence
from samsara.transcript_gates import LOOP_WARNING
from tests.test_hold_decoder_1052d import configure, evidence, segment
from tests.test_silent_hold_387 import make_worker


PARAMS = dict(no_speech_threshold=.6, log_prob_threshold=-1.,
              compression_ratio_threshold=2.4, initial_prompt='Synthetic vocabulary.')


def decode(path, worker, session, audio):
    if path == 'batch':
        return decoder.decode_hold(session.app, audio, PARAMS, len(audio) / 16000).text
    session._overlay = NS(show_empty_result=Mock(), flash_done_and_fade=Mock())
    session._mark_done = Mock()
    text = decoder.stream_final(worker, audio, PARAMS)[2]
    decoder.deliver_failure(session, len(audio) / 16000)
    return text


@pytest.mark.parametrize('path', ['stream', 'batch'])
def test_preview_loop_drop_then_clean_final_has_no_warning(make_worker, monkeypatch, path):
    worker, session, audio = configure(make_worker, monkeypatch, 1.2)
    session.app._dictation_consumer.snapshot_streaming_audio.return_value = audio
    attempts = iter([[segment('beep beep beep beep')], [segment('Please move this.')]])
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter(next(attempts)), NS(language='en'))
    assert worker._transcribe_partial() is None
    session.app._show_outcome_chip.assert_not_called()
    assert decode(path, worker, session, audio) == 'Please move this.'
    session.app._show_outcome_chip.assert_not_called()


@pytest.mark.parametrize('path', ['stream', 'batch'])
def test_loop_retry_replaced_by_clean_final_has_no_warning(make_worker, monkeypatch, path):
    worker, session, audio = configure(make_worker, monkeypatch, 1.2)
    attempts = iter([[segment('beep beep beep beep')], [segment('Please move this.')]])
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter(next(attempts)), NS(language='en'))
    assert decode(path, worker, session, audio) == 'Please move this.'
    assert session.app.model.transcribe.call_count == 2
    session.app._show_outcome_chip.assert_not_called()


@pytest.mark.parametrize('path', ['stream', 'batch'])
@pytest.mark.parametrize('loop', ['drop', 'strip'])
def test_final_omission_warns_and_keeps_original_audio(make_worker, monkeypatch, path, loop):
    worker, session, audio = configure(make_worker, monkeypatch, 1.2)
    if loop == 'drop':
        segments = [segment('Keep these four useful words.'), segment(' beep beep beep beep')]
    else:
        segments = [segment('Keep these four useful words. beep beep beep beep')]
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter(segments), NS(language='en'))
    assert decode(path, worker, session, audio) == 'Keep these four useful words.'
    session.app._show_outcome_chip.assert_called_once_with(LOOP_WARNING, 'warning')
    assert 'left out' in LOOP_WARNING and 'try again' in LOOP_WARNING
    np.testing.assert_array_equal(session.app._hold_retry_audio.audio, audio)
    assert not np.shares_memory(session.app._hold_retry_audio.audio, audio)
    assert session.app.model.transcribe.call_count == (2 if loop == 'drop' else 1)
    # "try again" decodes the retained recording, not merely the missing bit.
    session.app.model.transcribe.side_effect = lambda pcm, **k: (
        iter([segment('Try again.' if np.all(pcm == .01) else 'The complete recovered sentence.')]), NS(language='en'))
    request_audio = np.full_like(audio, .01)
    assert decode(path, worker, session, request_audio) == 'The complete recovered sentence.'


@pytest.mark.parametrize('path', ['stream', 'batch'])
def test_quiet_short_empty_decode_retries_once_with_same_pcm_and_guards(make_worker, monkeypatch, path):
    worker, session, audio = configure(make_worker, monkeypatch, 1.2)
    # Synthetic equivalent of 384 ms low-probability speech, no p>.3 frames.
    monkeypatch.setattr(speech_presence, 'measure', lambda *a: evidence(1.2, [(.704, 1.088)], level=.28))
    calls = []
    def transcribe(pcm, **params):
        calls.append((pcm, params))
        return iter([] if len(calls) == 1 else [segment('Please move this.')]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    assert decode(path, worker, session, audio) == 'Please move this.'
    assert len(calls) == 2
    assert calls[0][1]['no_speech_threshold'] == .6
    assert calls[1][1]['no_speech_threshold'] is None
    for pcm, params in calls:
        assert np.shares_memory(pcm, audio)
        np.testing.assert_array_equal(pcm, audio)
        assert params['log_prob_threshold'] == -1.
        assert params['compression_ratio_threshold'] == 2.4
        assert params['initial_prompt'] == PARAMS['initial_prompt']
    session.app._show_outcome_chip.assert_not_called()


def test_relaxed_success_reaches_streaming_final_delivery(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 1.2)
    attempts = iter([[], [segment('Please move this.')]])
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter(next(attempts)), NS(language='en'))
    worker._final_pass()
    assert session.on_final.call_args.args == ('Please move this.',)
    assert session.app.model.transcribe.call_count == 2


@pytest.mark.parametrize('path', ['stream', 'batch'])
def test_true_silence_never_retries_or_types(make_worker, monkeypatch, path):
    worker, session, audio = configure(make_worker, monkeypatch, 1.2, spans=[])
    assert decode(path, worker, session, audio) == ''
    session.app.model.transcribe.assert_not_called()
    session.app._show_outcome_chip.assert_not_called()


@pytest.mark.parametrize('spans,known', [([(.8, .896)], True), ([(0., 1.2)], False)])
def test_weak_or_unknown_evidence_cannot_relax_native_gate(make_worker, monkeypatch, spans, known):
    worker, session, audio = configure(make_worker, monkeypatch, 1.2)
    measured = evidence(1.2, spans, level=.28)
    if not known:
        measured = speech_presence.Presence(False)
    monkeypatch.setattr(speech_presence, 'measure', lambda *a: measured)
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter([]), NS(language='en'))
    assert decoder.stream_final(worker, audio, PARAMS)[2] == ''
    assert session.app.model.transcribe.call_count == 1
    np.testing.assert_array_equal(session.app._hold_retry_audio.audio, audio)


@pytest.mark.parametrize('path', ['stream', 'batch'])
@pytest.mark.parametrize('returned', ['empty', 'signoff', 'loop', 'compression'])
def test_relaxed_failure_still_retains_audio_and_never_runs_a_third_decode(make_worker, monkeypatch, path, returned):
    worker, session, audio = configure(make_worker, monkeypatch, 1.2)
    outputs = {
        'empty': [],
        'signoff': [segment('Thank you.', no_speech_prob=.7)],
        'loop': [segment('beep beep beep beep')],
        'compression': [segment('Untrusted output.', compression_ratio=8.)],
    }
    attempts = iter([[], outputs[returned]])
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter(next(attempts)), NS(language='en'))
    assert decode(path, worker, session, audio) == ''
    assert session.app.model.transcribe.call_count == 2
    np.testing.assert_array_equal(session.app._hold_retry_audio.audio, audio)
    if returned != 'loop':
        if path == 'batch':
            session.app._show_outcome_chip.assert_called_once_with(decoder.UNCLEAR, 'warning')
        else:
            session.app._show_outcome_chip.assert_not_called()
            session._overlay.show_empty_result.assert_called_once_with(LOOP_WARNING)


def test_press_grace_alone_cannot_authorize_relaxed_decode(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 1.2, spans=[(0., .384)])
    worker._speech_presence_capture = NS(offsets=lambda: (650., 0., 0.))
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter([]), NS(language='en'))
    assert decoder.stream_final(worker, audio, PARAMS)[2] == ''
    assert session.app.model.transcribe.call_count == 1


def test_empty_cuda_to_cpu_fallback_keeps_actual_logprob_ladder(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 1.2)
    session.app.device_type = 'cuda'
    calls = []
    def transcribe(pcm, **params):
        calls.append(params)
        session.app.device_type = 'cpu'
        segments = [] if len(calls) == 1 else [segment('Untrusted output.', temperature=.2, avg_logprob=-2.)]
        return iter(segments), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    assert decoder.stream_final(worker, audio, PARAMS)[2] == ''
    assert len(calls) == 2 and calls[1]['temperature'] == (0., .2)
    assert session._hold_failed_ranges[0][2] == 'fallback_exhausted'
