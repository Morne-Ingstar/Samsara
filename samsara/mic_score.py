"""Microphone word accuracy, capture comparison and everyday estimates.

Nothing here opens hardware, reads a profile or loads a model at import time.
Only the fixed read-aloud sentences and their recognitions are persisted.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from datetime import datetime, timezone
import json
import hashlib
import math
from pathlib import Path
import re
import threading
import time
import uuid

import numpy as np

SENTENCES = (
    "Open Stremio and play my favourite show.",
    "Samsara helps me write a message today.",
    "Jarvis please set a timer for ten minutes.",
    "Send Morne the notes after our next meeting.",
    "There are twenty three books on the shelf.",
)
# Lower inclusive accuracy bound, score. One source for every measured score.
SCORE_BANDS = ((.98, 10), (.95, 9), (.92, 8), (.89, 7), (.86, 6),
               (.83, 5), (.80, 4), (.77, 3), (.74, 2), (0., 1))
_ONES = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), range(20)))
_TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
_history = ContextVar("mic_score_history", default=None)
_store_lock = threading.Lock()


def normalize(text):
    tokens = re.findall(r"\w+", text.casefold().replace("’", "'"), re.UNICODE)
    result, index = [], 0
    while index < len(tokens):
        word = tokens[index]
        if word in _TENS:
            value = _TENS[word]
            if index + 1 < len(tokens) and tokens[index + 1] in _ONES and _ONES[tokens[index + 1]] < 10:
                index += 1
                value += _ONES[tokens[index]]
            result.append(str(value))
        else:
            result.append(str(_ONES[word]) if word in _ONES else word)
        index += 1
    return result


def word_accuracy(said, heard):
    """Levenshtein WER with aligned substitutions, deletions and insertions."""
    expected, actual = normalize(said), normalize(heard)
    matrix = [[0] * (len(actual) + 1) for _ in range(len(expected) + 1)]
    for i in range(len(expected) + 1):
        matrix[i][0] = i
    for j in range(len(actual) + 1):
        matrix[0][j] = j
    for i, left in enumerate(expected, 1):
        for j, right in enumerate(actual, 1):
            matrix[i][j] = min(matrix[i-1][j] + 1, matrix[i][j-1] + 1,
                               matrix[i-1][j-1] + (left != right))
    i, j, differences = len(expected), len(actual), []
    while i or j:
        if i and j and matrix[i][j] == matrix[i-1][j-1] + (expected[i-1] != actual[j-1]):
            if expected[i-1] != actual[j-1]:
                differences.append({"said": expected[i-1], "heard": actual[j-1]})
            i, j = i-1, j-1
        elif i and matrix[i][j] == matrix[i-1][j] + 1:
            differences.append({"said": expected[i-1], "heard": "(missed)"})
            i -= 1
        else:
            differences.append({"said": "(extra)", "heard": actual[j-1]})
            j -= 1
    errors = matrix[-1][-1]
    return {"accuracy": max(0., 1 - errors / max(1, len(expected))),
            "errors": errors, "words": len(expected), "differences": list(reversed(differences))}


def score_accuracy(accuracy):
    return next(score for bound, score in SCORE_BANDS if accuracy + 1e-9 >= bound)


def signal_summary(audio, quiet):
    pcm = np.asarray(audio, np.float32).reshape(-1)
    background = np.asarray(quiet, np.float32).reshape(-1)
    if not pcm.size or not np.isfinite(pcm).all():
        raise ValueError("No usable microphone audio arrived. Reconnect it and try again.")
    rms = float(np.sqrt(np.mean(pcm * pcm)))
    peak = float(np.max(np.abs(pcm)))
    noise = float(np.sqrt(np.mean(background * background))) if background.size else None
    return {"level": "clipping" if peak >= .99 else "too quiet" if rms < .005 else "good",
            "rms": rms, "peak": peak,
            "noise_dbfs": 20 * math.log10(max(noise, 1e-6)) if noise is not None else None}


def segment_log_prob(segments):
    values = []
    for segment in segments or ():
        try:
            value = float(segment.avg_logprob)
            if math.isfinite(value):
                values.append(value)
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
    return min(values) if values else None


def microphone_name(app):
    """Resolve Auto to the actual engine endpoint; never pool changing defaults."""
    engine = getattr(app, "_ace_engine", None)
    name = getattr(engine, "capture_name", None)
    if isinstance(name, str) and name:
        return name
    config = getattr(app, "config", {})
    name = config.get("microphone_name") if config.get("microphone") is not None else None
    return name or ""


def begin_history(app):
    """Start a capture-owned context, returned as data for a thread handoff."""
    evidence = {"microphone_name": microphone_name(app), "log_prob": None}
    _history.set((app, evidence))
    return evidence


def open_history(app):
    token = _history.set(None)
    begin_history(app)
    return token


def close_history(token):
    _history.reset(token)


def capture_history(app=None):
    """Scope evidence to one call, including empty/error exits and thread reuse."""
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            owner = app if app is not None else args[0]
            token = open_history(owner)
            try:
                return function(*args, **kwargs)
            finally:
                close_history(token)
        return run
    return decorate


def remember_segments(app, segments, evidence=None):
    current = _history.get()
    if evidence is None and current is not None and current[0] is app:
        evidence = current[1]
    if evidence is not None:
        evidence["log_prob"] = segment_log_prob(segments)


def transcript_text(app, segments):
    """Consume lazy decoder output once and retain confidence before cleanup."""
    segments = list(segments)
    remember_segments(app, segments)
    return "".join(segment.text for segment in segments).strip()


def history_fields(app, log_prob=None):
    current = _history.get()
    evidence = current[1] if current is not None and current[0] is app else {}
    return {"microphone_name": evidence.get("microphone_name", microphone_name(app)),
            "log_prob": log_prob if log_prob is not None else evidence.get("log_prob")}


def log_history(app, raw_text, display_text=None, duration_ms=0,
                mode="hold", status="success", app_context=None,
                entry_type="dictation", log_prob=None, matched_command=None):
    """Best-effort privacy-aware writer with this capture's microphone evidence."""
    from samsara.kept_dictations import wake_history
    wake_history(app, raw_text, display_text if display_text is not None else raw_text, mode, entry_type, status)
    if app.history_db is None:
        return
    try:
        from samsara.recent_privacy import write_history
        write_history(app,
            raw_text=raw_text,
            display_text=display_text if display_text is not None else raw_text,
            app_context=app_context if app_context is not None else app._get_foreground_app(),
            duration_ms=int(duration_ms),
            mode=mode,
            status=status,
            entry_type=entry_type,
            **history_fields(app, log_prob),
            matched_command=matched_command,
        )
    except Exception as error:
        import logging
        from samsara.log import describe_text, safe_exception
        safe_exception(logging.getLogger("Samsara"),
                       f"[HISTORY] log failed: {describe_text(error)}")


