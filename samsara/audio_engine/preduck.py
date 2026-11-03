"""1070: identify playback-only pre-roll; never trim by duck/press time.

The render reference is ephemeral, bounded, and never decoded or persisted.
This is a conservative source check, not the retired adaptive echo canceller.
"""
from collections import deque
import logging
import threading
import time

import numpy as np

from .frame import SAMPLE_RATE

logger = logging.getLogger(__name__)
MIN_CORRELATION = .995
MAX_RESIDUAL_RATIO = .03
MAX_RESIDUAL_RMS = .0005
ALIGNMENT_S = .25


def enabled(config):
    from samsara.config_defaults import cfg_get
    return isinstance(config, dict) and cfg_get(config, 'dictation.drop_playback_preroll') is True


def start_reference(engine):
    """Start with the permanent mic so render history predates a hold."""
    if not enabled(getattr(engine, '_config', None)):
        return
    try:
        from samsara.echo_cancel import LoopbackCapture, pyaudio

        class RenderCapture(LoopbackCapture):
            def __init__(self):
                super().__init__(target_rate=SAMPLE_RATE)
                self._chunks = deque(maxlen=40)
                self._chunks_lock = threading.Lock()

            def _stream_callback(self, data, count, timing, status):
                # Parent opens the stream before setting _running. Accept the
                # initial callback too; do not terminate it during startup.
                try:
                    pcm = np.frombuffer(data, np.float32)
                    pcm = pcm.reshape(-1, self._device_channels).mean(axis=1)
                    pcm = self._resample(pcm, self._device_rate, SAMPLE_RATE)
                    now = time.perf_counter()
                    with self._chunks_lock:
                        if status or (self._chunks and now-self._chunks[-1][0] > .15):
                            self._chunks.clear()
                        if not status:
                            self._chunks.append((now, pcm.astype(np.float32)))
                except Exception:
                    with self._chunks_lock:
                        self._chunks.clear()
                return None, pyaudio.paContinue

            def snapshot(self):
                with self._chunks_lock:
                    if not self.is_running or not self._chunks:
                        return None
                    return self._chunks[-1][0], np.concatenate([x[1] for x in self._chunks])

        reference = RenderCapture()
        if reference.start():
            engine._hold_render_reference = reference
            return
        reference.stop()
    except Exception:
        logger.debug('[PRE] Render reference unavailable', exc_info=True)
    engine._hold_render_reference = None
    log_unavailable(engine)


def stop_reference(engine):
    reference = getattr(engine, '_hold_render_reference', None)
    engine._hold_render_reference = None
    if reference is not None:
        try:
            reference.stop()
        except Exception:
            logger.debug('[PRE] Render reference close failed', exc_info=True)


def log_unavailable(engine):
    if not getattr(engine, '_preduck_unavailable_logged', False):
        engine._preduck_unavailable_logged = True
        logger.info('[PRE] Render loopback unavailable; preserving hold pre-roll')


class PlaybackPrefix:
    """Only a contiguous, confidently playback-only prefix can be removed.

    An unexplained non-silent frame latches preservation for the whole hold.
    Five 20ms residual checks protect a short/quiet user onset inside an
    otherwise highly correlated 100ms playback block. No signal subtraction.
    """
    def __init__(self, snapshot, end_at, epoch):
        self.snapshot = snapshot
        self.end_at = end_at
        self.epoch = epoch
        self.preserve = False
        self.removed = 0
        self.measurements = []

    def excludes(self, frame):
        if self.preserve or frame.t_capture > self.end_at + 1e-5:
            return False
        if frame.device_epoch != self.epoch:
            self.preserve = True
            return False
        mic = frame.pcm.astype(np.float64) / 32767.
        if np.sqrt(np.mean(mic*mic)) < .0001:
            return False
        matched, score, residual = playback_match(mic, frame.t_capture, self.snapshot)
        self.measurements.append((score, residual))
        if not matched:
            self.preserve = True
            return False
        self.removed += 1
        return True


