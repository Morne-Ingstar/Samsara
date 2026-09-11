"""Probe math only: no corpus, model, microphone, downloads, or torch."""
from unittest.mock import Mock

import numpy as np
import pytest

from tools.probes import sv_feasibility as probe


@pytest.mark.parametrize('sir_db', [6, 0, -6, -12])
@pytest.mark.parametrize('amplitude', [.03, .8])
def test_sir_mixing_hits_target_even_when_peak_scaling_is_needed(sir_db, amplitude):
    times = np.arange(32000) / 16000
    owner = amplitude * np.sin(2 * np.pi * 217 * times)
    media = .2 * np.cos(2 * np.pi * 391 * times)
    mixed, metadata = probe.mix_at_sir(owner, media, sir_db)
    interference = mixed / metadata['shared_peak_gain'] - owner
    achieved = 20 * np.log10(probe.rms(owner) / probe.rms(interference))
    assert abs(achieved - sir_db) < .5
    assert metadata['actual_whole_sir_db'] == pytest.approx(sir_db)
    assert np.max(np.abs(mixed)) <= .990001


def test_duck_envelope_and_mixture_have_requested_discontinuity():
    envelope = probe.duck_envelope(32000)
    np.testing.assert_array_equal(envelope[:8000], 1.0)
    np.testing.assert_array_equal(envelope[8000:], .15)
    owner = np.full(32000, .1)
    media = np.full(32000, .2)
    mixed, metadata = probe.mix_at_sir(owner, media, 0, duck=True)
    interference = mixed / metadata['shared_peak_gain'] - owner
    np.testing.assert_allclose(interference[:8000], .1, atol=1e-7)
    np.testing.assert_allclose(interference[8000:], .015, atol=1e-7)
    assert len(mixed[8000:]) == 24000


@pytest.mark.parametrize('duration, expected', [(.8, 6), (1.2, 4), (2, 2), (3, 1)])
def test_window_counts_and_nonoverlapping_hop(duration, expected):
    samples = np.arange(80000)
    result = list(probe.windows(samples, duration))
    size = round(duration * 16000)
    assert len(result) == expected
    assert [start for start, _ in result] == list(range(0, expected * size, size))
    np.testing.assert_array_equal(np.concatenate([audio for _, audio in result]), samples[:expected * size])
    assert list(probe.windows(samples[:size - 1], duration)) == []
    assert len(list(probe.windows(samples[:size], duration))) == 1


def test_zero_false_accept_threshold_rejects_ties_and_counts_owner_rejects():
    media = np.array([.1, .35, .35])
    threshold = probe.zero_false_accept_threshold(media)
    assert threshold == np.nextafter(.35, np.inf)
    assert not np.any(media >= threshold)
    assert probe.rejection_rate([.2, .35, .6, .8], threshold) == .5
    assert probe.rejection_rate([], threshold) is None


def test_zero_false_accept_threshold_requires_retained_media_scores():
    with pytest.raises(ValueError, match='media scores'):
        probe.zero_false_accept_threshold([])


def test_leave_one_out_excludes_held_out_embedding():
    vectors = [np.array([2., 0.])] + [np.array([0., 1.])] * 5
    embed = Mock(side_effect=vectors)
    full, scores = probe.enroll([np.zeros(10)] * 6, embed)
    assert embed.call_count == 6
    assert scores[0] == 0
    assert scores[1:] == pytest.approx([4 / np.sqrt(20)] * 5)
    np.testing.assert_allclose(full, np.array([2, 5]) / np.sqrt(29))


def test_vad_drops_do_not_call_embedding():
    embed = Mock(return_value=[1, 0])
    vad = Mock(return_value={'voiced_frames': 12, 'vad_frames': 25, 'voiced_fraction': .48})
    trial = probe.score_trial(np.zeros(12800), [1, 0], embed, vad,
                              condition='clean_0.8s', genuine=True)
    assert trial['included'] is False
    assert trial['drop_reason'] == 'vad_below_50_percent'
    assert trial['score'] is None
    embed.assert_not_called()


def test_exactly_half_voiced_is_scored_with_mocked_embedding():
    embed = Mock(return_value=[3, 4])
    vad = Mock(return_value={'voiced_frames': 10, 'vad_frames': 20, 'voiced_fraction': .5})
    trial = probe.score_trial(np.zeros(12800), [1, 0], embed, vad,
                              condition='clean_0.8s', genuine=True)
    assert trial['included'] is True
    assert trial['score'] == pytest.approx(.6)
    assert trial['embedding_ms'] >= 0
    embed.assert_called_once()


def test_eer_separated_and_tied_scores():
    assert probe.equal_error_rate([.7, .8], [.1, .2])['eer'] == 0
    assert probe.equal_error_rate([.5, .5], [.5, .5])['eer'] == .5


def test_guard_blocks_torch_submodules_without_a_placeholder():
    finder = probe.NoTorchFinder()
    with pytest.raises(ImportError, match='forbids torch'):
        finder.find_spec('torch.nn')
    assert finder.find_spec('onnxruntime') is None
