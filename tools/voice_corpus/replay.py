"""Offline FrameBus -> capture/tail -> StreamingWorker -> fake paste receipt.

Adapted from tools.hold_e2e_bench and the 1168 native capture rig. No
DictationApp import, application bootstrap, audio device, clipboard or hooks.
"""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace as NS

import numpy as np

from . import RATE
from .storage import external_path


def isolate(folder):
    """Call before production imports; never inspect the owner's profile."""
    home = external_path(folder)
    home.mkdir(parents=True, exist_ok=True)
    for name in ("HOME", "USERPROFILE", "SAMSARA_HOME_DIR", "APPDATA", "LOCALAPPDATA"):
        os.environ[name] = str(home)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", QT_QPA_PLATFORM="offscreen")
    return home


def build_fingerprint():
    root = Path(__file__).resolve().parents[2]
    hasher = hashlib.sha256()
    for path in sorted([root / "dictation.py", *root.joinpath("samsara").rglob("*.py"),
                        *Path(__file__).parent.glob("*.py")]):
        hasher.update(path.relative_to(root).as_posix().encode())
        hasher.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return hasher.hexdigest()


class Overlay:
    def __init__(self):
        self.text = ""

    def update_text(self, text, *args, **kwargs):
        self.text = text

    def flash_done_and_fade(self, callback):
        callback()

    def close(self):
        pass


class Dictionary:
    custom_vocab = ()
    _builtin_preferences = NS(disabled=set())

    def __init__(self, corrections=None):
        self.corrections = corrections or {}

    def get_initial_prompt(self, **kwargs):
        return None

    def apply_corrections(self, text):
        return text


def process_transcription(app, text):
    """Use the facade's pure transforms; no DictationApp bootstrap required."""
    import re
    app._skip_cleanup = False
    if not text:
        return text
    if app.config.get("enable_case_formatters", False):
        from samsara.formatters import apply_case_formatter
        formatted = apply_case_formatter(text)
        if formatted is not None:
            app._skip_cleanup = True
            return formatted
    if app.config.get("format_numbers", True):
        from samsara.number_format import format_spoken_numbers
        text = format_spoken_numbers(text)
    if app.config.get("auto_capitalize", True):
        text = text[:1].upper() + text[1:]
        text = re.sub(r"([.!?]\s+)([a-z])", lambda m: m[1] + m[2].upper(), text)
    return text


