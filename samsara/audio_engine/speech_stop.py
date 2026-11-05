"""Bounded local recognition of speech-stop phrases on the existing mic bus.

No new microphone, model, network, command dispatch or transcript logging.
The normal capture path remains half duplex. This path can only stop audio.
"""
from __future__ import annotations

from collections import deque
import logging
import threading
import time

import numpy as np

from samsara.runtime import thread_registry
from samsara.tts.speech_control import STOP_PHRASES, normalize, speech_token, stop_speaking
from .frame import FRAME_MS, SAMPLE_RATE

WINDOW_FRAMES = 2000 // FRAME_MS
STEP_FRAMES = 400 // FRAME_MS
MIN_FRAMES = 600 // FRAME_MS
MAX_RESULT_AGE_S = 2.0


def dictation_owns_words(app):
    """A held mic or a dictation lane always owns its words, including stop."""
    if any(getattr(app, flag, False) is True for flag in
           ("_hotkey_recording", "recording")):
        return True
    if getattr(app, "app_state", "") in ("quick_dictation", "long_dictation", "wake_session"):
        return True
    manager = getattr(app, "_session_mode_manager", None)
    mode = getattr(getattr(manager, "mode", None), "value", "")
    if getattr(app, "command_mode_active", False) and mode == "dictate":
        return True
    return False


def eligible(app):
    if dictation_owns_words(app) or any(getattr(app, flag, False) is True
                                       for flag in ("snoozed", "_shutting_down")):
        return False
    return (getattr(app, "wake_word_active", False) is True
            or (getattr(app, "command_mode_active", False) is True
                and app.config.get("command_mode", {}).get("mode") == "toggle"))


class SpeechStopListener:
    def __init__(self, app):
        self.app = app
        self.frames = deque(maxlen=WINDOW_FRAMES)
        self.context = None
        self.serial = 0
        self.last_seq = None
        self.step = 0
        self.busy = threading.Lock()
        self.accepted = threading.Event()
        self.quiet_until = 0.0

    def consume(self, frame):
        """Observe copied PCM; never wait for decoding on the capture thread."""
        now = time.perf_counter()
        token = speech_token(self.app) if eligible(self.app) else None
        fresh = 0 <= now - getattr(frame, "t_capture", now) < .5
        context = (token, frame.device_epoch) if token and fresh else None
        gap = self.last_seq is not None and frame.seq != self.last_seq + 1
        if context != self.context or gap or context is None:
            self.frames.clear()
            self.step = 0
            self.serial += 1
        self.context = context
        self.last_seq = frame.seq
        if context is None:
            return
        self.frames.append(frame.pcm.astype(np.float32) / 32768.0)
        self.step += 1
        if (len(self.frames) < MIN_FRAMES or self.step < STEP_FRAMES
                or getattr(self.app, "model", None) is None):
            return
        self.step = 0
        audio = np.concatenate(self.frames)
        if float(np.sqrt(np.mean(audio ** 2))) < .008 or not self.busy.acquire(False):
            return
        serial = self.serial
        try:
            thread_registry.spawn("speech-stop", lambda: self._decode(audio, context, serial, now), daemon=True)
        except Exception:
            self.busy.release()

    def _current(self, context, serial, captured):
        return (eligible(self.app) and self.context == context and self.serial == serial
                and speech_token(self.app) == context[0]
                and time.perf_counter() - captured <= MAX_RESULT_AGE_S)

    def _decode(self, audio, context, serial, captured):
        try:
            model = getattr(self.app, "model", None)
            lock = getattr(self.app, "model_lock", None)
            if model is None or lock is None or not self._current(context, serial, captured):
                return
            if not lock.acquire(False):
                return  # Never queue ahead of dictation or wait on a large decode.
            try:
                segments, _ = model.transcribe(
                    audio, language="en", beam_size=1, temperature=0,
                    condition_on_previous_text=False, vad_filter=False,
                    initial_prompt=None, word_timestamps=False,
                )
                segments = list(segments)
            finally:
                lock.release()
            if not segments or any(s.no_speech_prob > .4 or s.avg_logprob < -.7 for s in segments):
                return
            # Wait for a word ending, not a partial 'stop' at the start of a sentence.
            if segments[-1].end > len(audio) / SAMPLE_RATE - .15:
                return
            phrase = normalize(" ".join(s.text for s in segments))
            if phrase not in STOP_PHRASES or not self._current(context, serial, captured):
                return
            coordinator = self.app.audio_coordinator
            # Without acoustic echo cancellation, a phrase quoted in the current
            # readout is ambiguous. Prefer the visible Stop over self-cancelling.
            if any(f" {phrase} " in f" {normalize(text)} "
                   for text in coordinator._speech_echo_texts):
                return
            if stop_speaking(self.app, expected=context[0]):
                self.quiet_until = time.perf_counter() + .35
                self.accepted.set()
        except Exception:
            # A failed optional decode cannot kill the wake consumer or log audio.
            return
        finally:
            self.busy.release()


def observe(consumer, frame):
    """Reset changed-device capture before observing speech-stop input."""
    app = consumer._app
    # Epoch-change detection: abort utterance and reset state
    if consumer._last_epoch is not None and frame.device_epoch != consumer._last_epoch:
        consumer._log_frame(logging.WARNING, 'frame_562', "[ACE] Wake path: epoch change — aborting utterance, resetting state")
        consumer._utterance_frames   = []
        consumer._buffer_rms_history = []
        consumer._last_voice_ns = None
        consumer._speech_start_ns = None
        consumer._onset_context = None
        app.is_speaking   = False
        app.silence_start = None
        consumer._close_hands_free_duck_safe(app, consumer._hands_free_capture_duck_token)
        consumer._hands_free_capture_duck_token = None
        try:
            app._vad_reset()
        except Exception as e:
            consumer._log_frame(logging.DEBUG, 'frame_572', f"_vad_reset failed after epoch change: {e}")
    consumer._last_epoch = frame.device_epoch

    listener = getattr(consumer, "_speech_stop_listener", None)
    if listener is None:
        listener = consumer._speech_stop_listener = SpeechStopListener(consumer._app)
    listener.consume(frame)
    if listener.accepted.is_set():
        listener.accepted.clear()
        if eligible(consumer._app):
            consumer.abort_utterance()  # Only the wake lane's contaminated capture.
    return eligible(consumer._app) and time.perf_counter() < listener.quiet_until
