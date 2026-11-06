"""1170: speech alignment, fallback isolation and replacement, without a model."""
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara import decode_recovery as decoder, segment_speech_guard, speech_presence
from samsara.hold_final_policy import FinalAudit, current_audit
from samsara.vocabulary_decode import transcribe_with_vocabulary
from tests.test_hold_decoder_1052d import configure, evidence, segment
from tests.test_silent_hold_387 import make_worker  # noqa: F401


RATE = 16000
STRAYS = ['Box', 'Cube', 'Video', 'News', 'F', 'Button', 'Error', 'Ruff', 'Out']


def decode(make_worker, monkeypatch, pieces, *, mode='stream', spans=((0., 1.), (3., 4.))):
    worker, session, audio = configure(make_worker, monkeypatch, 6., spans=spans)
    session.app.model.transcribe.side_effect = lambda *a, **kw: (iter(pieces), NS(language='en'))
    if mode == 'stream':
        return decoder.stream_final(worker, audio, worker._final_params())[2], session
    return decoder.decode_hold(session.app, audio, worker._final_params(), 6.).text, session


@pytest.mark.parametrize('mode', ['stream', 'batch'])
@pytest.mark.parametrize('stray', STRAYS)
@pytest.mark.parametrize('position', ['middle', 'tail'])
def test_arbitrary_short_words_outside_speech_are_not_accepted(make_worker, monkeypatch, mode, stray, position):
    prefix = segment('Keep the complete project notes.', 0., 1.)
    continuation = segment('Include the final sentence.', 3., 4.)
    start = 1.7 if position == 'middle' else 4.7
    invented = segment(stray + '.', start, start + .2, avg_logprob=-1.45,
                       temperature=.2, no_speech_prob=.61)
    pieces = [prefix, invented, continuation] if position == 'middle' else [prefix, continuation, invented]
    text, session = decode(make_worker, monkeypatch, pieces, mode=mode)
    assert text == 'Keep the complete project notes. Include the final sentence.'
    assert session.app.model.transcribe.call_count == 1
    assert session.app.model.transcribe.call_args.kwargs['word_timestamps'] is False


@pytest.mark.parametrize('mode', ['stream', 'batch'])
def test_good_segment_cannot_hide_a_confident_hotword_in_a_pause(make_worker, monkeypatch, mode):
    words = [NS(word=' Keep the notes.', start=.1, end=.9),
             NS(word=' Cube.', start=1.7, end=1.9),
             NS(word=' Continue here.', start=3.1, end=3.9)]
    original = segment('Keep the notes. Cube. Continue here.', 0., 4., words=words)
    text, _ = decode(make_worker, monkeypatch, [original], mode=mode)
    assert text == 'Keep the notes. Continue here.'
    assert len(original.words) == 3 and 'Cube' in original.text


@pytest.mark.parametrize('mode', ['stream', 'batch'])
def test_near_zero_probability_boundary_fragment_cannot_hide_in_a_good_segment(make_worker, monkeypatch, mode):
    words = [NS(word=' Keep these words.', start=.1, end=.7, probability=.9),
             NS(word=' wall.', start=.7, end=.76, probability=.000025),
             NS(word=' Continue here.', start=3.1, end=3.9, probability=.8)]
    original = segment('Keep these words. wall. Continue here.', 0., 4., words=words)
    text, _ = decode(make_worker, monkeypatch, [original], mode=mode)
    assert text == 'Keep these words. Continue here.'


@pytest.mark.parametrize('probability,duration', [(.01, .06), (.000025, .14), (None, .06)])
def test_probability_or_duration_alone_cannot_remove_a_supported_word(probability, duration):
    word = NS(word=' F.', start=.2, end=.2 + duration, probability=probability)
    candidate = segment('F.', .2, .8, words=[word])
    assert segment_speech_guard.screen([candidate], [(0, RATE)], RATE, 0., {}) == [candidate]


@pytest.mark.parametrize('mode', ['stream', 'batch'])
@pytest.mark.parametrize('changes', [dict(no_speech_prob=.5), dict(no_speech_prob=.61),
                                   dict(compression_ratio=20.07)])
def test_failed_fallback_cannot_borrow_good_prefix_speech(make_worker, monkeypatch, mode, changes):
    prefix = segment('Keep these clear words.', 0., 1.)
    bad = segment(' Button.', 3., 4., avg_logprob=-1.45, temperature=.2, **changes)
    text, _ = decode(make_worker, monkeypatch, [prefix, bad], mode=mode)
    assert text == prefix.text.strip()


