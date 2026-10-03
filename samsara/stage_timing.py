"""Opt-in, per-utterance latency records for diagnosis (queue 290).

This module has no app, audio, Qt, or model imports. Callers create a recorder
only behind the cached ``diagnostics.stage_timing`` boolean. Transcripts are
never written; only a word count and timing metadata are retained.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
import uuid

from samsara.paths import samsara_home_dir
from samsara.runtime import thread_registry


STAGES = (
    "last_voice",
    "speech_end",
    "capture_finalised",
    "transcribe_start",
    "transcribe_end",
    "dispatch_start",
    "action_done",
    "feedback_done",
)
DELTA_STAGES = {
    "endpoint_wait_ms": ("last_voice", "speech_end"),
    "capture_finalize_ms": ("speech_end", "capture_finalised"),
    "pre_transcribe_ms": ("capture_finalised", "transcribe_start"),
    "transcribe_ms": ("transcribe_start", "transcribe_end"),
    "post_transcribe_ms": ("transcribe_end", "dispatch_start"),
    "action_ms": ("dispatch_start", "action_done"),
    "feedback_ms": ("action_done", "feedback_done"),
    "total_ms": ("speech_end", "feedback_done"),
}


def outcome_label(kind: str) -> str:
    """Bound dispatch details to the journal's fixed, non-text vocabulary."""
    if kind in {"dictate_injected", "dictate_committed"}:
        return "typed"
    if kind in {"dictate_staged", "dictation_staged_chunk"}:
        return "buffered"
    if kind in {"empty"}:
        return "empty"
    if kind in {"command_miss"}:
        return "miss"
    if "blocked" in kind or "refused" in kind or "suppressed" in kind:
        return "gated"
    if "failed" in kind or "unavailable" in kind:
        return "error"
    if kind in {"abort", "stopped", "scratch_success", "dictate_draft_cleared"}:
        return "discarded"
    return "command"

class _NoopTiming:
    """A zero-output recorder so instrumentation never forks app behavior."""

    handed_off = False
    audio_s: float | None = None
    words: int | None = None
    model: str | None = None
    beam: int | None = None
    outcome: str | None = None
    segment_index: int | None = None
    last_voice_source: str | None = None
    self_hear: bool = False

    def mark(self, stage: str, **fields) -> None:
        return None

    def mark_at(self, stage: str, monotonic_ns: int, **fields) -> None:
        return None

    @property
    def endpoint_wait_ms(self) -> float | None:
        return None

    def feedback_started(self) -> None:
        return None

    def feedback_finished(self) -> None:
        return None

    def complete(self, *, outcome: str | None = None) -> None:
        return None

    def handoff(self) -> None:
        return None


_NOOP_TIMING = _NoopTiming()
_CURRENT: ContextVar[StageTiming | _NoopTiming] = ContextVar("stage_timing", default=_NOOP_TIMING)
_APPEND_LOCK = threading.Lock()
_TIMING_ENABLED = False


def current_stage_timing() -> "StageTiming | _NoopTiming":
    return _CURRENT.get()


def set_current_stage_timing(record: "StageTiming"):
    return _CURRENT.set(record)


def reset_current_stage_timing(token) -> None:
    _CURRENT.reset(token)


@contextmanager
def bind_stage_timing(record: "StageTiming | _NoopTiming"):
    token = set_current_stage_timing(record)
    try:
        yield
    finally:
        reset_current_stage_timing(token)


def mark_current(stage: str, **fields) -> None:
    current_stage_timing().mark(stage, **fields)


