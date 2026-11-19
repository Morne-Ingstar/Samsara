import pytest
pytestmark = pytest.mark.skip(reason="1256 retry window reverted by 1265; re-enable when 604b0318 is re-landed")
"""1256: pause context, delivered spans and bounded fallback contracts."""
import threading
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara import decode_recovery as decoder, dictation_retry as retry, speech_presence
from tests.test_hold_decoder_1052d import evidence, segment


@pytest.mark.parametrize('text,delivered,expected', [
    ('Keep the revised project notes. More words.', 'Keep the complete project notes.', 'More words.'),
    ('Keep revised project notes. More words.', 'Keep complete project notes.', 'More words.'),
    ('Changed project notes. More words.', 'Original project notes.', 'More words.'),
    ('Keep project changed. More words.', 'Keep project notes.', 'More words.'),
    ('Keep extra project notes. More words.', 'Keep project notes.', 'More words.'),
    ('Keep project notes. Go to the store.', 'Keep project notes in the', 'Go to the store.'),
    ('Keep project notes. Go to the store.', 'Keep project notes.', 'Go to the store.'),
    ('Unrelated words at the end.', 'the', 'Unrelated words at the end.'),
    ('Keep project notes.', '', 'Keep project notes.'),
])
def test_delivered_substitutions_never_escape_as_insertions(text, delivered, expected):
    assert retry.remaining_text(text, delivered) == expected


@pytest.mark.parametrize('rate', [8000, 16000, 44100, 48000])
def test_evidence_slice_has_local_sample_count_pauses_and_speech_bounds(rate):
    seconds = 8
    measured = evidence(seconds, [(0, 1), (2, 6)])
    start, end = round(.377 * rate), round(7.41 * rate)
    local = decoder._slice_evidence(measured, rate, seconds * rate, start, end)
    assert decoder._valid_evidence(local, rate, end - start)
    assert local.samples == round((end - start) * 16000 / rate)
    assert decoder._pauses(local, rate, end - start)
    bounds = decoder._speech_bounds(local, rate, end - start)
    assert bounds and all(0 <= a < b <= end - start for a, b in bounds)
    assert local.passes()


@pytest.mark.parametrize('start,end', [(0, 0), (-1, 10), (1, 16001), (10, 5)])
def test_invalid_evidence_slice_stays_unknown(start, end):
    assert not decoder._slice_evidence(evidence(1), 16000, 16000, start, end).known


def test_foreign_evidence_stays_unknown():
    assert not decoder._slice_evidence(evidence(2), 16000, 16000, 0, 8000).known


def test_window_snaps_outward_to_confirmed_pauses_or_quarter_second():
    audio = np.zeros(10 * 16000, np.float32)
    a, b = retry._retry_window(audio, 16000, evidence(10, [(0, 2), (3, 7), (8, 10)]),
                               4 * 16000, 6 * 16000)
    assert 2 * 16000 <= a <= 3 * 16000
    assert 7 * 16000 <= b <= 8 * 16000
    assert retry._retry_window(audio, 16000, speech_presence.Presence(False), 64000, 96000) == (60000, 100000)


def test_short_missed_clause_keeps_its_normal_long_take_context():
    audio = np.zeros(45 * 16000, np.float32)
    assert retry._retry_window(audio, 16000, speech_presence.Presence(False),
                               4 * 16000, 6 * 16000) == (0, round(24.75 * 16000))


@pytest.mark.parametrize('device', ['cpu', 'cuda'])
def test_retry_keeps_device_ladder_and_cpu_bounds(device):
    app = NS(device_type=device)
    assert decoder._params(app, {'temperature': .4}, 2, retry=True)['temperature'] == decoder.temperature_ladder(app)
    cpu = decoder.cpu_decode_params({'temperature': .4, 'beam_size': 7}, 2, retry=True)
    assert cpu['temperature'] == (0., .2) and cpu['beam_size'] == 3 and cpu['best_of'] == 1


def slot_app(monkeypatch, seconds=10, missed=None):
    audio = np.arange(seconds * 16000, dtype=np.float32)
    app = NS(model_rate=16000, device_type='cpu', config={}, model_lock=threading.Lock())
    slot = decoder.RetainedAudio(audio, {'no_speech_threshold': .6}, 'synthetic',
                                 (missed or retry.MissedRange(4, 6),))
    monkeypatch.setattr(speech_presence, 'measure', lambda *a: speech_presence.Presence(False))
    return app, slot