@pytest.mark.parametrize('mode', ['stream', 'batch'])
def test_soft_supported_tail_survives_without_a_volume_floor(make_worker, monkeypatch, mode):
    prefix = segment('Keep the complete project notes.', 0., 1.)
    tail = segment('Include the quiet final words.', 3., 4., avg_logprob=-1.8, temperature=.2)
    worker, session, pcm = configure(make_worker, monkeypatch, 6., spans=[(0., 1.), (3., 4.)])
    pcm[3 * RATE:4 * RATE] = .0002
    session.app.model.transcribe.side_effect = lambda *a, **kw: (iter([prefix, tail]), NS(language='en'))
    if mode == 'stream':
        text = decoder.stream_final(worker, pcm, worker._final_params())[2]
    else:
        text = decoder.decode_hold(session.app, pcm, worker._final_params(), 6.).text
    assert text == 'Keep the complete project notes. Include the quiet final words.'


@pytest.mark.parametrize('stray', STRAYS)
def test_the_same_words_remain_legal_when_spoken(stray):
    candidate = segment(stray + '.', .2, .8, avg_logprob=-1.018)
    assert segment_speech_guard.screen([candidate], [(0, RATE)], RATE, 0., {}) == [candidate]


def test_chunk_word_alignment_uses_original_time_and_does_not_mutate_source():
    spoken = NS(word=' Box', start=.1, end=.4)
    stray = NS(word=' Error.', start=1.4, end=1.6)
    candidate = segment('Box Error.', 0., 2., words=[spoken, stray])
    result = segment_speech_guard.screen([candidate], [(10 * RATE, 11 * RATE)], RATE, 10., {})
    assert result[0].text == ' Box' and result[0].words == [spoken]
    assert candidate.words == [spoken, stray]


@pytest.mark.parametrize('bounds', [None, []])
def test_unknown_evidence_and_confirmed_silence_have_different_meanings(bounds):
    candidate = segment('Box', .2, .8)
    assert bool(segment_speech_guard.screen([candidate], bounds, RATE, 0., {})) is (bounds is None)


@pytest.mark.parametrize('start,end', [(1., 1.), (float('nan'), 1.)])
def test_short_token_without_valid_alignment_cannot_supply_speech(start, end):
    candidate = segment('F', start, end)
    assert segment_speech_guard.screen([candidate], [(0, RATE)], RATE, 0., {}) == []


@pytest.mark.parametrize('text', ['F', 'Keep the complete project notes.'])
@pytest.mark.parametrize('score', [-.2, -1.45])
def test_missing_legacy_segment_timing_keeps_existing_guard_for_confident_output(text, score):
    candidate = segment(text, None, None, avg_logprob=score)
    result = segment_speech_guard.screen([candidate], [(0, RATE)], RATE, 0., {})
    assert result == ([candidate] if score > -1. else [])
    assert segment_speech_guard.screen([candidate], [], RATE, 0., {}) == []


