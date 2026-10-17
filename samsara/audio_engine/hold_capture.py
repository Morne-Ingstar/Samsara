"""Hold capture boundaries and loss evidence, independent of either display."""
import math
import time

from .frame import FRAME_MS


def begin(consumer):
    consumer.capture_loss = False
    consumer.dropped_frames = 0
    consumer._last_capture_seq = consumer._reader._read_cursor - 1
    consumer._capture_overflows = getattr(consumer._engine, '_overflow_count', 0)


def observe(consumer, frame):
    """Account before policy filtering, including an overrun on the first read."""
    lost = max(0, frame.seq - consumer._last_capture_seq - 1)
    consumer._last_capture_seq = frame.seq
    overflows = getattr(consumer._engine, '_overflow_count', 0)
    if lost:
        consumer.dropped_frames += lost
        consumer.capture_loss = True
        consumer._capture_logger.warning('[CAP] dropped %d frames', lost)
    if overflows != consumer._capture_overflows:
        consumer.capture_loss = True
        consumer._capture_overflows = overflows
        consumer._capture_logger.warning('[CAP] input overflow; missing frame count unknown')
    epoch = getattr(consumer, '_capture_epoch', None)
    if epoch is not None and epoch != frame.device_epoch:
        consumer.capture_loss = True
        consumer._capture_logger.warning('[CAP] device changed during hold')
    consumer._capture_epoch = frame.device_epoch


def boundary(consumer, fallback):
    """618 supersedes 404/370 for ordinary holds; preserve other capture lanes."""
    app = consumer._app
    evidence = consumer._speech_presence_capture
    config = getattr(app, 'config', None)
    special = any(getattr(app, name, False) for name in
                  ('command_mode_recording', 'ava_mode_recording', '_memo_recording'))
    if (evidence is None or special or not isinstance(config, dict)
            or config.get('mode', 'hold') != 'hold'):
        return fallback
    try:
        milliseconds = float(config.get('dictation', {}).get('hold_preroll_ms', 400))
        if not math.isfinite(milliseconds):
            milliseconds = 400
    except (AttributeError, TypeError, ValueError):
        milliseconds = 400
    milliseconds = min(500, max(0, milliseconds))
    # Whole-block admission costs up to 100 ms: 400 ms retains 300-400 ms,
    # covering the observed 130-190 ms latency and most of the 445 ms outlier.
    selected = evidence.requested_at - milliseconds / 1000
    selected = max(selected, getattr(consumer, '_previous_capture_end', 0.0))
    coordinator = getattr(app, 'audio_coordinator', None)
    if ((coordinator and coordinator.is_speaking)
            or time.monotonic() - getattr(app, '_tts_last_speaking', 0.0) < .5):
        selected = max(selected, time.perf_counter())
    onset = consumer._capture_onset
    if onset is not None:
        onset.boundary = selected
        onset.policy = 'hold_preroll_618'
    return selected


def finish(consumer):
    consumer._previous_capture_end = consumer._capture_not_after or time.perf_counter()
    # Overflow can occur without a subsequent readable frame.
    if getattr(consumer._engine, '_overflow_count', 0) != consumer._capture_overflows:
        consumer.capture_loss = True
        consumer._capture_logger.warning('[CAP] input overflow; missing frame count unknown')
