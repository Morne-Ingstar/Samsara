"""1216 A/B: incident measurements with synthetic prose, no private takes."""
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from samsara import decode_recovery as decoder, segment_speech_guard as guard
from samsara.hold_final_policy import FinalAudit, current_audit
from tests.test_hold_decoder_1052d import configure, segment
from tests.test_silent_hold_387 import make_worker  # noqa: F401


RATE = 16000
A = [
    (6.98, .98583984375, 4.806, 7.418),
    (8.62, .99267578125, 8.070, 9.146),
    (14.96, .9951171875, 14.150, 15.418),
]
B = [
    (4.422, 6.298, 4.5, 6.4, -.3544921912252903, .038055419921875, .7391304347826086),
    (8.902, 10.874, 9.04, 16.46, -.3861177976314838, .212158203125, .9019607843137255),
    (2.310, 5.274, 2.28, 7.92, -.2277199007846691, .08984375, 1.1839080459770115),
]


@pytest.mark.parametrize('instant,probability,onset,end', A)
@pytest.mark.parametrize('origin', [0., 24.25])
def test_A_zero_duration_pronoun_with_measured_speech_survives(instant, probability, onset, end, origin):
    word = NS(word=' I', start=instant, end=instant, probability=probability)
    item = segment('I', onset, end, words=[word])
    snapshot = deepcopy(item)
    bounds = [(round((onset + origin) * RATE), round((end + origin) * RATE))]
    assert guard.screen([item], bounds, RATE, origin, {}) == [item]
    assert item == snapshot


@pytest.mark.parametrize('instant', [.4, 2., 3.])
def test_A_zero_duration_word_in_silence_cannot_borrow_segment_confidence(instant):
    words = [NS(word=' Keep', start=1.1, end=1.4, probability=.9),
             NS(word=' I', start=instant, end=instant, probability=.999)]
    item = segment('Keep I', 0., 4., words=words)
    result = guard.screen([item], [(RATE, 2 * RATE - 1)], RATE, 0., {})
    assert result[0].words == words[:1]


@pytest.mark.parametrize('probability', [0., .000025, .0099])
def test_A_existing_near_zero_confidence_anomaly_stays_rejected(probability):
    word = NS(word=' I', start=1.2, end=1.2, probability=probability)
    assert guard.screen([segment('I', 1., 2., words=[word])], [(RATE, 2 * RATE)], RATE, 0., {}) == []


@pytest.mark.parametrize('distinct', [False, True])
def test_A_multiple_words_cannot_claim_the_same_speech_instant(distinct):
    # Real rejected-alignment features, synthetic words. The two collapsed
    # words both sit in speech; even high confidence cannot locate both there.
    words = [NS(word=' Keep', start=21.98, end=22.3, probability=.1307373046875),
             NS(word=' these', start=22.3, end=22.3, probability=.97607421875),
             NS(word=' notes', start=22.31 if distinct else 22.3,
                end=22.31 if distinct else 22.3, probability=.52294921875),
             NS(word=' safely.', start=22.3, end=22.32, probability=.95654296875)]
    item = segment('Keep these notes safely.', 21.98, 22.32, words=words,
                   avg_logprob=-.6704101413488388, no_speech_prob=.320556640625,
                   compression_ratio=.7142857142857143)
    snapshot = deepcopy(item)
    result = guard.screen([item], [(328800, 364960)], RATE, 0., {})
    assert result[0].words == (words if distinct else [words[0], words[-1]])
    assert item == snapshot


@pytest.mark.parametrize('instant', [1., 2.])
def test_A_point_uses_existing_padded_boundaries_without_extra_tolerance(instant):
    word = NS(word=' I', start=instant, end=instant, probability=.99)
    item = segment('I', 1., 2., words=[word])
    assert guard.screen([item], [(RATE, 2 * RATE)], RATE, 0., {}) == [item]
    word.start = word.end = instant + (-1 if instant == 1 else 1) / RATE
    assert guard.screen([item], [(RATE, 2 * RATE)], RATE, 0., {}) == []


def opening(index=0):
    onset, end, start, stop, score, silence, compression = B[index]
    telemetry = dict(avg_logprob=score, no_speech_prob=silence,
                     compression_ratio=compression, temperature=0., seek=0)
    return [segment('I', 0., 2., **telemetry),
            segment('keep these notes.', start, stop, **telemetry)], [(round(onset * RATE), round(end * RATE))]


@pytest.mark.parametrize('index', range(3))
def test_B_incident_opening_is_corroborated_by_its_immediate_accepted_continuation(index):
    items, bounds = opening(index)
    snapshot = deepcopy(items)
    audit = FinalAudit()
    token = current_audit.set(audit)
    try:
        assert guard.screen(iter(items), bounds, RATE, 0., {}) == items
    finally:
        current_audit.reset(token)
    assert items == snapshot and not audit.dropped


