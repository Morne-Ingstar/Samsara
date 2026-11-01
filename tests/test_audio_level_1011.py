"""1011: measured diagnostics, without models, recording or application startup."""
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import audio_level as level, speech_presence
from samsara.gpu_fallback import ResilientSpeechModel


def evidence(scores):
    return speech_presence.Presence(True, max_probability=max(scores),
                                    probabilities=tuple(scores), samples=len(scores) * 512)


@pytest.fixture
def speech(monkeypatch):
    measure = Mock(side_effect=lambda audio, rate: evidence([.9] * ((len(audio) + 511) // 512)))
    monkeypatch.setattr(speech_presence._presence, 'measure', measure)
    return measure


@pytest.mark.parametrize('bad', [None, object(), 'file.wav', np.array([], np.float32),
                               np.zeros((2, 512), np.float32), np.ones(512, np.int16),
                               np.array([np.nan]), np.array([np.inf])])
def test_invalid_inputs_do_not_report_levels(bad):
    assert level.measure(bad) is None


def test_peak_rms_and_clipping_fraction():
    stats = level.measure(np.array([.1, -.2, .3, -.4], np.float32))
    assert stats.peak == pytest.approx(.4)
    assert stats.rms == pytest.approx(np.sqrt(.075))
    assert not stats.clipping and stats.clipped_fraction == 0


@pytest.mark.parametrize('peak, count, clipped', [(.993251, 50, False), (.9989, 50, False),
                                                 (1., 1, False), (1., 2, False),
                                                 (.9991, 3, True), (1., 10, True)])
def test_clipping_requires_multiple_samples_at_the_rail(peak, count, clipped):
    audio = np.zeros(1000, np.float32)
    audio[:count] = peak
    assert level.measure(audio).clipping is clipped


def test_sparse_rail_samples_do_not_flag_a_long_capture():
    audio = np.zeros(16000, np.float32)
    audio[:3] = 1
    assert not level.measure(audio).clipping


def test_percentiles_include_only_speech_and_partial_final_frame():
    audio = np.r_[np.ones(512), np.full(512, .1), np.full(256, .3)].astype(np.float32)
    peak, rms, frames = level.speech_percentiles(audio, evidence([0., .9, .9]))
    assert frames == 2
    assert peak == pytest.approx(.28) and rms == pytest.approx(.28)


@pytest.mark.parametrize('presence', [speech_presence.Presence(False), evidence([0., 0.]),
                                     speech_presence.Presence(True, total_s=1, longest_s=1)])
def test_unknown_noise_and_missing_frame_evidence_have_no_speech_percentiles(presence):
    assert level.speech_percentiles(np.ones(1024, np.float32), presence) is None


def test_one_info_per_loaded_session_with_explicit_scope_and_units(speech, caplog):
    session = level.SessionLevels()
    with caplog.at_level(logging.INFO, logger='Samsara'):
        for gain in (.1, .3, .7):
            session.observe(np.full(1024, gain, np.float32))
    lines = [r.message for r in caplog.records if '[INPUT_LEVEL]' in r.message]
    assert len(lines) == 1
    assert 'scope=first_speech_decode' in lines[0]
    assert 'speech_peak_p90=0.100000 speech_rms_p90=0.100000' in lines[0]
    assert 'peak_p90_dbfs=-20.00 rms_p90_dbfs=-20.00 normalization=off' in lines[0]
    assert speech.call_count == 1


def test_noise_is_unchanged_and_does_not_claim_speech(monkeypatch, caplog):
    audio = np.random.default_rng(1011).normal(0, .001, 2048).astype(np.float32)
    before = audio.copy()
    monkeypatch.setattr(speech_presence._presence, 'measure', lambda *args: evidence([0.] * 4))
    with caplog.at_level(logging.INFO, logger='Samsara'):
        level.SessionLevels().observe(audio)
    np.testing.assert_array_equal(audio, before)
    assert not [r for r in caplog.records if '[INPUT_LEVEL]' in r.message]


def test_clipping_hint_once_even_when_clipping_arrives_after_info(speech, caplog):
    session = level.SessionLevels()
    with caplog.at_level(logging.DEBUG, logger='Samsara'):
        session.observe(np.full(1024, .1, np.float32))
        session.observe(np.ones(1024, np.float32))
        session.observe(np.ones(1024, np.float32))
    assert sum(level.CLIPPING_HINT in r.message for r in caplog.records) == 1
    assert sum(r.levelno == logging.INFO for r in caplog.records) == 1


def test_probe_bounded_without_truncating_decode_input(speech):
    audio = np.full(31 * 16000, .1, np.float32)
    level.SessionLevels().observe(audio)
    assert speech.call_args.args[0].size == 30 * 16000
    assert audio.size == 31 * 16000


@pytest.mark.parametrize('keyword', [False, True])
@pytest.mark.parametrize('peak', [10 ** (-30 / 20), 10 ** (-12 / 20), 1.])
def test_decode_keeps_exact_float32_pcm_gain_and_kwargs(speech, keyword, peak):
    audio = np.array([0, peak, -peak] * 512, np.float32)
    before = audio.copy()
    segments, info = iter(['fixture']), object()
    native = Mock(transcribe=Mock(return_value=(segments, info)))
    model = ResilientSpeechModel(SimpleNamespace(), native, Mock(), device='cpu')
    params = dict(language='en', hotwords='fixture', vad_filter=False)
    result = model.transcribe(audio=audio, **params) if keyword else model.transcribe(audio, **params)
    assert result == (segments, info)
    passed = native.transcribe.call_args.kwargs['audio'] if keyword else native.transcribe.call_args.args[0]
    assert passed is audio and passed.dtype == np.float32
    assert np.max(np.abs(passed)) <= 1
    np.testing.assert_array_equal(audio, before)
    assert native.transcribe.call_args.kwargs == dict(params, **({'audio': audio} if keyword else {}))


def test_observer_failure_cannot_block_decode(monkeypatch):
    monkeypatch.setattr(level.SessionLevels, 'observe', Mock(side_effect=RuntimeError('fixture')))
    native = Mock(transcribe=Mock(return_value=(iter([]), None)))
    model = ResilientSpeechModel(SimpleNamespace(), native, Mock(), device='cpu')
    audio = np.zeros(512, np.float32)
    model.transcribe(audio)
    assert native.transcribe.call_args.args[0] is audio


def test_cpu_retry_observes_once_and_keeps_original_audio(speech, caplog):
    audio = np.full(1024, .1, np.float32)
    native = Mock(transcribe=Mock(side_effect=RuntimeError('CUDA failed with error 35')))
    cpu = Mock(transcribe=Mock(return_value=(iter(['fixture']), None)))
    model = ResilientSpeechModel(SimpleNamespace(), native, lambda: cpu, device='cuda', notify=Mock())
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert list(model.transcribe(audio)[0]) == ['fixture']
    assert native.transcribe.call_args.args[0] is cpu.transcribe.call_args.args[0] is audio
    assert speech.call_count == 1
    assert sum('[INPUT_LEVEL]' in r.message for r in caplog.records) == 1
