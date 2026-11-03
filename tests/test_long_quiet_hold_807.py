"""807 acoustic fixtures and delivery contracts; no model, devices or bootstrap.

Saved probabilities were measured with the bundled CPU Silero. Fixture speech
is a public LibriSpeech sentence, never an owner recording. The real Whisper
comparison is separate evidence, rather than a machine-dependent unit test.
"""
import base64
import gzip
import json
import logging
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import command_clip_trim, speech_presence as presence, decode_recovery as decoder
from tests.test_silent_hold_387 import make_worker, _segment


FIXTURES = Path(__file__).parent / 'fixtures' / '807'
RATE = 16000


def clips():
    raw = gzip.decompress(base64.b64decode((FIXTURES / 'sentence.pcm16.gz.b64').read_bytes()))
    sentence = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768
    room = np.random.default_rng(807).normal(0, .0003, 60*RATE).astype(np.float32)
    cricket = room.copy()
    n = int(.12*RATE)
    pulse = .08*np.sin(2*np.pi*4500*np.arange(n)/RATE)*np.hanning(n)
    for at in range(1, 59, 3):
        cricket[at*RATE:at*RATE+n] += pulse.astype(np.float32)
    mixed = cricket.copy()
    mixed[28*RATE:28*RATE+len(sentence)] += sentence
    normal = np.pad(sentence, (0, 5*RATE-len(sentence)))
    quiet_hold = room.copy()
    quiet_hold[28*RATE:28*RATE+len(sentence)] += sentence * .04
    return dict(crickets=cricket, mixed=mixed, normal=normal,
                quiet=normal*.04, quiet_hold=quiet_hold)


def measured(name):
    saved = json.loads((FIXTURES / 'vad.json').read_text())[name]
    return presence.Presence(True, saved['total_s'], saved['longest_s'], saved['max_probability'],
                             len(saved['probabilities']), tuple(saved['probabilities']), saved['samples'])


def evidence(scores, samples=None):
    scores = tuple(scores)
    return presence.Presence(True, max_probability=max(scores, default=0), frames=len(scores),
                             probabilities=scores, samples=samples if samples is not None else len(scores)*512)


def word(text, start, end):
    return NS(word=text, start=start, end=end, probability=.9)


def bind_audio(session, monkeypatch, audio, result):
    session._stop_capture.return_value = audio
    session.app._dictation_consumer.snapshot_streaming_audio.return_value = audio
    detector = NS(measure=Mock(return_value=result))
    monkeypatch.setattr(presence, '_presence', detector)
    return detector


def test_real_silero_rejects_synthetic_cricket_fixture():
    audio, result = clips()['crickets'], measured('crickets')
    assert result.known and not result.passes()
    assert decoder._speech_bounds(result, RATE, len(audio)) == []


@pytest.mark.parametrize('name', ['mixed', 'normal', 'quiet', 'quiet_hold'])
def test_measured_speech_survives_with_context_and_no_gain(name):
    audio, result = clips()[name], measured(name)
    before = audio.copy()
    bounds = decoder._speech_bounds(result, RATE, len(audio))
    assert result.passes() and bounds
    chunks = decoder.chunk_audio(audio, RATE, result)
    assert chunks[0].core_start == 0 and chunks[-1].core_end == len(audio)
    assert all(np.shares_memory(audio[c.start:c.end], audio) for c in chunks)
    np.testing.assert_array_equal(audio, before)


def test_780_onset_is_reused_and_command_gain_is_not_applied(monkeypatch):
    # 1052d retires compact/onset preparation; original PCM is never amplified.
    audio, result = clips()['mixed'], measured('mixed')
    onset = Mock(side_effect=AssertionError('hold must not prepare command PCM'))
    monkeypatch.setattr(command_clip_trim, 'prepare', onset)
    chunks = decoder.chunk_audio(audio, RATE, result)
    assert all(np.shares_memory(audio[c.start:c.end], audio) for c in chunks)
    onset.assert_not_called()


def test_short_quiet_word_is_not_replaced_by_a_later_long_run():
    scores = [0.]*200
    scores[10:12], scores[100:120] = [.11]*2, [.9]*20
    bounds = decoder._speech_bounds(evidence(scores), RATE, len(scores)*512)
    assert len(bounds) == 2
    assert bounds[0][0] <= 10*512 < 12*512 <= bounds[0][1]
    assert bounds[1][0] <= 100*512 < 120*512 <= bounds[1][1]


