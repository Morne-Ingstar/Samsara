"""Whole-buffer speech evidence; never trim PCM or judge decoded words.

Only captures with no Silero frame above 0.1 are rejected. Uncertain sounds
and detector failures retain normal decoding and all existing answer gates.
The detector owns a CPU session and lock, independent of the wake singleton.
"""
from dataclasses import dataclass
import logging
from math import gcd
from threading import RLock
from time import monotonic

import numpy as np


PROBABILITY = 0.1
CONFIG_KEY = 'dictation.speech_presence_gate'


@dataclass(frozen=True)
class Presence:
    known: bool
    total_s: float = 0.0
    longest_s: float = 0.0
    max_probability: float = 0.0
    frames: int = 0


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
                            float(np.max(scores)), scores.size)
        except Exception:
            return Presence(False)


_presence = SpeechPresence()


def skip_decode(pcm, sample_rate, config=None, *, final=True, path='capture', state=None,
                existing_gate=None):
    """Fail open, including app lookups. A worker caches speech for its hold.

    Pass the app itself as sample_rate at call sites so missing/raising app
    attributes are caught here. Existing batch scans retain their decision;
    only the old loud bypass needs a second detector. Cached final statistics
    describe the snapshot that established speech, not an unscored tail.
    """
    try:
        from samsara.config_defaults import DEFAULTS
        if not isinstance(sample_rate, (int, float)):
            app = sample_rate
            sample_rate = getattr(app, 'model_rate')
            config = getattr(app, 'config', None)
        duration_s = len(pcm) / sample_rate
        result = Presence(False)
        cached = False
        if (config or {}).get('dictation', {}).get(
                'speech_presence_gate', DEFAULTS[CONFIG_KEY]) is False:
            verdict = 'disabled'
        elif existing_gate is not None and getattr(existing_gate, 'path', None) != 'loud':
            verdict = 'batch_reject' if existing_gate else 'batch_pass'
        else:
            result = getattr(state, '_speech_presence_seen', None) if state is not None else None
            cached = isinstance(result, Presence) and result.known and result.total_s > 0
            if not cached:
                result = _presence.measure(pcm, sample_rate)
                if state is not None and result.known and result.total_s > 0:
                    state._speech_presence_seen = result
            verdict = 'unknown' if not result.known else 'pass' if result.total_s > 0 else 'reject'
        if final:
            # Hands-free's existing dropped-utterance log owns its reason and
            # releases the cancel window; don't repeat that reason here.
            reason = ' reason=no_speech_in_audio' if verdict in ('reject', 'batch_reject') and path != 'handsfree' else ''
            logging.getLogger('Samsara').info(
                '[GATE] speech_presence path=%s duration_s=%.3f frames=%d peak=%.2f verdict=%s cached=%d%s',
                path, duration_s, result.frames, result.max_probability if result.known else -1,
                verdict, cached, reason)
        return verdict == 'reject'
    except Exception:
        if final:
            try:
                logging.getLogger('Samsara').info(
                    '[GATE] speech_presence path=%s duration_s=-1 frames=0 peak=-1.00 verdict=unknown cached=0', path)
            except Exception:
                pass
        return False


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
