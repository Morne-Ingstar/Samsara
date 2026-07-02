"""DebugRecorder — ACE-02: passive WAV recorder for engine verification.

Registers as a named consumer on an AudioCaptureEngine and drains frames
from the FrameBus ring into timestamped WAV files in an output directory.
Intended to run behind the `ace_debug_capture: true` config flag only —
it is a diagnostic tool, not a production pipeline component.

== MA-2: pcm copy requirement ==

Frame.pcm is a VIEW into ring memory that the writer overwrites on each
new write cycle. A consumer that retains the view will silently read
corrupted audio after the writer wraps around (~RING_SECONDS of wall time).

DebugRecorder accumulates frames for assembly into WAV files, so every
frame MUST be copied:

    self._frames.append(frame.pcm.copy())  # [MA-2] VIEW -> owned array

This is the canonical example of the MA-2 contract. Any consumer that
stores multiple frames for later assembly must follow the same pattern.

== File layout ==

    output_dir/
        rec_000001_20260517T142301.wav
        rec_000002_20260517T142335.wav
        ...

One WAV per recording session. A session starts with start_recording()
and ends with stop_recording() or when max_seconds of audio is accumulated.
The file index is a monotonic counter per DebugRecorder instance (resets
across process restarts).
"""

import os
import threading
import time
import wave
from datetime import datetime
from typing import Optional

import numpy as np

from .engine import AudioCaptureEngine
from .frame import FRAME_SIZE, SAMPLE_RATE
from .ring import EMPTY
from samsara.log import get_logger

logger = get_logger(__name__)


class DebugRecorder:
    """Accumulates audio frames from the engine ring into WAV files.

    Args:
        engine:     AudioCaptureEngine instance to consume from.
        output_dir: Directory for WAV output. Created if it does not exist.
        max_seconds: Cap on recording length. The accumulator stops adding
                     frames after this many seconds of audio and flushes to
                     disk automatically. Default 30s.
    """

    def __init__(
        self,
        engine: AudioCaptureEngine,
        output_dir: str,
        max_seconds: float = 30.0,
    ) -> None:
        self._engine     = engine
        self._output_dir = output_dir
        self._max_frames = int(max_seconds * 1000 // 100)  # FRAME_MS=100

        os.makedirs(output_dir, exist_ok=True)

        self._reader        = None
        self._frames: list  = []       # list[np.ndarray int16] — owned copies
        self._recording     = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event    = threading.Event()
        self._file_index    = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def start_recording(self) -> None:
        """Register with the engine and begin accumulating frames.

        Idempotent — calling when already recording has no effect.
        """
        if self._recording:
            return

        self._reader = self._engine.register_consumer("debug_recorder")
        self._frames = []
        self._recording = True
        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._drain_loop,
            daemon=True,
            name="debug-recorder",
        )
        self._thread.start()
        logger.info(f"[DebugRecorder] Recording started -> {self._output_dir}")

    def stop_recording(self) -> Optional[str]:
        """Stop accumulating and flush accumulated frames to a WAV file.

        Returns:
            Absolute path to the written WAV file, or None if no frames
            were accumulated (e.g., stopped immediately after starting).
        """
        if not self._recording:
            return None

        self._recording = False
        self._stop_event.set()

        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        if self._reader is not None:
            self._engine.unregister_consumer(self._reader)
            self._reader = None

        if not self._frames:
            logger.info("[DebugRecorder] No frames accumulated — no file written.")
            return None

        path = self._flush_wav()
        self._frames = []
        return path

    # ── Drain loop (consumer thread) ─────────────────────────────────────────

    def _drain_loop(self) -> None:
        """Read frames from the ring until stop is requested or max reached."""
        while self._recording and not self._stop_event.is_set():
            if self._reader is None:
                break

            frame = self._reader.read_next()
            if frame is EMPTY:
                time.sleep(0.005)   # 5ms poll — well under FRAME_MS=100ms
                continue

            # [MA-2] COPY the pcm — we retain frames for WAV assembly.
            # Frame.pcm is a VIEW into ring memory that the writer will
            # overwrite. Retaining the view would silently corrupt the
            # accumulated audio. Every accumulating consumer must copy.
            self._frames.append(frame.pcm.copy())

            if len(self._frames) >= self._max_frames:
                logger.debug(
                    f"[DebugRecorder] max_seconds reached "
                    f"({self._max_frames} frames) — auto-flushing."
                )
                path = self._flush_wav()
                self._frames = []
                logger.debug(f"[DebugRecorder] Auto-flush wrote: {path}")

    # ── WAV output ────────────────────────────────────────────────────────────

    def _flush_wav(self) -> str:
        """Concatenate accumulated frames and write to a timestamped WAV."""
        self._file_index += 1
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        filename = f"rec_{self._file_index:06d}_{ts}.wav"
        path = os.path.join(self._output_dir, filename)

        audio = np.concatenate(self._frames).astype(np.int16)

        with wave.open(path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)          # int16 = 2 bytes/sample
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())

        duration_s = len(audio) / SAMPLE_RATE
        logger.info(
            f"[DebugRecorder] Wrote {path}  "
            f"({duration_s:.1f}s, {len(self._frames)} frames)"
        )
        return path

    def __repr__(self) -> str:
        return (
            f"DebugRecorder(recording={self._recording}, "
            f"frames={len(self._frames)}, "
            f"output_dir={self._output_dir!r})"
        )
