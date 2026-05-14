"""Audio utility helpers for the TTS subsystem."""

import io
import wave
from math import gcd
from typing import Tuple

import numpy as np


def parse_wav(raw_bytes: bytes) -> Tuple[np.ndarray, int, int]:
    """Parse a WAV byte buffer into a float32 numpy array.

    Returns:
        (pcm_f32, sample_rate, channels) where pcm_f32 is shape (N,) mono
        or (N, channels) for multi-channel, normalized to [-1, 1].
    """
    with wave.open(io.BytesIO(raw_bytes)) as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())

    if sw == 1:
        arr = np.frombuffer(frames, dtype=np.uint8).astype(np.float32)
        arr = (arr - 128.0) / 128.0
    elif sw == 2:
        arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
        arr /= 32768.0
    else:
        arr = np.frombuffer(frames, dtype=np.int32).astype(np.float32)
        arr /= 2 ** (sw * 8 - 1)

    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)

    return arr, sr, ch


def resample_pcm(pcm_f32: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Resample mono float32 PCM from from_rate to to_rate (polyphase).

    Returns the input array unchanged if rates already match, so callers
    can call this unconditionally without a branch.
    """
    if from_rate == to_rate:
        return pcm_f32
    from scipy.signal import resample_poly
    g = gcd(from_rate, to_rate)
    return resample_poly(pcm_f32, to_rate // g, from_rate // g).astype(np.float32)