def test_confident_last_partial_frame_is_preserved():
    samples = 512*40+13
    bounds = decoder._speech_bounds(evidence([0.]*40+[.9], samples), RATE, samples)
    assert bounds and bounds[-1][1] == samples


def test_padding_merges_and_mapping_keeps_each_original_island():
    scores = [0.]*100
    scores[10:14], scores[17:21], scores[70:74] = [.9]*4, [.9]*4, [.9]*4
    bounds = decoder._speech_bounds(evidence(scores), RATE, len(scores)*512)
    assert len(bounds) == 2
    assert bounds[0][0] <= 10*512 and bounds[0][1] >= 21*512
    assert bounds[1][0] <= 70*512 and bounds[1][1] >= 74*512


def test_earcon_boundary_is_measured_and_padding_does_not_reinclude_it():
    scores = [.8]*8 + [0.]*20 + [.8]*8 + [0.]*84
    capture = presence.HoldEvidence(0, .4, 0)
    bounds = decoder._speech_bounds(evidence(scores), RATE, len(scores)*512, capture)
    assert len(bounds) == 1 and bounds[0][0] >= round(.4*RATE)


@pytest.mark.parametrize('bad', [presence.Presence(False), evidence([float('nan')]*2),
                               evidence([1.2]*2), evidence([.9], 100)])
def test_unknown_or_invalid_evidence_preserves_normal_decode(bad):
    assert decoder._speech_bounds(bad, RATE, 1024) is None
    chunks = decoder.chunk_audio(np.zeros(1024), RATE, bad)
    assert len(chunks) == 1 and chunks[0].end == 1024


@pytest.mark.parametrize('start,end,kept', [(0,.1,False), (.4,1.4,True), (.39,1.39,False),
                                        (.9,1.,True), (1.5,1.6,False),
                                        (1.,1.,False), (float('nan'),1.,False),
                                        (-.1,.8,False), (0,float('inf'),False)])
def test_words_need_sixty_percent_of_their_duration(start, end, kept, caplog):
    seg = _segment(text=' invented', words=[word(' invented', start, end)])
    filtered = decoder._aligned([seg], [(round(.8*RATE), round(1.4*RATE))], RATE, 0)
    assert bool(filtered) is kept
    assert seg.text == ' invented'


def test_missing_word_timestamps_cannot_authorize_sparse_text():
    assert decoder._aligned([_segment(text='Unaligned output')], [(0,RATE)], RATE, 0) == []


@pytest.mark.parametrize('name', ['mixed', 'quiet_hold'])
def test_final_only_delivers_aligned_sentence_after_long_pause(make_worker, monkeypatch, name):
    audio, result = clips()[name], measured(name)
    worker, session = make_worker([])
    bind_audio(session, monkeypatch, audio, result)
    def decode(pcm, **params):
        offset = (pcm.ctypes.data-audio.ctypes.data)//audio.itemsize/RATE
        words = [word(' phantom', -.2, -.1), word(' Real sentence.', 28.5-offset, 29.-offset),
                 word(' phantom', len(pcm)/RATE+.1, len(pcm)/RATE+.2)]
        return iter([_segment(text=' phantom Real sentence. phantom', words=words)]), NS(language='en')
    session.app.model.transcribe.side_effect = decode
    worker._final_pass()
    assert session.on_final.call_args.args == ('Real sentence.',)
    assert session.on_final.call_args.kwargs['duration_s'] == 60
    for call in session.app.model.transcribe.call_args_list:
        assert np.shares_memory(call.args[0], audio) and len(call.args[0]) <= 25*RATE
        assert call.kwargs['word_timestamps']
    assert not session.app.model_lock.locked()


def test_rejected_sparse_words_cannot_return_via_partial_or_retry(make_worker, monkeypatch):
    worker, session = make_worker([_segment(text=' phantom', words=[word(' phantom', -.2,-.1)])])
    bind_audio(session, monkeypatch, clips()['mixed'], measured('mixed'))
    session._last_partial = 'Previous unaligned phantom'
    worker._final_pass()
    assert session.on_final.call_args.args == (None,)
    assert session.app.model.transcribe.call_count == 1
    session.app.process_transcription.assert_not_called()


def test_crickets_never_decode_or_reach_delivery(make_worker, monkeypatch):
    worker, session = make_worker([_segment(text='Invented words')])
    bind_audio(session, monkeypatch, clips()['crickets'], measured('crickets'))
    worker._final_pass()
    session.app.model.transcribe.assert_not_called()
    session.app.process_transcription.assert_not_called()
    assert session.on_final.call_args.args == (None,)


