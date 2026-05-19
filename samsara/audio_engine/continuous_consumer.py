"""ContinuousConsumer — ACE-04A: ring consumer for continuous dictation mode.

Replaces continuous_audio_callback + the per-mode PortAudio InputStream
in start_continuous_mode with a single long-lived ring consumer.

Policy is unchanged from continuous_audio_callback:
  - RMS gate for speech detection (configurable threshold).
  - No prebuffer rewind — continuous mode has no pre-trigger window.
  - Echo cancellation applied (AEC output used for Whisper, same as old callback).
  - Silence timeout flushes accumulated frames to transcribe_continuous_buffer.

Thread model: single daemon thread polling the ring at ~5ms intervals.
Flushed utterances are dispatched to transcribe_continuous_buffer on
separate daemon threads (same as the old callback).
"""

import threading
import time

import numpy as np

from .frame import FRAME_MS, SAMPLE_RATE
from .ring import EMPTY

from samsara.constants import (
    DEFAULT_MIN_SPEECH_DURATION,
    DEFAULT_SILENCE_TIMEOUT,
    DEFAULT_SPEECH_THRESHOLD,
)


class ContinuousConsumer:
    """Polls the ACE ring for speech and dispatches utterances.

    Args:
        engine: AudioCaptureEngine — the sole ring writer.
        app:    DictationApp — provides config, echo_canceller, and
                transcribe_continuous_buffer().
    """

    def __init__(self, engine, app) -> None:
        self._engine = engine
        self._app    = app
        self._reader = engine.register_consumer("continuous")
        self._running = False
        self._thread: threading.Thread | None = None

        # Per-utterance state — local, not shared with WakeConsumer
        self._speech_frames: list = []   # float32 arrays at SAMPLE_RATE
        self._is_speaking   = False
        self._silence_start: float | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin consuming ring frames. Idempotent."""
        if self._running:
            return
        self._speech_frames = []
        self._is_speaking   = False
        self._silence_start = None
        self._running = True
        # Snap to current write head — skip stale ring history
        self._reader._read_cursor = self._engine._ring.write_cursor
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="continuous-consumer"
        )
        self._thread.start()

    def stop(self) -> list:
        """Stop polling and return any accumulated speech frames for final flush."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        remaining = list(self._speech_frames)
        self._speech_frames = []
        self._is_speaking   = False
        self._silence_start = None
        return remaining

    def deactivate(self) -> None:
        """Stop and unregister from the engine at app shutdown."""
        self.stop()
        try:
            self._engine.unregister_consumer(self._reader)
        except Exception:
            pass

    # ── Poll loop ─────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        app = self._app
        while self._running:
            if not app.continuous_active:
                time.sleep(0.005)
                continue

            frame = self._reader.read_next()
            if frame is EMPTY:
                time.sleep(0.005)
                continue

            self._process_frame(frame)

    def _process_frame(self, frame) -> None:
        app = self._app

        # Convert int16 ring frame → float32
        pcm_f32 = frame.pcm.astype(np.float32) / 32767.0

        # Echo cancellation (AEC output used for Whisper — same as legacy callback)
        echo_canceller = getattr(app, 'echo_canceller', None)
        if echo_canceller and echo_canceller.is_active:
            audio_chunk = echo_canceller.process(pcm_f32.reshape(-1, 1)).flatten()
        else:
            audio_chunk = pcm_f32

        # RMS energy for speech detection
        rms = float(np.sqrt(np.mean(audio_chunk ** 2)))

        # Thresholds (same config keys as the legacy continuous_audio_callback)
        speech_threshold  = app.config.get('continuous_speech_threshold', DEFAULT_SPEECH_THRESHOLD)
        silence_threshold = app.config.get('silence_threshold', DEFAULT_SILENCE_TIMEOUT)
        min_speech        = app.config.get('min_speech_duration', DEFAULT_MIN_SPEECH_DURATION)

        if rms > speech_threshold:
            self._is_speaking   = True
            self._silence_start = None
            self._speech_frames.append(audio_chunk)
        else:
            if self._is_speaking:
                self._speech_frames.append(audio_chunk)

                if self._silence_start is None:
                    self._silence_start = time.time()
                elif time.time() - self._silence_start >= silence_threshold:
                    speech_duration = len(self._speech_frames) * (FRAME_MS / 1000.0)
                    if speech_duration >= min_speech:
                        buffer_copy = list(self._speech_frames)
                    else:
                        buffer_copy = None
                    self._speech_frames = []
                    self._is_speaking   = False
                    self._silence_start = None

                    if buffer_copy is not None:
                        threading.Thread(
                            target=app.transcribe_continuous_buffer,
                            args=(buffer_copy, SAMPLE_RATE),
                            daemon=True,
                        ).start()

    def __repr__(self) -> str:
        return (
            f"ContinuousConsumer(running={self._running}, "
            f"speaking={self._is_speaking}, "
            f"frames={len(self._speech_frames)})"
        )