def test_normal_context_is_input_only_and_success_does_not_fall_back(monkeypatch):
    app, slot = slot_app(monkeypatch, missed=retry.MissedRange(4, 6, 'Already delivered.'))
    calls = []
    def transcribe(app, pcm, params, deadline, cancel, guard=None, **kw):
        calls.append((pcm, params, deadline, cancel, kw))
        words = [NS(word=' Context', start=0, end=.2), NS(word=' Recovered.', start=.3, end=2.1),
                 NS(word=' Neighbor.', start=2.3, end=2.5)]
        return decoder.DecodedHold(segments=[segment('Context Recovered. Neighbor.', 0, 2.5, words=words)])
    monkeypatch.setattr(decoder, '_transcribe', transcribe)
    result = retry.decode_slot(app, slot)
    assert len(calls) == 1
    pcm, params, _, _, kw = calls[0]
    np.testing.assert_array_equal(pcm, slot.audio[60000:100000])
    assert np.shares_memory(pcm, slot.audio)
    assert params == {'no_speech_threshold': None, 'word_timestamps': True} and not kw
    assert ''.join(s.text for s in result.segments).strip() == 'Recovered.'
    assert result.segments[0].start == 4.05
    assert not result.missed


def test_short_wholly_missing_segment_keeps_exact_pcm_and_offsets(monkeypatch):
    app, slot = slot_app(monkeypatch)
    calls = []
    def transcribe(app, pcm, params, *args, **kwargs):
        calls.append((pcm, params, kwargs))
        return decoder.DecodedHold(segments=[segment('Recovered.', .1, 1.9)])
    monkeypatch.setattr(decoder, '_transcribe', transcribe)
    result = retry.decode_slot(app, slot)
    assert len(calls) == 1 and not calls[0][2]
    np.testing.assert_array_equal(calls[0][0], slot.audio[64000:96000])
    assert result.segments[0].start == 4.1 and result.segments[0].end == 5.9
    assert ''.join(s.text for s in result.segments).strip() == 'Recovered.'


def test_segment_crossing_context_boundary_needs_word_timing():
    assert not retry._range_segments([segment('Context and missing words.', 0, 3)], .25, 2.25, 3)


def test_failed_normal_stage_uses_prompt_and_same_deadline_for_bounded_fallback(monkeypatch):
    app, slot = slot_app(monkeypatch, missed=retry.MissedRange(2, 8, 'Already delivered.'))
    calls = []
    def transcribe(app, pcm, params, deadline, cancel, guard=None, **kw):
        calls.append((len(pcm), params, deadline, cancel, kw))
        return decoder.DecodedHold(segments=[] if len(calls) == 1 else [segment('Already changed. Recovered.', 0, 6)])
    monkeypatch.setattr(decoder, '_transcribe', transcribe)
    result = retry.decode_slot(app, slot)
    assert len(calls) == 2
    assert calls[0][1]['no_speech_threshold'] == .6
    assert calls[1][1]['initial_prompt'] == 'Already delivered.'
    assert calls[1][1]['no_speech_threshold'] is None and calls[1][4] == {'recovery': True}
    assert calls[0][2] is calls[1][2] and calls[0][3] is calls[1][3]
    assert ''.join(s.text for s in result.segments).strip() == 'Recovered.'
    assert slot.params == {'no_speech_threshold': .6} and slot.ranges[0].delivered == 'Already delivered.'


def test_empty_fallback_reports_original_range(monkeypatch):
    app, slot = slot_app(monkeypatch)
    monkeypatch.setattr(decoder, '_transcribe', lambda *a, **k: decoder.DecodedHold())
    result = retry.decode_slot(app, slot)
    assert result.missed == [retry.MissedRange(4, 6)]


@pytest.mark.parametrize('start,end,expected', [(0, 10, True), (.25, 10, False), (0, 9.75, False)])
def test_untimed_text_requires_the_entire_window_to_be_missed(start, end, expected):
    untimed = NS(text=' Accepted words.')
    assert retry._range_segments([untimed], start, end, 10) == ([untimed] if expected else [])


def test_cancellation_is_not_retried(monkeypatch):
    app, slot = slot_app(monkeypatch)
    calls = []
    def transcribe(*a, **k):
        calls.append(1)
        raise TimeoutError('synthetic cancellation')
    monkeypatch.setattr(decoder, '_transcribe', transcribe)
    with pytest.raises(TimeoutError):
        retry.decode_slot(app, slot)
    assert len(calls) == 1
