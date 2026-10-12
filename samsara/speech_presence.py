"""Whole-buffer speech evidence; never trim PCM or judge decoded words.

Speech needs a 64 ms run above 0.1 reaching past press grace, or a frame
above 0.5 anywhere. The grace boundary follows retained capture timestamps.
Uncertain sounds and detector failures retain normal decoding and answer gates.
The detector owns a CPU session and lock, independent of the wake singleton.
"""
from dataclasses import dataclass
import logging
from math import ceil, gcd, isfinite
from threading import RLock
from time import monotonic, perf_counter, sleep

import numpy as np

from samsara.constants import HEAD_GRACE_CLICK_PAD_MS


PROBABILITY = 0.1
THRESHOLDS = (0.1, 0.3, 0.5)
MIN_RUN_S = 2 * 512 / 16000
CONFIG_KEY = 'dictation.speech_presence_gate'


@dataclass(frozen=True)
class Presence:
    known: bool
    total_s: float = 0.0
    longest_s: float = 0.0
    max_probability: float = 0.0
    frames: int = 0
    # Frame probabilities only: no PCM, words or per-owner recording retained.
    probabilities: tuple = ()
    samples: int = 0

    def statistics(self, head_grace_ms=0.0):
        """Clip frame widths at the evidence boundary and actual capture end."""
        if not self.probabilities:
            return ((self.total_s, self.longest_s), (0.0, 0.0), (0.0, 0.0)), self.max_probability
        start = max(0, ceil(head_grace_ms * 16))
        totals = [0, 0, 0]
        longest = [0, 0, 0]
        runs = [0, 0, 0]
        peak = 0.0
        for i, score in enumerate(self.probabilities):
            width = max(0, min(self.samples, (i + 1) * 512) - max(start, i * 512))
            if width:
                peak = max(peak, score)
            for j, threshold in enumerate(THRESHOLDS):
                if width and score > threshold:
                    totals[j] += width
                    runs[j] += width
                    longest[j] = max(longest[j], runs[j])
                else:
                    runs[j] = 0
        return tuple((total / 16000, run / 16000) for total, run in zip(totals, longest)), peak

    def passes(self, head_grace_ms=0.0):
        if not self.known:
            return False
        # Strong onset speech remains evidence even inside grace. Weak runs
        # crossing the boundary count in full, preserving a quiet short word.
        if self.max_probability > 0.5:
            return True
        if not self.probabilities:
            return head_grace_ms == 0 and self.longest_s >= MIN_RUN_S
        boundary = max(0, ceil(head_grace_ms * 16))
        run = 0
        for i, score in enumerate(self.probabilities):
            end = min(self.samples, (i + 1) * 512)
            run = run + end - i * 512 if score > PROBABILITY else 0
            if end > boundary and run >= MIN_RUN_S * 16000:
                return True
        return False


@dataclass(frozen=True)
class EarlyEvidence:
    capture_s: float
    head_grace_ms: float


@dataclass
class HoldEvidence:
    """One hold's timing metadata, shared with its accumulator and worker.

    The first kept block supplies the actual pre-roll, including ring phase,
    rewind suppression and strict boundaries. Never infer it from PCM length.
    """
    requested_at: float
    earcon_end_at: float = None
    first_kept_block_start: float = None

    def observe(self, start):
        if self.first_kept_block_start is None:
            self.first_kept_block_start = start

    def offsets(self):
        if self.first_kept_block_start is None:
            raise ValueError('capture timestamp unavailable')
        press = (self.requested_at - self.first_kept_block_start) * 1000
        end = max(self.requested_at, self.earcon_end_at or self.requested_at)
        grace = max(0.0, (end - self.first_kept_block_start) * 1000 + HEAD_GRACE_CLICK_PAD_MS)
        earcon_end = ((self.earcon_end_at - self.first_kept_block_start) * 1000
                      if self.earcon_end_at is not None else -1.0)
        if not all(isfinite(v) for v in (press, grace, earcon_end)):
            raise ValueError('invalid capture timestamp')
        return grace, press, earcon_end


def begin_hold(app):
    app._speech_presence_capture = HoldEvidence(perf_counter())


def capture_context(app):
    context = getattr(app, '_speech_presence_capture', None)
    return context if isinstance(context, HoldEvidence) else None


