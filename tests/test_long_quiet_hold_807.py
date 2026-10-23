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

from samsara import command_clip_trim, speech_presence as presence, speech_regions as regions
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
    audio = clips()['crickets']
    result = measured('crickets')
    assert result.known and not result.passes()
    plan = regions.locate(audio, result)
    assert not plan.speech and not plan.padded
    assert plan.concatenate(audio)[0].size == 0


@pytest.mark.parametrize('name', ['mixed', 'normal', 'quiet', 'quiet_hold'])
def test_measured_speech_survives_with_context_and_no_gain(name):
    audio = clips()[name]
    before = audio.copy()
    result = measured(name)
    assert result.passes()
    plan = regions.locate(audio, result)
    compact, aligned = plan.concatenate(audio)
    assert plan.speech and aligned
    assert compact.size < 6*RATE
    for start, end in plan.speech:
        assert any(a <= start <= end <= b for a,b in plan.padded)
    np.testing.assert_array_equal(audio, before)
    np.testing.assert_array_equal(compact, np.concatenate([audio[a:b] for a,b in plan.padded]))


def test_780_onset_is_reused_and_command_gain_is_not_applied(monkeypatch):
    audio = clips()['mixed']
    onset = Mock(wraps=command_clip_trim.prepare)
    monkeypatch.setattr(command_clip_trim, 'prepare', onset)
    plan = regions.locate(audio, measured('mixed'))
    assert onset.call_count >= 1
    assert plan.speech[0][0] > 27*RATE
    for call in onset.call_args_list:
        assert len(call.args[0]) <= command_clip_trim.MAX_CLIP_S*RATE


def test_short_quiet_word_is_not_replaced_by_a_later_long_run():
    scores = [0.]*200
    scores[10:12] = [.11]*2  # 64ms quiet evidence, below 780's 96ms.
    scores[100:120] = [.9]*20
    audio = np.zeros(len(scores)*512, np.float32)
    plan = regions.locate(audio, evidence(scores))
    assert plan.speech == ((10*512, 12*512), (100*512, 120*512))


def test_confident_last_partial_frame_is_preserved():
    audio = np.zeros(512*40+13, np.float32)
    scores = [0.]*40 + [.9]
    plan = regions.locate(audio, evidence(scores, len(audio)))
    assert plan.speech == ((512*40, len(audio)),)


def test_padding_merges_and_mapping_keeps_each_original_island():
    audio = np.arange(512*100, dtype=np.float32)
    scores = [0.]*100
    scores[10:14], scores[17:21], scores[70:74] = [.9]*4, [.9]*4, [.9]*4
    plan = regions.locate(audio, evidence(scores))
    compact, aligned = plan.concatenate(audio)
    assert len(plan.padded) == 2 and len(aligned) == 2
    assert len(compact) == sum(b-a for a,b in plan.padded)
    first_end = (plan.padded[0][1]-plan.padded[0][0])/RATE
    assert aligned[-1][0] >= first_end


def test_earcon_boundary_is_measured_and_padding_does_not_reinclude_it():
    audio = np.zeros(512*120, np.float32)
    scores = [.8]*8 + [0.]*20 + [.8]*8 + [0.]*84
    capture = presence.HoldEvidence(0, .4, 0)
    plan = regions.locate(audio, evidence(scores), capture=capture)
    assert plan.speech == ((28*512, 36*512),)
    assert all(a >= round(.4*RATE) for a,b in plan.padded)


@pytest.mark.parametrize('bad', [presence.Presence(False), evidence([float('nan')]*2),
                               evidence([1.2]*2), evidence([.9], 100)])
def test_unknown_or_invalid_evidence_preserves_normal_decode(bad):
    assert regions.locate(np.zeros(1024), bad) is None


@pytest.mark.parametrize('start,end,kept', [(0,.1,False), (.4,1.4,True), (.39,1.39,False),
                                        (.9,1.,True), (1.5,1.6,False),
                                        (1.,1.,False), (float('nan'),1.,False),
                                        (-.1,.8,False), (0,float('inf'),False)])
