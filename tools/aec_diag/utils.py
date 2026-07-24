"""Pure math/helpers used by AEC diagnostics."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np


RESULTS_DIR = Path(__file__).resolve().parent / "results"


@dataclass(frozen=True)
class DelayEstimate:
    lag_samples: int
    delay_ms: float
    confidence: float


def _to_float32(audio: Sequence[float]) -> np.ndarray:
    return np.asarray(audio, dtype=np.float32).reshape(-1)


def estimate_delay_samples(
    mic: Sequence[float],
    reference: Sequence[float],
    sample_rate: int,
    max_lag_seconds: float | None = 0.25,
) -> DelayEstimate | None:
    """Cross-correlate two snippets and return the signed lag in samples.

    Positive lag means `reference` arrives before `mic` (normal playback->mic path),
    negative lag means `mic` arrives before `reference` (causality violation).
    """
    x = _to_float32(mic)
    y = _to_float32(reference)
    if x.size == 0 or y.size == 0:
        return None

    n = min(x.size, y.size)
    x = x[-n:]
    y = y[-n:]

    if n < 16:
        return None

    x = x - np.mean(x)
    y = y - np.mean(y)
    x /= np.linalg.norm(x) + 1e-12
    y /= np.linalg.norm(y) + 1e-12

    corr = np.correlate(x, y, mode="full")
    full_lags = np.arange(-n + 1, n, dtype=np.int64)

    if max_lag_seconds is not None:
        max_lag = int(sample_rate * max_lag_seconds)
        max_lag = max(1, min(max_lag, n - 1))
        center = n - 1
        lo = center - max_lag
        hi = center + max_lag + 1
        corr = corr[lo:hi]
        lags = full_lags[lo:hi]
    else:
        lags = full_lags

    if corr.size == 0:
        return None

    idx = int(np.argmax(np.abs(corr)))
    lag_samples = int(lags[idx])
    confidence = float(np.abs(corr[idx]) / (np.median(np.abs(corr)) + 1e-12))

    delay_ms = lag_samples * 1000.0 / float(sample_rate)
    return DelayEstimate(
        lag_samples=lag_samples,
        delay_ms=float(delay_ms),
        confidence=confidence,
    )


def align_by_delay(
    reference: Sequence[float],
    mic: Sequence[float],
    lag_samples: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Return `(aligned_reference, aligned_mic)` preserving overlap by delay.

    `lag_samples` comes from `estimate_delay_samples` and is the signed delay of
    `mic` relative to `reference`.
    """
    ref = _to_float32(reference)
    mic_signal = _to_float32(mic)

    if ref.size == 0 or mic_signal.size == 0:
        return ref.copy(), mic_signal.copy()

    if lag_samples > 0:
        # reference is earlier than mic -> delay reference by lag_samples
        aligned_ref = np.concatenate([np.zeros(lag_samples, dtype=np.float32), ref])
        aligned_mic = mic_signal
    elif lag_samples < 0:
        # mic is earlier -> delay mic by -lag_samples
        aligned_mic = np.concatenate([np.zeros(-lag_samples, dtype=np.float32), mic_signal])
        aligned_ref = ref
    else:
        aligned_ref = ref
        aligned_mic = mic_signal

    n = min(aligned_ref.size, aligned_mic.size)
    if n == 0:
        return aligned_ref, aligned_mic

    return aligned_ref[:n], aligned_mic[:n]


def resample_linear(audio: Sequence[float], source_rate: int, target_rate: int) -> np.ndarray:
    """Linear interpolation resample (mirrors LoopbackCapture._resample behavior)."""
    arr = _to_float32(audio)
    if source_rate == target_rate:
        return arr
    ratio = target_rate / float(source_rate)
    n_out = int(round(arr.size * ratio))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    indices = np.arange(n_out) / ratio
    indices = np.clip(indices, 0.0, arr.size - 1)
    lo = indices.astype(np.int64)
    hi = np.minimum(lo + 1, arr.size - 1)
    frac = indices - lo
    return arr[lo] * (1.0 - frac) + arr[hi] * frac


def resample_poly_or_soxr(audio: Sequence[float], source_rate: int, target_rate: int) -> np.ndarray:
    """Resample with soxr when available, otherwise scipy.signal.resample_poly."""
    arr = _to_float32(audio)
    if source_rate == target_rate:
        return arr

    try:
        import soxr  # type: ignore

        return soxr.resample(arr, source_rate, target_rate, quality="VHQ").astype(np.float32)
    except Exception:
        try:
            from math import gcd

            from scipy.signal import resample_poly

            g = gcd(int(source_rate), int(target_rate))
            up = target_rate // g
            down = source_rate // g
            return resample_poly(arr, up, down).astype(np.float32)
        except Exception as exc:
            raise RuntimeError(
                "Could not import soxr or scipy.signal.resample_poly for resampling."
            ) from exc


def compute_erle_db(mic: Sequence[float], cleaned: Sequence[float]) -> float:
    """Energy-Ratio Loudness Estimation (ERLE) in dB for one utterance."""
    mic_f32 = _to_float32(mic)
    cleaned_f32 = _to_float32(cleaned)

    if mic_f32.size == 0:
        return float("nan")

    n = min(mic_f32.size, cleaned_f32.size)
    if n == 0:
        return float("nan")

    mic_f32 = mic_f32[:n]
    cleaned_f32 = cleaned_f32[:n]

    eps = 1e-12
    mic_power = float(np.mean(mic_f32.astype(np.float64) ** 2))
    error_power = float(np.mean(((mic_f32 - cleaned_f32).astype(np.float64)) ** 2))

    if error_power < eps:
        return float("inf")
    if mic_power < eps:
        return 0.0
    return 10.0 * math.log10((mic_power + eps) / (error_power + eps))


def ensure_results_dir(path: Path | None = None) -> Path:
    target = path or RESULTS_DIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_results_json(
    payload: dict[str, Any],
    tool_name: str,
    output_dir: Path | None = None,
) -> Path:
    """Serialize a probe payload to tools/aec_diag/results."""
    folder = ensure_results_dir(output_dir)
    safe_name = tool_name.replace(" ", "_")
    filename = f"{safe_name}_{int(__import__('time').time())}.json"
    path = folder / filename
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    return path

