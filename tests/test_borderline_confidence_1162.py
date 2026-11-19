"""1162: synthetic borderline scores preserve a deterministic final and recovery."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from samsara import decode_recovery as decoder, dictation_retry, streaming
from samsara.borderline_confidence import supported
from samsara.hallucination_filter import decoder_failure_reason
from tests.test_hold_decoder_1052d import configure, segment
from tests.test_message_lifetime_1038 import display  # noqa: F401
from tests.test_silent_hold_387 import make_worker  # noqa: F401

RATE = 16000


def borderline(**changes):
    return segment(**(dict(text='Keep these synthetic words.', start=0., end=6.1,
                          avg_logprob=-1.018, compression_ratio=.43,
                          temperature=1., no_speech_prob=.1) | changes))


@pytest.mark.parametrize('device', ['cpu', 'cuda'])
@pytest.mark.parametrize('decoder_end', [6.1, 6.4])
def test_logged_case_delivers_temperature_zero_with_original_confidence(make_worker, monkeypatch, device, decoder_end):
    worker, session, audio = configure(make_worker, monkeypatch, 6.1)
    audio[:] = .0061
    session.app.device_type = device
    session._release_tail_signals = dict(speech_level=.0061, speech_floor=.008)
    session._last_partial = 'Synthetic preview'
    calls = []
    chosen = borderline(temperature=0., end=decoder_end)
    def transcribe(pcm, **params):
        calls.append((pcm, params))
        value = chosen if params['temperature'] == 0. else borderline(text='A different sampled candidate.', end=decoder_end)
        return iter([value]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    worker._final_pass()
    assert session.on_final.call_args.args == (chosen.text.strip(),)
    assert len(calls) == 2 and isinstance(calls[0][1]['temperature'], tuple)
    assert calls[1][1]['temperature'] == 0.
    assert all(np.shares_memory(pcm, audio) for pcm, _ in calls)
    assert all(p['log_prob_threshold'] == -1. for _, p in calls)
    assert all(p['no_speech_threshold'] == .6 for _, p in calls)
    assert chosen.avg_logprob == -1.018 and chosen.compression_ratio == .43
    assert not session._hold_failed_ranges and dictation_retry.live(session.app) is None
    assert streaming.diagnostics.record.call_args.args[0].outcome == 'low_confidence'
    session.app._show_outcome_chip.assert_not_called()


@pytest.mark.parametrize('score,level,compression,no_speech,expected', [
    (-1.018, .0061, .43, .1, True),
    (-1.1, .0061, 2.4, .6, True),
    (-1.10001, .0061, .43, .1, False),
    (-1.018, .0005, .43, .1, False),
    (-1.018, .0061, 2.4001, .1, False),
    (-1.018, .0061, .43, .6001, False),
    (-1.018, .0061, float('nan'), .1, False),
    (-1.018, .0061, .43, None, False),
])
def test_borderline_exception_requires_each_independent_signal(score, level, compression, no_speech, expected):
    candidate = borderline(avg_logprob=score, compression_ratio=compression,
                           no_speech_prob=no_speech, temperature=0.)
    assert supported([candidate], speech_level=level, zero_temperature=True) is expected


def test_borderline_exception_never_relaxes_partials_or_accepts_sampled_final():
    candidate = borderline(temperature=0.)
    assert decoder_failure_reason([candidate], speech_level=.0061) is None
    assert decoder_failure_reason([candidate], speech_level=.0061, partial=True) == 'partial_logprob'
    assert decoder_failure_reason([borderline()], speech_level=.0061) == 'fallback_exhausted'


@pytest.mark.parametrize('changes,level', [
    ({'compression_ratio': 4.}, .0061),
    ({}, .0004),
    ({'no_speech_prob': .9}, .0061),
    ({'avg_logprob': -2.}, .0061),
])
def test_unsafe_final_stays_refused_without_borderline_replay(make_worker, monkeypatch, changes, level):
    worker, session, audio = configure(make_worker, monkeypatch, 6.1)
    audio[:] = level
    session._release_tail_signals = dict(speech_level=.0061, speech_floor=.008)
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter([borderline(**changes)]), NS(language='en'))
    segments, _, text = decoder.stream_final(worker, audio, worker._final_params())
    assert not text and not segments
    # 1165: the refused long range also gets two guarded smaller pieces.
    assert session.app.model.transcribe.call_count == 3
    assert all(call.kwargs['temperature'] != 0. for call in session.app.model.transcribe.call_args_list)
    np.testing.assert_array_equal(dictation_retry.live(session.app).audio, audio)


def test_replay_still_screens_an_unsupported_outro_tail(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 6.1)
    audio[:] = .0061
    audio[4 * RATE:] = .0002
    session._release_tail_signals = dict(speech_level=.0061, speech_floor=.008)
    calls = []
    def transcribe(*args, **params):
        calls.append(params)
        if params['temperature'] == 0.:
            return iter([borderline(temperature=0., end=4.),
                         segment(' Please subscribe.', 4., 6.1, avg_logprob=-1.018,
                                 no_speech_prob=.6, compression_ratio=.43, temperature=0.)]), NS(language='en')
        return iter([borderline(end=4.)]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    segments, _, text = decoder.stream_final(worker, audio, worker._final_params())
    assert text == 'Keep these synthetic words.' and len(segments) == 1
    assert len(calls) == 2


@pytest.mark.parametrize('bad_replay', ['compression', 'silence_score', 'very_low_score', 'empty'])
def test_deterministic_replay_is_guarded_instead_of_blindly_admitted(make_worker, monkeypatch, bad_replay):
    worker, session, audio = configure(make_worker, monkeypatch, 6.1)
    audio[:] = .0061
    session._release_tail_signals = dict(speech_level=.0061, speech_floor=.008)
    changes = {'compression': {'compression_ratio': 4.}, 'silence_score': {'no_speech_prob': .9},
               'very_low_score': {'avg_logprob': -2.}, 'empty': {}}[bad_replay]
    def transcribe(*args, **params):
        replay = params['temperature'] == 0.
        candidates = ([] if bad_replay == 'empty' else [borderline(temperature=0., **changes)]) if replay else [borderline()]
        return iter(candidates), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    assert decoder.stream_final(worker, audio, worker._final_params())[2] == ''
    # 1165: the initial range and its two smaller pieces each retain 1162's replay guard.
    assert session.app.model.transcribe.call_count == 6
    assert all(len(call.args[0]) <= 5 * RATE for call in session.app.model.transcribe.call_args_list[2:])
    assert dictation_retry.live(session.app) is not None


def test_refusal_offers_working_try_again_on_both_displays(display, make_worker, monkeypatch, qapp):
    worker, session, audio = configure(make_worker, monkeypatch, 6.1)
    audio[:] = .0061
    session.app.listening_indicator = display.i
    session.app._schedule_ui = lambda fn, *args: fn(*args)
    session.app._paste_preserving_clipboard = Mock(return_value=(True, True))  # 1165: explicit receipt
    session.app.model.transcribe.side_effect = lambda *a, **k: (
        iter([borderline(compression_ratio=4.)]), NS(language='en'))
    session._last_partial = 'Synthetic preview'
    session._overlay = NS(show_empty_result=Mock(), flash_done_and_fade=Mock())
    session._mark_done = Mock()
    session.app._log_history = Mock()
    session.on_final.side_effect = lambda *a, **k: decoder.deliver_failure(session, 6.1)
    worker._final_pass()
    display.flush(); qapp.processEvents()
    assert display.text() == dictation_retry.EMPTY
    button = display.band().report
    assert button.isVisible() and button.text() == 'Try again'
    np.testing.assert_array_equal(dictation_retry.live(session.app).audio, audio)
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda name, target, **kw: target())
    # 1165: model the two original PCM ranges rather than repeat one whole-take fake.
    session.app.model.transcribe.side_effect = lambda pcm, **k: (
        iter([segment('Recovered' if len(pcm) > 3 * RATE else 'synthetic words.', .25, 1.5)]), NS(language='en'))
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    display.flush(); qapp.processEvents()
    session.app._paste_preserving_clipboard.assert_called_once_with(
        'Recovered synthetic words.', return_delivery_confirmation=True)  # 1165: confirmed insertion
