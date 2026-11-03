"""1021: synthetic PCM and deterministic decoder fixtures; no model/network.

The decoder spy reproduces the delivery defect, not Whisper's probability of
inventing words. Actual profile kwargs and production decode callers are used.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.command_fast_path import CommandFastPathMixin
from samsara.hallucination_filter import OUTRO_PHRASES, transcribe_without_outro
from samsara.transcript_gates import _apply_segment_quality_gates
from samsara.transcription_params import get_transcription_params, build_hotkey_transcribe_params
from samsara.vocabulary_decode import transcribe_with_vocabulary
from tests.test_silent_hold_387 import make_worker
from tests.test_vocabulary_decode_1009 import app_store

RATE = 16000


def seg(text, start=0., end=1., **kw):
    values = dict(text=text, start=start, end=end, no_speech_prob=.1,
                  avg_logprob=-.2, compression_ratio=1., temperature=0.)
    values.update(kw)
    return SimpleNamespace(**values)


def pcm(tail=1.e-5):
    # First second stands for audible speech; two seconds of quiet tail.
    return np.r_[np.full(RATE, .03), np.full(2 * RATE, tail)].astype(np.float32)


def profile(mode='accurate', hold=True):
    params = get_transcription_params({'performance_mode': mode}, language='en',
                                     initial_prompt=None, no_speech_threshold=.6,
                                     log_prob_threshold=-1.)
    return build_hotkey_transcribe_params(params, command_mode_recording=False) if hold else params


def decode(segments, audio, **kw):
    info = SimpleNamespace(language='en')
    model = SimpleNamespace(transcribe=Mock(return_value=(iter(segments), info)))
    returned, got_info = transcribe_without_outro(model, audio, profile(), **kw)
    assert got_info is info
    return list(returned), model


@pytest.mark.parametrize('phrase', OUTRO_PHRASES)
@pytest.mark.parametrize('tail', [0., 1.e-5])
def test_silent_or_low_noise_final_outro_removed_only_at_end(phrase, tail):
    real = seg('Save this draft for tomorrow.')
    outro = seg(' ' + phrase + '!', 1., 2.8)
    kept, model = decode([real, outro], pcm(tail))
    assert kept == [real]
    assert _apply_segment_quality_gates(kept, profile(), 3.) == (real.text, False)
    assert model.transcribe.call_args.kwargs == profile()


@pytest.mark.parametrize('text', ['Thanks for watching everyone.',
                                  'Please write thanks for watching inside this sentence.',
                                  'Please subscribe to the mailing list.'])
def test_extra_words_are_preserved_even_with_silent_pcm(text):
    real = seg(text, no_speech_prob=.6)
    kept, _ = decode([real], np.zeros(3 * RATE, np.float32))
    assert kept == [real]
    assert _apply_segment_quality_gates(kept, profile(), 3.) == (text, False)


@pytest.mark.parametrize('phrase', OUTRO_PHRASES)
def test_spoken_exact_outro_with_energy_survives_even_weak_score(phrase):
    real = seg(phrase, no_speech_prob=.7)
    kept, _ = decode([real], pcm())
    assert kept == [real]
    assert _apply_segment_quality_gates(kept, profile(), 3.) == (phrase, False)


def test_known_phrase_in_a_middle_segment_is_not_removed():
    segments = [seg('An introduction.'), seg(' Thanks for watching.', 1., 2., no_speech_prob=.6),
                seg(' Then the discussion continued.', 2., 3.)]
    kept, _ = decode(segments, pcm())
    assert kept == segments
    assert _apply_segment_quality_gates(kept, profile(), 3.)[0] == ''.join(s.text for s in segments)


@pytest.mark.parametrize('signals', [dict(no_speech_prob=.6), dict(avg_logprob=-1.1)])
def test_low_confidence_final_outro_drops_without_pcm_timing(signals):
    real = seg('Preserve this introduction.')
    outro = seg(' Subscribe.', start=None, end=None, **signals)
    assert decode([real, outro], pcm())[0] == [real, outro]  # 1026: scores alone cannot prove silence.


@pytest.mark.parametrize('signals', [dict(no_speech_prob=None, avg_logprob=None),
                                   dict(no_speech_prob=float('nan'), avg_logprob=float('inf'))])
def test_missing_or_nonfinite_evidence_fails_open(signals):
    outro = seg('Thanks for watching.', start=None, end=None, **signals)
    assert decode([outro], object())[0] == [outro]


@pytest.mark.parametrize('mode', ['fast', 'balanced', 'accurate'])
def test_synthetic_zero_tail_reproduces_cause_with_actual_hold_settings(mode):
    real = seg('Keep the complete spoken introduction.')
    outro = seg(' Thanks for watching.', 1.8, 2.8)
    params = profile(mode)
    audio = pcm(0.)
    original = audio.copy()
    def decoder(samples, **kwargs):
        assert kwargs == params
        assert kwargs['vad_filter'] is False
        return iter([real, outro] if len(samples) > 1.8 * RATE else [real]), None
    model = SimpleNamespace(transcribe=Mock(side_effect=decoder))
    # Raw decode of exactly the same synthetic fixture/settings reproduces it.
    assert list(model.transcribe(audio, **params)[0]) == [real, outro]
    kept, _ = transcribe_without_outro(model, audio, params)
    assert list(kept) == [real]
    assert len(model.transcribe.call_args.args[0]) == round(1.3 * RATE)
    np.testing.assert_array_equal(audio, original)


def test_quiet_nonzero_tail_is_never_threshold_trimmed():
    audio = pcm(1.e-8)
    _, model = decode([seg('Ordinary speech.')], audio)
    assert model.transcribe.call_args.args[0] is audio


@pytest.mark.parametrize('path', ['hold', 'hold_long', 'hold_final', 'session', 'commit_redecode'])
def test_actual_shared_decode_boundary_screens_each_final_path(tmp_path, path):
    app, _ = app_store(tmp_path)
    app.model_rate = RATE
    real, outro = seg('Keep the spoken draft.'), seg(' Subscribe.', 1., 2.8)
    app.model.transcribe.return_value = (iter([real, outro]), SimpleNamespace(language='en'))
    returned, _ = transcribe_with_vocabulary(app, pcm(), path=path, **profile())
    assert list(returned) == [real]


@pytest.mark.parametrize('path', ['hold_partial', 'handsfree_preview'])
def test_preview_paths_keep_their_existing_policy(tmp_path, path):
    app, _ = app_store(tmp_path)
    outro = seg('Subscribe.', 1., 2.8)
    app.model.transcribe.return_value = ([outro], None)
    audio = pcm(0.)
    returned, _ = transcribe_with_vocabulary(app, audio, path=path, **profile())
    assert list(returned) == [outro]
    assert app.model.transcribe.call_args.args[0] is audio


def test_existing_filter_toggle_disables_trimming_and_segment_suppression(tmp_path):
    app, _ = app_store(tmp_path, {'dictation': {'signoff_filter': False}})
    outro = seg('Subscribe.', 1., 2.8)
    app.model.transcribe.return_value = ([outro], None)
    audio = pcm(0.)
    returned, _ = transcribe_with_vocabulary(app, audio, path='hold_final', **profile())
    assert list(returned) == [outro]
    assert app.model.transcribe.call_args.args[0] is audio


def test_real_streaming_final_caller_preserves_prefix(make_worker):
    real, outro = seg('Keep the spoken draft.'), seg(' Thanks for watching.', 1., 2.8)
    worker, session = make_worker([real, outro])
    session._stop_capture.return_value = pcm()
    worker._final_pass()
    assert session.on_final.call_args.args == (real.text,)
    assert session.app.model.transcribe.call_count == 1


def test_real_streaming_final_keeps_a_spoken_exact_outro(make_worker):
    real = seg('Thanks for watching.', no_speech_prob=.7)
    worker, session = make_worker([real])
    session._stop_capture.return_value = pcm()
    worker._final_pass()
    assert session.on_final.call_args.args == (real.text,)


def test_real_handsfree_decode_caller_preserves_prefix(tmp_path):
    import threading
    app, _ = app_store(tmp_path)
    app.model_rate, app.model_lock = RATE, threading.Lock()
    real, outro = seg('Keep the spoken draft.'), seg(' Subscribe.', 1., 2.8)
    app.model.transcribe.return_value = (iter([real, outro]), SimpleNamespace(language='en'))
    returned, _ = CommandFastPathMixin._decode_session_audio(app, pcm(), profile(hold=False), command_lane=False)
    assert returned == [real]


def test_lazy_segments_are_not_eagerly_materialized_and_exceptions_propagate():
    consumed = []
    def source():
        consumed.append(1)
        yield seg('An introduction.')
        consumed.append(2)
        yield seg(' Subscribe.', 1., 2.8)
        raise RuntimeError('native decode failed')
    model = SimpleNamespace(transcribe=lambda *a, **kw: (source(), None))
    returned, _ = transcribe_without_outro(model, pcm(), profile())
    assert consumed == []
    assert next(returned).text == 'An introduction.'
    assert consumed == [1, 2]
    with pytest.raises(RuntimeError, match='native decode failed'):
        next(returned)
