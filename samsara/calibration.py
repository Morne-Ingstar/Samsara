"""
Samsara microphone calibration.

Measures ambient noise level and computes a speech detection threshold
using IQR-based outlier rejection (Tukey's fences).
"""

import numpy as np
try:
    import sounddevice as sd
except Exception:
    sd = None
import time

from samsara.constants import (
    CALIBRATION_DURATION,
    CALIBRATION_CHUNK_MS,
    CALIBRATION_MULTIPLIER,
    CALIBRATION_FLOOR,
    CALIBRATION_CEILING,
    DEFAULT_SPEECH_THRESHOLD,
)


def measure_ambient_rms(device_id, capture_rate, duration=CALIBRATION_DURATION,
                        chunk_ms=CALIBRATION_CHUNK_MS):
    """Record ambient noise and return per-chunk RMS values.

    Opens a short InputStream at the device's native rate, collects RMS
    for *duration* seconds in *chunk_ms*-millisecond blocks.

    Returns:
        list of float RMS values, one per chunk.
    """
    if sd is None:
        print("[CAL] sounddevice not available — cannot measure ambient RMS")
        return []
    blocksize = int(capture_rate * chunk_ms / 1000)
    rms_values = []

    def _callback(indata, frames, time_info, status):
        chunk = indata.copy().flatten()
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        rms_values.append(rms)

    stream = sd.InputStream(
        samplerate=capture_rate,
        channels=1,
        dtype=np.float32,
        callback=_callback,
        device=device_id,
        blocksize=blocksize,
    )
    stream.start()
    time.sleep(duration)
    stream.stop()
    stream.close()

    return rms_values


def calibrate_threshold(rms_samples, multiplier=CALIBRATION_MULTIPLIER,
                        floor=CALIBRATION_FLOOR, ceiling=CALIBRATION_CEILING):
    """Compute speech threshold from ambient RMS samples.

    Uses IQR-based outlier rejection (Tukey's fences) to remove transient
    spikes (coughs, bumps), then takes the median of the cleaned set and
    multiplies by *multiplier*.

    Args:
        rms_samples: list of float RMS values from measure_ambient_rms()
        multiplier: how far above ambient to set the threshold
        floor: minimum returned threshold (guards against electrical noise)
        ceiling: maximum returned threshold (sanity cap)

    Returns:
        float threshold clamped between floor and ceiling.
        Falls back to DEFAULT_SPEECH_THRESHOLD if fewer than 3 samples.
    """
    if len(rms_samples) < 3:
        return DEFAULT_SPEECH_THRESHOLD

    arr = np.array(rms_samples)
    q1 = np.percentile(arr, 25)
    q3 = np.percentile(arr, 75)
    iqr = q3 - q1
    upper_fence = q3 + 1.5 * iqr

    cleaned = arr[arr <= upper_fence]
    if len(cleaned) < 1:
        cleaned = arr  # all outliers -- use everything

    ambient = float(np.median(cleaned))
    threshold = ambient * multiplier

    return float(np.clip(threshold, floor, ceiling))
