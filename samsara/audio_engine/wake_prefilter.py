"""The one signal path into OpenWakeWord: shared by the live wake consumer and
the mic setup guide's wake-word test (queue 67).

The guide's test exists to tell the user whether their wake word works, so it
must score exactly what production scores. Until 67 it had its own capture
stream, its own linear-interpolation resampler and its own copy of the gain,
and on the owner's 2-channel Focusrite it scored 0.007-0.058 on the same words
the live detector scored 0.977 / 0.436 on (samsara.log, 2026-09-15 02:09).
Measured cause: a blocking-mode channels=1 WASAPI read on that 2-channel
endpoint returns samples with their time structure destroyed (lag-1
autocorrelation ~0.00 against ~0.7-0.99 for the same microphone read by the
engine's callback stream). RMS survives, speech does not.

Everything that shapes what the detector hears lives here:

  * pcm_to_float      ring int16 frame -> float32, exactly as consumers decode it
  * oww_prefilter     the automatic gain applied before OpenWakeWord
  * NativeRateFrames  native-rate capture blocks -> the 16 kHz frames the engine
                      would have written to the ring (the engine's own polyphase
                      filter and int16 quantisation), for a caller that cannot
                      read the ring because the engine is not running
"""

from __future__ import annotations

import numpy as np

from .engine import _gcd, _get_polyphase_filter, _polyphase_resample
from .frame import FRAME_MS, FRAME_SIZE, SAMPLE_RATE

#: A chunk at or below this RMS is fed to OpenWakeWord unamplified.
OWW_GAIN_FLOOR_RMS = 0.005
#: Above the floor, a chunk is amplified toward this RMS...
OWW_TARGET_RMS = 0.10
#: ...by at most this factor.
OWW_MAX_GAIN = 20.0


def pcm_to_float(pcm: np.ndarray) -> np.ndarray:
    """A ring frame's int16 PCM as float32 in [-1, 1] (always a copy: ring
    frames are views into memory the writer reuses)."""
    return pcm.astype(np.float32) / 32767.0


def chunk_rms(chunk: np.ndarray) -> float:
    return float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0


def oww_prefilter(chunk: np.ndarray, rms: float | None = None) -> np.ndarray:
    """The 16 kHz chunk OpenWakeWord is fed: a copy of ``chunk``, amplified
    toward OWW_TARGET_RMS (gain capped at OWW_MAX_GAIN, clipped to [-1, 1])
    when its RMS is above OWW_GAIN_FLOOR_RMS. ``rms`` is the caller's
    already-computed RMS of ``chunk``; computed here when omitted."""
    out = chunk.copy()
    if rms is None:
        rms = chunk_rms(chunk)
    if rms > OWW_GAIN_FLOOR_RMS:
        gain = min(OWW_TARGET_RMS / rms, OWW_MAX_GAIN)
        out = np.clip(out * gain, -1.0, 1.0)
    return out


class NativeRateFrames:
    """Converts native-rate mono capture blocks (float32, one block per
    100 ms) into the float32 16 kHz frames a ring consumer would decode for
    the same blocks: AudioCaptureEngine's polyphase resample, pad/truncate to
    FRAME_SIZE, int16 quantisation, then pcm_to_float. Pinned to the engine's
    own callback by tests/test_wizard_wake_signal_path.py."""

    def __init__(self, native_rate: int):
        self.native_rate = int(native_rate)
        self.blocksize = int(self.native_rate * FRAME_MS // 1000)   # engine's own blocksize
        g = _gcd(self.native_rate, SAMPLE_RATE)
        self._up = SAMPLE_RATE // g
        self._down = self.native_rate // g
        self._state = None
        if self._up != 1 or self._down != 1:
            h_padded, taps_per_phase, n_pre_remove = _get_polyphase_filter(self._up, self._down)
            n_out = self.blocksize * self._up
            n_out = n_out // self._down + bool(n_out % self._down)
            self._state = (h_padded, taps_per_phase, n_pre_remove, n_out)

    def convert(self, block: np.ndarray) -> np.ndarray:
        flat = np.asarray(block, dtype=np.float32).reshape(-1)
        if len(flat) != self.blocksize:
            # The precomputed polyphase indices assume exactly one engine
            # block; a short read would index past the input.
            fixed = np.zeros(self.blocksize, dtype=np.float32)
            n = min(len(flat), self.blocksize)
            fixed[:n] = flat[:n]
            flat = fixed
        if self._state is not None:
            h_padded, taps_per_phase, n_pre_remove, n_out = self._state
            resampled = _polyphase_resample(flat, self._up, self._down,
                                            h_padded, taps_per_phase, n_pre_remove, n_out)
        else:
            resampled = flat
        if len(resampled) < FRAME_SIZE:
            padded = np.zeros(FRAME_SIZE, dtype=np.float32)
            padded[:len(resampled)] = resampled
            resampled = padded
        pcm = np.clip(resampled[:FRAME_SIZE] * 32767.0, -32768, 32767).astype(np.int16)
        return pcm_to_float(pcm)