def prepare_wake_frame(consumer):
    """Discard test speech before wake admission; preserve normal rearm order."""
    app = consumer._app
    if getattr(app, '_mic_score_active', False) is True:
        consumer.abort_utterance()
        consumer._hold_not_before = time.perf_counter()
        return False
    if app.app_state == 'wake_session':
        app._expire_wake_session()
    if getattr(app, '_wake_rearm_needs_onset', False) is True:
        consumer.abort_utterance()
        consumer._await_wake_onset = True
        app._wake_rearm_needs_onset = False
    return True


def start_streaming_history(session, app):
    """Start the session clock and bind evidence to its original endpoint."""
    session.app = app
    session.start_time = time.time()
    session._mic_score_evidence = {"microphone_name": microphone_name(app), "log_prob": None}


def stream_final(worker, audio, params):
    """Retain raw final-segment confidence before streaming cleanup/delivery."""
    from samsara.decode_recovery import stream_final as decode_final
    result = decode_final(worker, audio, params)
    session = worker._session
    remember_segments(session.app, result[0], getattr(session, '_mic_score_evidence', None))
    return result


def deliver_with_history(session, callback, *args):
    """Restore only this streaming capture's evidence on the delivery thread."""
    token = _history.set((session.app, getattr(session, "_mic_score_evidence", {})))
    try:
        return callback(*args)
    finally:
        _history.reset(token)


def everyday_estimate(rows, name, *, limit=100):
    """Independent recognition estimate and router matching rate, never WER.

    Recognition uses exp(mean logprob) times nonempty rate, with at least
    five attempts and three confidences. Routing never changes recognition.
    """
    selected = []
    for original in rows:
        row = dict(original)
        if (name and row.get("microphone_name") == name and not row.get("privacy_reason")
                and row.get("entry_type") in ("dictation", "command", "wake_command", "failed")
                and row.get("status") != "buffered"):
            selected.append(row)
        if len(selected) == limit:
            break
    probs = [float(r["log_prob"]) for r in selected if r.get("log_prob") is not None
             and math.isfinite(float(r["log_prob"]))]
    empty = sum(not str(r.get("raw_text", "")).strip() for r in selected) / max(1, len(selected))
    commands = [r for r in selected if r.get("entry_type") in ("command", "wake_command")]
    misses = sum(not r.get("matched_command") for r in commands)
    matching = {"match_rate": 1 - misses / len(commands), "samples": len(commands),
                "miss_rate": misses / len(commands)} if commands else None
    recognition = None
    if len(selected) >= 5 and len(probs) >= 3:
        mean = sum(probs) / len(probs)
        quality = math.exp(min(0., mean)) * (1 - empty)
        recognition = {"score": max(1, min(10, round(1 + 9 * quality))),
                       "samples": len(selected), "empty_rate": empty, "mean_log_prob": mean}
    return {"recognition": recognition, "command_matching": matching}


