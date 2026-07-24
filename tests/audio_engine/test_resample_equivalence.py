"""Numerical equivalence: samsara.audio_engine.engine._polyphase_resample
vs scipy.signal.resample_poly.

Part of the 2026-07-2x P0 fix: the RT audio callback (AudioCaptureEngine.
_on_audio_block) can no longer call scipy.signal.resample_poly at all (see
engine.py's module docstring for why -- its array-API-compat probing
crashes while torch is mid-import in another thread). _polyphase_resample
is a from-scratch numpy-only replacement; this file is the proof that it
actually reproduces resample_poly's output, not just that it runs.

Covers both (up, down) pairs the engine's own GCD reduction actually
produces for its two real-world native rates (see engine.py's
"Resampling" docstring): 44100Hz -> (160, 441), 48000Hz -> (1, 3).
"""
import numpy as np
import pytest
from scipy.signal import resample_poly

from samsara.audio_engine.engine import _design_polyphase_filter, _polyphase_resample
from samsara.audio_engine.frame import FRAME_MS, FRAME_SIZE

# (up, down, native_rate) -- the exact pairs _open_stream() computes for
# the engine's two real-world native device rates.
RATE_CASES = [
    pytest.param(160, 441, 44100, id="44100Hz"),
    pytest.param(1, 3, 48000, id="48000Hz"),
]

# Tight tolerance: float32 machine epsilon is ~1.19e-7; both paths operate
# on float32-range audio (roughly [-1, 1]), so any real algorithmic
# divergence would show up as many times this, not fractions of it.
MAX_ABS_TOLERANCE = 5e-6


def _blocksize(native_rate: int) -> int:
    return int(native_rate * FRAME_MS // 1000)


def _resample_new(x, up, down):
    h_padded, taps_per_phase, n_pre_remove = _design_polyphase_filter(up, down)
    n_out = x.shape[0] * up
    n_out = n_out // down + bool(n_out % down)
    return _polyphase_resample(x, up, down, h_padded, taps_per_phase, n_pre_remove, n_out)


def _sine_fixture(native_rate, blocksize, freq=440.0, amplitude=0.3, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(blocksize) / native_rate
    sine = amplitude * np.sin(2 * np.pi * freq * t)
    noise = 0.05 * rng.standard_normal(blocksize)
    return (sine + noise).astype(np.float32)


class TestOutputShape:
    @pytest.mark.parametrize("up,down,native_rate", RATE_CASES)
    def test_output_length_matches_frame_size(self, up, down, native_rate):
        blocksize = _blocksize(native_rate)
        x = _sine_fixture(native_rate, blocksize)
        y = _resample_new(x, up, down)
        assert len(y) == FRAME_SIZE

    @pytest.mark.parametrize("up,down,native_rate", RATE_CASES)
    def test_output_length_matches_scipy_reference(self, up, down, native_rate):
        blocksize = _blocksize(native_rate)
        x = _sine_fixture(native_rate, blocksize)
        y_new = _resample_new(x, up, down)
        y_ref = resample_poly(x, up, down)
        assert len(y_new) == len(y_ref)


class TestNumericalEquivalence:
    @pytest.mark.parametrize("up,down,native_rate", RATE_CASES)
    def test_sine_plus_noise_matches_scipy_within_tolerance(self, up, down, native_rate):
        blocksize = _blocksize(native_rate)
        x = _sine_fixture(native_rate, blocksize, freq=440.0, seed=1)
        y_new = _resample_new(x, up, down)
        y_ref = resample_poly(x, up, down).astype(np.float32)
        max_dev = np.max(np.abs(y_new - y_ref))
        assert max_dev < MAX_ABS_TOLERANCE, f"max abs deviation {max_dev} >= {MAX_ABS_TOLERANCE}"

    @pytest.mark.parametrize("up,down,native_rate", RATE_CASES)
    @pytest.mark.parametrize("freq", [110.0, 1000.0, 4000.0])
    def test_multiple_frequencies_match_scipy(self, up, down, native_rate, freq):
        blocksize = _blocksize(native_rate)
        x = _sine_fixture(native_rate, blocksize, freq=freq, seed=2)
        y_new = _resample_new(x, up, down)
        y_ref = resample_poly(x, up, down).astype(np.float32)
        max_dev = np.max(np.abs(y_new - y_ref))
        assert max_dev < MAX_ABS_TOLERANCE

    @pytest.mark.parametrize("up,down,native_rate", RATE_CASES)
    def test_pure_noise_matches_scipy(self, up, down, native_rate):
        blocksize = _blocksize(native_rate)
        rng = np.random.default_rng(3)
        x = (0.5 * rng.standard_normal(blocksize)).astype(np.float32)
        y_new = _resample_new(x, up, down)
        y_ref = resample_poly(x, up, down).astype(np.float32)
        max_dev = np.max(np.abs(y_new - y_ref))
        assert max_dev < MAX_ABS_TOLERANCE

    @pytest.mark.parametrize("up,down,native_rate", RATE_CASES)
    def test_silence_matches_scipy(self, up, down, native_rate):
        blocksize = _blocksize(native_rate)
        x = np.zeros(blocksize, dtype=np.float32)
        y_new = _resample_new(x, up, down)
        y_ref = resample_poly(x, up, down).astype(np.float32)
        assert np.max(np.abs(y_new - y_ref)) < MAX_ABS_TOLERANCE

    @pytest.mark.parametrize("up,down,native_rate", RATE_CASES)
    def test_full_scale_does_not_diverge(self, up, down, native_rate):
        """Near full-scale input (close to the +/-1 range real captured
        audio is normalized to) -- checks the tolerance isn't only tight
        because the earlier fixtures happen to be quiet."""
        blocksize = _blocksize(native_rate)
        x = _sine_fixture(native_rate, blocksize, freq=880.0, amplitude=0.95, seed=4)
        y_new = _resample_new(x, up, down)
        y_ref = resample_poly(x, up, down).astype(np.float32)
        max_dev = np.max(np.abs(y_new - y_ref))
        assert max_dev < MAX_ABS_TOLERANCE
