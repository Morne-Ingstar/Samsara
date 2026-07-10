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

import threading
import time

import numpy as np

from .frame import PREBUFFER_FRAMES, FRAME_MS
from .ring import EMPTY
from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)


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

        A background drain thread then reads the ring continuously for the
        duration of the hold and accumulates copied frames into _frames.
        This is what allows utterances longer than the ring's ~58.5s
        effective window: the reader never falls behind the writer, so the
        overrun handler never fires. (The previous design read the ring
        only once at release in drain(), so any hold longer than the ring
        capacity was lapped and truncated to the last prebuffer window.)
        """
        self._frames.clear()
        self._active         = True
        self._epoch_at_start = None
        self._drain_stop     = threading.Event()
        self._frames_lock    = threading.Lock()

        # Snap cursor to the current write head BEFORE rewinding.
        # Between the previous drain() and this activate(), the writer
        # has been continuously advancing. Without this snap, the cursor
        # is still at the old drain position (potentially seconds stale),
        # and rewind(PREBUFFER_FRAMES) goes backwards from THERE —
        # capturing stale audio from the gap between recordings, which
        # produces doubled transcriptions.
        self._reader.snap_to_head()

        _coordinator = getattr(self._app, 'audio_coordinator', None)
        if _coordinator and _coordinator.is_speaking:
            logger.debug("[PRE] Pre-buffer skipped — TTS actively speaking")
        elif time.monotonic() - getattr(self._app, '_tts_last_speaking', 0.0) < 0.5:
            logger.debug("[PRE] Pre-buffer skipped — TTS ended too recently")
        else:
            self._reader.rewind(PREBUFFER_FRAMES)

        self._drain_thread = thread_registry.spawn(
            "dictation-hold-consumer", self._hold_drain_loop, daemon=True
        )

    def _hold_drain_loop(self) -> None:
        """Background thread: drain ring → _frames continuously during a hold.

        Mirrors _streaming_drain_loop. Copies each frame (MA-2: frame.pcm is
        a view into ring memory that the writer overwrites after the ring
        wraps) and accumulates the raw int16 pcm. Epoch checking and echo
        cancellation are deferred to drain(), which assembles the final
        audio — keeping per-frame work in this hot loop minimal.
        """
        while not self._drain_stop.is_set():
            frame = self._reader.read_next()
            if frame is EMPTY:
                time.sleep(0.005)
                continue
            if self._epoch_at_start is None:
                self._epoch_at_start = frame.device_epoch
            with self._frames_lock:
                # seq (monotonic ring write-cursor snapshot) is carried
                # through so drain() can verify the assembled buffer is
                # actually contiguous -- see _log_seam_diagnostics.
                self._frames.append((frame.seq, frame.device_epoch, frame.pcm.copy()))  # [MA-2]

    def cancel(self) -> None:
        """Discard accumulated frames without assembling audio.

        Safe to call even if activate() was never called.
        """
        stop = getattr(self, '_drain_stop', None)
        if stop is not None:
            stop.set()
        thread = getattr(self, '_drain_thread', None)
        if thread is not None:
            thread.join(timeout=2.0)
            self._drain_thread = None
        self._frames.clear()
        self._active = False

    def drain(self) -> 'np.ndarray | None':
        """Stop the drain thread and return assembled float32 audio.

        The background _hold_drain_loop has been accumulating (epoch, pcm)
        tuples for the whole hold. Here we stop it, then assemble: apply the
        epoch-discontinuity check and per-frame echo cancellation, and
        concatenate into a single float32 ndarray at 16 kHz (model rate).

        Returns:
            float32 ndarray at 16kHz, or None when:
            - no frames were accumulated (silent tap), or
            - an epoch change occurred mid-utterance (stream discontinuity).

        Callers must treat None as "no audio" and skip transcription.
        """
        self._active = False

        # Stop the background drain thread and flush any final frames.
        stop = getattr(self, '_drain_stop', None)
        if stop is not None:
            stop.set()
        thread = getattr(self, '_drain_thread', None)
        if thread is not None:
            thread.join(timeout=2.0)
            self._drain_thread = None

        if self._reader is None:
            return None

        # Catch any frames written between the thread's last read and stop.
        while True:
            frame = self._reader.read_next()
            if frame is EMPTY:
                break
            if self._epoch_at_start is None:
                self._epoch_at_start = frame.device_epoch
            with self._frames_lock:
                self._frames.append((frame.seq, frame.device_epoch, frame.pcm.copy()))  # [MA-2]

        with self._frames_lock:
            collected = list(self._frames)
            self._frames.clear()

        if not collected:
            return None

        self._log_seam_diagnostics(collected)

        echo_canceller = getattr(self._app, 'echo_canceller', None)
        start_epoch = collected[0][1]
        out_frames = []

        for seq, epoch, pcm in collected:
            if epoch != start_epoch:
                logger.warning("[ACE] Epoch change mid-utterance — aborting dictation")
                return None

            if echo_canceller and echo_canceller.is_active:
                pcm_f32   = pcm.astype(np.float32) / 32767.0
                processed = echo_canceller.process(
                    pcm_f32.reshape(-1, 1)
                ).flatten()
                pcm = np.clip(processed * 32767.0, -32768, 32767).astype(np.int16)

            out_frames.append(pcm)

        pcm_int16 = np.concatenate(out_frames)
        return pcm_int16.astype(np.float32) / 32767.0

    def _log_seam_diagnostics(self, collected: list) -> None:
        """Debug-level, cheap, permanent seam diagnostics (hotkey word-loss
        investigation, 2026-07-10). `collected` is the list of
        (seq, epoch, pcm) tuples assembled for this utterance.

        seq is FrameBus's monotonic write-cursor snapshot -- the ONLY
        counter available for detecting a real ring-position discontinuity.
        Nothing previously stored or checked it here; a gap (frames
        skipped, e.g. an overrun) or overlap (duplicate/backwards seq)
        anywhere in the buffer, including at the nominal prebuffer/live
        boundary established by activate()'s rewind(PREBUFFER_FRAMES),
        would previously have been silently invisible.

        The "prebuffer" and "live" portions are not structurally distinct
        in this consumer (a single continuous read loop after one rewind,
        unlike WakeConsumer's onset-triggered re-read) -- the boundary
        logged below is the FIRST PREBUFFER_FRAMES frames vs the rest, an
        approximation of the intended semantics rather than a code-level
        split point.
        """
        n = len(collected)
        total_ms = n * FRAME_MS

        if n > PREBUFFER_FRAMES:
            pre_first_seq = collected[0][0]
            pre_last_seq  = collected[PREBUFFER_FRAMES - 1][0]
            live_first_seq = collected[PREBUFFER_FRAMES][0]
            live_last_seq  = collected[-1][0]
            # Healthy contiguous audio: live_first_seq == pre_last_seq + 1,
            # i.e. seam_delta_ms == 0. Positive = gap (frames missing at
            # the seam); negative = overlap (duplicated/backwards seq).
            seam_delta_ms = (live_first_seq - pre_last_seq - 1) * FRAME_MS
            logger.debug(
                "[SEAM] pre-buffer seq [%d..%d] (%dms) | live seq [%d..%d] "
                "(%dms) | seam gap/overlap=%+dms",
                pre_first_seq, pre_last_seq, PREBUFFER_FRAMES * FRAME_MS,
                live_first_seq, live_last_seq, (n - PREBUFFER_FRAMES) * FRAME_MS,
                seam_delta_ms,
            )
        else:
            logger.debug(
                "[SEAM] buffer shorter than nominal prebuffer (%d/%d frames, "
                "%dms) -- no live portion to measure a seam against",
                n, PREBUFFER_FRAMES, total_ms,
            )

        # Full-buffer scan: a real discontinuity can occur anywhere, not
        # just at the nominal seam (e.g. a drain-thread scheduling stall
        # causing an overrun mid-hold).
        discontinuities = 0
        for i in range(1, n):
            prev_seq = collected[i - 1][0]
            cur_seq  = collected[i][0]
            delta = cur_seq - prev_seq
            if delta != 1:
                discontinuities += 1
                gap_ms = (delta - 1) * FRAME_MS
                logger.debug(
                    "[SEAM] discontinuity at frame %d/%d: seq %d -> %d (%+dms vs expected)",
                    i, n, prev_seq, cur_seq, gap_ms,
                )
        if discontinuities == 0:
            logger.debug("[SEAM] no seq discontinuities across %d frames (%dms total)", n, total_ms)

    # ── CapsLock streaming accumulator (ACE-04B) ─────────────────────────────
    #
    # The streaming session (streaming.py StreamingWorker) calls
    # snapshot_streaming_audio() repeatedly for partial passes, then
    # stop_streaming() for the final pass. Frames accumulate in
    # _streaming_frames; snapshots are non-destructive reads.

    def activate_streaming(self) -> None:
        """Start accumulating for a CapsLock streaming session.

        Snaps to current write head and rewinds to prebuffer window (same
        TTS guards as activate()). A drain thread accumulates frames into
        _streaming_frames for StreamingWorker to snapshot.
        """
        self._streaming_frames: list = []
        self._streaming_stop  = threading.Event()
        self._streaming_lock  = threading.Lock()

        # Snap + rewind (same logic as activate())
        self._reader.snap_to_head()
        _coordinator = getattr(self._app, 'audio_coordinator', None)
        if _coordinator and _coordinator.is_speaking:
            logger.debug("[PRE] Streaming: pre-buffer skipped — TTS actively speaking")
        elif time.monotonic() - getattr(self._app, '_tts_last_speaking', 0.0) < 0.5:
            logger.debug("[PRE] Streaming: pre-buffer skipped — TTS ended too recently")
        else:
            self._reader.rewind(PREBUFFER_FRAMES)

        self._streaming_thread = thread_registry.spawn(
            "streaming-consumer", self._streaming_drain_loop, daemon=True
        )

    def _streaming_drain_loop(self) -> None:
        """Background thread: drain ring → _streaming_frames while session runs."""
        while not self._streaming_stop.is_set():
            frame = self._reader.read_next()
            if frame is EMPTY:
                time.sleep(0.005)
                continue
            pcm = frame.pcm.copy()   # [MA-2]
            pcm_f32 = pcm.astype(np.float32) / 32767.0
            with self._streaming_lock:
                self._streaming_frames.append(pcm_f32)

    def snapshot_streaming_audio(self) -> 'np.ndarray | None':
        """Return assembled float32 audio without clearing the accumulator.

        Called by StreamingWorker for each partial transcription pass.
        Thread-safe non-destructive read.
        """
        with self._streaming_lock:
            frames = list(self._streaming_frames)
        if not frames:
            return None
        audio = np.concatenate(frames)
        if audio.size == 0:
            return None
        return audio

    def stop_streaming(self) -> 'np.ndarray | None':
        """Stop accumulating and return all audio for the final pass."""
        self._streaming_stop.set()
        if hasattr(self, '_streaming_thread') and self._streaming_thread is not None:
            self._streaming_thread.join(timeout=2.0)
            self._streaming_thread = None
        with self._streaming_lock:
            frames = list(self._streaming_frames)
            self._streaming_frames = []
        if not frames:
            return None
        return np.concatenate(frames)

    # ── App shutdown ──────────────────────────────────────────────────────────

    def deactivate(self) -> None:
        """Unregister from the engine. Call once at app shutdown."""
        self._active = False
        if self._reader is not None:
            try:
                self._engine.unregister_consumer(self._reader)
            except Exception as e:
                logger.debug(f"unregister_consumer failed during deactivate: {e}")
            self._reader = None

    def __repr__(self) -> str:
        return (
            f"DictationSessionConsumer(active={self._active}, "
            f"frames={len(self._frames)}, "
            f"epoch={self._epoch_at_start})"
        )