def comparison_context(app, name, sample_rate=None, *, params=None):
    """Snapshot accuracy settings without persisting custom prompt contents."""
    from samsara import __version__
    from samsara.config_defaults import DEFAULTS
    from samsara.command_clip_trim import build_params
    from samsara.speech_presence import PROBABILITY, MIN_RUN_S
    config = getattr(app, "config", {})
    if params is None:
        builder = getattr(app, "get_transcription_params", None)
        params = build_params(app, is_command=False) if callable(builder) else {}
    from samsara.decode_recovery import _params
    params = _params(app, params, None)
    settings = dict(params)
    for key in ("initial_prompt", "hotwords", "prefix"):
        if key in settings:
            text = settings.pop(key)
            settings[key + "_sha256"] = hashlib.sha256(str(text or "").encode()).hexdigest()
    # Bound-method globals expose the actual admission constants without
    # importing the application module (which would start application state).
    gate = getattr(getattr(app, "_buffer_should_skip_decode", None), "__func__", None)
    globals_ = getattr(gate, "__globals__", {})
    thresholds = {key: globals_.get(key) for key in (
        "_GATE_MAX_BUFFER_S", "_GATE_MIN_CONTIG_MS", "_SANITY_RMS_FLOOR_DB", "_GATE_VAD_PROB")}
    thresholds.update(speech_presence=config.get("dictation", {}).get(
        "speech_presence_gate", DEFAULTS["dictation.speech_presence_gate"]),
        presence_probability=PROBABILITY, presence_min_run_s=MIN_RUN_S,
        vad_available=bool(getattr(app, "_vad_available", False)), head_grace_ms=0,
        minimum_audio_s=.51)
    return {"schema": 1, "model_name": config.get("model_size"),
            "compute_device": getattr(app, "device_type", None),
            "compute_type": getattr(app, "compute_type", None),
            "decode_settings": settings, "admission": thresholds,
            "recovery_policy": {"path": "ordinary-hold-bounded-retry", "chunk_s": 5,
                                "cpu_token_budget": "min(192, max(32, int(audio_s * 12 + 24)))"},
            "input_device_name": name, "input_sample_rate": sample_rate,
            "model_sample_rate": getattr(app, "model_rate", None),
            "samsara_version": __version__}


def context_difference(result, current):
    context = result.get("context")
    if not context or any(context.get(key) is None for key in (
            "model_name", "compute_device", "input_sample_rate", "model_sample_rate")):
        return "Comparison context unavailable; compatibility unknown."
    if result.get("context_changed"):
        return "Settings changed during this test; repeat before comparing."
    # Device identity is the variable being compared, not a settings mismatch.
    keys = (context.keys() | current.keys()) - {"input_device_name", "input_sample_rate"}
    changed = [key.replace("_", " ") for key in sorted(keys) if context.get(key) != current.get(key)]
    rate = current.get("input_sample_rate")
    if rate is not None and context["input_sample_rate"] != rate:
        changed.append("input sample rate")
    if changed:
        return "Different comparison context: " + ", ".join(changed) + "."
    return "Current input sample rate unavailable; compatibility unknown." if rate is None else ""


def rank_results(results, estimates):
    """Only recognition estimates order mic choices; router/WER never do."""
    def key(row):
        recognition = (estimates.get(row["device_name"]) or {}).get("recognition")
        return (-(recognition["score"] if recognition else 0), row["device_name"], row["date"])
    return sorted(results, key=key)


def diagnostic_lines(estimate):
    estimate = estimate or {}
    recognition, matching = estimate.get("recognition"), estimate.get("command_matching")
    audio = (f"{recognition['score']} / 10 ({recognition['samples']} recent attempts)"
             if recognition else "not enough data")
    router = (f"{matching['match_rate']:.0%} matched ({matching['samples']} commands)"
              if matching else "no recent commands")
    return f"Recognition (decoder confidence estimate): {audio}\nCommand matching (router, not about the mic): {router}"