@pytest.mark.parametrize('changes', [
    dict(avg_logprob=None), dict(avg_logprob=-1.01), dict(no_speech_prob=None),
    dict(no_speech_prob=.61), dict(compression_ratio=None), dict(compression_ratio=2.41),
    dict(temperature=.2), dict(temperature=None), dict(seek=None), dict(seek=3000),
    dict(start=.02), dict(end=1.9), dict(end=3.), dict(text=' I.'),
    dict(text=' I agree'), dict(text=''),
])
def test_B_weak_unknown_fallback_or_completed_opening_cannot_borrow_speech(changes):
    items, bounds = opening()
    # Change both confidence records: matching telemetry alone is insufficient.
    for key, value in changes.items():
        setattr(items[0], key, value)
        if key in ('avg_logprob', 'no_speech_prob', 'compression_ratio', 'temperature', 'seek'):
            setattr(items[1], key, value)
    assert guard.screen(items, bounds, RATE, 0., {}) == items[1:]


@pytest.mark.parametrize('key,value', [('avg_logprob', -.3), ('no_speech_prob', .2),
                                      ('compression_ratio', .8), ('temperature', .2), ('seek', 100)])
def test_B_different_native_window_cannot_corroborate_opening(key, value):
    items, bounds = opening()
    setattr(items[1], key, value)
    assert guard.screen(items, bounds, RATE, 0., {}) == items[1:]


@pytest.mark.parametrize('origin', [0., 24.25])
def test_B_silence_and_unknown_evidence_do_not_gain_a_new_acceptance_path(origin):
    items, _ = opening()
    assert guard.screen(items, [], RATE, origin, {}) == []
    assert guard.screen(items, None, RATE, origin, {}) == items


def test_B_only_the_first_capture_window_can_reanchor():
    items, bounds = opening()
    shifted = [(a + 24 * RATE, b + 24 * RATE) for a, b in bounds]
    assert guard.screen(items, shifted, RATE, 24., {}) == items[1:]


def test_B_rejected_or_nonadjacent_continuation_does_not_supply_evidence():
    items, bounds = opening()
    assert guard.screen(items[:1], bounds, RATE, 0., {}) == []
    intervening = segment('Reject.', 2.1, 2.2)
    assert guard.screen([items[0], intervening, items[1]], bounds, RATE, 0., {}) == items[1:]
    items[1].start = items[1].end + 1.
    assert guard.screen(items, bounds, RATE, 0., {}) == []


@pytest.mark.parametrize('bounds', [[(3 * RATE, 3 * RATE + 100), (5 * RATE, 6 * RATE)],
                                   [(6 * RATE, 7 * RATE)], [(20 * RATE, 21 * RATE)]])
def test_B_speech_elsewhere_cannot_reanchor_the_prefix(bounds):
    items, _ = opening()
    assert items[0] not in guard.screen(items, bounds, RATE, 0., {})


def test_B_valid_word_alignment_in_silence_is_not_overridden_by_segment_heuristic():
    items, bounds = opening()
    items[0].words = [NS(word=' I', start=.5, end=.7, probability=.99)]
    assert guard.screen(items, bounds, RATE, 0., {}) == items[1:]


def test_B_dropping_an_unrelated_tail_still_records_one_drop():
    items, bounds = opening()
    items.append(segment('Reject.', 8., 8.2))
    audit = FinalAudit()
    token = current_audit.set(audit)
    try:
        assert guard.screen(items, bounds, RATE, 0., {}) == items[:2]
    finally:
        current_audit.reset(token)
    assert audit.dropped == {'unsupported_speech': 1}


@pytest.mark.parametrize('mode', ['stream', 'batch', 'commit'])
def test_B_complete_decode_paths_retain_opening_without_an_extra_native_decode(make_worker, monkeypatch, mode):
    items, _ = opening()
    worker, session, audio = configure(make_worker, monkeypatch, 6.4, spans=[(4.672, 6.048)])
    snapshot = deepcopy(items)
    if mode == 'commit':
        output, info = guard.screen_commit((iter(items), 'metadata'), audio, RATE, {})
        assert list(output) == items and info == 'metadata'
    else:
        session.app.model.transcribe.side_effect = lambda *a, **k: (iter(items), NS(language='en'))
        text = (decoder.stream_final(worker, audio, worker._final_params())[2] if mode == 'stream'
                else decoder.decode_hold(session.app, audio, worker._final_params(), 6.4).text)
        assert text.strip() == 'I keep these notes.'
        assert session.app.model.transcribe.call_count == 1
    assert items == snapshot
