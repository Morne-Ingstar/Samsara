"""1259: opt-in continuous diagnostic capture; never participates in decisions.

Only an explicitly selected .samsara-private profile may enable this recorder.
All disk I/O runs on one bounded-queue writer. An incomplete capture is labelled
as such; it must never be admitted as evidence of replay fidelity.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid
import wave

import numpy as np

RATE = 16000
MAX_SECONDS = 1800
MAX_BYTES = 256 * 1024 * 1024
MAX_EVENT_BYTES = 16 * 1024 * 1024
MAX_AGE_S = 7 * 86400
SWITCH = "diagnostics.handsfree_session_capture"


def _profile(app):
    config = getattr(app, "config", {}) or {}
    if (config.get("diagnostics") or {}).get("handsfree_session_capture") is not True:
        return None
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


def prune(root, *, now=None, max_bytes=MAX_BYTES, max_age_s=MAX_AGE_S):
    """Delete only this recorder's UUID-named completed pairs, oldest first."""
    root = Path(root).resolve()
    now = time.time() if now is None else now
    pairs = []
    stems = {p.stem for p in root.iterdir()} if root.is_dir() else set()
    for stem in stems:
        if len(stem) != 32 or any(c not in "0123456789abcdef" for c in stem):
            continue
        files = [root / (stem + suffix) for suffix in (".json", ".wav", ".jsonl")]
        if any(p.is_symlink() or p.resolve().parent != root for p in files):
            continue
        present = [p for p in files if p.is_file()]
        if present:
            pairs.append((max(p.stat().st_mtime for p in present), present,
                          sum(p.stat().st_size for p in present)))
    total = sum(size for _, _, size in pairs)
    for modified, files, size in sorted(pairs):
        if now - modified > max_age_s or total > max_bytes:
            for path in files:
                path.unlink(missing_ok=True)
            total -= size