class ResultStore:
    def __init__(self, path=None):
        if path is None:
            from samsara.paths import samsara_home_dir
            path = samsara_home_dir() / "mic_scores.json"
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return []
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("Saved mic scores could not be read.")
        return sorted(rows, key=lambda r: (-r["score"], -r["accuracy"], r["date"]), reverse=False)

    def save(self, result):
        with _store_lock:
            rows = self.load()
            rows.append(result)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pending = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
            try:
                pending.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
                pending.replace(self.path)
            finally:
                pending.unlink(missing_ok=True)


class DictationTranscriber:
    """Use the loaded ordinary-hold model, params, fade and admission gates.

    Stop at raw decoder output: no correction dictionary, clean_text, number
    formatting, smart corrections, history write or command dispatch.
    """
    def __init__(self, app):
        self.app = app
        self.last_context = None

    def __call__(self, audio):
        from copy import copy
        from samsara.audio_tools import _fade_edges
        from samsara.command_clip_trim import build_params
        from samsara.decode_recovery import decode_hold
        from samsara.speech_presence import skip_decode
        # The decoder may retain failed audio for "try again". Keep that test
        # state on a shallow facade while sharing the loaded model and lock.
        app = copy(self.app)
        if getattr(app, "model", None) is None:
            raise RuntimeError("The dictation model is still loading. Try again shortly.")
        rate = app.model_rate
        params = build_params(app, is_command=False)
        self.last_context = comparison_context(self.app, "", params=params)
        pcm = _fade_edges(audio, rate)
        duration = len(pcm) / rate
        gate = app._buffer_should_skip_decode(pcm, rate, head_grace_ms=0)
        if duration < .51 or gate or skip_decode(pcm, app, path="batch", existing_gate=gate):
            return "", None
        decoded = decode_hold(app, pcm, params, duration)
        self.last_context = comparison_context(self.app, "", params=params)
        return decoded.text, segment_log_prob(decoded.seg_list)


class RingSource:
    def __init__(self, engine):
        self.engine = engine
        self.name = getattr(engine, "capture_name", None)
        self.sample_rate = getattr(engine, "_native_rate", None)
        self.reader = engine.register_consumer("mic-score-" + uuid.uuid4().hex)

    def pull(self):
        from samsara.audio_engine.guide_capture import drain_float
        if getattr(self.engine, "capture_name", None) != self.name:
            raise RuntimeError("The microphone changed during the test. Please start again.")
        before = self.reader.overrun_count
        block = drain_float(self.reader)
        if self.reader.overrun_count != before:
            raise RuntimeError("Microphone audio was lost. Please repeat this sentence.")
        return block

    def close(self):
        self.engine.unregister_consumer(self.reader)


class StreamSource:
    def __init__(self, device):
        import sounddevice as sd
        from samsara.audio_devices import detect_capture_rate, get_device_info
        from samsara.audio_engine.wake_prefilter import NativeRateFrames
        converter = NativeRateFrames(detect_capture_rate(device["id"]))
        self.sample_rate = converter.native_rate
        self.lock, self.parts, self.error = threading.Lock(), [], False

        def callback(data, frames, timing, status):
            with self.lock:
                self.error = self.error or bool(status)
                self.parts.append(converter.convert(np.asarray(data)[:, 0]).copy())
                if len(self.parts) > 200:
                    self.error = True
                    self.parts = self.parts[-200:]

        channels = max(1, int(get_device_info(device["id"], "input").get("max_input_channels", 1)))
        self.stream = None
        for count in dict.fromkeys((channels, 1)):
            candidate = None
            try:
                candidate = sd.InputStream(device=device["id"], samplerate=converter.native_rate,
                                           channels=count, dtype=np.float32,
                                           blocksize=converter.blocksize, callback=callback)
                candidate.start()
                self.stream = candidate
                break
            except Exception:
                if candidate is not None:
                    candidate.close()
                if count == 1:
                    raise

    def pull(self):
        with self.lock:
            parts, self.parts = self.parts, []
            error, self.error = self.error, False
        if error:
            raise RuntimeError("Microphone audio was interrupted. Please repeat this sentence.")
        return np.concatenate(parts) if parts else np.empty(0, np.float32)

    def close(self):
        try:
            self.stream.stop()
        finally:
            self.stream.close()

    def reset(self):
        with self.lock:
            self.parts, self.error = [], False


def open_source(app, device):
    from samsara.audio_engine.guide_capture import running_engine
    engine = running_engine(app)
    if engine is not None and device["name"] == microphone_name(app):
        return RingSource(engine)
    return StreamSource(device)


