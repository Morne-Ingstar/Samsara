"""Experimental classic-DSP double clicks. Shadow telemetry only; no actions.

Five-ms analysis bins span bus boundaries. The fixed spectral/duration gates
are deliberately conservative, not proof that a sound came from a person.
Only the pair window (150--350 ms), refractory and RMS floor are configurable.
"""
from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

import numpy as np

from .frame import FRAME_SIZE, SAMPLE_RATE
from samsara.paths import samsara_home_dir

logger = logging.getLogger(__name__)
_BIN = SAMPLE_RATE // 200
_FREQ = np.fft.rfftfreq(_BIN, 1 / SAMPLE_RATE)
_HIGH = (_FREQ >= 2000) & (_FREQ <= 7000)


class MouthSoundDetector:
    """Bounded streaming feature/pair helper; exceptions belong to its guard."""

    def __init__(self, pair_window_ms=(150, 350), refractory_ms=800, onset_rms=.025):
        low, high = pair_window_ms
        if (not all(math.isfinite(x) for x in (low, high, refractory_ms, onset_rms))
                or not 50 <= low < high <= 1000 or not 350 <= refractory_ms <= 5000
                or not .001 <= onset_rms <= .5):
            raise ValueError('invalid mouth-sound thresholds')
        self.window = (float(low), float(high))
        self.refractory = refractory_ms / 1000
        self.floor = float(onset_rms)
        self.reset()

    def reset(self):
        self._tail = np.empty(0, dtype=np.float32)
        self._previous = np.zeros(_FREQ.size)
        self._previous_rms = 0.0
        self._active = None
        self._first = None
        self._until = -math.inf

    def feed(self, pcm, start):
        """Process at most one bus frame; start is its first sample's timestamp."""
        if (not isinstance(pcm, np.ndarray) or pcm.ndim != 1 or pcm.dtype != np.int16
                or not 0 < pcm.size <= FRAME_SIZE or not math.isfinite(start)):
            raise ValueError('invalid mouth-sound frame')
        data = np.concatenate((self._tail, pcm.astype(np.float32) / 32768))
        start -= self._tail.size / SAMPLE_RATE
        count = data.size // _BIN
        self._tail = data[count * _BIN:].copy()
        if not count:
            return []
        bins = data[:count * _BIN].reshape(count, _BIN)
        rms = np.sqrt(np.mean(bins * bins, axis=1))
        power = np.abs(np.fft.rfft(bins, axis=1)) ** 2
        totals = power.sum(axis=1) + 1e-15
        centroids = (power * _FREQ).sum(axis=1) / totals
        ratios = power[:, _HIGH].sum(axis=1) / totals
        flux = np.maximum(power - np.vstack((self._previous, power[:-1])), 0).sum(axis=1) / totals
        self._previous = power[-1].copy()
        events = []
        for i in range(count):
            ts = start + i * _BIN / SAMPLE_RATE
            level = float(rms[i])
            active = self._active
            if active is not None:
                onset, peak, score = active
                if level < max(self.floor * .5, peak * .3):
                    duration = ts - onset
                    if .010 - 1e-8 <= duration <= .025 + 1e-8 and score > 0:
                        event = self._transient(onset, score)
                        if event:
                            events.append(event)
                    self._active = None
                else:
                    # A sustained sound remains active until quiet, but cannot pair.
                    self._active = (onset, max(peak, level),
                                    score if ts - onset <= .025 else 0)
            elif (level >= self.floor and level >= max(self._previous_rms * 3, self.floor)
                  and flux[i] >= .65 and 1800 <= centroids[i] <= 6000 and ratios[i] >= .6):
                self._active = (ts, level, float(min(1, ratios[i] * flux[i])))
            self._previous_rms = level
        return events

    def _transient(self, ts, score):
        if ts < self._until:
            return None
        if self._first is not None:
            first, first_score = self._first
            gap = (ts - first) * 1000
            if self.window[0] - 1e-6 <= gap <= self.window[1] + 1e-6:
                self._first = None
                self._until = ts + self.refractory
                return dict(ts_first=first, ts_second=ts, gap_ms=round(gap, 3),
                            score=round(min(first_score, score), 4), reason='pair')
            if gap < self.window[0]:
                return None
        self._first = (ts, score)
        return None