class StageTiming:
    """Accumulate monotonic stage marks and append one JSONL record."""

    def __init__(self, route: str, mode: str, *, home_dir: str | Path | None = None):
        global _TIMING_ENABLED
        _TIMING_ENABLED = True
        home = Path(home_dir) if home_dir is not None else samsara_home_dir()
        self.path = home / "perf" / "stage_timing.jsonl"
        self.utterance_id = uuid.uuid4().hex
        self.route = str(route)
        self.mode = str(mode)
        self.created_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self.audio_s: float | None = None
        self.words: int | None = None
        self.model: str | None = None
        self.beam: int | None = None
        self.outcome: str | None = None
        self.segment_index: int | None = None
        self.last_voice_source: str | None = None
        self.self_hear = False
        self._monotonic_ns: dict[str, int | None] = dict.fromkeys(STAGES)
        self._utc: dict[str, str | None] = dict.fromkeys(STAGES)
        self._pending_feedback = 0
        self._closed = False
        self._written = False
        self._handed_off = False
        self._lock = threading.Lock()

    @classmethod
    def create(
        cls,
        route: str,
        mode: str,
        *,
        enabled: bool,
        home_dir: str | Path | None = None,
    ) -> "StageTiming | _NoopTiming":
        if not enabled:
            return _NOOP_TIMING
        return cls(route, mode, home_dir=home_dir)

    @property
    def handed_off(self) -> bool:
        return self._handed_off

    def handoff(self) -> None:
        self._handed_off = True

    def mark(self, stage: str, **fields) -> None:
        self.mark_at(stage, time.perf_counter_ns(), **fields)

    def mark_at(self, stage: str, monotonic_ns: int, **fields) -> None:
        """Record a stage using a timestamp captured earlier on this clock."""
        if stage not in self._monotonic_ns:
            raise ValueError(f"unknown timing stage: {stage}")
        mark_ns = int(monotonic_ns)
        utc_now = datetime.now(timezone.utc)
        offset_s = (time.perf_counter_ns() - mark_ns) / 1_000_000_000
        mark_utc = (utc_now - timedelta(seconds=offset_s)).isoformat(timespec="milliseconds")
        with self._lock:
            if self._monotonic_ns[stage] is None:
                self._monotonic_ns[stage] = mark_ns
                self._utc[stage] = mark_utc
            for key in ("audio_s", "words", "model", "beam", "outcome",
                        "segment_index", "last_voice_source", "self_hear"):
                if key in fields and fields[key] is not None:
                    setattr(self, key, fields[key])

    @property
    def endpoint_wait_ms(self) -> float | None:
        last_voice_ns = self._monotonic_ns["last_voice"]
        speech_end_ns = self._monotonic_ns["speech_end"]
        if last_voice_ns is None or speech_end_ns is None:
            return None
        return round((speech_end_ns - last_voice_ns) / 1_000_000, 3)

    def feedback_started(self) -> None:
        with self._lock:
            self._pending_feedback += 1

    def feedback_finished(self) -> None:
        ready = False
        with self._lock:
            self._pending_feedback = max(0, self._pending_feedback - 1)
            ready = self._prepare_write_locked()
        if ready:
            # This can run on PortAudio's callback thread. Keep disk I/O off it.
            thread_registry.spawn("stage-timing-write", self._append_record, daemon=True)

    def complete(self, *, outcome: str | None = None) -> None:
        with self._lock:
            if outcome is not None:
                self.outcome = outcome
            self._closed = True
            ready = self._prepare_write_locked()
        if ready:
            self._append_record()

    def _prepare_write_locked(self) -> bool:
        if not self._closed or self._pending_feedback or self._written:
            return False
        if self._monotonic_ns["feedback_done"] is None:
            self._monotonic_ns["feedback_done"] = time.perf_counter_ns()
            self._utc["feedback_done"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self._written = True
        return True

    def _record(self) -> dict:
        marks = self._monotonic_ns
        deltas = {}
        for label, (start, end) in DELTA_STAGES.items():
            a, b = marks[start], marks[end]
            deltas[label] = round((b - a) / 1_000_000, 3) if a is not None and b is not None else None
        return {
            "utterance_id": self.utterance_id,
            "created_at": self.created_at,
            "route": self.route,
            "mode": self.mode,
            "audio_s": self.audio_s,
            "words": self.words,
            "model": self.model,
            "beam": self.beam,
            "outcome": self.outcome,
            "segment_index": self.segment_index,
            "last_voice_source": self.last_voice_source,
            "self_hear": self.self_hear,
            "timestamps_ns": dict(marks),
            "timestamps_utc": dict(self._utc),
            "deltas_ms": deltas,
        }

    def _append_record(self) -> None:
        try:
            line = json.dumps(self._record(), ensure_ascii=False, separators=(",", ":"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _APPEND_LOCK, self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
        except Exception:
            # Diagnostics must never change capture, dispatch, or feedback.
            return
