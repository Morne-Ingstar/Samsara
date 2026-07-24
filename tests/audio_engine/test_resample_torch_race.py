"""Regression test for the 2026-07-2x P0 incident: the RT audio callback
(AudioCaptureEngine._on_audio_block) crashed on EVERY block while torch
was mid-import in another thread.

sys.modules['torch'] exists from the moment `import torch` *starts* in
any thread (well before the module body finishes executing and binds
names like `Tensor`) -- so any other thread calling scipy.signal.
resample_poly() in that window hits AttributeError: "partially
initialized module 'torch' has no attribute 'Tensor'" inside scipy's
array-API-compat probing (array_namespace(x) -> is_array_api_obj ->
getattr(torch, 'Tensor')). The fix (engine.py's _polyphase_resample) is
a numpy-only replacement that never calls resample_poly at all -- this
file proves the hot path survives a broken/mid-import torch AND never
touches the dangerous scipy call in the first place.
"""
import sys
import types

import numpy as np
import pytest

from samsara.audio_engine.engine import AudioCaptureEngine, _design_polyphase_filter
from samsara.audio_engine.frame import FRAME_MS, FRAME_SIZE
from samsara.audio_engine.ring import FrameBus


class _PartiallyInitializedTorchStub(types.ModuleType):
    """Stands in for `torch` mid-import: present in sys.modules (so
    `'torch' in sys.modules` is True, exactly like a real in-progress
    import), but missing `Tensor` and everything else the real module's
    body would eventually bind -- the exact shape that crashed
    resample_poly in production."""


def _build_engine(up: int, down: int, native_rate: int) -> tuple[AudioCaptureEngine, int]:
    """Sets up an AudioCaptureEngine's resample state the same way
    _open_stream() would, WITHOUT touching sounddevice/PortAudio (no
    audio hardware needed for this test) -- mirrors _open_stream()'s own
    precompute-at-open-time sequence exactly."""
    engine = AudioCaptureEngine(FrameBus())
    blocksize = int(native_rate * FRAME_MS // 1000)
    engine._up = up
    engine._down = down
    engine._blocksize = blocksize
    if up != 1 or down != 1:
        h_padded, taps_per_phase, n_pre_remove = _design_polyphase_filter(up, down)
        n_out = blocksize * up
        n_out = n_out // down + bool(n_out % down)
        engine._resample_state = (h_padded, taps_per_phase, n_pre_remove, n_out)
    else:
        engine._resample_state = None
    return engine, blocksize


def _make_indata(blocksize: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    mono = (0.3 * np.sin(2 * np.pi * 440 * np.arange(blocksize) / 44100)
            + 0.02 * rng.standard_normal(blocksize)).astype(np.float32)
    return mono.reshape(-1, 1)  # sounddevice shape: (frames, channels)


def _read_latest_pcm(engine: AudioCaptureEngine) -> np.ndarray:
    reader = engine.register_consumer("test")
    reader.rewind(1)
    frame = reader.read_next()
    assert frame is not None and hasattr(frame, "pcm"), "expected a written frame, got EMPTY"
    return frame.pcm.copy()


class TestSurvivesTorchMidImport:
    def test_no_exception_with_broken_torch_present(self, monkeypatch):
        engine, blocksize = _build_engine(160, 441, 44100)
        monkeypatch.setitem(sys.modules, "torch", _PartiallyInitializedTorchStub("torch"))
        assert not hasattr(sys.modules["torch"], "Tensor")  # sanity: the stub really is broken

        indata = _make_indata(blocksize)

        # Must not raise -- this is the actual bug: resample_poly's
        # array-API probe crashed on every block in this exact state.
        engine._on_audio_block(indata, blocksize, None, None)

    def test_output_correct_with_broken_torch_present(self, monkeypatch):
        """Not just "doesn't crash" -- the resampled frame written to the
        ring must be the SAME as it would be with a healthy torch (or no
        torch at all), proving the callback's behavior is unaffected by
        torch's import state, not just that it degrades gracefully."""
        engine_broken, blocksize = _build_engine(160, 441, 44100)
        indata = _make_indata(blocksize, seed=7)

        monkeypatch.setitem(sys.modules, "torch", _PartiallyInitializedTorchStub("torch"))
        engine_broken._on_audio_block(indata, blocksize, None, None)
        pcm_under_broken_torch = _read_latest_pcm(engine_broken)
        monkeypatch.undo()

        engine_healthy, _ = _build_engine(160, 441, 44100)
        engine_healthy._on_audio_block(indata, blocksize, None, None)
        pcm_healthy = _read_latest_pcm(engine_healthy)

        assert pcm_under_broken_torch.shape == (FRAME_SIZE,)
        np.testing.assert_array_equal(pcm_under_broken_torch, pcm_healthy)

    def test_no_exception_for_48khz_ratio_too(self, monkeypatch):
        engine, blocksize = _build_engine(1, 3, 48000)
        monkeypatch.setitem(sys.modules, "torch", _PartiallyInitializedTorchStub("torch"))
        indata = _make_indata(blocksize)
        engine._on_audio_block(indata, blocksize, None, None)


class TestHotPathNeverCallsResamplePoly:
    def test_resample_poly_is_never_invoked(self, monkeypatch):
        """Direct proxy for "never touches scipy's array-API probe at
        call time": poison scipy.signal.resample_poly itself (the exact
        function whose probing crashed in production) so any call raises
        -- the callback must complete without ever calling it."""
        import scipy.signal

        def _poison(*args, **kwargs):
            raise AssertionError(
                "resample_poly must never be called from the RT audio callback"
            )

        monkeypatch.setattr(scipy.signal, "resample_poly", _poison)

        engine, blocksize = _build_engine(160, 441, 44100)
        indata = _make_indata(blocksize)

        # Would raise the AssertionError above if the hot path still
        # called resample_poly anywhere (directly or via a module-level
        # binding captured before the patch).
        engine._on_audio_block(indata, blocksize, None, None)

    def test_identity_path_also_never_calls_resample_poly(self, monkeypatch):
        """up == down == 1 (no resampling needed) is the other branch
        through _on_audio_block -- confirm it's equally scipy-free."""
        import scipy.signal

        monkeypatch.setattr(
            scipy.signal, "resample_poly",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
        )

        engine, blocksize = _build_engine(1, 1, 16000)
        indata = _make_indata(blocksize)

        engine._on_audio_block(indata, blocksize, None, None)