def test_words_need_sixty_percent_of_their_duration(start, end, kept, caplog):
    seg = _segment(text=' invented', words=[word(' invented', start, end)])
    with caplog.at_level(logging.INFO, logger='Samsara'):
        filtered, text = regions.filter_words([seg], ((.8, 1.4),))
    assert bool(filtered) is kept
    assert bool(text) is kept
    assert seg.text == ' invented'  # Decoder objects must not be mutated.
    if not kept:
        assert '[GUARD] dropped words outside speech regions (1 words)' in caplog.text
        assert 'invented' not in caplog.text


def test_missing_word_timestamps_cannot_authorize_sparse_text():
    assert regions.filter_words([_segment(text='Unaligned output')], ((0.,1.),)) == ([], '')


@pytest.mark.parametrize('name', ['mixed', 'quiet_hold'])
def test_final_only_delivers_aligned_sentence_after_long_pause(make_worker, monkeypatch, name):
    audio = clips()[name]
    result = measured(name)
    compact, intervals = regions.locate(audio, result).concatenate(audio)
    a, b = max(intervals, key=lambda pair: pair[1]-pair[0])
    words = [word(' phantom', -.2, -.1), word(' Real sentence.', a+.4, min(b, a+.9)),
             word(' phantom', len(compact)/RATE+.1, len(compact)/RATE+.2)]
    worker, session = make_worker([_segment(text=' phantom Real sentence. phantom', words=words)])
    bind_audio(session, monkeypatch, audio, result)
    worker._final_pass()
    assert session.on_final.call_args.args == ('Real sentence.',)
    assert session.on_final.call_args.kwargs['duration_s'] == 60
    session.app.process_transcription.assert_called_once_with('Real sentence.')
    args, kwargs = session.app.model.transcribe.call_args
    np.testing.assert_array_equal(args[0], compact)
    assert kwargs['word_timestamps'] and not kwargs['without_timestamps']
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
    assert session.app.model.transcribe.call_args.args[0] is audio
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
    audio, result = clips()['mixed'], measured('mixed')
    compact, intervals = regions.locate(audio, result).concatenate(audio)
    a, b = intervals[0]
    seg = _segment(text=' Clear.', words=[word(' Clear.', a+.4, min(b, a+.9))])
    worker, session = make_worker([seg])
    bind_audio(session, monkeypatch, audio, result)
    guard = Mock(side_effect=['decoder_failure', None])
    monkeypatch.setattr(worker, '_decoder_guard', guard)
    worker._final_pass()
    assert session.app.model.transcribe.call_count == 2
    for call in session.app.model.transcribe.call_args_list:
        np.testing.assert_array_equal(call.args[0], compact)
        assert call.kwargs['word_timestamps']
    assert session.on_final.call_args.args == ('Clear.',)


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
    assert session.app.model.transcribe.call_args.args[0] is audio
    assert session.on_final.call_args.args == ('Real sentence.',)


def test_sparse_recovery_retains_audio_and_preserves_explicit_try_again(make_worker, monkeypatch):
    audio, result = clips()['mixed'], measured('mixed')
    compact, bounds = regions.locate(audio, result).concatenate(audio)
    a,b = bounds[0]
    seg = _segment(text=' Recovered.', words=[word(' Recovered.', a+.4,min(b,a+.9))])
    worker, session = make_worker([seg])
    bind_audio(session, monkeypatch, audio, result)
    original_guard = worker._decoder_guard
    monkeypatch.setattr(worker, '_decoder_guard', Mock(return_value='decoder_failure'))
    worker._final_pass()
    retained = session.app._hold_retry_audio
    np.testing.assert_array_equal(retained.audio, compact)
    assert retained.speech_regions == bounds
    # A subsequent ordinary short hold saying try again uses retained compact
    # PCM; its words still cross the sparse alignment check before delivery.
    monkeypatch.setattr(worker, '_decoder_guard', original_guard)
    session._stop_capture.return_value = np.zeros(RATE, np.float32)
    monkeypatch.setattr(presence._presence, 'measure', lambda *args: presence.Presence(False))
    session.app.model.transcribe.side_effect = [
        (iter([_segment(text='try again')]), NS(language='en')),
        (iter([seg]), NS(language='en')),
    ]
    worker._final_pass()
    assert session.on_final.call_args.args == ('Recovered.',)
