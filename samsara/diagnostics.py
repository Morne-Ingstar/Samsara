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

# One-shot completion hooks -- the canonical, thread-safe tap point for
# "tell me about the NEXT dictation completion" consumers (currently just
# the Stress Test Wizard: samsara/ui/stress_wizard_qt.py). record() is the
# single choke point every dictation path (hotkey/wake/streaming/command,
# success/empty/gated) already funnels through -- the same call sites that
# write history -- so hooking here means no separate capture mechanism
# (clipboard/paste watching, polling a Qt widget) is needed at all.
#
# record() runs on whatever thread the caller is on (the hotkey/wake worker
# thread, not the Qt thread) -- callbacks registered here fire on THAT
# thread too. A Qt-based consumer MUST marshal back via qt_runtime.post()
# inside its own callback; this module has no Qt dependency and does none
# of that for you.
_hooks_lock = threading.Lock()
_one_shot_hooks: list = []


def add_one_shot_hook(callback) -> None:
    """Register callback(rec: DiagRecord) to fire on the NEXT record() call,
    then automatically deregister. Never raises into record()'s caller if
    callback itself raises -- see _fire_one_shot_hooks."""
    with _hooks_lock:
        _one_shot_hooks.append(callback)


def remove_one_shot_hook(callback) -> None:
    """Explicit unhook -- idempotent (no error if callback already fired or
    was never registered). Callers MUST call this on cancel/skip/close so a
    closed window never leaks a callback into a future record() call."""
    with _hooks_lock:
        try:
            _one_shot_hooks.remove(callback)
        except ValueError:
            pass


def _fire_one_shot_hooks(rec) -> None:
    with _hooks_lock:
        hooks, _one_shot_hooks[:] = list(_one_shot_hooks), []
    for cb in hooks:
        try:
            cb(rec)
        except Exception as exc:
            logger.debug(f"[DIAG] one-shot hook failed: {exc}")


@dataclass
class DiagRecord:
    mode: str                      # "hotkey" | "wake" | "streaming" | "command" | "hands_free"
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
    # outcome/path: FM3 (blank-transcription) diagnostics. Defaulted so
    # every pre-existing call site (a normal, non-empty result) still
    # validates unchanged -- only the new empty/gated/low_confidence call
    # sites in dictation.py pass non-default values.
    # "low_confidence": every segment failed the quality gate but the
    # never-silently-empty floor (_keep_low_confidence_long_chunk) delivered
    # the text anyway rather than discarding it -- see
    # dictation._apply_segment_quality_gates.
    # "suspected_loss": a long recording with sustained speech-RMS coverage
    # delivered implausibly little text (chars/sec sanity check) -- Whisper
    # itself likely dropped mid-decode content rather than any post-decode
    # gate rejecting it. See dictation._suspected_silent_data_loss and the
    # 2026-07-16 incident it was added for.
    outcome: str = "ok"              # "ok" | "empty" | "gated" | "low_confidence" | "suspected_loss"
    path: str = ""                   # "long" | "short" | "" (n/a, e.g. gated)
    lane: str = ""                   # hands-free lane: "dictate" | "command" | "ava"


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

    # FM3 (blank-transcription) outcomes are more specific than the
    # generic "no output" rule below, and the CRITICAL disambiguator is
    # segment_count (n_segments): zero segments means the model returned
    # nothing at all, non-zero means segments came back but were
    # suppressed/blank (hallucination guard, native no_speech/log_prob
    # thresholds) -- never conflate the two.
    if rec.outcome == "gated":
        verdicts.append("Gated upstream — no contiguous speech detected before transcription")
    elif rec.outcome == "empty" and rec.n_segments == 0:
        verdicts.append("Empty result — model returned zero segments")
    elif rec.outcome == "empty":
        verdicts.append("Empty result — segments present but text suppressed/blank")
    elif rec.outcome == "low_confidence":
        verdicts.append(
            "Low-confidence delivery — every segment failed quality gates "
            "but text was kept instead of discarded"
        )
    elif rec.outcome == "suspected_loss":
        verdicts.append(
            "Suspected silent data loss — long recording with speech "
            "present delivered implausibly little text"
        )

    if not rec.text and rec.audio_s > 2 and rec.outcome == "ok":
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
    if _mic_lab_observer is not None:
        _mic_lab_record(rec, app)
    try:
        rec.text = (rec.text or "")[:_TEXT_CHAR_CAP]
        rec.verdicts = classify(rec)
        with _lock:
            _ring.append(rec)
    except Exception as exc:
        logger.debug(f"[DIAG] record() failed: {exc}")
        return

    _fire_one_shot_hooks(rec)

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