def run_take(pcm, release_s, model, config=None, *, partials=True):
    if not os.environ.get("SAMSARA_HOME_DIR"):
        raise ValueError("Replay requires an explicitly isolated profile")
    external_path(os.environ["SAMSARA_HOME_DIR"])
    from samsara.streaming import StreamingSession, StreamingWorker
    from samsara.audio_engine import FrameBus, DictationSessionConsumer, FRAME_SIZE
    from samsara.audio_engine.frame import FRAME_MS
    from samsara.transcription_params import get_transcription_params
    from samsara.formatting_tokens import apply_formatting_tokens_if_enabled
    from samsara import config_defaults
    from samsara.constants import HOLD_RELEASE_TAIL_SPEECH_THRESHOLD
    from samsara.mic_silence_watch import SilenceWatch
    from samsara.speech_presence import HoldEvidence

    settings = dict(config_defaults.DEFAULTS)
    settings.update(copy.deepcopy(config or {}))
    if settings.get("smart_corrections", {}).get("modes", {}).get("streaming", False):
        raise ValueError("Offline corpus replay requires streaming smart corrections disabled")
    settings.update(mode="hold", auto_paste=True, streaming_direct_paste=False,
                    diagnostics={"write_jsonl": False}, kept_dictations={"enabled": False},
                    benchmark={"enabled": False})
    delivered, history, finals = [], [], []
    def paste(text, **kwargs):
        delivered.append(text)
        return NS(sent=True, confirmed=True, text=text)
    app = NS(config=settings, model=model, model_rate=RATE,
             device_type=settings.get("device", "cpu"), model_lock=threading.Lock(),
             dictionary_store=Dictionary(), recording=True, _hotkey_recording=True,
             _hold_down_mouse=True, _main_hotkey_source="mouse", _stop_in_flight=False,
             _show_outcome_chip=lambda *a, **k: None, add_to_history=lambda *a, **k: None,
             _notify_main_window=lambda *a: None, _get_foreground_app=lambda: "Corpus fake target",
             _schedule_ui=lambda fn, *a: fn(*a),
             _on_streaming_session_finished=lambda *a: None, _paste_preserving_clipboard=paste,
             _log_history=lambda **row: history.append(row))
    app._mic_silence_watch_1252 = SilenceWatch(app, deliver=lambda *_: None)
    app.process_transcription = lambda text: process_transcription(app, text)
    app._apply_formatting_tokens = lambda text: apply_formatting_tokens_if_enabled(
        text, settings.get("formatting_tokens", {}).get("enabled", True))
    app.get_transcription_params = lambda **kw: get_transcription_params(
        settings, language=settings.get("language", "en"), initial_prompt=settings.get("initial_prompt"),
        no_speech_threshold=.6, log_prob_threshold=-1.)

    class ReplayConsumer(DictationSessionConsumer):
        # Keep the production threshold, cap and audit path. Only the wait
        # loop uses the replay clock, scoped to this consumer.
        def _wait_for_release_quiet(self, next_rms, threshold, silence_ms, max_tail_ms,
                                    should_abort=None):
            result = dict(reason="max", elapsed_ms=max_tail_ms, frames=0,
                          threshold=threshold, last_frame_rms=0.)
            if max_tail_ms <= 0:
                return result
            # The first pending block arrives at release. A block arriving
            # at the cutoff is committed before capture closes.
            quiet_ms = 0
            for at_ms in range(0, max_tail_ms + 1, FRAME_MS):
                if should_abort is not None and should_abort():
                    result.update(reason="abort", elapsed_ms=at_ms)
                    break
                feed_frame()
                drained()
                last_rms, = next_rms()
                result.update(frames=result["frames"] + 1, last_frame_rms=last_rms)
                quiet_ms = quiet_ms + FRAME_MS if last_rms < threshold else 0
                if quiet_ms >= silence_ms:
                    result.update(reason="silence", elapsed_ms=at_ms)
                    break
            return result

    bus = FrameBus()
    consumer = ReplayConsumer(NS(register_consumer=bus.new_reader, _overflow_count=0), app)
    app._dictation_consumer = consumer
    session = StreamingSession.__new__(StreamingSession)
    session.app, session.start_time = app, time.time()
    session.stop_event, session.cancel_event = threading.Event(), threading.Event()
    session._state, session._state_lock = session.STATE_STREAMING, threading.Lock()
    session._capture_cleanup_lock = threading.Lock()
    session._capture_cleaned = session._finished_notified = False
    session._direct_paste = session._last_pasted = session._parking_enabled = False
    session._tutorial_owner, session._last_partial = None, ""
    session._overlay = Overlay()
    app._streaming_session = session
    def observe_final(final_text, raw_text=None, duration_s=0., elapsed_ms=0):
        finals.append((final_text or "", raw_text if raw_text is not None else final_text or ""))
        return StreamingSession.on_final(session, final_text, raw_text, duration_s, elapsed_ms)
    session.on_final = observe_final
    worker = StreamingWorker(session)
    session._worker = worker
    pcm = np.asarray(pcm, dtype="<i2").reshape(-1)
    # Missing post-release samples are silence; never truncate a supplied tail.
    release_frame = int(np.ceil(max(0, release_s) * RATE / FRAME_SIZE))
    if release_frame * FRAME_SIZE > len(pcm) + FRAME_SIZE:
        raise ValueError("Release boundary is outside the input")
    silence_ms = int(settings.get("recording_tail_silence_ms", 300))
    max_tail_ms = int(settings.get("recording_tail_max_ms", 1200))
    speech_floor = float(settings.get("recording_tail_speech_threshold", HOLD_RELEASE_TAIL_SPEECH_THRESHOLD))
    needed = max(len(pcm), release_frame * FRAME_SIZE)
    padded = np.pad(pcm, (0, int(np.ceil(needed / FRAME_SIZE)) * FRAME_SIZE - len(pcm)))
    silence = np.zeros(FRAME_SIZE, dtype="<i2")
    # Timestamp the PCM on its sample timeline, ending before the live
    # perf_counter cutoff used by finish_capture().
    capture_span = (release_frame + 1) * FRAME_SIZE / RATE + max(0, silence_ms, max_tail_ms) / 1000
    capture_start = time.perf_counter() - capture_span
    app._speech_presence_capture = HoldEvidence(capture_start)
    preview_count = 0

    def drained():
        deadline = time.monotonic() + 3
        while True:
            with consumer._streaming_lock:
                if len(consumer._streaming_frames) == bus.write_cursor:
                    return
            if time.monotonic() >= deadline:
                raise RuntimeError("Replay capture drain timed out")
            time.sleep(.002)

    def feed_frame():
        index = bus.write_cursor
        start = index * FRAME_SIZE
        block = padded[start:start + FRAME_SIZE] if start < len(padded) else silence
        bus.write(block, capture_start + (index + 1) * FRAME_SIZE / RATE, 0)

    try:
        if not consumer.activate_streaming():
            raise RuntimeError("Replay capture did not activate")
        next_preview = 13.
        for index in range(release_frame):
            feed_frame()
            if index % 20 == 19:
                drained()
            if partials and index * FRAME_SIZE / RATE >= next_preview:
                drained()
                preview = worker._transcribe_partial()
                if preview:
                    session.on_partial(preview)
                    preview_count += 1
                next_preview += 13.
        drained()
        tail = consumer.wait_for_streaming_release_tail(
            silence_ms=silence_ms, max_tail_ms=max_tail_ms, speech_threshold=speech_floor,
            should_abort=session.cancel_event.is_set)
        session._release_tail_signals = dict(tail, speech_floor=speech_floor)
        consumer.finish_capture(force=True)
        final_started = time.perf_counter()
        session.finalize()
        worker._final_pass()
        job = getattr(app, "_hold_decode_job", None)
        if job is not None and not job.done.wait(60):
            raise RuntimeError("Offline model did not settle; stop this replay")
        if session._state != session.STATE_DONE:
            raise RuntimeError("Production delivery did not finish")
        if consumer.capture_loss:
            raise RuntimeError("Replay lost PCM frames")
        captured = getattr(session, "_stopped_audio_1042", None)
        expected_samples = bus.write_cursor * FRAME_SIZE
        expected = pcm[:expected_samples].astype(np.float32) / 32767
        preserved = (captured is not None and len(captured) == expected_samples
                     and np.array_equal(captured[:len(expected)], expected)
                     and not np.any(captured[len(expected):]))
        if not preserved:
            raise RuntimeError("Replay changed captured PCM")
        text = "".join(delivered).strip()
        raw = finals[-1][1] if finals else ""
        return dict(text=text, raw=raw,
                    release_to_text_ms=tail["elapsed_ms"] + (time.perf_counter() - final_started) * 1000,
                    captured_samples=len(captured), pcm_preserved=preserved, partials=preview_count,
                    tail=session._release_tail_signals,
                    final_state=session._state, delivery_count=len(delivered),
                    decode_failed=bool(getattr(session, "_decode_failure", None)),
                    held_keep_routed=not delivered and getattr(getattr(app, "_kept_dictations", None), "armed", False))
    finally:
        session.cancel_event.set()
        consumer.stop_streaming()
        consumer.cancel()
        store = getattr(app, "_kept_dictations", None)
        if store is not None:
            store.flush()
