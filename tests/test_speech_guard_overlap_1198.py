"""1198: real numeric alignment evidence with synthetic words, no private PCM."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara import decode_recovery as decoder, segment_speech_guard as guard, speech_presence
from samsara.hold_final_policy import FinalAudit, current_audit
from tests.test_hold_decoder_1052d import configure, segment
from tests.test_silent_hold_387 import make_worker  # noqa: F401


FEATURES = json.loads((Path(__file__).parent / 'fixtures/speech_guard_overlap_1198.json').read_text())
RATE = FEATURES['rate']
SYNTHETIC = 'Synthetic example.'


def piece(**changes):
    data = FEATURES['segments'][0]
    return segment(SYNTHETIC, **{key: data[key] for key in (
        'start', 'end', 'avg_logprob', 'no_speech_prob', 'compression_ratio', 'temperature')},
        **changes)


def presence():
    return speech_presence.Presence(True, frames=len(FEATURES['probabilities']),
        probabilities=tuple(FEATURES['probabilities']), samples=FEATURES['samples'])


def test_incident_fixture_has_only_numeric_leaves_and_reproduces_local_speech():
    def numeric(value):
        if isinstance(value, dict):
            return all(numeric(v) for v in value.values())
        if isinstance(value, list):
            return all(numeric(v) for v in value)
        return type(value) in (int, float)
    assert numeric(FEATURES)
    assert FEATURES['samples'] == 176000
    assert len(FEATURES['probabilities']) == (FEATURES['samples'] + 511) // 512
    assert decoder._speech_bounds(presence(), RATE, FEATURES['samples']) == [tuple(b) for b in FEATURES['bounds']]
    assert FEATURES['segments'][0]['timed_words'] == 0
    assert FEATURES['segments'][0]['word_count'] == len(SYNTHETIC.split()) == 2


@pytest.mark.parametrize('timed_words', [None, []])
@pytest.mark.parametrize('origin', [0., 24.25])
def test_wide_uncertain_legacy_segment_keeps_local_speech_without_source_mutation(timed_words, origin):
    item = piece(words=timed_words)
    snapshot = deepcopy(item)
    bounds = [(a + round(origin * RATE), b + round(origin * RATE)) for a, b in FEATURES['bounds']]
    assert guard._overlaps(item, bounds, RATE, origin) is False
    assert guard._overlaps(item, bounds, RATE, origin, fraction=0.) is True
    audit = FinalAudit()
    token = current_audit.set(audit)
    try:
        assert guard.screen([item], bounds, RATE, origin, {}) == [item]
    finally:
        current_audit.reset(token)
    assert item == snapshot and not audit.dropped


@pytest.mark.parametrize('mode', ['stream', 'batch'])
def test_native_recovery_keeps_both_words_in_both_final_paths(make_worker, monkeypatch, mode):
    worker, session, audio = configure(make_worker, monkeypatch, FEATURES['samples'] / RATE)
    monkeypatch.setattr(speech_presence, 'measure', lambda *a: presence())
    session.app.device_type = 'cuda'
    calls = []
    original = [piece()]
    snapshot = deepcopy(original)

    def transcribe(pcm, **params):
        assert np.shares_memory(pcm, audio) and len(pcm) == FEATURES['samples']
        assert params['word_timestamps'] is False
        calls.append(params)
        assert len(calls) <= 2, 'No extra native alignment decode'
        if len(calls) == 1:
            assert params['temperature'] == (0., .2, .4, .6, .8, 1.)
            first = deepcopy(original)
            first[0].temperature = 1.
            return iter(first), NS(language='en')
        assert params['temperature'] == 0.
        return iter(original), NS(language='en')

    session.app.model.transcribe.side_effect = transcribe
    text = (decoder.stream_final(worker, audio, worker._final_params())[2] if mode == 'stream'
            else decoder.decode_hold(session.app, audio, worker._final_params(), len(audio) / RATE).text)
    assert text == SYNTHETIC and len(calls) == 2
    assert original == snapshot


def test_commit_redecode_uses_same_legacy_segment_support(monkeypatch):
    monkeypatch.setattr(speech_presence, 'measure', lambda *a: presence())
    item = piece()
    output, info = guard.screen_commit((iter([item]), 'metadata'),
        np.zeros(FEATURES['samples'], np.float32), RATE, {})
    assert list(output) == [item] and info == 'metadata'


@pytest.mark.parametrize('bounds', [[], [(0, 16000)], [(157088, 176000)]])
def test_uncertain_legacy_segment_cannot_borrow_speech_outside_its_timestamps(bounds):
    item = piece()
    if bounds == [(0, 16000)]:
        item.start = 2.
    assert guard.screen([item], bounds, RATE, 0., {}) == []


@pytest.mark.parametrize('changes', [dict(no_speech_prob=.5), dict(no_speech_prob=.9),
                                   dict(compression_ratio=2.41)])
def test_unsafe_fallback_still_cannot_use_small_local_overlap(changes):
    item = piece()
    item.temperature = 1.
    for key, value in changes.items():
        setattr(item, key, value)
    assert guard.screen([item], FEATURES['bounds'], RATE, 0., {}) == []


@pytest.mark.parametrize('timing', [(None, None), (0., 0.), (-1., 9.36),
                                  (float('nan'), 9.36), (0., float('inf'))])
def test_uncertain_legacy_segment_still_needs_valid_timing(timing):
    item = piece()
    item.start, item.end = timing
    assert guard.screen([item], FEATURES['bounds'], RATE, 0., {}) == []
