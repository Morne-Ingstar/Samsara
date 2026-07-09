"""Local benchmark sample collection for the personal WER harness.

Opt-in (config `benchmark.collect_samples`, default off). When enabled, the
hotkey dictation call site in dictation.py saves the raw captured audio as
16kHz mono WAV under ~/.samsara/benchmark/audio/ and appends one row to
~/.samsara/benchmark/samples.jsonl -- {id, wav, raw_transcript, final_text,
model, duration_s, ts, gold}. No audio ever leaves the machine.

append_sample() must never raise into its caller -- a collection failure
must never affect dictation. Gold transcripts are reviewed and filled in by
samsara/ui/benchmark_review_qt.py (null until then); tools/benchmark_eval.py
reads only gold-confirmed rows for offline WER evaluation.
"""

import json
import time
import uuid
import wave
from pathlib import Path

import numpy as np

from samsara.log import get_logger
from samsara.paths import samsara_home_dir

logger = get_logger(__name__)

_DEFAULT_MAX_SAMPLES = 200

# Fires at most once per process -- "silently stop, INFO once" per the cap
# requirement, not once per append_sample call.
_cap_notice_shown = False


def _benchmark_dir() -> Path:
    return samsara_home_dir() / "benchmark"


def audio_dir() -> Path:
    return _benchmark_dir() / "audio"


def _samples_path() -> Path:
    return _benchmark_dir() / "samples.jsonl"


def audio_path(row: dict) -> Path:
    """Full path to a sample row's WAV file."""
    return audio_dir() / row.get("wav", "")


def _read_rows() -> list:
    path = _samples_path()
    if not path.exists():
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _write_rows(rows: list) -> None:
    path = _samples_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _to_int16_pcm(audio: np.ndarray) -> np.ndarray:
    pcm = np.asarray(audio)
    if np.issubdtype(pcm.dtype, np.floating):
        return np.clip(pcm * 32767.0, -32768, 32767).astype(np.int16)
    return pcm.astype(np.int16)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def append_sample(app, audio: np.ndarray, sample_rate: int, raw_transcript: str,
                   final_text: str, model: str) -> bool:
    """Save one hotkey dictation utterance as a benchmark sample.

    Gated on `app.config['benchmark']['collect_samples']`. Writes a 16kHz
    mono WAV plus a samples.jsonl row. Never raises -- any failure (disk,
    config shape, bad audio) is logged at DEBUG and swallowed. Returns True
    only when a sample was actually written.
    """
    global _cap_notice_shown
    try:
        cfg = (getattr(app, "config", {}) or {}).get("benchmark", {}) or {}
        if not cfg.get("collect_samples", False):
            return False

        max_samples = cfg.get("max_samples", _DEFAULT_MAX_SAMPLES)
        rows = _read_rows()
        if len(rows) >= max_samples:
            if not _cap_notice_shown:
                _cap_notice_shown = True
                logger.info(
                    f"[BENCH] Sample cap reached ({max_samples}) -- collection stopped"
                )
            return False

        pcm = _to_int16_pcm(audio)
        if pcm.size == 0:
            return False

        sample_id = uuid.uuid4().hex[:12]
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        wav_name = f"{ts}_{sample_id}.wav"

        out_dir = audio_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_dir / wav_name), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm.tobytes())

        duration_s = len(pcm) / sample_rate if sample_rate else 0.0

        rows.append({
            "id": sample_id,
            "wav": wav_name,
            "raw_transcript": raw_transcript,
            "final_text": final_text,
            "model": model,
            "duration_s": duration_s,
            "ts": ts,
            "gold": None,
        })
        _write_rows(rows)
        return True
    except Exception as exc:
        logger.debug(f"[BENCH] append_sample failed: {exc}")
        return False


def list_samples() -> list:
    """All collected samples (list of dict rows), oldest first."""
    try:
        return _read_rows()
    except Exception as exc:
        logger.debug(f"[BENCH] list_samples failed: {exc}")
        return []


def set_gold(sample_id: str, text: "str | None") -> bool:
    """Set (or clear, with text=None) the gold transcript for a sample.

    Returns False if no row with that id exists.
    """
    try:
        rows = _read_rows()
        found = False
        for row in rows:
            if row.get("id") == sample_id:
                row["gold"] = text
                found = True
                break
        if not found:
            return False
        _write_rows(rows)
        return True
    except Exception as exc:
        logger.debug(f"[BENCH] set_gold failed: {exc}")
        return False


def discard_sample(sample_id: str) -> bool:
    """Remove a sample row and its WAV file. Returns False if not found."""
    try:
        rows = _read_rows()
        removed = next((r for r in rows if r.get("id") == sample_id), None)
        if removed is None:
            return False
        _write_rows([r for r in rows if r.get("id") != sample_id])
        try:
            audio_path(removed).unlink(missing_ok=True)
        except Exception as exc:
            logger.debug(f"[BENCH] discard_sample wav unlink failed: {exc}")
        return True
    except Exception as exc:
        logger.debug(f"[BENCH] discard_sample failed: {exc}")
        return False


def stats() -> dict:
    """Counts for the review UI's progress label and eval script's guard."""
    rows = _read_rows()
    gold_count = sum(1 for r in rows if r.get("gold"))
    return {
        "total": len(rows),
        "gold_confirmed": gold_count,
        "pending_review": len(rows) - gold_count,
    }