class ShadowLog:
    """Numeric telemetry and fixed reason codes, bounded to two 1-MiB files."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else samsara_home_dir() / 'logs' / 'mouth_sounds_shadow.jsonl'
        self.counts = {}

    def write(self, reason, **numbers):
        self.counts[reason] = self.counts.get(reason, 0) + 1
        record = dict(reason=reason, count=self.counts[reason], **numbers)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size >= 1024 * 1024:
            self.path.replace(self.path.with_suffix('.jsonl.1'))
        with self.path.open('a', encoding='utf-8') as out:
            out.write(json.dumps(record, allow_nan=False, separators=(',', ':')) + '\n')


class MouthSoundShadow:
    """WakeConsumer's fail-closed admission/deduplication and telemetry guard.

    Observe before early returns; consume only at the admitted pre-VAD hook.
    A duck token is only continuity metadata, never evidence of muted media.
    Shadow scoring requires no duck and must never request one.
    """

    def __init__(self, config, sink=None):
        self.disabled = False
        self.sink = sink if sink is not None else ShadowLog()
        self.detector = None
        self._context = None
        self._seen = None
        self._observed = None
        self._blocked = True
        self._not_before = -math.inf
        self._tts_until = -math.inf
        try:
            settings = config.get('mouth_sounds', {})
            self.detector = MouthSoundDetector(settings.get('pair_window_ms', (150, 350)),
                                               settings.get('refractory_ms', 800),
                                               settings.get('onset_rms', .025))
        except Exception:
            self._fail()

    def _fail(self):
        if not self.disabled:
            self.disabled = True
            if self.detector is not None:
                self.detector.reset()
            logger.warning('[MOUTH-SHADOW] disabled: detector_error')

    def invalidate(self):
        if not self.disabled:
            self.detector.reset()
            self._not_before = max(self._not_before, time.perf_counter())

    @staticmethod
    def _snapshot(consumer):
        app = consumer._app
        return (app.app_state, getattr(app, '_ava_cmd_generation', None),
                getattr(app, 'command_mode_active', False),
                getattr(app, 'ava_command_session_active', False),
                getattr(app, '_wake_capture_admission', None),
                getattr(app, '_hands_free_capture_duck_token', None),
                consumer._hands_free_capture_duck_token,
                getattr(getattr(app, '_session_mode_manager', None), 'mode', None))

    def observe(self, consumer, frame):
        if self.disabled:
            return
        try:
            app = consumer._app
            now = time.perf_counter()
            coordinator = getattr(app, 'audio_coordinator', None)
            tts = bool(coordinator is not None and coordinator.is_speaking)
            if tts:
                self._tts_until = now + .3
            active_session = (consumer._is_toggle_cmd(app)
                              or getattr(app, 'ava_command_session_active', False) is True)
            blocked = bool(app._hotkey_recording or tts or now < self._tts_until
                           or (app.app_state == 'asleep' and not active_session))
            context = self._snapshot(consumer)
            previous = self._observed
            current = (frame.device_epoch, frame.seq, frame.t_capture)
            reason = None
            if previous is not None and current[0] != previous[0]:
                reason = 'epoch'
                self._seen = None
            elif context != self._context:
                reason = 'session'
            elif blocked != self._blocked:
                reason = 'suppression'
            elif (previous is not None and current[1] > previous[1]
                  and (current[1] != previous[1] + 1
                       or abs(current[2] - previous[2] - .1) > .02)):
                reason = 'gap'
            if reason:
                self.detector.reset()
                if self._context is not None:
                    self._not_before = max(self._not_before, now)
                self.sink.write(reason)
            self._context, self._blocked = context, blocked
            # Rewinds never lower the high-water mark or replay suppressed audio.
            if previous is None or current[0] != previous[0] or current[1] > previous[1]:
                self._observed = current
            if blocked:
                self._not_before = max(self._not_before, frame.t_capture, now)
        except Exception:
            self._fail()

    def consume(self, consumer, frame, admission):
        if self.disabled or self._blocked:
            return
        try:
            identity = (frame.device_epoch, frame.seq)
            if self._seen is not None and identity[0] == self._seen[0] and identity[1] <= self._seen[1]:
                return
            if self._seen is not None and (identity[0] != self._seen[0] or identity[1] != self._seen[1] + 1):
                self.detector.reset()  # Includes frames skipped by existing echo/admission guards.
            self._seen = identity
            boundary = self._not_before
            if admission is not None:
                boundary = max(boundary, admission[0] + admission[2] / 1000)
            boundary = max(boundary, getattr(consumer, '_hold_not_before', None) or -math.inf)
            start = frame.t_capture - frame.pcm.size / SAMPLE_RATE
            skip = max(0, min(frame.pcm.size, math.ceil((boundary - start) * SAMPLE_RATE))) if math.isfinite(boundary) else 0
            if skip == frame.pcm.size:
                self.detector.reset()
                return
            events = self.detector.feed(frame.pcm[skip:], start + skip / SAMPLE_RATE)
            for event in events:
                # Recheck after DSP: a generation/session change invalidates delivery.
                if self._context != self._snapshot(consumer) or consumer._app._hotkey_recording:
                    self.detector.reset()
                    return
                coordinator = getattr(consumer._app, 'audio_coordinator', None)
                if coordinator is not None and coordinator.is_speaking:
                    self.detector.reset()
                    return
                reason = event.pop('reason')
                self.sink.write(reason, **event, delivery_ms=round((frame.t_capture - event['ts_first']) * 1000, 3))
        except Exception:
            self._fail()