@pytest.mark.parametrize('kind', ['short', 'unknown', 'dense', 'disabled'])
def test_ordinary_decoding_keeps_audio_parameters_and_existing_guards(make_worker, monkeypatch, kind):
    audio = clips()['normal'] if kind == 'short' else np.zeros(20*RATE, np.float32)
    scores = [.9]*int(np.ceil(len(audio)/512))
    result = presence.Presence(False) if kind == 'unknown' else evidence(scores, len(audio))
    worker, session = make_worker([_segment(text='Real sentence.')])
    bind_audio(session, monkeypatch, audio, result)
    if kind == 'disabled':
        session.app.config['dictation'] = {'speech_presence_gate': False}
    worker._final_pass()
    assert np.shares_memory(session.app.model.transcribe.call_args.args[0], audio)  # 1052d source slice
    assert not session.app.model.transcribe.call_args.kwargs.get('word_timestamps', False)
    assert session.on_final.call_args.args == ('Real sentence.',)


def test_cancelled_sparse_final_does_not_start_native_decode(make_worker, monkeypatch):
    worker, session = make_worker([_segment(text='Unused')])
    bind_audio(session, monkeypatch, clips()['mixed'], measured('mixed'))
    session.cancel_event.set()
    worker._final_pass()
    session.app.model.transcribe.assert_not_called()
    session.app.process_transcription.assert_not_called()
    assert session.on_final.call_args.args == (None,)


def test_sparse_decoder_rejection_retries_same_compact_timeline(make_worker, monkeypatch):
    # 1052d: exhausted native ladder is reported; no compact/5 s recovery.
    audio, result = clips()['mixed'], measured('mixed')
    worker, session = make_worker([_segment(text=' Clear.', words=[word(' Clear.', 2., 2.5)])])
    bind_audio(session, monkeypatch, audio, result)
    monkeypatch.setattr(worker, '_decoder_guard', Mock(return_value='decoder_failure'))
    worker._final_pass()
    assert session.on_final.call_args.args == (None,)
    assert session.app.model.transcribe.call_count == 1
    np.testing.assert_array_equal(session.app._hold_retry_audio.audio, audio)


def test_known_empty_region_result_does_not_decode_even_if_earlier_gate_passed(make_worker, monkeypatch):
    worker, session = make_worker([_segment(text='Invented words')])
    result = measured('mixed')
    detector = bind_audio(session, monkeypatch, clips()['mixed'], result)
    detector.measure.side_effect = [result, evidence([0.]*1875, 60*RATE)]
    worker._final_pass()
    session.app.model.transcribe.assert_not_called()
    assert session.on_final.call_args.args == (None,)


def test_plan_inference_failure_retains_existing_decoder(make_worker, monkeypatch):
    worker, session = make_worker([_segment(text='Real sentence.')])
    audio, result = clips()['mixed'], measured('mixed')
    detector = bind_audio(session, monkeypatch, audio, result)
    detector.measure.side_effect = [result, RuntimeError('synthetic detector failure')]
    worker._final_pass()
    assert session.on_final.call_args.args[0]
    assert all(np.shares_memory(call.args[0], audio) for call in session.app.model.transcribe.call_args_list)
    assert all(len(call.args[0]) <= 25*RATE for call in session.app.model.transcribe.call_args_list)


def test_sparse_recovery_retains_audio_and_preserves_explicit_try_again(make_worker, monkeypatch):
    audio, result = clips()['mixed'], measured('mixed')
    worker, session = make_worker([_segment(text=' Recovered.', words=[word(' Recovered.', 2., 2.5)])])
    bind_audio(session, monkeypatch, audio, result)
    monkeypatch.setattr(worker, '_decoder_guard', Mock(return_value='decoder_failure'))
    worker._final_pass()
    np.testing.assert_array_equal(session.app._hold_retry_audio.audio, audio)
    monkeypatch.setattr(worker, '_decoder_guard', Mock(return_value=None))
    session._stop_capture.return_value = np.zeros(RATE, np.float32)
    monkeypatch.setattr(presence._presence, 'measure', lambda *a: presence.Presence(False))
    responses = iter([[_segment(text='try again')], [_segment(text=' Recovered.')]])
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter(next(responses, [])), NS(language='en'))
    worker._final_pass()
    assert 'Recovered.' in session.on_final.call_args.args[0]
    np.testing.assert_array_equal(session._decode_audio, session.app._hold_retry_audio.audio)
