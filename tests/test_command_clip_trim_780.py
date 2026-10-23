"""780 command preparation contracts; synthetic PCM, no devices/model downloads.

These are not substitutes for the six incident WAV recognizer regressions.
Those captures were not supplied outside the prohibited live profile.
"""
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import numpy as np
import pytest

from samsara import command_clip_trim as trim
from samsara.speech_presence import HoldEvidence


RATE = 16000
FRAME = 512


def capture(earcon_end=1.84):
    return HoldEvidence(requested_at=11.6, earcon_end_at=10 + earcon_end,
                        first_kept_block_start=10)


def example():
    """Louder start sound at 1.6 s, quiet speech at 1.92 s, release tail."""
    pcm = np.zeros(FRAME * 96, dtype=np.float32)
    t = np.arange(len(pcm)) / RATE
    pcm[50 * FRAME:57 * FRAME] = .12 * np.sin(2 * np.pi * 900 * t[50 * FRAME:57 * FRAME])
    pcm[60 * FRAME:78 * FRAME] = .035 * np.sin(2 * np.pi * 220 * t[60 * FRAME:78 * FRAME])
    scores = np.zeros(96, dtype=np.float32)
    scores[50:57] = .3  # even a speech-like start cue is excluded by timing
    scores[60:78] = .8
    return pcm, scores


def test_preroll_and_actual_earcon_removed_voice_and_release_tail_retained():
    pcm, scores = example()
    before = pcm.copy()
    result = trim.prepare(pcm, RATE, scores, capture=capture())
    assert result.start_sample == round(1.84 * RATE)
    assert result.speech_sample == 60 * FRAME
    assert result.longest_s == pytest.approx(18 * FRAME / RATE)
    assert result.gain > 1
    np.testing.assert_allclose(result.audio / result.gain, pcm[result.start_sample:], atol=1e-8)
    np.testing.assert_array_equal(pcm, before)
    assert result.audio[-1] == pcm[-1] == 0


def test_unknown_timing_uses_onset_and_keeps_200ms():
    pcm, scores = example()
    scores[:60] = 0  # non-speech transient, identified acoustically
    result = trim.prepare(pcm, RATE, scores)
    assert result.start_sample == 60 * FRAME - round(.2 * RATE)
    assert result.earcon_end_ms == -1


def test_first_short_word_survives_a_later_longer_run():
    pcm, scores = example()
    scores[:] = 0
    scores[20:24] = .4
    scores[60:78] = .8
    pcm[20 * FRAME:24 * FRAME] = .1
    result = trim.prepare(pcm, RATE, scores)
    assert result.speech_sample == 20 * FRAME
    assert result.start_sample == 20 * FRAME - round(.2 * RATE)
    assert result.longest_s == pytest.approx(.576)


@pytest.mark.parametrize('offset', [0, 1, 15])
def test_early_speech_without_earcon_keeps_onset(offset):
    pcm, scores = example()
    scores[:] = 0
    scores[offset:offset + 5] = .8
    result = trim.prepare(pcm, RATE, scores)
    assert result.start_sample <= offset * FRAME
    assert result.start_sample == max(0, offset * FRAME - round(.2 * RATE))


def test_short_burst_does_not_anchor_trim():
    pcm, scores = example()
    scores[:60] = 0
    scores[50:52] = .99
    result = trim.prepare(pcm, RATE, scores)
    assert result.speech_sample == 60 * FRAME


def test_gain_uses_speech_rms_instead_of_louder_cue():
    pcm, scores = example()
    result = trim.prepare(pcm, RATE, scores, capture=capture())
    speech = result.audio[60 * FRAME - result.start_sample:78 * FRAME - result.start_sample]
    assert np.sqrt(np.mean(speech ** 2)) == pytest.approx(.05, rel=.03)


def test_gain_bounded_and_does_not_clip_retained_transient():
    pcm, scores = example()
    pcm *= .0001
    result = trim.prepare(pcm, RATE, scores, capture=capture())
    assert result.gain == 4
    pcm[85 * FRAME] = .8
    result = trim.prepare(pcm, RATE, scores, capture=capture())
    assert result.gain == pytest.approx(.95 / .8)
    assert np.max(np.abs(result.audio)) <= .950001


def test_loud_speech_is_not_amplified():
    pcm, scores = example()
    pcm *= 5
    result = trim.prepare(pcm, RATE, scores, capture=capture())
    assert result.gain == 1


def test_silence_is_unchanged():
    pcm = np.zeros(FRAME * 40, dtype=np.float32)
    result = trim.prepare(pcm, RATE, np.zeros(40))
    assert result.audio is pcm
    assert result.gain == 1
    assert result.longest_s == 0


