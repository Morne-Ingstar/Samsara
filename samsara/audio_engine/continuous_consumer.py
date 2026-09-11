"""ContinuousConsumer — ACE-04A: ring consumer for continuous dictation mode.

Replaces continuous_audio_callback + the per-mode PortAudio InputStream
in start_continuous_mode with a single long-lived ring consumer.

Policy is unchanged from continuous_audio_callback when
continuous_commit_trigger == "silence" (the default):
  - RMS gate for speech detection (configurable threshold).
  - No prebuffer rewind — continuous mode has no pre-trigger window.
  - Echo cancellation applied (AEC output used for Whisper, same as old callback).
  - Silence timeout flushes accumulated frames to transcribe_continuous_buffer.

When continuous_commit_trigger == "key" (variant A, commit-as-you-go):
  - Silence never auto-commits -- it's unlimited thinking time.
  - Dead-air frames during silence are NOT appended (only speech frames are),
    so a manual commit's buffer is speech-only across pause-separated phrases.
  - commit_now() (called from the hotkey handler) flushes on demand.
  - continuous_max_buffer_s bounds an un-committed session so it can't grow
    unbounded if the user forgets to tap the commit hotkey.

Thread model: single daemon thread polling the ring at ~5ms intervals.
Flushed utterances are dispatched to transcribe_continuous_buffer on
separate daemon threads (same as the old callback). commit_now() may be
called from a different thread (the keyboard listener) while the poll
thread is concurrently appending frames; _frames_lock protects the shared
frame-list state against that race.
"""

import threading
import time

import numpy as np

from .frame import FRAME_MS, SAMPLE_RATE
from .ring import EMPTY
from samsara.log import get_logger
from samsara.runtime import thread_registry

from samsara.constants import (
    DEFAULT_CONTINUOUS_COMMIT_TRIGGER,
    DEFAULT_CONTINUOUS_MAX_BUFFER_S,
    DEFAULT_MIN_SPEECH_DURATION,
    DEFAULT_SILENCE_TIMEOUT,
    DEFAULT_SPEECH_THRESHOLD,
)

logger = get_logger(__name__)
_THREAD_JOIN_TIMEOUT_S = 2.0