def note_hold_earcon(app, sound_type, sound):
    """Include delayed Command/Ava hold start sounds after their debounce.

    Keep the legacy batch gate's duration bookkeeping unchanged. Session-arm
    sounds and later unrelated feedback do not belong to a held capture.
    """
    if sound_type != 'start' or not getattr(app, '_hotkey_recording', False):
        return
    context = capture_context(app)
    if context is not None:
        context.earcon_end_at = perf_counter() + len(sound) / app._sound_stream_sr


def play_hold_start(app, play_earcon):
    """Keep the existing start sound/wait and mark when it was queued.

    Taking the time after play_sound returns includes output setup/duck delay.
    The shared click pad also supplies the speaker-to-mic decay tail.
    """
    app._last_recording_earcon_ms = 0.0
    context = capture_context(app)
    if play_earcon:
        app.play_sound('start', use_winsound=True)
        queued_at = perf_counter()
        sound = app._sound_cache.get('start')
        if (sound is not None and app.config.get('audio_feedback', True)
                and app.config.get('sound_volume', 0.5) > 0):
            app._last_recording_earcon_ms = len(sound) / app._sound_stream_sr * 1000
            if context is not None:
                context.earcon_end_at = queued_at + app._last_recording_earcon_ms / 1000
        sleep(0.15)


def finish_streaming_if_empty(worker, audio):
    """Keep final feedback at its existing owner, including absent PCM."""
    app = worker._session.app
    absent = audio is None or audio.size == 0
    if absent:
        skip_decode([] if audio is None else audio, app, path='streaming',
                    state=worker, skip_reason='not_decoded')
        worker._session.on_final(None)
    elif skip_decode(audio, app, path='streaming', state=worker):
        worker._session.on_final(None, duration_s=len(audio) / app.model_rate)
    else:
        return False
    return True


def reject_wake(app, audio, timing):
    if not skip_decode(audio, app, path='wake'):
        return False
    if timing is not None:
        timing.mark('transcribe_end', outcome='empty')
    return True


