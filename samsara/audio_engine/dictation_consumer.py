"""DictationSessionConsumer — ACE-03: ring consumer for hold-mode dictation.

Replaces the bespoke _prebuffer deque + audio_callback + per-keypress
PortAudio InputStream that the hold/toggle path opened on every hotkey
press with a single long-lived consumer on the permanent ACE engine ring.

== Lifecycle ==

One DictationSessionConsumer is created at app startup and persists for
the app's lifetime. It is never destroyed mid-session.

  startup:  DictationSessionConsumer(engine, app)
  hotkey:   consumer.activate()      -- rewinds to prebuffer, clears frames
  release:  consumer.drain()         -- reads ring, returns float32 audio
  cancel:   consumer.cancel()        -- discards accumulated frames
  shutdown: consumer.deactivate()    -- unregisters from engine

== MA-2: pcm copy ==

Frame.pcm is a VIEW into ring memory. The writer overwrites the slot
approximately every RING_SECONDS of wall time. Every accumulated frame
MUST be copied before appending to self._frames. A retained view
silently reads corrupted audio after the ring wraps around.

== TTS contamination guard ==

If TTS was actively speaking at the moment of hotkey press, the prebuffer
window contains synthesised speech. The guard from the old start_recording
path is replicated here: "don't rewind" (start from current head only)
rather than "discard a copy" — both produce the same result.

== Epoch change ==

A device_epoch change mid-utterance means the audio device was interrupted
(BT reconnect, device switch). Stitching audio across an epoch boundary
produces garbage transcripts. drain() returns None on epoch mismatch;
stop_recording treats None as "no audio captured" and plays an error earcon.
"""

import time

import numpy as np

from .frame import PREBUFFER_FRAMES
from .ring import EMPTY


class DictationSessionConsumer:
    """Accumulates ring frames for one hold-mode dictation utterance."""

    def __init__(self, engine, app) -> None:
        self._engine = engine
        self._app    = app
        self._reader = engine.register_consumer("dictation_hold")
        self._frames: list = []
        self._active         = False
        self._epoch_at_start = None

    # ── Utterance lifecycle ───────────────────────────────────────────────────

    def activate(self) -> None:
        """Call at hotkey press (speech onset).

        Rewinds to include prebuffer history unless TTS was speaking
        recently, then clears accumulated frames for the new utterance.
        """
        self._frames.clear()
        self._active         = True
        self._epoch_at_start = None

        # Snap cursor to the current write head BEFORE rewinding.
        # Between the previous drain() and this activate(), the writer
        # has been continuously advancing. Without this snap, the cursor
        # is still at the old drain position (potentially seconds stale),
        # and rewind(PREBUFFER_FRAMES) goes backwards from THERE —
        # capturing stale audio from the gap between recordings, which
        # produces doubled transcriptions.
        self._reader._read_cursor = self._engine._ring.write_cursor

        _coordinator = getattr(self._app, 'audio_coordinator', None)
        if _coordinator and _coordinator.is_speaking:
            print("[PRE] Pre-buffer skipped — TTS actively speaking")
        elif time.monotonic() - getattr(self._app, '_tts_last_speaking', 0.0) < 0.5:
            print("[PRE] Pre-buffer skipped — TTS ended too recently")
        else:
            self._reader.rewind(PREBUFFER_FRAMES)

    def cancel(self) -> None:
        """Discard accumulated frames without assembling audio.

        Safe to call even if activate() was never called.
        """
        self._frames.clear()
        self._active = False

    def drain(self) -> 'np.ndarray | None':
        """Read all available ring frames and return assembled float32 audio.

        Reads until the ring is empty, then assembles accumulated frames
        into a single float32 ndarray at 16 kHz (model rate). Echo
        cancellation is applied per-frame when the canceller is active.

        Returns:
            float32 ndarray at 16kHz, or None when:
            - no frames were accumulated (silent tap), or
            - an epoch change occurred mid-utterance (stream discontinuity).

        Callers must treat None as "no audio" and skip transcription.
        """
        self._active = False

        if self._reader is None:
            return None

        echo_canceller = getattr(self._app, 'echo_canceller', None)

        while True:
            frame = self._reader.read_next()
            if frame is EMPTY:
                break

            if self._epoch_at_start is None:
                self._epoch_at_start = frame.device_epoch

            if frame.device_epoch != self._epoch_at_start:
                print("[ACE] Epoch change mid-utterance — aborting dictation")
                self._frames.clear()
                return None

            pcm = frame.pcm.copy()   # [MA-2] COPY: frame.pcm is a VIEW into ring memory

            if echo_canceller and echo_canceller.is_active:
                pcm_f32   = pcm.astype(np.float32) / 32767.0
                processed = echo_canceller.process(
                    pcm_f32.reshape(-1, 1)
                ).flatten()
                pcm = np.clip(processed * 32767.0, -32768, 32767).astype(np.int16)

            self._frames.append(pcm)

        if not self._frames:
            return None

        pcm_int16 = np.concatenate(self._frames)
        self._frames.clear()
        return pcm_int16.astype(np.float32) / 32767.0

    # ── App shutdown ──────────────────────────────────────────────────────────

    def deactivate(self) -> None:
        """Unregister from the engine. Call once at app shutdown."""
        self._active = False
        if self._reader is not None:
            try:
                self._engine.unregister_consumer(self._reader)
            except Exception:
                pass
            self._reader = None

    def __repr__(self) -> str:
        return (
            f"DictationSessionConsumer(active={self._active}, "
            f"frames={len(self._frames)}, "
            f"epoch={self._epoch_at_start})"
        )