class Capture:
    def __init__(self, root, settings, *, queue_size=256):
        self.root = Path(root)
        self.id = uuid.uuid4().hex
        self.settings = settings
        self.queue = queue.Queue(queue_size)
        self.closed = threading.Event()
        self.finished = threading.Event()
        self.failed = False
        self.failure_reason = None
        self.dropped = 0
        self.samples = 0
        self.last_state = None
        self.thread = threading.Thread(target=self._write, name="handsfree-diagnostics", daemon=True)
        self.thread.start()

    def put(self, kind, value):
        if self.closed.is_set() or self.failed:
            return
        try:
            self.queue.put_nowait((kind, value, time.perf_counter_ns()))
        except queue.Full:
            self.dropped += 1
            self.failed = True
            self.failure_reason = "queue_overflow"
            self.close()

    def close(self):
        self.closed.set()

    def _write(self):
        count = event_bytes = frames = 0
        first_t = last_t = last_seq = last_epoch = None
        incomplete = None
        hashes = {}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            prune(self.root, max_bytes=MAX_BYTES - MAX_SECONDS * RATE * 2 - MAX_EVENT_BYTES)
            source = Path(__file__).resolve().parents[1]
            for name in ("dictation.py", "samsara/audio_engine/wake_consumer.py",
                         "samsara/session_modes.py", "samsara/session_text.py",
                         "samsara/open_span.py", "samsara/draft_reread.py"):
                path = source / name
                if path.is_file():
                    hashes[name] = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            with wave.open(str(self.root / (self.id + ".wav")), "wb") as wav, (
                    self.root / (self.id + ".jsonl")).open("w", encoding="utf-8") as journal:
                wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
                while not self.closed.is_set() or not self.queue.empty():
                    try:
                        kind, value, observed = self.queue.get(timeout=.1)
                    except queue.Empty:
                        continue
                    row = dict(event=kind, observed_ns=observed, sample_offset=count)
                    if kind == "frame":
                        pcm, capture_t, epoch, seq = value
                        if first_t is None:
                            first_t = capture_t
                        if last_seq is not None and (seq != last_seq + 1 or epoch != last_epoch
                                                     or abs(capture_t - last_t - len(pcm) / RATE) > .15):
                            incomplete = "frame_discontinuity"
                        last_seq, last_epoch, last_t = seq, epoch, capture_t
                        if count + len(pcm) > MAX_SECONDS * RATE:
                            incomplete = "duration_cap"
                            self.close()
                            break
                        wav.writeframesraw(pcm.tobytes())
                        count += len(pcm)
                        frames += 1
                        row.update(samples=len(pcm), capture_t=capture_t, device_epoch=epoch,
                                   seq=seq)
                    else:
                        row.update(value)
                    encoded = json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
                    event_bytes += len(encoded.encode("utf-8"))
                    if event_bytes > MAX_EVENT_BYTES:
                        incomplete = "event_cap"
                        self.close()
                        break
                    journal.write(encoded)
                self.samples = count
            metadata = dict(schema=1, kind="REAL_SESSION", id=self.id, sample_rate=RATE,
                            samples=count, duration_s=count / RATE, frames=frames,
                            first_capture_t=first_t, settings=self.settings, source_sha256=hashes,
                            dropped_events=self.dropped, complete=bool(count) and not (incomplete or self.failed),
                            incomplete_reason=incomplete or self.failure_reason or ("no_frames" if not count else None),
                            fidelity_ready=False,
                            caveat="Configuration/provenance and event coverage require offline admission.")
            (self.root / (self.id + ".json")).write_text(
                json.dumps(metadata, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            prune(self.root)
        except Exception:
            self.failed = True  # No partial capture gets a completed manifest.
        finally:
            self.finished.set()


def poll_frames(consumer, stop_event, *, sleep):
    """Poll production frames with the existing idle wait and capture observation."""
    app = consumer._app
    while consumer._running and not stop_event.is_set():
        observe_state(app)
        if not (
            app.wake_word_active
            or consumer._is_toggle_cmd(app) or getattr(getattr(app, '_ava_question', None), 'opened', False)
        ):
            sleep(0.005)
            continue

        if not consumer._poll_frame(stop_event):
            sleep(0.005)


def observe_state(app):
    """Lifecycle/draft observation; the off path creates no thread or file."""
    try:
        current = getattr(app, "_handsfree_capture_1259", None)
        enabled = _profile(app)
        active = enabled is not None and _active(app)
        if current is not None and not active:
            pending = bool(getattr(getattr(app, "_wake_consumer", None), "_toggle_worker_active", False))
            current.put("end", dict(pending_decode=pending))
            if pending:
                current.failed, current.failure_reason = True, "ended_with_pending_decode"
            current.close()
            app._handsfree_capture_1259 = None
        elif active and current is None:
            current = app._handsfree_capture_1259 = Capture(enabled / "handsfree_sessions", _settings(app))
        if active and current is not None:
            manager = getattr(app, "_session_mode_manager", None)
            doc = getattr(manager, "draft_document", None)
            state = (getattr(getattr(manager, "mode", None), "value", "unknown"),
                     str(getattr(doc, "text", "")), getattr(doc, "revision", None))
            if state != current.last_state:
                current.last_state = state
                current.put("draft", dict(mode=state[0], text=state[1], revision=state[2], origin="sampled"))
    except Exception:
        pass  # Diagnostics never change the capture/dispatch decision.


def observe_frame(app, frame):
    try:
        observe_state(app)
        current = getattr(app, "_handsfree_capture_1259", None)
        if current is not None:
            data = np.asarray(frame.pcm)
            pcm = (np.clip(data, -1, 1) * 32767).astype("<i2") if data.dtype.kind == "f" else data.astype("<i2", copy=True)
            current.put("frame", (pcm, float(frame.t_capture), int(frame.device_epoch),
                                  int(frame.seq)))
    except Exception:
        pass


def observe_chunk(app, buffer, **endpoints):
    try:
        current = getattr(app, "_handsfree_capture_1259", None)
        if current is not None:
            current.put("chunk", dict(samples=sum(len(part) for part in buffer), **endpoints))
    except Exception:
        pass


def observe_dispatch(manager, outcome):
    try:
        app = getattr(getattr(manager, "_commit_redecode_fn", None), "__self__", None)
        current = getattr(app, "_handsfree_capture_1259", None)
        if current is not None:
            current.put("dispatch", dict(kind=outcome.kind,
                draft=str(getattr(getattr(manager, "draft_document", None), "text", "")),
                delivered=(outcome.detail or {}).get("text", "") if outcome.kind in
                    ("dictate_committed", "dictate_injected") else ""))
    except Exception:
        pass


def close(app):
    try:
        current = getattr(app, "_handsfree_capture_1259", None)
        if current is not None:
            if getattr(getattr(app, "_wake_consumer", None), "_toggle_worker_active", False):
                current.failed, current.failure_reason = True, "ended_with_pending_decode"
            current.close()
        app._handsfree_capture_1259 = None
    except Exception:
        pass