class SpeechPresence:
    """Serialize loading/inference; copy input because Silero mutates context."""

    def __init__(self):
        self._lock = RLock()
        self._model = None
        self._retry_at = 0.0
        self._load_error_logged = False

    def _load_model(self):
        from pathlib import Path
        from faster_whisper.utils import get_assets_path
        from faster_whisper.vad import SileroVADModel
        return SileroVADModel(str(Path(get_assets_path()) / 'silero_vad_v6.onnx'))

    def measure(self, pcm, sample_rate):
        try:
            rate = int(sample_rate)
            audio = np.array(pcm, dtype=np.float32, copy=True)
            if rate <= 0 or audio.ndim != 1 or not np.isfinite(audio).all():
                return Presence(False)
            if not audio.size:
                return Presence(True)
            if rate != 16000:
                from scipy.signal import resample_poly
                divisor = gcd(rate, 16000)
                audio = resample_poly(audio, 16000 // divisor, rate // divisor).astype(np.float32)
            samples = audio.size
            audio = np.pad(audio, (0, (-samples) % 512))
            with self._lock:
                if self._model is None:
                    if monotonic() < self._retry_at:
                        return Presence(False)
                    try:
                        self._model = self._load_model()
                    except Exception as exc:
                        self._retry_at = monotonic() + 60.0
                        if not self._load_error_logged:
                            logging.getLogger('Samsara').warning(
                                '[GATE] speech_presence_unavailable exception_type=%s', type(exc).__name__)
                            self._load_error_logged = True
                        return Presence(False)
                scores = np.asarray(self._model(audio), dtype=np.float32).reshape(-1)
            if (scores.size != audio.size // 512 or not np.isfinite(scores).all()
                    or np.any(scores < 0) or np.any(scores > 1)):
                return Presence(False)
            total = longest = run = 0
            for i, score in enumerate(scores):
                width = min(512, samples - i * 512)
                if score > PROBABILITY:
                    total += width
                    run += width
                    longest = max(longest, run)
                else:
                    run = 0
            return Presence(True, total / 16000, longest / 16000,
                            float(np.max(scores)), scores.size, tuple(map(float, scores)), samples)
        except Exception:
            return Presence(False)


_presence = SpeechPresence()


def skip_decode(pcm, sample_rate, config=None, *, final=True, path='capture', state=None,
                existing_gate=None, capture=None, skip_reason=None):
    """Measure admitted finals afresh; never rescan an existing batch decision."""
    result = Presence(False)
    duration_s, grace_ms = -1.0, 0.0
    press_ms = earcon_end_ms = -1.0
    early = None
    observed_early = None
    verdict = 'unknown'
    try:
        from samsara.config_defaults import DEFAULTS
        app = None
        if not isinstance(sample_rate, (int, float)):
            app = sample_rate
            sample_rate = getattr(app, 'model_rate')
            config = getattr(app, 'config', None)
        duration_s = len(pcm) / sample_rate
        candidate = getattr(state, '_speech_presence_seen', None) if state is not None else None
        observed_early = candidate if isinstance(candidate, EarlyEvidence) else None
        disabled = (config or {}).get('dictation', {}).get(
            'speech_presence_gate', DEFAULTS[CONFIG_KEY]) is False
        minimum = {'batch': .51, 'continuous': .51, 'handsfree': .3, 'ava_session': .3}.get(path, 0)
        if skip_reason is None:
            if duration_s < minimum or not len(pcm):
                skip_reason = 'not_decoded'
            elif disabled:
                skip_reason = 'switch_off'
            elif existing_gate is not None and (getattr(existing_gate, 'path', None) != 'loud' or existing_gate):
                skip_reason = 'batch_decided'
        if skip_reason is not None:
            verdict = 'skipped'
            return False
        if path in ('streaming', 'batch'):
            if capture is None:
                capture = (getattr(state, '_speech_presence_capture', None)
                           if state is not None else None)
            if capture is None and app is not None:
                capture = capture_context(app)
            if capture is not None:
                grace_ms, press_ms, earcon_end_ms = capture.offsets()
        if (isinstance(candidate, EarlyEvidence) and candidate.head_grace_ms == grace_ms
                and duration_s >= candidate.capture_s):
            early = candidate
        if final or early is None:
            result = _presence.measure(pcm, sample_rate)
        # A cached partial never binds a final, even if final inference fails.
        passed = (early is not None and not final) or result.passes(grace_ms)
        verdict = 'pass' if passed else 'unknown' if not result.known else 'reject'
        if not final and state is not None and result.passes(grace_ms) and early is None:
            state._speech_presence_seen = EarlyEvidence(duration_s, grace_ms)
        return verdict == 'reject'
    except Exception:
        return False
    finally:
        if final:
            _log_final(path, duration_s, result, grace_ms, verdict, observed_early,
                       skip_reason, press_ms, earcon_end_ms)


def _log_final(path, duration_s, result, grace_ms, verdict, early,
               skip_reason=None, press_ms=-1.0, earcon_end_ms=-1.0):
    """One grep-friendly final line; statistics always describe this capture."""
    try:
        if verdict == 'skipped':
            logging.getLogger('Samsara').info(
                '[GATE] speech_presence path=%s duration_s=%.3f verdict=skipped reason=%s '
                'cached=%d cached_at_s=%.3f', path, duration_s, skip_reason,
                early is not None, early.capture_s if early is not None else -1)
            return
        fields = []
        for prefix, grace in (('', 0.0), ('grace_', grace_ms)):
            stats, peak = result.statistics(grace)
            fields.append(f'{prefix}peak={peak if result.known else -1:.6f}')
            for threshold, (total, longest) in zip(('01', '03', '05'), stats):
                fields.extend((f'{prefix}total_p{threshold}_s={total if result.known else -1:.3f}',
                               f'{prefix}run_p{threshold}_s={longest if result.known else -1:.3f}'))
        reason = (' reason=no_speech_in_audio'
                  if verdict == 'reject' and path != 'handsfree' else '')
        logging.getLogger('Samsara').info(
            '[GATE] speech_presence path=%s duration_s=%.3f frames=%d %s head_grace_ms=%.3f '
            'press_offset_ms=%.3f earcon_end_offset_ms=%.3f verdict=%s cached=%d cached_at_s=%.3f%s',
            path, duration_s, result.frames, ' '.join(fields), grace_ms,
            press_ms, earcon_end_ms, verdict, early is not None, early.capture_s if early is not None else -1, reason)
    except Exception:
        pass


def show_empty_capture(app, duration_s, mode='hold'):
    """Reuse existing feedback/history seams without touching an output target."""
    try:
        app._show_outcome_chip('No speech', 'info')
    except Exception:
        pass
    try:
        if duration_s > .5:
            app._log_history(raw_text='', display_text='(no speech detected)',
                             duration_ms=int(duration_s * 1000), mode=mode,
                             status='empty', entry_type='failed')
    except Exception:
        pass
