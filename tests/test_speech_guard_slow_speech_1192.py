"""1192: incident timings with synthetic prose; no private audio or transcript."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara import decode_recovery as decoder, segment_speech_guard as guard, speech_presence
from tests.test_hold_decoder_1052d import configure, segment
from tests.test_silent_hold_387 import make_worker  # noqa: F401


FEATURES = json.loads((Path(__file__).parent / 'fixtures/slow_speech_1192.json').read_text())
RATE = FEATURES['rate']
# Same number of tokens and a negation at the lost incident index, new prose.
SENTENCE = 'The scribe should never drop a word while I pause, but must keep the quiet tail as spoken.'
TOKENS = SENTENCE.split()


def pieces(chunk):
    result = []
    for data in FEATURES['chunks'][chunk]['segments']:
        words = [NS(word=' ' + TOKENS[w['reference_index']], start=w['start'], end=w['end'],
                    probability=w['probability']) for w in data['words']]
        result.append(segment(''.join(w.word for w in words).strip(), words=words,
                              **{key: data[key] for key in ('start', 'end', 'avg_logprob',
                                 'compression_ratio', 'no_speech_prob', 'temperature')}))
    return result


def presence():
    return speech_presence.Presence(True, frames=len(FEATURES['probabilities']),
        probabilities=tuple(FEATURES['probabilities']), samples=FEATURES['samples'])


def test_incident_features_are_numeric_and_cover_the_complete_capture():
    assert FEATURES['samples'] == 497600 and len(TOKENS) == 18
    assert decoder._speech_bounds(presence(), RATE, FEATURES['samples']) == [tuple(b) for b in FEATURES['bounds']]
    assert len(FEATURES['probabilities']) == (FEATURES['samples'] + 511) // 512
    for chunk in FEATURES['chunks']:
        for s in chunk['segments']:
            assert all(isinstance(value, (int, float)) for w in s['words'] for value in w.values())
    assert not any(key in json.dumps(FEATURES) for key in ('text', 'transcript', '.wav'))


def test_incident_word_timing_keeps_every_reference_word_once_without_source_mutation():
    outputs = []
    for i, chunk in enumerate(FEATURES['chunks']):
        original = pieces(i)
        snapshot = deepcopy(original)
        result = guard.screen(original, FEATURES['bounds'], RATE, chunk['origin_s'], {})
        outputs.extend(result)
        assert original == snapshot
        if i == 0:
            # The exhausted, near-zero-confidence seam copy still fails the
            # original majority-overlap policy. Its better second copy survives.
            assert result == original[:1]
        else:
            assert result == original
    assert ''.join(s.text for s in outputs).strip() == SENTENCE
    assert len([w for s in outputs for w in s.words]) == 18


@pytest.mark.parametrize('mode', ['stream', 'batch'])
def test_complete_slow_take_survives_both_final_paths_and_the_chunk_seam(make_worker, monkeypatch, mode):
    worker, session, audio = configure(make_worker, monkeypatch, FEATURES['samples'] / RATE)
    monkeypatch.setattr(speech_presence, 'measure', lambda *a: presence())
    session.app.device_type = 'cuda'
    calls = []

    def transcribe(pcm, **params):
        assert np.shares_memory(pcm, audio)
        origin = (pcm.ctypes.data - audio.ctypes.data) / audio.itemsize / RATE
        index = len(calls)
        calls.append((origin, len(pcm), params))
        assert index < 2, 'Alignment must not add another native decode'
        chunk = FEATURES['chunks'][index]
        assert (origin, len(pcm)) == (chunk['origin_s'], chunk['samples'])
        return iter(pieces(index)), NS(language='en')

    session.app.model.transcribe.side_effect = transcribe
    if mode == 'stream':
        text = decoder.stream_final(worker, audio, worker._final_params())[2]
    else:
        text = decoder.decode_hold(session.app, audio, worker._final_params(), len(audio) / RATE).text
    assert text == SENTENCE
    assert len(calls) == 2 and all(c[2]['word_timestamps'] is True for c in calls)


def test_commit_redecode_uses_the_same_slow_speech_policy(monkeypatch):
    monkeypatch.setattr(speech_presence, 'measure', lambda *a: presence())
    items = pieces(0) + [decoder._offset(s, 24.25) for s in pieces(1)]
    result, info = guard.screen_commit((iter(items), 'metadata'), np.zeros(FEATURES['samples'], np.float32), RATE, {})
    assert ''.join(s.text for s in result).strip() == SENTENCE
    assert info == 'metadata'


@pytest.mark.parametrize('gap', [1., 1.5, 2.])
@pytest.mark.parametrize('mode', ['stream', 'batch'])
def test_one_to_two_second_pauses_between_words_keep_the_whole_sentence(make_worker, monkeypatch, gap, mode):
    tokens = 'Please never omit my softly spoken ending.'.split()
    width = gap + .2
    spans = [(i * width + gap, (i + 1) * width) for i in range(len(tokens))]
    duration = len(tokens) * width + .5
    worker, session, audio = configure(make_worker, monkeypatch, duration, spans=spans)
    words = [NS(word=' ' + token, start=i * width, end=(i + 1) * width, probability=.9)
             for i, token in enumerate(tokens)]
    item = segment(' '.join(tokens), 0., len(tokens) * width, words=words)
    session.app.model.transcribe.side_effect = lambda *a, **kw: (iter([item]), NS(language='en'))
    params = dict(worker._final_params(), word_timestamps=True)
    text = (decoder.stream_final(worker, audio, params)[2] if mode == 'stream'
            else decoder.decode_hold(session.app, audio, params, duration).text)
    assert text == ' '.join(tokens)
    assert session.app.model.transcribe.call_count == 1


@pytest.mark.parametrize('change', [
    dict(avg_logprob=None), dict(avg_logprob=-1.01),
    dict(no_speech_prob=None), dict(no_speech_prob=.61),
    dict(compression_ratio=None), dict(compression_ratio=2.41),
])
def test_uncertain_segment_cannot_relax_alignment(change):
    word = NS(word=' Synthetic.', start=0., end=2., probability=.9)
    item = segment('Synthetic.', 0., 2., words=[word], **change)
    assert guard.screen([item], [(round(1.8 * RATE), 2 * RATE)], RATE, 0., {}) == []


@pytest.mark.parametrize('probability', [None, 0., .49, float('nan')])
def test_unsupported_weak_or_unknown_word_cannot_borrow_a_good_segments_speech(probability):
    words = [NS(word=' Keep.', start=.1, end=.4, probability=.9),
             NS(word=' Invented.', start=2., end=2.2, probability=probability)]
    item = segment('Keep. Invented.', 0., 3., words=words)
    result = guard.screen([item], [(0, RATE)], RATE, 0., {})
    assert result[0].words == words[:1]


@pytest.mark.parametrize('timing', [(2., 2.), (-.1, .2), (float('nan'), 2.), (3.1, 3.3)])
def test_invalid_or_out_of_segment_word_cannot_borrow_speech(timing):
    words = [NS(word=' Keep.', start=.1, end=.4, probability=.9),
             NS(word=' Invalid.', start=timing[0], end=timing[1], probability=.9)]
    item = segment('Keep. Invalid.', 0., 3., words=words)
    # The negative interval would otherwise overlap VAD: invalid timing must
    # not qualify even through the existing majority-overlap path.
    result = guard.screen([item], [(0, RATE)], RATE, 0., {})
    assert result[0].words == words[:1]


def test_confident_decoder_words_still_need_independent_speech_in_their_segment():
    item = segment('Keep this.', 2., 3., words=[NS(word=' Keep this.', start=2., end=3., probability=.99)])
    assert guard.screen([item], [(0, RATE)], RATE, 0., {}) == []