@pytest.mark.parametrize('bad', ['nan', 'negative', 'above_one', 'wrong_length', 'two_dimensional'])
def test_uncertain_scores_preserve_original(bad):
    pcm, scores = example()
    if bad == 'nan':
        scores[0] = np.nan
    elif bad == 'negative':
        scores[0] = -.1
    elif bad == 'above_one':
        scores[0] = 1.1
    elif bad == 'wrong_length':
        scores = scores[:-1]
    else:
        scores = scores.reshape(1, -1)
    assert trim.prepare(pcm, RATE, scores).audio is pcm


@pytest.mark.parametrize('bad_capture', [SimpleNamespace(offsets=lambda: (0, 0, float('nan'))),
                                      SimpleNamespace(offsets=lambda: (0, 0, 9000)),
                                      HoldEvidence(10)])
def test_bad_or_outside_capture_timing_preserves_audio(bad_capture):
    pcm, scores = example()
    assert trim.prepare(pcm, RATE, scores, capture=bad_capture).audio is pcm


def test_long_or_non_model_rate_capture_unchanged():
    pcm = np.ones(RATE * 6, dtype=np.float32)
    assert trim.prepare(pcm, RATE, np.ones(len(pcm) // FRAME)).audio is pcm
    pcm, scores = example()
    assert trim.prepare(pcm, 48000, scores).audio is pcm


def app_for(pcm, scores):
    def base(*, include_vocabulary):
        return dict(language='de', initial_prompt='command vocabulary' if include_vocabulary else '',
                    no_speech_threshold=.6, log_prob_threshold=-1, beam_size=5)
    return SimpleNamespace(model_rate=RATE, command_mode_recording=False,
                           get_transcription_params=Mock(side_effect=base),
                           _decode_hotkey_audio=Mock(return_value=SimpleNamespace(text='')),
                           _vad_available=True, _vad_model=object(), _vad_lock=threading.Lock(),
                           _vad_probabilities=Mock(return_value=scores))


def test_command_after_flags_cleared_uses_vocabulary_english_and_real_decoder():
    pcm, scores = example()
    app = app_for(pcm, scores)
    params = trim.build_params(app, is_command=True)
    original_params = dict(params)
    trim.decode(app, pcm, params, len(pcm) / RATE, is_command=True, capture=capture())
    args, kwargs = app._decode_hotkey_audio.call_args
    assert len(args[0]) < len(pcm)
    assert args[2] == len(args[0]) / RATE
    assert args[1]['language'] == 'en'
    assert args[1]['initial_prompt'] == 'command vocabulary'
    assert args[1]['no_speech_threshold'] == .6
    assert args[1]['log_prob_threshold'] == -1
    assert args[1]['vad_filter'] is False
    assert args[1]['condition_on_previous_text'] is False
    assert kwargs == {'free_form': False}
    app.get_transcription_params.assert_called_once_with(include_vocabulary=True)
    assert params == original_params


def test_dictation_skips_preparation_and_preserves_pcm_params_duration():
    pcm, scores = example()
    app = app_for(pcm, scores)
    params = {'language': 'de'}
    trim.decode(app, pcm, params, 3.072, is_command=False, capture=capture())
    args, kwargs = app._decode_hotkey_audio.call_args
    assert args[0] is pcm and args[1] is params and args[2] == 3.072
    assert kwargs == {'free_form': True}
    app.get_transcription_params.assert_not_called()
    app._vad_probabilities.assert_not_called()


def test_silent_command_stays_empty_and_has_no_retry():
    pcm = np.zeros(FRAME * 96, dtype=np.float32)
    app = app_for(pcm, np.zeros(96))
    result = trim.decode(app, pcm, {}, len(pcm) / RATE, is_command=True)
    assert result.text == ''
    assert app._decode_hotkey_audio.call_count == 1
    assert app._decode_hotkey_audio.call_args.args[0] is pcm


@pytest.mark.parametrize('unavailable', ['disabled', 'error'])
def test_vad_unavailable_preserves_command_pcm(unavailable):
    pcm, scores = example()
    app = app_for(pcm, scores)
    if unavailable == 'disabled':
        app._vad_available = False
    else:
        app._vad_probabilities.side_effect = RuntimeError('synthetic VAD failure')
    trim.decode(app, pcm, {}, len(pcm) / RATE, is_command=True)
    assert app._decode_hotkey_audio.call_args.args[0] is pcm


def test_real_release_path_uses_captured_command_ownership(tmp_path, monkeypatch):
    from tests.test_commands_only_key_391 import _app, _run
    app, spies = _app(tmp_path, monkeypatch, 'scroll down')
    app.config['language'] = 'de'
    app.get_transcription_params = Mock(wraps=app.get_transcription_params)
    _run(app)
    assert not app.command_mode_recording
    assert spies.model.transcribe.call_count == 1
    assert spies.model.transcribe.call_args.kwargs['language'] == 'en'
    assert app.get_transcription_params.call_args.kwargs['include_vocabulary'] is True
    assert spies.hand.call_count == 1