class ContinuousConsumer:
    """Polls the ACE ring for speech and dispatches utterances.

    Args:
        engine: AudioCaptureEngine — the sole ring writer.
        app:    DictationApp — provides config, echo_canceller, and
                transcribe_continuous_buffer().
        on_fatal: Optional callback(exc) after fatal cleanup on the poll thread;
                  defaults to app.play_sound('error').
    """

    def __init__(self, engine, app, *, on_fatal=None) -> None:
        self._engine = engine
        self._app    = app
        self._reader = engine.register_consumer("continuous")
        self._running = False
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()
        self._stop_event = threading.Event()
        self.on_fatal = on_fatal  # Optional callback(exc); otherwise use app.play_sound('error').
        self._fatal_reported = False
        self._hands_free_capture_duck_token = None
        self._last_frame_log = float('-inf')

        # Per-utterance state — local, not shared with WakeConsumer
        self._speech_frames: list = []   # float32 arrays at SAMPLE_RATE
        self._is_speaking   = False
        self._silence_start: float | None = None
        # Guards _speech_frames/_is_speaking/_silence_start against the poll
        # thread (_process_frame) and any other thread (commit_now(), stop())
        # touching them concurrently.
        self._frames_lock = threading.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Begin consuming only after the previous poll generation has exited."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                logger.error("[ACE] Continuous start refused: thread %s is still alive", self._thread.name)
                return False
            self.abort()
            self._fatal_reported = False
            self._stop_event = threading.Event()
            self._reader.snap_to_head()
            self._running = True
            self._thread = thread_registry.spawn(
                "continuous-consumer", self._poll_loop, args=(self._stop_event,), daemon=True
            )
            return True

    def stop(self) -> list:
        """Stop polling and return any accumulated speech frames for final flush."""
        with self._lifecycle_lock:
            self._running = False
            self._stop_event.set()
            if self._thread is not None:
                if self._thread is threading.current_thread():
                    return []
                self._thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)
                if self._thread.is_alive():
                    logger.error("[ACE] Continuous stop incomplete: thread %s is still alive", self._thread.name)
                    return []
                self._thread = None
            self._close_capture_duck()
            with self._frames_lock:
                remaining = list(self._speech_frames)
                self._speech_frames = []
                self._is_speaking = False
                self._silence_start = None
            return remaining

    def deactivate(self) -> None:
        """Stop and unregister from the engine at app shutdown."""
        self.stop()
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            self._engine.unregister_consumer(self._reader)
        except Exception as e:
            logger.debug(f"unregister_consumer failed during deactivate: {e}")

    def abort(self) -> None:
        """Immediately discard in-progress speech accumulation -- called
        when the audio device dies mid-utterance. Discards rather than
        flushes: the audio may be truncated mid-word with no guarantee
        frames resume soon, unlike a normal silence-timeout commit."""
        with self._frames_lock:
            self._speech_frames = []
            self._is_speaking   = False
            self._silence_start = None

    # ── Manual commit (continuous_commit_trigger == "key") ──────────────────

    def commit_now(self) -> None:
        """Manually commit accumulated speech now.

        Safe to call from any thread (e.g. the keyboard listener) while
        _process_frame runs concurrently on the poll thread. No-op if
        there's nothing to commit (accumulated speech below
        min_speech_duration) -- logs nothing in that case, since the user
        didn't actually say anything to commit.
        """
        committed = self._flush()
        if committed:
            logger.info("[CONTINUOUS] Manual commit")

    # ── Poll loop ─────────────────────────────────────────────────────────────

    def _close_capture_duck(self) -> None:
        token, self._hands_free_capture_duck_token = self._hands_free_capture_duck_token, None
        close_duck = getattr(self._app, '_close_hands_free_capture_duck', None)
        if token is not None and callable(close_duck):
            try:
                close_duck(token)
            except Exception as exc:
                logger.warning("[ACE] Continuous duck cleanup failed: %s", exc)

    def _handle_fatal(self, exc: Exception) -> None:
        if self._fatal_reported:
            return
        self._fatal_reported = True
        self._running = False
        self._stop_event.set()
        logger.error("[ACE] Continuous consumer stopped after fatal error: %s", exc,
                     exc_info=(type(exc), exc, exc.__traceback__))
        self.abort()
        self._close_capture_duck()
        app = self._app
        app.continuous_active = False
        update = getattr(app, 'set_app_state', None)
        if callable(update):
            try:
                update(continuous_active=False)
            except Exception as cleanup_exc:
                logger.warning("[ACE] Continuous state notification failed: %s", cleanup_exc)
            finally:
                app.continuous_active = False
        try:
            if self.on_fatal is not None:
                self.on_fatal(exc)
            elif callable(getattr(app, 'play_sound', None)):
                app.play_sound('error')
        except Exception as callback_exc:
            logger.warning("[ACE] Continuous on_fatal callback failed: %s", callback_exc)

    def _poll_loop(self, stop_event=None) -> None:
        app = self._app
        stop_event = self._stop_event if stop_event is None else stop_event
        try:
            while self._running and not stop_event.is_set():
                if not app.continuous_active:
                    time.sleep(0.005)
                    continue

                frame = self._reader.read_next()
                if stop_event.is_set():
                    break
                if frame is EMPTY:
                    time.sleep(0.005)
                    continue

                self._process_frame(frame)
        except Exception as exc:
            self._handle_fatal(exc)
        finally:
            self._running = False
            self._close_capture_duck()

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
        trigger = app.config.get('continuous_commit_trigger', DEFAULT_CONTINUOUS_COMMIT_TRIGGER)

        if rms > speech_threshold:
            with self._frames_lock:
                self._is_speaking   = True
                self._silence_start = None
                self._speech_frames.append(audio_chunk)
        elif self._is_speaking:
            if trigger == 'key':
                # Key-commit mode: silence is unlimited thinking time, never
                # a commit -- the silence-timeout flush below is skipped
                # entirely. Dead-air frames are NOT appended either, so the
                # eventual commit's buffer stays speech-only across
                # pause-separated phrases. The session stays open
                # (_is_speaking untouched) until commit_now() or the safety
                # cap below fires.
                max_buffer_s = app.config.get('continuous_max_buffer_s', DEFAULT_CONTINUOUS_MAX_BUFFER_S)
                speech_duration = len(self._speech_frames) * (FRAME_MS / 1000.0)
                if speech_duration >= max_buffer_s:
                    now = time.monotonic()
                    if now - self._last_frame_log >= 5.0:
                        self._last_frame_log = now
                        logger.debug("[CONTINUOUS] Max buffer reached — auto-committing")
                    self._flush()
            else:
                # trigger == "silence" (default): EXACTLY today's behavior,
                # unchanged.
                with self._frames_lock:
                    self._speech_frames.append(audio_chunk)

                if self._silence_start is None:
                    self._silence_start = time.time()
                elif time.time() - self._silence_start >= silence_threshold:
                    self._flush()

    def _flush(self) -> bool:
        """Copy accumulated speech frames (if any), reset state, dispatch transcription.

        Thread-safe against _process_frame's concurrent appends via
        _frames_lock. Returns True if a transcription was actually
        dispatched, False if there was nothing to commit (below
        min_speech_duration).
        """
        app = self._app
        min_speech = app.config.get('min_speech_duration', DEFAULT_MIN_SPEECH_DURATION)

        with self._frames_lock:
            speech_duration = len(self._speech_frames) * (FRAME_MS / 1000.0)
            if speech_duration >= min_speech:
                buffer_copy = list(self._speech_frames)
            else:
                buffer_copy = None
            self._speech_frames = []
            self._is_speaking   = False
            self._silence_start = None

        if buffer_copy is None:
            return False

        thread_registry.spawn(
            "continuous_consumer.transcribe_continuous_buffer",
            app.transcribe_continuous_buffer,
            args=(buffer_copy, SAMPLE_RATE),
            daemon=True,
        )
        return True

    def __repr__(self) -> str:
        return (
            f"ContinuousConsumer(running={self._running}, "
            f"speaking={self._is_speaking}, "
            f"frames={len(self._speech_frames)})"
        )
