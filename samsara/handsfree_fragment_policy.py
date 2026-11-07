"""Per-utterance evidence for the unenabled hands-free fragment policy.

Speech support and decoder quality cannot establish who intended a word.
1174b's generated near/background pairs did not clear the zero-false-reject
gate. No cutoff, adaptive baseline, rejection or warning suppression is enabled.
This module never retains audio/text, loads a model or starts background work.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite

import numpy as np


def _number(value):
    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, (bool, np.bool_)):
        return float(value) if isfinite(value) else None
    return None


def _generation(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class SegmentQuality:
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    temperature: float | None = None
    compression_ratio: float | None = None


@dataclass(frozen=True)
class SpeechSupport:
    duration_s: float | None = None
    known: bool = False
    # Same existing p>.1/.3/.5 totals/runs, clipped at capture end.
    totals_and_runs_s: tuple | None = None
    speech_duty: float | None = None
    peak_probability: float | None = None
    active_rms: float | None = None
    verdict: str = 'unknown'


@dataclass(frozen=True)
class CaptureBinding:
    manager_id: int | None
    session_epoch: int | None
    execution_generation: int | None
    mic_epoch: int | None

    def matches(self, other):
        pairs = tuple(zip(self.__dict__.values(), other.__dict__.values()))
        if any(a is not None and b is not None and a != b for a, b in pairs):
            return False
        return True if all(a is not None and b is not None for a, b in pairs) else None


def _binding(app):
    manager = getattr(app, '_session_mode_manager', None)
    engine = getattr(app, '_ace_engine', None)
    ring = getattr(engine, '_ring', None)
    return CaptureBinding(
        id(manager) if manager is not None else None,
        _generation(getattr(manager, '_session_epoch', None)),
        _generation(getattr(app, '_ava_cmd_generation', None)),
        _generation(getattr(ring, 'device_epoch', None)),
    )


@dataclass(frozen=True)
class FragmentEvidence:
    capture_token: object
    binding: CaptureBinding
    fresh: bool | None
    speech: SpeechSupport
    segments: tuple[SegmentQuality, ...] = ()


def _support(presence, pcm, sample_rate, verdict):
    rate = _number(sample_rate)
    duration = len(pcm) / rate if rate is not None and rate > 0 else None
    if not presence.known or verdict not in ('pass', 'reject'):
        return SpeechSupport(duration_s=duration, verdict=verdict)
    stats, peak = presence.statistics()
    active_rms = None
    # Map the existing 16 kHz VAD windows onto the original capture. No
    # resampling/inference and no energy baseline. Unknown alignment stays unknown.
    if (rate and presence.probabilities and presence.samples > 0
            and abs(presence.samples / 16000 - duration) <= 1 / 16000):
        audio = np.asarray(pcm)
        if audio.ndim == 1 and np.isfinite(audio).all():
            energy, count = 0.0, 0
            for frame, probability in enumerate(presence.probabilities):
                if probability > .1:
                    start = round(frame * 512 * rate / 16000)
                    end = min(len(audio), round((frame + 1) * 512 * rate / 16000))
                    part = audio[start:end].astype(np.float64)
                    energy += float(np.dot(part, part))
                    count += len(part)
            if count:
                active_rms = (energy / count) ** .5
    return SpeechSupport(duration, True, stats,
                         stats[0][0] / duration if duration else None,
                         peak, active_rms, verdict)


class CaptureEvidence:
    """Local to one FIFO take; a sink for its existing final VAD decision.

    The caller holds this object on its worker stack, never on the app or a
    global/latest slot. Binding is sampled before measurement and checked
    after decoding. It describes worker-time provenance, not capture onset;
    entry/late-completion acceptance remains with the existing lifecycle owner.
    """

    def __init__(self, app):
        self._token = object()
        self._binding = _binding(app)
        self._speech = SpeechSupport()
        self._seen = False

    def __call__(self, presence, pcm, sample_rate, verdict):
        if self._seen:
            # An accidentally reused collector must not stand in for this take.
            self._speech = SpeechSupport()
            return
        self._seen = True
        self._speech = _support(presence, pcm, sample_rate, verdict)

    def attach(self, signals, segments, app):
        """Preserve original segment fields, including missing/nonfinite values."""
        from samsara.session_modes import UtteranceSignals
        if not isinstance(signals, UtteranceSignals):
            return signals  # Legacy/adaptor stand-ins keep their existing behavior.
        try:
            quality = tuple(SegmentQuality(*(_number(getattr(s, field, None)) for field in (
                'avg_logprob', 'no_speech_prob', 'temperature', 'compression_ratio')))
                for s in segments)
            evidence = FragmentEvidence(self._token, self._binding,
                                        self._binding.matches(_binding(app)), self._speech, quality)
            return replace(signals, fragment_evidence=evidence)
        except Exception:
            return signals  # Numeric observation must never interrupt dispatch.