class ComparisonRun:
    """Open all devices together, then explicitly retry incompatible ones alone."""
    def __init__(self, app, devices, *, source_factory=None, transcriber=None, store=None):
        self.app, self.devices = app, list(devices)
        self.factory = source_factory or (lambda d: open_source(app, d))
        self.transcribe = transcriber or DictationTranscriber(app)
        self.store = store or ResultStore()
        self.sources, self.fallback, self.failures, self.takes = {}, [], {}, {}
        self.sequential = False
        self.completed = []

    def prepare(self):
        for device in self.devices:
            try:
                self.sources[device["name"]] = self.factory(device)
            except Exception:
                self.fallback.append(device)
        if not self.sources:
            return self.next_group()
        return True

    def close(self):
        sources, self.sources = self.sources, {}
        for source in sources.values():
            try:
                source.close()
            except Exception:
                pass

    def next_group(self):
        self.close()
        self.sequential = True
        while self.fallback:
            device = self.fallback.pop(0)
            try:
                self.sources[device["name"]] = self.factory(device)
                return True
            except Exception:
                self.failures[device["name"]] = "Could not open this microphone, even on its own."
        return False

    def record(self, done, cancel, listening, *, clock=time.monotonic, sleep=time.sleep):
        quiet = {name: [] for name in self.sources}
        speech = {name: [] for name in self.sources}
        for source in self.sources.values():
            if hasattr(source, "reset"):
                source.reset()
            else:
                source.pull()  # Discard old audio; the next second measures the room.
        start, announced = clock(), False
        while clock() - start < 16:
            if cancel.is_set():
                raise InterruptedError("Mic test cancelled.")
            elapsed = clock() - start
            if elapsed >= 1 and not announced:
                announced = True
                listening()
            for name, source in self.sources.items():
                block = source.pull()
                (quiet if elapsed < 1 else speech)[name].append(block)
            if announced and done.is_set():
                break
            sleep(.02)
        return {name: (np.concatenate(speech[name]) if speech[name] else np.empty(0, np.float32),
                       np.concatenate(quiet[name]) if quiet[name] else np.empty(0, np.float32))
                for name in self.sources}

    def score_take(self, sentence_index, recordings, cancel=None):
        for name, (audio, quiet) in recordings.items():
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Mic test cancelled.")
            signals = signal_summary(audio, quiet)
            heard, confidence = self.transcribe(audio)
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Mic test cancelled.")
            said = SENTENCES[sentence_index]
            context = getattr(self.transcribe, "last_context", None)
            context = dict(context) if context else comparison_context(self.app, name)
            context.update(input_device_name=name,
                           input_sample_rate=getattr(self.sources.get(name), "sample_rate", None))
            item = dict(word_accuracy(said, heard), said=said, heard=heard,
                        log_prob=confidence, context=context, **signals)
            takes = self.takes.setdefault(name, [])
            takes.append(item)
            if len(takes) == len(SENTENCES):
                accuracy = max(0., 1 - sum(t["errors"] for t in takes) / sum(t["words"] for t in takes))
                result = dict(device_name=name, date=datetime.now(timezone.utc).isoformat(),
                              score=score_accuracy(accuracy), accuracy=accuracy,
                              measurement="read_aloud_word_accuracy",
                              decoder_confidence={"mean_log_prob": float(np.mean([
                                  t["log_prob"] for t in takes if t["log_prob"] is not None]))}
                                  if any(t["log_prob"] is not None for t in takes) else None,
                              context=takes[0]["context"],
                              context_changed=any(t["context"] != takes[0]["context"] for t in takes),
                              sequential=self.sequential, takes=takes)
                self.store.save(result)
                self.completed.append(result)


def use_microphone(app, name):
    devices = app.get_available_microphones()
    match = next((d for d in devices if d["name"] == name), None)
    if match is None:
        raise RuntimeError("That microphone is disconnected. Reconnect it and try again.")
    if getattr(app, "recording", False):
        raise RuntimeError("Finish your current recording before switching microphones.")
    app.available_mics = devices
    app.switch_microphone(match["id"])


def current_estimate(app):
    manager = getattr(app, "history_db", None)
    name = microphone_name(app)
    return everyday_estimate(manager.recent_for_microphone(name), name) if manager and name else None


def current_read_aloud(app):
    name = microphone_name(app)
    try:
        rows = [r for r in ResultStore().load() if r["device_name"] == name]
        return max(rows, key=lambda r: r["date"], default=None)
    except (OSError, ValueError, KeyError):
        return None
