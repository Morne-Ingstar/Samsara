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
    from samsara.transcription_params import get_transcription_params
    from samsara.formatting_tokens import apply_formatting_tokens_if_enabled
    from samsara import config_defaults
    from samsara.constants import HOLD_RELEASE_TAIL_SPEECH_THRESHOLD
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
    app.process_transcription = lambda text: process_transcription(app, text)
    app._apply_formatting_tokens = lambda text: apply_formatting_tokens_if_enabled(
        text, settings.get("formatting_tokens", {}).get("enabled", True))
    app.get_transcription_params = lambda **kw: get_transcription_params(
        settings, language=settings.get("language", "en"), initial_prompt=settings.get("initial_prompt"),
        no_speech_threshold=.6, log_prob_threshold=-1.)
    bus = FrameBus()
    consumer = DictationSessionConsumer(NS(register_consumer=bus.new_reader, _overflow_count=0), app)
    app._dictation_consumer = consumer
    app._speech_presence_capture = HoldEvidence(time.perf_counter())
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
    needed = max(len(pcm), release_frame * FRAME_SIZE + round(1.2 * RATE))
    padded = np.pad(pcm, (0, int(np.ceil(needed / FRAME_SIZE)) * FRAME_SIZE - len(pcm)))
    tail_thread = None
    tail_errors = []
    release_time = None
    preview_count = 0
    def drained():
        deadline = time.monotonic() + 3
        while consumer._reader._read_cursor < bus.write_cursor:
            if time.monotonic() >= deadline:
                raise RuntimeError("Replay capture drain timed out")
            time.sleep(.002)
    def tail():
        try:
            result = consumer.wait_for_streaming_release_tail(
                silence_ms=300, max_tail_ms=1200, speech_threshold=HOLD_RELEASE_TAIL_SPEECH_THRESHOLD,
                should_abort=session.cancel_event.is_set)
            session._release_tail_signals = dict(result, speech_floor=HOLD_RELEASE_TAIL_SPEECH_THRESHOLD)
            consumer.finish_capture(force=True)
        except Exception as error:
            tail_errors.append(error)
    try:
        if not consumer.activate_streaming():
            raise RuntimeError("Replay capture did not activate")
        next_preview = 13.
        for index, start in enumerate(range(0, len(padded), FRAME_SIZE)):
            if index == release_frame:
                drained()
                release_time = time.perf_counter()
                tail_thread = threading.Thread(target=tail, name="corpus-release-tail")
                tail_thread.start()
                time.sleep(.015)
            if tail_thread is not None and not tail_thread.is_alive():
                break
            bus.write(padded[start:start + FRAME_SIZE], time.perf_counter(), 0)
            if tail_thread is not None:
                # Tail policy is a real clock boundary, so deliver at device pace.
                time.sleep(FRAME_SIZE / RATE)
            elif index % 20 == 19:
                drained()
            if partials and tail_thread is None and start / RATE >= next_preview:
                drained()
                preview = worker._transcribe_partial()
                if preview:
                    session.on_partial(preview)
                    preview_count += 1
                next_preview += 13.
        if tail_thread:
            tail_thread.join(3)
            if tail_thread.is_alive() or tail_errors:
                raise RuntimeError("Production release tail did not complete")
        if release_time is None:
            raise RuntimeError("Release boundary was not replayed")
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
        expected = padded[:len(captured)] if captured is not None else np.array([], dtype="<i2")
        preserved = captured is not None and np.allclose(captured, expected.astype(np.float32) / 32767, atol=1e-7)
        if not preserved:
            raise RuntimeError("Replay changed captured PCM")
        text = "".join(delivered).strip()
        raw = finals[-1][1] if finals else ""
        return dict(text=text, raw=raw, release_to_text_ms=(time.perf_counter() - release_time) * 1000,
                    captured_samples=len(captured), pcm_preserved=preserved, partials=preview_count,
                    tail=session._release_tail_signals,
                    final_state=session._state, delivery_count=len(delivered),
                    decode_failed=bool(getattr(session, "_decode_failure", None)),
                    held_keep_routed=not delivered and getattr(getattr(app, "_kept_dictations", None), "armed", False))
    finally:
        session.cancel_event.set()
        if tail_thread:
            tail_thread.join(3)
        consumer.stop_streaming()
        consumer.cancel()
        store = getattr(app, "_kept_dictations", None)
        if store is not None:
            store.flush()
