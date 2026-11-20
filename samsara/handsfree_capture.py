"""Private, opt-in evidence from the hands-free listener and decode FIFO."""
from __future__ import annotations

from contextlib import ExitStack
from contextvars import ContextVar
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import struct
import threading
import time
import uuid
import wave

import numpy as np

from samsara.audio_engine.frame import FRAME_SIZE, SAMPLE_RATE
from samsara.constants import LIVE_VAD_PROB_THRESHOLD

RATE = SAMPLE_RATE
MAX_SECONDS = 1800
MAX_BYTES = 1024 ** 3
MAX_EVENT_BYTES = 2 * 1024 ** 2
MAX_AGE_S = 2 * 86400
FLAGGED_AGE_S = 7 * 86400
DECODE_WAIT_S = 120
SWITCH = "diagnostics.handsfree_session_capture"
SHORT_REJECT_S = 1.500001

# seq, capture_ns, observed_ns, session_sample, samples, epoch,
# RMS, live peak, selected threshold, silence_s, state, vad_used.
FRAME_ROW = struct.Struct("<QqQQIIffffBB")
IDLE, ONSET, UTTERANCE, HOLD, TTS, SKIPPED = range(6)
_CLOSED_FILE = re.compile(r"([0-9a-f]{32})(-p[0-9]{6,})?\.(wav|frames|jsonl|json|tmp)")
_storage_lock = threading.RLock()
_active_parts = {}
_decode = ContextVar("handsfree_diagnostic_decode", default=None)
_logger = logging.getLogger("Samsara")


def enabled(app):
    config = getattr(app, "config", {}) or {}
    diagnostics = config.get("diagnostics") if isinstance(config, dict) else None
    return isinstance(diagnostics, dict) and diagnostics.get("handsfree_session_capture") is True


def _profile():
    # No implicit resolution of the live home, and no profile read.
    raw = os.environ.get("SAMSARA_HOME_DIR", "")
    path = Path(raw).resolve() if raw else None
    return path if path and path.name.casefold() == ".samsara-private" else None


def _active(app):
    return (getattr(app, "command_mode_active", False) is True
            and (app.config.get("command_mode") or {}).get("mode") == "toggle")


def _settings(app):
    # Whitelist scalar decoding settings: never copy API/provider dictionaries.
    keys = ("model_size", "compute_type", "language", "beam_size", "temperature",
            "auto_capitalize", "format_numbers", "recording_tail_silence_ms")
    result = {k: app.config[k] for k in keys if k in app.config
              and isinstance(app.config[k], (str, int, float, bool, type(None)))}
    result["device_type"] = str(getattr(app, "device_type", "unknown"))
    result["dictate_utterance_silence_s"] = (app.config.get("command_mode") or {}).get(
        "dictate_utterance_silence_s", .65)
    return result


def prune(root, *, now=None, max_bytes=None, max_age_s=None):
    """Prune recorder-owned bundles, reserving space for all open parts."""
    root = Path(root).resolve()
    now = time.time() if now is None else now
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    max_age_s = MAX_AGE_S if max_age_s is None else max_age_s
    with _storage_lock:
        active = {stem: size for (folder, stem), size in _active_parts.items() if folder == root}
        groups, manifests, flagged = {}, {}, set()
        for path in root.iterdir() if root.is_dir() else ():
            match = _CLOSED_FILE.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            session, part, extension = match.groups()
            stem = session + (part or "")
            if stem in active:
                continue
            info = path.stat()
            if not part and extension == "json":
                try:
                    metadata = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    metadata = None
                manifests[session] = (path, info, metadata)
                if isinstance(metadata, dict) and metadata.get("priority") is True:
                    flagged.add(session)
            else:
                groups.setdefault((session, stem), []).append((path, info))

        active_ids = {stem.split("-", 1)[0] for stem in active}
        totals = {}
        for session, _ in groups:
            totals[session] = totals.get(session, 0) + 1
        # A manifest by itself is an orphan, unless its first part is still open.
        for session in manifests.keys() - totals.keys() - active_ids:
            groups[(session, session)] = []
            totals[session] = 1
        total = sum(active.values()) + sum(info.st_size for _, info, _ in manifests.values())
        parts = []
        for (session, stem), files in groups.items():
            times = [info.st_mtime for _, info in files]
            if stem == session and session in manifests:
                times.append(manifests[session][1].st_mtime)
            size = sum(info.st_size for _, info in files)
            total += size
            parts.append((session in flagged, max(times), session, stem, files, size))

        removed_parts = set()
        for priority, modified, session, stem, files, size in sorted(parts, key=lambda part: part[:4]):
            age_limit = max(max_age_s, FLAGGED_AGE_S) if priority else max_age_s
            if now - modified <= age_limit and total <= max_bytes:
                continue
            for path, _ in files:
                path.unlink(missing_ok=True)
            total -= size
            totals[session] -= 1
            if stem != session:
                removed_parts.add(session)
            if not totals[session] and session not in active_ids and session in manifests:
                path, info, _ = manifests[session]
                path.unlink(missing_ok=True)
                total -= info.st_size

        for session in removed_parts - active_ids:
            if totals[session] and session in manifests:
                path, _, metadata = manifests[session]
                if isinstance(metadata, dict) and metadata.get("schema") == 2 and metadata.get("complete"):
                    metadata.update(complete=False, incomplete_reason="retention_pruned")
                    _write_manifest(path, metadata)
        return total


