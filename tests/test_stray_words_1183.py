"""Numeric incident features, synthetic prose and fake local decodes only."""
from copy import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara import decode_recovery as decoder, segment_speech_guard as guard
from samsara.hold_final_policy import FinalAudit, current_audit
from tests.test_hold_decoder_1052d import configure, segment
from tests.test_silent_hold_387 import make_worker  # noqa: F401


FEATURES = json.loads((Path(__file__).parent / 'fixtures/stray_word_features_1183.json').read_text())
RATE = 16000


def pieces(take=1, token='Me'):
    features = FEATURES[take]['segments'][0]
    words = [NS(word=text, **values) for text, values in
             zip((' Keep', ' these', ' notes.'), features['words'])]
    prefix = segment('Keep these notes.', features['start'], features['end'], words=words)
    if not take:
        return [prefix]
    features = FEATURES[take]['segments'][1]
    word = NS(word=' ' + token + '.', **features['words'][0])
    stray = segment(token + '.', features['start'], features['end'], words=[word],
                    **{key: features[key] for key in ('avg_logprob', 'no_speech_prob',
                                                      'compression_ratio', 'temperature')})
    return [prefix, stray]


def confirmation(original, origin, *, spoken=False, change=None):
    result = [decoder._offset(original[0], -origin)]
    if spoken:
        result.append(decoder._offset(original[1], -origin))
    if change:
        change(result)
    return result


@pytest.mark.parametrize('mode', ['stream', 'batch'])
@pytest.mark.parametrize('token', ['Money', 'Me', 'Up', 'Zed'])
@pytest.mark.parametrize('timing,probability', [((19.7, 19.84), .173583984375),
                                             ((19.68, 19.84), .129270195961)])
def test_supported_short_fragment_needs_independent_confirmation(make_worker, monkeypatch, mode, token, timing, probability):
    features = FEATURES[1]
    worker, session, audio = configure(make_worker, monkeypatch, features['samples'] / RATE,
                                        spans=features['speech_spans'])
    original = pieces(token=token)
    original[0].end = original[0].words[-1].end = timing[0]
    original[1].start, original[1].end = timing
    original[1].words[0].start, original[1].words[0].end = timing
    original[1].words[0].probability = probability
    calls = []
    def transcribe(pcm, **params):
        calls.append((pcm, params))
        assert np.shares_memory(pcm, audio)
        origin = (pcm.ctypes.data - audio.ctypes.data) / audio.itemsize / RATE
        return iter(original if len(calls) == 1 else confirmation(original, origin)), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    result = (decoder.stream_final(worker, audio, worker._final_params())[2] if mode == 'stream'
              else decoder.decode_hold(session.app, audio, worker._final_params(), len(audio)/RATE).text)
    assert result == 'Keep these notes.'
    assert len(calls) == 2
    assert calls[1][1]['temperature'] == 0. and calls[1][1]['word_timestamps'] is True
    assert len(calls[1][0]) / RATE <= 6.75
    assert original[1].text.strip() == token + '.'  # No source mutation.


@pytest.mark.parametrize('take', [0, 1])
def test_both_saved_takes_are_numeric_features_not_owner_audio_or_text(take):
    data = FEATURES[take]
    assert data['samples'] < data['captured_audio_ms'] * 16  # Saved chunks, not whole takes.
    assert all(isinstance(value, (int, float)) for s in data['segments'] for w in s['words'] for value in w.values())
    assert not any(key in json.dumps(data) for key in ('text', 'transcript', '.wav'))


def run_confirmation(monkeypatch, *, spoken=False, change=None, error=False):
    original = pieces()
    audio = np.zeros(FEATURES[1]['samples'], np.float32)
    calls = []
    def transcribe(app, pcm, **params):
        calls.append((pcm, params))
        if error:
            raise RuntimeError('Synthetic native failure')
        origin = (pcm.ctypes.data - audio.ctypes.data) / audio.itemsize / RATE
        return iter(confirmation(original, origin, spoken=spoken, change=change)), NS(language='en')
    monkeypatch.setattr('samsara.vocabulary_decode.transcribe_with_vocabulary', transcribe)
    audit = FinalAudit()
    token = current_audit.set(audit)
    try:
        result = guard.confirm_fragments(NS(model_rate=RATE), audio, original, {}, lambda: None)
    finally:
        current_audit.reset(token)
    return original, result, calls, audit


