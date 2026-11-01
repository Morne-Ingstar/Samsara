"""Speech-level diagnostics at the decoder boundary (1011).

Observe only: gain changes require acoustic evidence of an accuracy benefit.
Never retain PCM or decoded text. The existing speech gate supplies evidence;
unknown or absent speech never produces a speech-level summary.
"""
from dataclasses import dataclass
import logging
from math import log10
from threading import Lock

import numpy as np


logger = logging.getLogger('Samsara')
CLIP_SAMPLE = .999
CLIP_FRACTION = .001
MAX_PROBE_S = 30
# PLACEHOLDER COPY (1011)
CLIPPING_HINT = 'Input is clipping - lower the mic level.'
_creation_lock = Lock()


@dataclass(frozen=True)
class Levels:
    peak: float
    rms: float
    clipped_fraction: float
    clipping: bool


def measure(audio):
    """Reject non-PCM/invalid inputs; one near-full-scale peak is not clipping."""
    if (not isinstance(audio, np.ndarray) or audio.ndim != 1 or not audio.size
            or audio.dtype.kind != 'f' or not np.isfinite(audio).all()):
        return None
    absolute = np.abs(audio.astype(np.float64))
    count = int(np.count_nonzero(absolute >= CLIP_SAMPLE))
    fraction = count / audio.size
    return Levels(float(np.max(absolute)), float(np.sqrt(np.mean(absolute ** 2))),
                  fraction, count >= 3 and fraction >= CLIP_FRACTION)


def speech_percentiles(audio, presence):
    """90th percentile of 32 ms frame peaks/RMS, only on gated speech frames."""
    from samsara.speech_presence import PROBABILITY
    if not presence.known or not presence.passes() or not presence.probabilities:
        return None
    peaks, rms = [], []
    for i, probability in enumerate(presence.probabilities):
        if probability <= PROBABILITY:
            continue
        frame = audio[i * 512:(i + 1) * 512].astype(np.float64)
        if frame.size:
            peaks.append(float(np.max(np.abs(frame))))
            rms.append(float(np.sqrt(np.mean(frame ** 2))))
    if not peaks or not max(peaks):
        return None
    return float(np.percentile(peaks, 90)), float(np.percentile(rms, 90)), len(peaks)


class SessionLevels:
    """One INFO sample and at most one clipping hint per loaded-model session."""
    def __init__(self):
        self.lock = Lock()
        self.summarized = False
        self.warned = False

    def observe(self, audio):
        with self.lock:
            if self.summarized and self.warned:
                return
            levels = measure(audio)
            if levels is None:
                return
            if levels.clipping and not self.warned:
                self.warned = True
                logger.debug('[INPUT_LEVEL] %s peak=%.6f clipped_fraction=%.6f',
                             CLIPPING_HINT, levels.peak, levels.clipped_fraction)
            if self.summarized:
                return
            from samsara.speech_presence import _presence
            # Bound diagnostics work on runaway recordings; decode still gets all PCM.
            probe = audio[:MAX_PROBE_S * 16000]
            stats = speech_percentiles(probe, _presence.measure(probe, 16000))
            if stats is None:
                return
            peak, rms, frames = stats
            self.summarized = True
            logger.info(
                '[INPUT_LEVEL] scope=first_speech_decode probe_s=%.3f '
                'speech_frames=%d speech_peak_p90=%.6f speech_rms_p90=%.6f '
                'peak_p90_dbfs=%.2f rms_p90_dbfs=%.2f normalization=off',
                len(probe) / 16000, frames, peak, rms,
                20 * log10(max(peak, 1e-6)), 20 * log10(max(rms, 1e-6)))


def observe_decode(model, args, kwargs):
    """Best-effort facade seam: preserve input identity, dtype and decoder kwargs."""
    try:
        audio = args[0] if args else kwargs.get('audio')
        # Access our facade's own state without delegating through __getattr__.
        with _creation_lock:
            state = model.__dict__.get('_input_level_session')
            if state is None:
                state = model._input_level_session = SessionLevels()
        state.observe(audio)
    except Exception:
        # Diagnostics must never interfere with an admitted decode or CPU retry.
        logger.debug('[INPUT_LEVEL] measurement unavailable')