def _write_manifest(path, metadata):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(metadata, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _encoded(event, observed_ns, **values):
    return (json.dumps(dict(event=event, observed_ns=observed_ns, **values),
                       allow_nan=False, separators=(",", ":")) + "\n").encode()


def _event_rows(kind, values, observed):
    spans = values.get("ring_spans", ()) if kind == "chunk" else ()
    if len(spans) <= 128:
        yield _encoded(kind, observed, **values)
        return
    # An unusually fragmented long take can exceed a single journal segment.
    yield _encoded(kind, observed, **dict(values, ring_spans=spans[:128]))
    for index in range(128, len(spans), 128):
        yield _encoded("chunk_spans", observed, chunk_id=values["chunk_id"],
                       first_span=index, ring_spans=spans[index:index + 128])


class _Part:
    def __init__(self, capture, *, opened_ns):
        self.capture = capture
        self.number = capture.parts + 1
        self.stem = f"{capture.id}-p{self.number:06d}"
        self.start = capture.samples
        self.key = (capture.root, self.stem)
        self.files = ExitStack()
        self.event_bytes = 0
        reserve = capture.sample_limit * 2 + capture.frame_limit * FRAME_ROW.size + MAX_EVENT_BYTES + 8192
        with _storage_lock:
            _active_parts[self.key] = reserve
            try:
                if prune(capture.root) > MAX_BYTES:
                    raise OSError("capture storage reservation exceeds budget")
                audio = self.files.enter_context(capture.root.joinpath(self.stem + ".wav").open("xb"))
                self.wav = self.files.enter_context(wave.open(audio, "wb"))
                self.wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
                self.frames = self.files.enter_context(capture.root.joinpath(self.stem + ".frames").open("xb"))
                self.journal = self.files.enter_context(capture.root.joinpath(self.stem + ".jsonl").open("xb"))
                self.event(_encoded("part_start", opened_ns, sample_offset=self.start,
                                    session_id=capture.id, part=self.number))
            except BaseException:
                self.close()
                raise
        capture.parts = self.number

    def event(self, row):
        self.journal.write(row)
        self.event_bytes += len(row)

    def finish(self, observed_ns):
        self.event(_encoded("part_end", observed_ns, sample_offset=self.capture.samples, part=self.number))
        if self.capture.samples == self.start:
            self.wav.writeframesraw(b"")  # Valid WAV even for an events-only part.
        self.close()

    def close(self):
        try:
            self.files.close()
        finally:
            with _storage_lock:
                _active_parts.pop(self.key, None)


class Capture:
    def __init__(self, root, settings, *, queue_size=256):
        self.root = Path(root)
        self.id = uuid.uuid4().hex
        self.settings = settings
        self.started_ns, self.wall_ns = time.perf_counter_ns(), time.time_ns()
        self.queue = queue.Queue(queue_size)
        self.closed = threading.Event()
        self.finished = threading.Event()
        self._lock = threading.Lock()
        self._pending = 0
        self._saw_chunk = False
        self._deadline = math.inf
        self.failed = False
        self.failure_reason = None
        self.dropped = 0
        self.samples = self.parts = self.frames = 0
        self.first_t = self.last_frame = None
        self.incomplete = None
        self.priority = False
        self.exiting = False
        self.last_state = None
        self.last_hold = self.last_duck = False
        self.sample_limit = max(FRAME_SIZE, int(MAX_SECONDS * RATE))
        self.frame_limit = max(1, self.sample_limit // FRAME_SIZE)
        self.thread = threading.Thread(target=self._write, name="handsfree-diagnostics", daemon=True)
        self.thread.start()

    def put(self, kind, value, *, observed_ns=None):
        with self._lock:
            if not self.closed.is_set():
                self._put(kind, value, observed_ns)

    def _put(self, kind, value, observed_ns=None):
        if self.failed:
            return False
        try:
            self.queue.put_nowait((kind, value, observed_ns if observed_ns is not None else time.perf_counter_ns()))
            return True
        except queue.Full:
            self.dropped += 1
            self.failed, self.failure_reason = True, "queue_overflow"
            self._close()
            return False

    def add_chunk(self, values):
        with self._lock:
            if self.closed.is_set() or not self._put("chunk", values):
                return None
            self._pending += 1
            self._saw_chunk = True
        return ChunkEvidence(self, values["chunk_id"])

    def decode_event(self, kind, values):
        with self._lock:
            self._put(kind, values)
            if kind == "decode_end":
                self._pending -= 1

    def close(self):
        with self._lock:
            self._close()

    def _close(self):
        if not self.closed.is_set():
            self._deadline = time.monotonic() + DECODE_WAIT_S
            self.closed.set()

    def _done(self):
        with self._lock:
            if not self.closed.is_set() or not self.queue.empty():
                return False
            if not self._pending or self.failed:
                return True
            if time.monotonic() >= self._deadline:
                self.failed, self.failure_reason = True, "ended_with_pending_decode"
                return True
            return False

    def _manifest(self, hashes, *, finished=False):
        reason = self.incomplete or self.failure_reason
        if finished and not self.samples:
            reason = reason or "no_frames"
        with _storage_lock:
            first = self.root / f"{self.id}-p000001.wav"
            if finished and not first.is_file():
                reason = reason or "retention_pruned"
            metadata = dict(schema=2, kind="REAL_SESSION", id=self.id, sample_rate=RATE,
                            samples=self.samples, duration_s=self.samples / RATE, frames=self.frames,
                            parts=self.parts, first_capture_t=self.first_t,
                            started_ns=self.started_ns, wall_ns=self.wall_ns,
                            settings=self.settings, source_sha256=hashes, priority=self.priority,
                            dropped_events=self.dropped, complete=finished and not reason,
                            incomplete_reason=reason if finished else "recording",
                            fidelity_ready=False)
            _write_manifest(self.root / (self.id + ".json"), metadata)

    def _write(self):
        part = None
        try:
            self.root = self.root.resolve()
            self.root.mkdir(parents=True, exist_ok=True)
            hashes = _source_hashes()
            part = _Part(self, opened_ns=self.started_ns)
            self._manifest(hashes)
            while not self._done():
                try:
                    kind, value, observed = self.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                if kind == "frame":
                    if (self.samples - part.start + len(value.pcm) > self.sample_limit
                            or part.frames.tell() >= self.frame_limit * FRAME_ROW.size
                            or part.event_bytes >= MAX_EVENT_BYTES):
                        part = self._rotate(part, hashes, observed)
                    self._write_frame(part, value)
                else:
                    for row in _event_rows(kind, value, observed):
                        if part.event_bytes + len(row) > MAX_EVENT_BYTES:
                            part = self._rotate(part, hashes, observed)
                        part.event(row)
                    if not self.priority and _is_priority(kind, value):
                        self.priority = True
                        self._manifest(hashes)
            part.finish(time.perf_counter_ns())
            part = None
            self._manifest(hashes, finished=True)
            prune(self.root)
        except Exception as exc:
            self.failed, self.failure_reason = True, "disk_error"
            _logger.warning("Hands-free diagnostics writer failed (%s)", type(exc).__name__)
        finally:
            try:
                if part is not None:
                    part.close()
            except Exception:
                pass
            self.finished.set()

    def _rotate(self, part, hashes, observed):
        # Hold the reservation across rotation so another writer cannot remove
        # the session manifest in the gap between closing and opening a part.
        with _storage_lock:
            part.finish(observed)
            next_part = _Part(self, opened_ns=observed)
        try:
            self._manifest(hashes)
        except Exception:
            next_part.close()
            raise
        return next_part

    def _write_frame(self, part, frame):
        pcm, seq, epoch, stamp = frame.pcm, frame.seq, frame.epoch, frame.capture_ns
        if self.first_t is None:
            self.first_t = stamp / 1e9
        if self.last_frame is not None:
            prev_seq, prev_epoch, prev_stamp = self.last_frame
            if epoch != prev_epoch:
                part.event(_encoded("mic_device_change", frame.observed_ns, old_epoch=prev_epoch,
                                    device_epoch=epoch, seq=seq, sample_offset=self.samples))
            if (seq != prev_seq + 1 or epoch != prev_epoch
                    or abs(stamp - prev_stamp - len(pcm) * 1e9 / RATE) > .15e9):
                self.incomplete = "frame_discontinuity"
                part.event(_encoded("frame_discontinuity", frame.observed_ns, previous_seq=prev_seq,
                                    seq=seq, device_epoch=epoch, sample_offset=self.samples))
        self.last_frame = seq, epoch, stamp
        part.wav.writeframesraw(pcm.tobytes())
        part.frames.write(FRAME_ROW.pack(seq, stamp, frame.observed_ns, self.samples, len(pcm),
                                        epoch, frame.rms, frame.peak, frame.threshold,
                                        frame.silence_s, frame.state, frame.vad_used))
        self.samples += len(pcm)
        self.frames += 1


def _source_hashes():
    source = Path(__file__).resolve().parents[1]
    hashes = {}
    for name in ("dictation.py", "samsara/audio_engine/wake_consumer.py", "samsara/handsfree_capture.py",
                 "samsara/session_modes.py", "samsara/session_text.py", "samsara/open_span.py",
                 "samsara/draft_reread.py"):
        path = source / name
        if path.is_file():
            hashes[name] = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return hashes


def _is_priority(kind, values):
    return (kind == "parked_draft" or kind == "close" and values["reason"] == "too_short"
            or kind == "gate" and values["verdict"] == "reject" and values["duration_s"] <= SHORT_REJECT_S
            or kind == "decode_drop" and values["reason"] in (
                "no_speech_in_audio", "empty_transcription", "signoff_hallucination", "too_short")
            and values["duration_s"] <= SHORT_REJECT_S)


def poll_frames(consumer, stop_event, *, sleep):
    """Poll production frames with the existing idle wait and capture observation."""
    app = consumer._app
    while consumer._running and not stop_event.is_set():
        if (
            app.wake_word_active
            or consumer._is_toggle_cmd(app) or getattr(getattr(app, '_ava_question', None), 'opened', False)
        ) and consumer._poll_frame(stop_event):
            continue
        if enabled(app) or getattr(app, "_handsfree_capture_1259", None) is not None:
            observe_state(app)
        sleep(0.005)


def observe_state(app):
    """Sample only changes visible to the running listener."""
    try:
        current = getattr(app, "_handsfree_capture_1259", None)
        if not enabled(app) or not _active(app):
            if current is not None and not current.exiting:
                close(app)
            return None
        if current is None:
            profile = _profile()
            if profile is None:
                return None
            current = app._handsfree_capture_1259 = Capture(profile / "handsfree_sessions", _settings(app))
            current.put("enter", {})
        if current.failed or current.exiting:
            return None

        manager = getattr(app, "_session_mode_manager", None)
        doc = getattr(manager, "draft_document", None)
        mode = getattr(getattr(manager, "mode", None), "value", "unknown")
        chars = _text_chars(doc)
        state = mode, chars, getattr(doc, "revision", None)
        previous = current.last_state
        if previous is None or previous[0] != mode:
            current.put("mode", dict(mode=mode))
        if previous != state:
            current.last_state = state
            current.put("draft", dict(chars=chars, revision=state[2]))
        hold = getattr(app, "_hotkey_recording", False) is True
        if hold != current.last_hold:
            current.last_hold = hold
            current.put("hold_press" if hold else "hold_release", {})
        duck = getattr(app, "_hands_free_capture_ducker", None) is not None
        if duck != current.last_duck:
            current.last_duck = duck
            current.put("duck_engage" if duck else "duck_restore", {})
        return current
    except Exception:
        return None  # Diagnostics never change any listener decision.


def _text_chars(doc):
    chars = getattr(doc, "chars", None)
    return chars if isinstance(chars, int) else len(getattr(doc, "text", "") or "")


def begin_exit(current):
    try:
        current.exiting = True
        current.put("leave", {})
    except Exception:
        pass


def close(app, *, current=None, parked=None):
    try:
        if current is None:
            current = getattr(app, "_handsfree_capture_1259", None)
            if current is None or current.exiting:
                return
        if parked:
            current.put("parked_draft", dict(chars=int(parked)))
        if not current.exiting:
            current.put("leave", {})
        with current._lock:
            if (not current._pending and not current.failed
                    and getattr(getattr(app, "_wake_consumer", None), "_toggle_worker_active", False)
                    and not current._saw_chunk):
                current.incomplete = "ended_with_pending_decode"
            current._close()
        if getattr(app, "_handsfree_capture_1259", None) is current:
            app._handsfree_capture_1259 = None
    except Exception:
        pass


class FrameEvidence:
    def __init__(self, current, frame):
        self.current = current
        self.observed_ns = time.perf_counter_ns()
        self.capture_ns = round(float(frame.t_capture) * 1e9)
        self.epoch, self.seq = int(frame.device_epoch), int(frame.seq)
        data = np.asarray(frame.pcm)
        self.pcm = ((np.clip(data, -1, 1) * 32767).astype("<i2") if data.dtype.kind == "f"
                    else data.astype("<i2", copy=True))
        self.rms = self.peak = self.threshold = self.silence_s = math.nan
        self.state, self.vad_used = SKIPPED, False

    def probabilities(self, values):
        try:
            self.peak = float(np.max(values)) if len(values) else math.nan
        except Exception:
            pass

    def decision(self, rms, rms_threshold, silence_s, vad_used, speaking):
        self.rms = rms
        self.threshold = LIVE_VAD_PROB_THRESHOLD if vad_used else rms_threshold
        self.silence_s = silence_s
        self.state = UTTERANCE if speaking else IDLE
        self.vad_used = vad_used

    def span(self, buffer, frame, chunk):
        try:
            spans = getattr(buffer, "handsfree_spans", None)
            if spans is None:
                spans = buffer.handsfree_spans = []
            start = frame.seq * FRAME_SIZE + len(frame.pcm) - len(chunk)
            end = start + len(chunk)
            if spans and spans[-1][0] == frame.device_epoch and spans[-1][2] == start:
                spans[-1][2] = end
            else:
                spans.append([frame.device_epoch, start, end])
        except Exception:
            pass

    def copy_spans(self, source, buffer):
        try:
            if hasattr(source, "handsfree_spans"):
                buffer.handsfree_spans = source.handsfree_spans
        except Exception:
            pass

    def close(self, reason, *, rms_threshold, since_release_s, rms=None, peak=None):
        try:
            self.current.put("close", dict(reason=reason, seq=self.seq, device_epoch=self.epoch,
                vad_used=self.vad_used, threshold=self.threshold,
                vad_peak=peak if peak is not None else _finite_or_missing(self.peak),
                rms=self.rms if rms is None else rms, rms_threshold=rms_threshold, silence_s=self.silence_s,
                since_release_s=since_release_s))
        except Exception:
            pass

    def discard(self, consumer, reason, rms_threshold, now):
        released = consumer._last_suppression_release
        since_release = now() - released if released is not None else -1.
        self.close(reason, rms_threshold=rms_threshold, since_release_s=since_release)

    def record(self):
        self.current.put("frame", self, observed_ns=self.observed_ns)


def _finite_or_missing(value):
    return value if math.isfinite(value) else -1.


def observe_frame(app, frame):
    current = observe_state(app)
    if current is not None:
        try:
            return FrameEvidence(current, frame)
        except Exception:
            current.incomplete = "frame_observation_error"
    return None


def process_frame(consumer, frame):
    evidence = observe_frame(consumer._app, frame)
    if evidence is None:
        return consumer._listen_frame(frame)
    try:
        return consumer._listen_frame(frame, evidence)
    finally:
        try:
            evidence.record()
        except Exception:
            evidence.current.incomplete = "frame_observation_error"


def observe_chunk(app, buffer, **endpoints):
    try:
        current = getattr(app, "_handsfree_capture_1259", None)
        if current is None:
            return
        samples = sum(len(part) for part in buffer)
        spans = getattr(buffer, "handsfree_spans", [])
        fields = dict(chunk_id=uuid.uuid4().hex, samples=samples, duration_s=samples / RATE,
                      ring_spans=spans, unmapped_samples=samples - sum(end - start for _, start, end in spans),
                      **endpoints)
        if hasattr(buffer, "tutorial_owner"):
            buffer.handsfree_evidence = current.add_chunk(fields)
        else:
            current.put("chunk", fields)
    except Exception:
        pass


class ChunkEvidence:
    def __init__(self, capture, chunk_id):
        self.capture = capture
        self.chunk_id = chunk_id
        self.dispatched = False
        self.gated = False

    def put(self, event, **fields):
        self.capture.decode_event(event, dict(chunk_id=self.chunk_id, **fields))

    def drop(self, reason, duration_s):
        try:
            self.put("decode_drop", reason="error" if reason.startswith("error:") else reason,
                     duration_s=duration_s)
        except Exception:
            pass

    def gate_sink(self, original):
        def report(presence, pcm, rate, verdict):
            try:
                self.put("gate", verdict=verdict, duration_s=len(pcm) / rate)
                self.gated = True
            except Exception:
                pass
            return original(presence, pcm, rate, verdict)
        return report

    def finish(self, status):
        try:
            fields = dict(status=status, gate_observed=self.gated)
            if not self.dispatched:
                fields.update(delivered_chars=0, delivered_words=0)
            self.put("decode_end", **fields)
        except Exception:
            pass


def decode_if_observed(run, frames, *args, **kwargs):
    evidence = getattr(frames, "handsfree_evidence", None)
    if evidence is None:
        return run(frames, *args, **kwargs)
    return run_decode(evidence, run, frames, *args, **kwargs)


def run_decode(evidence, run, *args, **kwargs):
    token = _decode.set(evidence)
    status = "returned"
    try:
        return run(*args, **kwargs)
    except BaseException:
        status = "error"
        raise
    finally:
        _decode.reset(token)
        evidence.finish(status)


def dispatch_target(app):
    return _decode.get() or getattr(app, "_handsfree_capture_1259", None)


def decode_target():
    return _decode.get()


def observe_dispatch(manager, outcome):
    try:
        app = getattr(getattr(manager, "_commit_redecode_fn", None), "__self__", None)
        target = dispatch_target(app)
        if target is None:
            return
        text = (outcome.detail or {}).get("text", "") if outcome.kind in (
            "dictate_staged", "dictate_committed", "dictate_injected") else ""
        fields = dict(kind=outcome.kind, draft_chars=_text_chars(getattr(manager, "draft_document", None)),
                      delivered_chars=len(text or ""), delivered_words=len((text or "").split()))
        if isinstance(target, ChunkEvidence):
            target.put("dispatch", **fields)
            target.dispatched = True
        else:
            target.put("dispatch", fields)
    except Exception:
        pass


def log_silence_close(consumer, chunk, rms, rms_threshold, silence_s, vad_used, *,
                      logger, now, evidence=None):
    # One stateless rescore of the closing frame; never changes admission.
    peak = -1.0
    if vad_used:
        try:
            with consumer._app._vad_lock:
                probabilities = consumer._app._vad_probabilities(chunk.copy())
            peak = float(np.max(probabilities)) if len(probabilities) else -1.0
        except Exception:
            pass  # Missing telemetry cannot interrupt capture or dispatch.
    released = consumer._last_suppression_release
    since_release = now() - released if released is not None else -1.
    logger.info(
        '[HANDSFREE-SILENCE] vad_used=%d threshold=%.6f vad_peak=%.6f rms=%.6f '
        'rms_threshold=%.6f silence_s=%.3f since_release_s=%.3f',
        vad_used, LIVE_VAD_PROB_THRESHOLD if vad_used else rms_threshold, peak,
        rms, rms_threshold, silence_s, since_release)
    if evidence is not None:
        evidence.close("silence_flush", rms_threshold=rms_threshold, since_release_s=since_release, rms=rms,
                       peak=_finite_or_missing(peak))