def test_borderline_redecode_replaces_the_first_candidate(make_worker, monkeypatch):
    worker, session, pcm = configure(make_worker, monkeypatch, 6., spans=[(0., 6.)])
    first = segment('Sampled candidate to replace.', 0., 5., avg_logprob=-1.018, temperature=.2)
    replacement = segment('Keep this deterministic result.', 0., 5., avg_logprob=-1.018)
    def transcribe(*a, **kw):
        return iter([replacement if kw['temperature'] == 0. else first]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    text = decoder.stream_final(worker, pcm, worker._final_params())[2]
    assert text == replacement.text.strip() and first.text.strip() not in text
    assert session.app.model.transcribe.call_count == 2


@pytest.mark.parametrize('changes,reason', [
    ({'compression_ratio': 4.}, 'compression_ratio'),
    ({'no_speech_prob': .9}, 'fallback_exhausted'),
])
def test_unsafe_final_preserves_refusal_before_stray_screen(make_worker, monkeypatch, changes, reason):
    from samsara import dictation_retry

    worker, session, pcm = configure(make_worker, monkeypatch, 2.)
    pcm[:] = .0061
    session._release_tail_signals = dict(speech_level=.0061, speech_floor=.008)
    refused = segment('Unsafe synthetic final.', 0., 2., avg_logprob=-1.018,
                      temperature=1., **changes)
    session.app.model.transcribe.side_effect = lambda *a, **kw: (iter([refused]), NS(language='en'))
    original_screen, screened = segment_speech_guard.screen, []

    def observe(segments, *args):
        screened.append(list(segments))
        return original_screen(segments, *args)

    monkeypatch.setattr(segment_speech_guard, 'screen', observe)
    segments, _, text = decoder.stream_final(worker, pcm, worker._final_params())
    assert not segments and not text
    assert screened == [[]]  # Only the guard's accepted output may reach 1170.
    assert session.app.model.transcribe.call_count == 1
    retained = dictation_retry.live(session.app)
    assert retained.reason == reason
    np.testing.assert_array_equal(retained.audio, pcm)


def test_acceptance_sees_original_output_before_stray_words_are_removed(make_worker, monkeypatch):
    worker, session, pcm = configure(make_worker, monkeypatch, 2., spans=[(0., .8)])
    spoken, stray = segment('Keep these words.', .1, .6), segment('Box.', 1.5, 1.8)
    session.app.model.transcribe.side_effect = lambda *a, **kw: (iter([spoken, stray]), NS(language='en'))
    original_guard, original_screen = worker._decoder_guard, segment_speech_guard.screen
    events = []

    def guard(segments, *args, **kwargs):
        events.append(('guard', list(segments)))
        return original_guard(segments, *args, **kwargs)

    def screen(segments, *args):
        events.append(('screen', list(segments)))
        return original_screen(segments, *args)

    monkeypatch.setattr(worker, '_decoder_guard', guard)
    monkeypatch.setattr(segment_speech_guard, 'screen', screen)
    assert decoder.stream_final(worker, pcm, worker._final_params())[2] == spoken.text.strip()
    assert events == [('guard', [spoken, stray]), ('screen', [spoken, stray])]
    assert session.app.model.transcribe.call_count == 1


@pytest.mark.parametrize('timing', [{}, {'word_timestamps': False}, {'word_timestamps': True}])
def test_commit_redecode_keeps_vocabulary_but_screens_unspoken_hotwords(make_worker, monkeypatch, timing):
    _, session, pcm = configure(make_worker, monkeypatch, 6., spans=[(0., 1.)])
    app = session.app
    app.voice_training_window.custom_vocab = ['Cube']
    app.model.transcribe.side_effect = lambda *a, **kw: (
        iter([segment('Keep these words.', .1, .9), segment(' Cube.', 3., 3.3)]), NS(language='en'))
    result, _ = transcribe_with_vocabulary(app, pcm, path='commit_redecode', language='en', **timing)
    assert ''.join(s.text for s in result).strip() == 'Keep these words.'
    assert 'Cube' in app.model.transcribe.call_args.kwargs['hotwords']
    assert {key: app.model.transcribe.call_args.kwargs[key] for key in timing} == timing
    assert ('word_timestamps' in app.model.transcribe.call_args.kwargs) == bool(timing)


@pytest.mark.parametrize('timing', [False, True])
def test_ordinary_hold_preserves_requested_word_timing_and_screens_after_decode(make_worker, monkeypatch, timing):
    worker, session, pcm = configure(make_worker, monkeypatch, 6., spans=[(0., 1.)])
    spoken = segment('Keep these words.', .1, .9)
    stray = segment('Box.', 3., 3.3)
    session.app.model.transcribe.side_effect = lambda *a, **kw: (iter([spoken, stray]), NS(language='en'))
    params = dict(worker._final_params(), word_timestamps=timing)
    result = decoder.stream_final(worker, pcm, params)[2]
    assert result == 'Keep these words.'
    assert session.app.model.transcribe.call_count == 1
    assert session.app.model.transcribe.call_args.kwargs['word_timestamps'] is timing


def test_non_pcm_commit_adapter_keeps_existing_guards_in_charge():
    result = (iter([segment('Keep this legacy adapter result.')]), NS(language='en'))
    assert segment_speech_guard.screen_commit(result, object(), RATE, {}) is result


def test_unsupported_token_never_reaches_corrected_postprocessing(make_worker, monkeypatch):
    worker, session, pcm = configure(make_worker, monkeypatch, 6., spans=[(0., 1.)])
    session.app.model.transcribe.side_effect = lambda *a, **kw: (
        iter([segment('Keep these words.', .1, .9), segment(' Button.', 3., 3.3)]), NS(language='en'))
    worker._final_pass()
    session.app.voice_training_window.apply_corrections.assert_called_once_with('Keep these words.')
    assert session.on_final.call_args.args == ('Keep these words.',)


def test_alignment_audit_contains_counts_and_never_transcript(caplog):
    candidate = segment('Box.', 2., 2.2)
    audit = FinalAudit()
    token = current_audit.set(audit)
    try:
        with caplog.at_level('INFO'):
            assert segment_speech_guard.screen([candidate], [(0, RATE)], RATE, 0., {}) == []
    finally:
        current_audit.reset(token)
    assert audit.dropped == {'unsupported_speech': 1}
    assert 'Box' not in caplog.text and 'segments_dropped=1' in caplog.text
