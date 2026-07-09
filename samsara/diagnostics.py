"""Per-utterance dictation pipeline diagnostics.

Records audio stats, Whisper quality signals, per-stage timings, and a
plain-English verdict for every dictation utterance, so problems like "wrong
model configured", "mic too quiet", or "smart_correct is the slow stage" are
visible without log archaeology. See samsara/ui/diagnostics_qt.py for the
viewer.

Call sites (dictation.py hotkey/wake, samsara/streaming.py) build a
DiagRecord and pass it to record(). Nothing in this module may raise into
the caller -- a diagnostics failure must never affect dictation output or
latency.
"""

import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime

from samsara.paths import samsara_home_dir

logger = logging.getLogger(__name__)

_MAX_RECORDS = 200
_TEXT_CHAR_CAP = 200

_lock = threading.Lock()
_ring: deque = deque(maxlen=_MAX_RECORDS)


@dataclass
class DiagRecord:
    mode: str                      # "hotkey" | "wake" | "streaming" | "command"
    audio_s: float
    model_name: str
    device: str
    compute_type: str
    ts: str = field(default_factory=lambda: datetime.now().isoformat())
    t_transcribe_ms: int = -1
    t_corrections_ms: int = -1
    t_smart_ms: int = -1
    t_total_ms: int = -1
    avg_logprob: "float | None" = None
    compression_ratio: "float | None" = None
    no_speech_prob: "float | None" = None
    temperature: "float | None" = None
    n_segments: int = 0
    text: str = ""
    smart_changed: bool = False
    language: str = ""              # configured code, or "auto->{detected}"
    verdicts: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Classification -- pure function, unit-testable
# ---------------------------------------------------------------------------

def classify(rec: DiagRecord) -> list:
    """Human-readable verdicts for a record. Pure -- no I/O, no side effects.

    Multiple verdicts can apply at once; only falls back to ["OK"] when none
    of the rules below fire.
    """
    verdicts = []

    if rec.audio_s < 0.5:
        verdicts.append("Ultra-short audio — accidental hold?")

    if rec.no_speech_prob is not None and rec.no_speech_prob > 0.6:
        verdicts.append("Likely no speech — hallucination risk")

    if rec.compression_ratio is not None and rec.compression_ratio > 2.4:
        verdicts.append("High compression ratio — repetitive/hallucinated output likely")

    if rec.temperature is not None and rec.temperature > 0.0:
        verdicts.append(
            f"Fallback ladder engaged (temp {rec.temperature:.2f}) — low-confidence transcription"
        )

    if rec.avg_logprob is not None and rec.avg_logprob < -1.0:
        verdicts.append("Very low confidence")

    if rec.model_name in ("tiny", "tiny.en", "base", "base.en"):
        verdicts.append("Small model configured — accuracy limited")

    if rec.t_smart_ms > 0 and rec.t_smart_ms > rec.t_transcribe_ms:
        verdicts.append("Smart Corrections is the slowest stage")

    if rec.t_total_ms > 3000:
        verdicts.append("Slow end-to-end (>3s)")

    if not rec.text and rec.audio_s > 2:
        verdicts.append("Speech produced no output")

    return verdicts if verdicts else ["OK"]


# ---------------------------------------------------------------------------
# Whisper segment signal extraction -- shared by every transcribe call site
# ---------------------------------------------------------------------------

def segment_signals(seg_list) -> dict:
    """Worst-across-segments Whisper quality signals plus a segment count.

    Never raises -- a missing attribute on a segment object (older/newer
    faster-whisper versions) just yields None for that field instead of
    blowing up the caller.
    """
    avg_logprob = None
    compression_ratio = None
    no_speech_prob = None
    temperature = None
    n = 0
    for seg in seg_list or []:
        n += 1
        try:
            lp = getattr(seg, 'avg_logprob', None)
            if lp is not None:
                avg_logprob = lp if avg_logprob is None else min(avg_logprob, lp)
            cr = getattr(seg, 'compression_ratio', None)
            if cr is not None:
                compression_ratio = cr if compression_ratio is None else max(compression_ratio, cr)
            nsp = getattr(seg, 'no_speech_prob', None)
            if nsp is not None:
                no_speech_prob = nsp if no_speech_prob is None else max(no_speech_prob, nsp)
            temp = getattr(seg, 'temperature', None)
            if temp is not None:
                temperature = temp if temperature is None else max(temperature, temp)
        except Exception:
            continue
    return {
        'avg_logprob': avg_logprob,
        'compression_ratio': compression_ratio,
        'no_speech_prob': no_speech_prob,
        'temperature': temperature,
        'n_segments': n,
    }


# ---------------------------------------------------------------------------
# Ring buffer + optional JSONL persistence
# ---------------------------------------------------------------------------

def record(rec: DiagRecord, app=None) -> None:
    """Classify, truncate, and store a record. Never raises.

    Always appends to the in-memory ring buffer. Additionally appends one
    JSON line to ~/.samsara/diagnostics.jsonl when `app.config['diagnostics']
    ['write_jsonl']` is true (app is optional -- omit it to skip the JSONL
    gate entirely and only use the ring buffer, e.g. in tests).
    """
    try:
        rec.text = (rec.text or "")[:_TEXT_CHAR_CAP]
        rec.verdicts = classify(rec)
        with _lock:
            _ring.append(rec)
    except Exception as exc:
        logger.debug(f"[DIAG] record() failed: {exc}")
        return

    try:
        write_enabled = False
        if app is not None:
            write_enabled = bool(
                getattr(app, 'config', {}).get('diagnostics', {}).get('write_jsonl', False)
            )
        if write_enabled:
            path = samsara_home_dir() / "diagnostics.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(rec)) + "\n")
    except Exception as exc:
        logger.debug(f"[DIAG] JSONL write failed: {exc}")


def recent(n: int = 200) -> list:
    """Most-recent-last list of up to n records (newest at the end)."""
    with _lock:
        items = list(_ring)
    return items[-n:] if n < len(items) else items


def clear() -> None:
    with _lock:
        _ring.clear()
