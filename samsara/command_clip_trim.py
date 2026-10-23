"""Prepare short command-key captures without changing dictation or admission.

Only measured speech and the capture's actual earcon endpoint locate the trim.
Unknown VAD evidence leaves PCM intact. No recognizer thresholds are relaxed.
"""
from dataclasses import dataclass
import logging
from math import ceil
from time import perf_counter

import numpy as np

from samsara.transcription_params import build_hotkey_transcribe_params


MAX_CLIP_S = 5.0
MARGIN_S = .2
MIN_RUN_S = .096
TARGET_RMS = .05
MAX_GAIN = 4.0
FRAME = 512
RATE = 16000
logger = logging.getLogger('Samsara')


def build_params(app, *, is_command=None):
    """Resolve parameters from immutable ownership when release has cleared flags."""
    if is_command is None:
        is_command = bool(getattr(app, 'command_mode_recording', False))
    return build_hotkey_transcribe_params(
        app.get_transcription_params(include_vocabulary=is_command),
        command_mode_recording=is_command,
    )


@dataclass(frozen=True)
class CommandClip:
    audio: np.ndarray
    start_sample: int = 0
    speech_sample: int = 0
    longest_s: float = 0.0
    gain: float = 1.0
    earcon_end_ms: float = -1.0


def prepare(audio, sample_rate, probabilities, *, capture=None):
    """Keep 200 ms before the first >=96 ms speech run, after our start sound.

    Scores are the existing app Silero's complete 32 ms frames. The first
    qualifying run wins, even if a later run is longer. Normalisation uses
    peak *speech-frame* RMS, capped at 4x and at .95 peak to avoid clipping.
    Missing timing uses onset alone; it never guesses pre-roll or cue length.
    """
    unchanged = CommandClip(audio)
    pcm = np.asarray(audio)
    if (sample_rate != RATE or pcm.ndim != 1 or not len(pcm)
            or len(pcm) > MAX_CLIP_S * sample_rate or not np.isfinite(pcm).all()):
        return unchanged
    scores = np.asarray(probabilities, dtype=np.float32)
    if (scores.ndim != 1 or len(scores) != len(pcm) // FRAME
            or not np.isfinite(scores).all() or np.any(scores < 0) or np.any(scores > 1)):
        return unchanged
    earcon_end_ms = -1.0
    if capture is not None:
        try:
            earcon_end_ms = float(capture.offsets()[2])
            if not np.isfinite(earcon_end_ms):
                return unchanged
        except (AttributeError, TypeError, ValueError, OverflowError):
            return unchanged
    boundary = max(0, ceil(earcon_end_ms * sample_rate / 1000))
    runs = []
    run_start = None
    for index in range(len(scores) + 1):
        positive = index < len(scores) and scores[index] > .1
        if positive and run_start is None:
            run_start = index * FRAME
        if not positive and run_start is not None:
            start, end = max(run_start, boundary), index * FRAME
            if end - start >= ceil(MIN_RUN_S * sample_rate):
                runs.append((start, end))
            run_start = None
    if not runs:
        return unchanged
    speech_start = runs[0][0]
    start = max(boundary, speech_start - round(MARGIN_S * sample_rate))
    peak_rms = 0.0
    for begin, end in runs:
        for frame_start in range(begin, end, FRAME):
            frame = pcm[frame_start:min(end, frame_start + FRAME)].astype(np.float64)
            peak_rms = max(peak_rms, float(np.sqrt(np.mean(frame ** 2))))
    gain = 1.0
    peak = float(np.max(np.abs(pcm[start:])))
    if 0 < peak_rms < TARGET_RMS and peak > 0:
        gain = max(1.0, min(MAX_GAIN, TARGET_RMS / peak_rms, .95 / peak))
    trimmed = audio if start == 0 and gain == 1 else (pcm[start:] * gain).astype(np.float32)
    return CommandClip(trimmed, start, speech_start,
                       max(end - begin for begin, end in runs) / sample_rate,
                       gain, earcon_end_ms)


def decode(app, audio, params, duration, *, is_command, capture=None):
    """Use the real command decoder; preserve the ordinary hold call unchanged."""
    if not is_command:
        return app._decode_hotkey_audio(audio, params, duration, free_form=True)
    prepared = CommandClip(audio)
    started = perf_counter()
    if (app.model_rate == RATE and 0 < len(audio) <= MAX_CLIP_S * RATE
            and getattr(app, '_vad_available', False)
            and getattr(app, '_vad_model', None) is not None):
        try:
            with app._vad_lock:
                # Copy: inference must not mutate the buffer retained by callers.
                scores = app._vad_probabilities(np.array(audio, dtype=np.float32, copy=True))
            prepared = prepare(audio, app.model_rate, scores, capture=capture)
        except Exception as exc:
            logger.debug('[COMMAND_CLIP] preparation unavailable exception_type=%s', type(exc).__name__)
    logger.info(
        '[COMMAND_CLIP] source_s=%.3f decode_s=%.3f onset_ms=%.1f '
        'trim_ms=%.1f earcon_end_ms=%.1f longest_s=%.3f gain=%.3f prepare_ms=%.1f',
        duration, len(prepared.audio) / app.model_rate,
        prepared.speech_sample / app.model_rate * 1000,
        prepared.start_sample / app.model_rate * 1000, prepared.earcon_end_ms,
        prepared.longest_s, prepared.gain, (perf_counter() - started) * 1000,
    )
    return app._decode_hotkey_audio(
        prepared.audio, params, len(prepared.audio) / app.model_rate, free_form=False,
    )