def playback_match(mic, end_at, snapshot):
    """Search bounded lag; require waveform AND locally negligible residual.

    The fitted gain tolerates speaker volume. A match still needs positive
    gain, broadband content, and every 20ms residual below both limits.
    Missing, stale, silent, tonal or incomplete evidence always keeps PCM.
    """
    if snapshot is None:
        return False, 0., float('inf')
    ref_end, reference = snapshot
    ref_start = ref_end - len(reference)/SAMPLE_RATE
    mic_start = end_at - len(mic)/SAMPLE_RATE
    lo = max(0, int((mic_start-ALIGNMENT_S-ref_start)*SAMPLE_RATE))
    hi = min(len(reference), int((end_at+.05-ref_start)*SAMPLE_RATE))
    ref = np.asarray(reference[lo:hi], dtype=np.float64)
    if len(ref) < len(mic) or not np.all(np.isfinite(ref)):
        return False, 0., float('inf')
    x = mic - mic.mean()
    energy = float(x @ x)
    if energy < 1e-8:
        return False, 0., float('inf')
    n = len(x)
    sums = np.concatenate(([0.], np.cumsum(ref)))
    squares = np.concatenate(([0.], np.cumsum(ref*ref)))
    ref_energy = squares[n:]-squares[:-n] - (sums[n:]-sums[:-n])**2/n
    dots = np.correlate(ref, x, 'valid')
    scores = dots / np.sqrt(np.maximum(ref_energy, 1e-20)*energy)
    best = int(np.argmax(scores))
    score = float(scores[best])
    chosen = ref[best:best+n]
    chosen = chosen-chosen.mean()
    gain = float(chosen @ x) / max(float(chosen @ chosen), 1e-20)
    residual = x - gain*chosen
    # A narrow tone can correlate by chance; require a diverse waveform.
    spectrum = np.abs(np.fft.rfft(chosen))**2
    broadband = spectrum.max() < .25*max(spectrum.sum(), 1e-20)
    local_residual = np.sqrt(np.mean(residual.reshape(5, -1)**2, axis=1))
    local_mic = np.sqrt(np.mean(x.reshape(5, -1)**2, axis=1))
    matched = (score >= MIN_CORRELATION and gain > 0 and broadband
               and np.all(local_residual <= MAX_RESIDUAL_RMS)
               and np.all(local_residual <= MAX_RESIDUAL_RATIO*local_mic))
    return bool(matched), score, float(local_residual.max())


def begin(consumer):
    consumer._playback_prefix = None
    evidence = getattr(consumer, '_speech_presence_capture', None)
    app = consumer._app
    config = getattr(app, 'config', None)
    if (not enabled(config) or evidence is None
            or config.get('mode', 'hold') != 'hold'
            or any(getattr(app, name, False) for name in
                   ('command_mode_recording', 'ava_mode_recording', '_memo_recording'))):
        return
    reference = getattr(consumer._engine, '_hold_render_reference', None)
    snapshot = None
    if reference is not None:
        try:
            snapshot = reference.snapshot()
        except Exception:
            logger.debug('[PRE] Render snapshot failed', exc_info=True)
    if snapshot is None:
        log_unavailable(consumer._engine)
        return
    # Only pre-press history is pre-roll. Preserve everything from the press,
    # including the duck transition: user onset there is especially likely.
    # Time only bounds eligibility; it can never authorize suppression.
    end = min(time.perf_counter(), evidence.requested_at,
              getattr(app, '_hold_capture_duck_confirmed_at', None) or evidence.requested_at)
    consumer._playback_prefix = PlaybackPrefix(snapshot, end, consumer._reader._bus.device_epoch)


def excludes(consumer, frame):
    prefix = getattr(consumer, '_playback_prefix', None)
    return prefix is not None and prefix.excludes(frame)


def finish(consumer):
    prefix = getattr(consumer, '_playback_prefix', None)
    if prefix is not None:
        prefix.snapshot = None
        if prefix.removed:
            logger.debug('[PRE] Suppressed %d render-matched pre-roll blocks', prefix.removed)