def test_genuinely_spoken_low_probability_word_is_kept(monkeypatch):
    original, result, calls, audit = run_confirmation(monkeypatch, spoken=True)
    assert result == original and len(calls) == 1 and not audit.dropped


@pytest.mark.parametrize('change', [
    lambda items: items.clear(),
    lambda items: setattr(items[0], 'words', None),
    lambda items: setattr(items[0], 'temperature', .2),
    lambda items: setattr(items[0], 'avg_logprob', -1.01),
    lambda items: setattr(items[0], 'no_speech_prob', .61),
    lambda items: setattr(items[0], 'compression_ratio', 2.41),
    lambda items: setattr(items[0].words[-1], 'probability', .49),
    lambda items: setattr(items[0].words[-1], 'end', 1.),
    lambda items: setattr(items[0].words[-1], 'word', ' unrelated'),
])
def test_uncorroborated_omission_never_erases_accepted_text(monkeypatch, change):
    original, result, calls, audit = run_confirmation(monkeypatch, change=change)
    assert result == original and len(calls) == 1 and not audit.dropped


def test_confirmation_failure_keeps_accepted_text(monkeypatch):
    original, result, calls, audit = run_confirmation(monkeypatch, error=True)
    assert result == original and len(calls) == 1 and not audit.dropped


def test_confirmation_audit_records_counts_only(monkeypatch, caplog):
    with caplog.at_level('INFO'):
        original, result, calls, audit = run_confirmation(monkeypatch)
    assert result == original[:1] and audit.dropped == {'unsupported_speech': 1}
    assert 'confirmed_fragments_dropped=1' in caplog.text
    assert 'Me' not in caplog.text and 'notes' not in caplog.text


@pytest.mark.parametrize('change', [
    lambda items: items.pop(0),  # A standalone one-word utterance is legal.
    lambda items: setattr(items[1].words[0], 'probability', .2),
    lambda items: setattr(items[1].words[0], 'probability', None),
    lambda items: setattr(items[1].words[0], 'end', 20.1),
    lambda items: setattr(items[1], 'words', None),
])
def test_ordinary_words_do_not_trigger_redecode(monkeypatch, change):
    original = pieces()
    change(original)
    def forbidden(*a, **k):
        pytest.fail('Ordinary accepted speech must not be decoded twice')
    monkeypatch.setattr('samsara.vocabulary_decode.transcribe_with_vocabulary', forbidden)
    assert guard.confirm_fragments(NS(model_rate=RATE), np.zeros(21*RATE), original, {}, lambda: None) == original


def test_first_take_without_boundary_fragment_is_not_redecoded(monkeypatch):
    original = pieces(0)
    def forbidden(*a, **k):
        pytest.fail('No suspicious fragment')
    monkeypatch.setattr('samsara.vocabulary_decode.transcribe_with_vocabulary', forbidden)
    assert guard.confirm_fragments(NS(model_rate=RATE), np.zeros(22*RATE), original, {}, lambda: None) == original


@pytest.mark.parametrize('mode', ['stream', 'batch'])
def test_confirmation_deadline_preserves_already_accepted_words(make_worker, monkeypatch, mode):
    worker, session, audio = configure(make_worker, monkeypatch, FEATURES[1]['samples']/RATE,
                                        spans=FEATURES[1]['speech_spans'])
    original = pieces()
    entered, release = threading.Event(), threading.Event()
    calls = []
    def transcribe(pcm, **params):
        calls.append(params)
        if len(calls) == 1:
            return iter(original), NS(language='en')
        def blocked():
            entered.set()
            release.wait(2)
            yield segment('Synthetic confirmation.')
        return blocked(), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    monkeypatch.setattr(decoder, '_remaining', lambda deadline: 0. if entered.is_set() else 2.)
    try:
        text = (decoder.stream_final(worker, audio, worker._final_params())[2] if mode == 'stream'
                else decoder.decode_hold(session.app, audio, worker._final_params(), len(audio)/RATE).text)
        assert entered.is_set() and len(calls) == 2
        assert text == 'Keep these notes. Me.'
    finally:
        release.set()
        assert session.app._hold_decode_job.done.wait(2)
