"""Synthetic math/ERLE assertions for AEC diagnostic probes."""

from __future__ import annotations

import numpy as np

from tools.aec_diag.utils import (
    DelayEstimate,
    align_by_delay,
    compute_erle_db,
    estimate_delay_samples,
)


class TestDelayAlignmentMath:
    def test_positive_delay_indicates_mic_lags_reference(self):
        sample_rate = 16000
        ref = np.zeros(sample_rate, dtype=np.float32)
        ref[1200:1210] = 1.0
        mic = np.zeros_like(ref)
        lag = 80
        mic[1200 + lag : 1210 + lag] = 1.0

        estimate = estimate_delay_samples(mic, ref, sample_rate=sample_rate, max_lag_seconds=0.2)
        assert estimate is not None
        assert isinstance(estimate, DelayEstimate)
        assert abs(estimate.lag_samples - lag) <= 1

    def test_negative_delay_indicates_mic_precedes_reference(self):
        sample_rate = 16000
        ref = np.zeros(sample_rate, dtype=np.float32)
        ref[1200:1210] = 1.0
        mic = np.zeros_like(ref)
        lag = -70
        mic[1200 + lag : 1210 + lag] = 1.0
        estimate = estimate_delay_samples(mic, ref, sample_rate=sample_rate, max_lag_seconds=0.2)
        assert estimate is not None
        assert abs(estimate.lag_samples + abs(lag)) <= 1


class TestAlignmentCompensation:
    def test_align_by_delay_positive_pads_reference(self):
        ref = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        mic = np.array([9.0, 10.0, 11.0, 12.0, 13.0], dtype=np.float32)
        aligned_ref, aligned_mic = align_by_delay(ref, mic, lag_samples=2)
        assert np.array_equal(aligned_mic, mic)
        assert np.array_equal(aligned_ref[:2], np.array([0.0, 0.0], dtype=np.float32))

    def test_align_by_delay_negative_pads_mic(self):
        ref = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        mic = np.array([9.0, 10.0, 11.0], dtype=np.float32)
        aligned_ref, aligned_mic = align_by_delay(ref, mic, lag_samples=-1)
        assert np.array_equal(aligned_ref, ref[:3])
        assert np.array_equal(aligned_mic[:1], np.array([0.0], dtype=np.float32))


class TestErleComputation:
    def test_erle_improves_when_residual_is_smaller(self):
        mic = np.array([1.0, -1.0, 0.5, -0.5], dtype=np.float32)
        cleaned = mic * 0.98
        erle = compute_erle_db(mic, cleaned)
        assert erle > 25.0

    def test_erle_nan_for_empty_inputs(self):
        assert np.isnan(compute_erle_db([], []))

    def test_erle_reports_infinite_when_residual_zero(self):
        mic = np.array([0.1, -0.1, 0.05], dtype=np.float32)
        cleaned = mic.copy()
        assert compute_erle_db(mic, cleaned) == float("inf")