# 1267: this sink is absent, including its queue/thread, until explicitly set.
# All producers below run on decode/completion threads, never PortAudio's
# callback or the ring drain. Only the sink thread invokes consumer code.
_mic_lab_observer = None
_mic_lab_registration_lock = threading.Lock()
_mic_lab_pending = threading.local()


class _MicLabSink:
    def __init__(self, callback):
        from queue import SimpleQueue
        self.callback = callback
        self.queue = SimpleQueue()
        self.warned = False
        self.thread = threading.Thread(target=self._run, name='mic-lab-observer', daemon=True)
        self.thread.start()

    def warning(self):
        if not self.warned:
            self.warned = True
            try:
                logger.warning('[MIC_LAB] observation_failed=1')
            except Exception:
                pass

    def _run(self):
        while True:
            item = self.queue.get()
            try:
                if item is None:
                    return
                if isinstance(item, threading.Event):
                    item.set()
                else:
                    self.callback(item)
            except Exception:
                self.warning()  # Never log audio, text, params or exception bodies.


def set_mic_lab_observer(callback) -> None:
    """Set callback(MicLabHold dict) or None. Queued callbacks finish on detach.

    Call flush_mic_lab_observer before reading a sink's results. Do not register
    from a capture callback. Replacing/detaching a sink does not block decoding.
    In-flight holds from an old registration are discarded, never sent to a new
    consumer. A consumer must detach on close; no PCM is persisted by this API.
    """
    global _mic_lab_observer
    if callback is not None and not callable(callback):
        raise TypeError('Mic Lab observer must be callable or None')
    with _mic_lab_registration_lock:
        old = _mic_lab_observer
        _mic_lab_observer = _MicLabSink(callback) if callback is not None else None
        if old is not None:
            old.queue.put(None)


def flush_mic_lab_observer(timeout=30.) -> bool:
    sink = _mic_lab_observer
    if sink is None:
        return True
    if threading.current_thread() is sink.thread:
        return False
    done = threading.Event()
    sink.queue.put(done)
    return done.wait(timeout)


def _mic_lab_safe(operation):
    from functools import wraps
    @wraps(operation)
    def observe(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Exception:
            sink = _mic_lab_observer
            if sink is not None:
                sink.warning()
    return observe


def _mic_lab_coordinates(audio):
    """Exact snapshot frame intersections; unknown provenance stays unknown.

    Ring frame indices are FrameBus sequence numbers, end exclusive. Within a
    frame, *_offset identifies the precise sample. Packed capture coordinates
    remain separate: a sequence gap is never fabricated as silence.
    """
    snapshot = getattr(audio, 'capture_snapshot', None)
    if snapshot is None:
        return dict(start_frame=None, end_frame=None, ranges=[],
                    capture_start=None, capture_end=None, provenance='unavailable')
    origin = int(audio.capture_start)
    end, offset, ranges = origin + len(audio), 0, []
    for frame in snapshot.frames:
        size = len(frame.pcm) // 2
        a, b = max(origin, offset), min(end, offset + size)
        if a < b:
            ranges.append(dict(start_frame=frame.seq, end_frame=frame.seq + 1,
                start_offset=a - offset, end_offset=b - offset, epoch=frame.epoch,
                capture_start=a, capture_end=b))
        offset += size
    valid = sum(r['capture_end'] - r['capture_start'] for r in ranges) == len(audio)
    return dict(start_frame=ranges[0]['start_frame'] if ranges and valid else None,
                end_frame=ranges[-1]['end_frame'] if ranges and valid else None,
                ranges=ranges if valid else [], capture_start=origin if valid else None,
                capture_end=end if valid else None,
                provenance='ring_snapshot' if valid else 'unavailable')


@_mic_lab_safe
def begin_mic_lab_hold(audit, audio, *, app, route):
    import uuid
    import numpy as np
    sink = _mic_lab_observer
    if sink is None or audit is None:
        return
    # Copy the pre-trim ring capture on this decode caller, never the drain.
    snapshot = getattr(audio, 'capture_snapshot', None)
    raw = snapshot.audio() if snapshot is not None else audio
    audit._mic_lab_1267 = dict(id=uuid.uuid4().hex, hold_seq_id=getattr(audit, 'hold_seq_id', None),
        route=route, raw=np.array(raw, dtype=np.float32, copy=True),
        raw_coordinates=_mic_lab_coordinates(raw),
        capture_receipt=snapshot.receipt() if snapshot is not None else None,
        sample_rate=getattr(app, 'model_rate', 16000), slices=[], text='',
        sink=sink, complete=False)


@_mic_lab_safe
def begin_mic_lab_batch(audit, app, args, kwargs):
    audio = args[1] if len(args) > 1 else kwargs['audio']
    begin_mic_lab_hold(audit, audio, app=app, route='batch')


def mic_lab_model(model, audit, app):
    """Wrap only observation; native calls/errors/lazy pulls retain their order."""
    class Observed:
        def __getattr__(self, name):
            return getattr(model, name)

        def transcribe(self, audio, **params):
            state = getattr(audit, '_mic_lab_1267', None)
            if state is None or state['complete']:
                begin_mic_lab_hold(audit, audio, app=app, route='hold')
                state = getattr(audit, '_mic_lab_1267', None)
            item = _mic_lab_input(state, audio, params)
            # Decoder exceptions deliberately propagate; only observer work is
            # best effort. Do not eagerly consume a faster-whisper generator.
            segments, info = model.transcribe(audio, **params)
            def observed():
                for segment in segments:
                    _mic_lab_segment(item, segment)
                    yield segment
            return observed(), info
    try:
        return Observed()
    except Exception:
        sink = _mic_lab_observer
        if sink is not None:
            sink.warning()
        return model


@_mic_lab_safe
def _mic_lab_input(state, audio, params):
    import copy
    import numpy as np
    if state is None or state['sink'] is not _mic_lab_observer:
        return
    item = dict(audio=np.array(audio, dtype=np.float32, copy=True),
        **_mic_lab_coordinates(audio), params=copy.deepcopy(params), segments=[],
        temperature=None, avg_logprob=None, compression_ratio=None,
        retry=state['route'] == 'retry', attempt=len(state['slices']))
    # A relaxed no-speech retry can still return zero segments at temperature
    # zero. Repeated identical native PCM is an attempt retry independently of
    # segment metadata (ordinary neighboring chunks overlap but differ).
    item['retry'] |= any(np.array_equal(item['audio'], prior['audio']) for prior in state['slices'])
    state['slices'].append(item)
    return item


@_mic_lab_safe
def _mic_lab_segment(item, segment):
    import math
    if item is None:
        return
    values = {}
    for key in ('start', 'end', 'temperature', 'avg_logprob', 'compression_ratio', 'no_speech_prob'):
        value = getattr(segment, key, None)
        values[key] = float(value) if isinstance(value, (float, int)) and math.isfinite(value) else None
    item['segments'].append(values)
    for key, operation in (('temperature', max), ('avg_logprob', min), ('compression_ratio', max)):
        value = values[key]
        if value is not None:
            item[key] = value if item[key] is None else operation(item[key], value)
    item['retry'] |= bool(values['temperature'] and values['temperature'] > 0)


@_mic_lab_safe
def complete_mic_lab_hold(audit, text):
    state = getattr(audit, '_mic_lab_1267', None)
    if state is None or state['complete'] or state['sink'] is not _mic_lab_observer:
        return
    state['complete'] = True
    state['text'] = str(text or '')  # Full final, before DiagRecord's 200-char cap.
    sink = state['sink']
    sink.queue.put({k: v for k, v in state.items() if k not in ('sink', 'complete')})
    # The queue owns PCM now; the production audit retains no observer buffers.
    audit._mic_lab_1267 = None


@_mic_lab_safe
def complete_mic_lab_session(session, text):
    complete_mic_lab_hold(getattr(session, '_hold_final_audit', None), text)


@_mic_lab_safe
def pending_mic_lab_batch(audit, app, text):
    # Completion may apply cleanup before record(). Correlate on this caller's
    # thread and app identity, rather than the app's mutable latest-hold field.
    _mic_lab_pending.batch = (app, audit, text)


@_mic_lab_safe
def _mic_lab_record(rec, app):
    pending = getattr(_mic_lab_pending, 'batch', None)
    if pending is not None and pending[0] is app and rec.mode == 'hotkey':
        _mic_lab_pending.batch = None
        complete_mic_lab_hold(pending[1], rec.text)


@_mic_lab_safe
def begin_mic_lab_retry(app, slot):
    if slot.audit is not None:
        begin_mic_lab_hold(slot.audit, slot.audio, app=app, route='retry')


@_mic_lab_safe
def complete_mic_lab_retry(slot, result):
    complete_mic_lab_hold(slot.audit, ''.join(segment.text for segment in result.segments).strip())
