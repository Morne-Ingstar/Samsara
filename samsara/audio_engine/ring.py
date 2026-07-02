"""FrameBus: the lock-free, writer-dominant, lossy ring buffer.

This is the transport core of the AudioCaptureEngine. All design decisions
are frozen per the spec at:
C:\\Users\\Morne\\Documents\\Claude\\audiocaptureengine_01_spec_v2_FROZEN.md

== Concurrency model (ACE-00 cleared, Model A) ==

A single writer (the PortAudio capture thread, added in ACE-02) and many
reader threads (WakeDetector, VAD, DictationSession, etc.) access the ring
concurrently. NO locks exist on the read/write path. Locks on the critical
audio path were identified by ARC as the mechanism by which a slow consumer
could transitively stall capture — the lossy design exists precisely to
break that dependency chain.

Atomicity claim (ACE-00 referenced):
  The write_cursor field is a plain Python int updated via += 1. Under
  CPython's GIL, the STORE_ATTR bytecode that commits the new integer
  object reference is effectively atomic from the perspective of other
  threads: a reading thread sees either the old or the new value, never a
  torn intermediate. This is not a lock; it is the word-sized reference
  assignment guarantee that the CPython object model provides. ACE-00
  empirically confirmed that CTranslate2, PyTorch, and ONNX Runtime all
  release the GIL during native inference, so inference threads cannot
  monopolize it and starve the write_cursor read on the capture thread.

Visibility ordering:
  The writer populates ring slots (NumPy writes, which also release the
  GIL internally) BEFORE incrementing write_cursor. A reader that observes
  write_cursor = N is guaranteed to see completed data for all slots 0..N-1,
  because the GIL enforces sequential consistency within each thread —
  the write_cursor increment bytecode cannot execute before the NumPy
  writes preceding it in the same thread complete.

== [LOCKED] Writer-dominant lossy semantics ==

  write(pcm, t_capture, device_epoch):
      slot = write_cursor % RING_FRAMES
      copy pcm into ring at slot; record seq, t_capture, device_epoch
      write_cursor += 1          <- commit / visibility fence
      # Never inspects any reader cursor. Ever.

  Reader.read_next():
      if read_cursor == write_cursor: return EMPTY
      if (write_cursor - read_cursor) > RING_FRAMES:
          # Reader was lapped — recover to prebuffer window
          overrun_count += 1
          read_cursor = max(0, write_cursor - PREBUFFER_FRAMES)
          # Fall through — return the frame at the new cursor position
      frame = ring[read_cursor % RING_FRAMES]
      read_cursor += 1
      return frame

Consequence: a slow, crashed, or zombie consumer only damages itself.
Capture is structurally impossible to stall from the consumer side.
This invariant is proven by forced-overload tests in tests/audio_engine/.

== [LOCKED] Prebuffer as ring-head cursor rewind ==

  Reader.rewind(n_frames):
      read_cursor = max(read_cursor - n_frames,
                        max(0, write_cursor - RING_FRAMES))

"Drain prebuffer on speech onset" is rewind(PREBUFFER_FRAMES) — a cursor
move, never a copy. A consumer that skips the rewind call simply starts
reading from the current head rather than silently losing prebuffer audio.
There is no copy step to accidentally omit.
"""

import threading
from typing import Union

import numpy as np

from .frame import (
    Frame,
    FRAME_MS,
    FRAME_SIZE,
    RING_FRAMES,
    PREBUFFER_FRAMES,
)
from samsara.log import get_logger

logger = get_logger(__name__)


# ── Sentinels ─────────────────────────────────────────────────────────────────

class _Sentinel:
    __slots__ = ('_name',)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name

    def __bool__(self) -> bool:
        return False


EMPTY   = _Sentinel('EMPTY')
"""Returned by Reader.read_next() when no new frame is available
(read_cursor == write_cursor)."""

OVERRUN = _Sentinel('OVERRUN')
"""Exported for documentation and type annotations. NOT returned by
read_next() — overruns are handled internally (overrun_count metric
incremented, cursor repositioned to write_cursor - PREBUFFER_FRAMES)
and read_next() proceeds to return the frame at the repositioned cursor.
Consumers detect overruns via Reader.overrun_count."""


# ── Reader ────────────────────────────────────────────────────────────────────

class Reader:
    """Per-consumer read cursor into a FrameBus ring.

    Each consumer gets its own Reader instance from FrameBus.new_reader()
    or AudioCaptureEngine.register_consumer(). Readers are independent: one
    slow reader cannot affect another's read_cursor or the writer.

    Ownership: the FrameBus (and by extension the AudioCaptureEngine) owns
    Reader lifetime records. A Reader that has been unregistered via
    AudioCaptureEngine.unregister_consumer() is invalidated — further calls
    to read_next() or rewind() raise RuntimeError. The FrameBus's lossy
    design means a dead reader (one that is never read) simply gets lapped
    and its overrun_count increments harmlessly; no stall, no memory leak
    beyond the Reader object itself until the registry drops it.
    """

    __slots__ = ('_bus', '_read_cursor', 'overrun_count', '_invalidated', '_name')

    def __init__(self, bus: 'FrameBus', initial_cursor: int) -> None:
        self._bus          = bus
        self._read_cursor  = initial_cursor
        self.overrun_count = 0
        self._invalidated  = False
        self._name: str | None = None

    def read_next(self) -> Union[Frame, _Sentinel]:
        """Return the next Frame, or EMPTY if the ring has no new data.

        On overrun (writer has lapped this reader), the overrun_count
        metric is incremented, the read cursor is repositioned to
        write_cursor - PREBUFFER_FRAMES, and the frame at that position is
        returned. No exception is raised; the consumer simply resumes from
        the most recent prebuffer window.

        Returns:
            Frame: next available frame (seq is monotonic; gaps indicate
                   dropped frames — check seq deltas to detect them).
            EMPTY: no new frames since the last read_next() call.

        Raises:
            RuntimeError: if this Reader has been invalidated by
                          AudioCaptureEngine.unregister_consumer().
        """
        if self._invalidated:
            raise RuntimeError(
                "Reader is invalidated: its consumer was unregistered. "
                "Do not retain Reader references after unregister_consumer()."
            )

        wc = self._bus._write_cursor

        if self._read_cursor == wc:
            return EMPTY

        # Overrun detection: writer has lapped this reader.
        # For hold-mode dictation this means the utterance exceeded RING_SECONDS
        # (~58.5 s effective limit). Only the last PREBUFFER_FRAMES are returned.
        if (wc - self._read_cursor) > RING_FRAMES:
            self.overrun_count += 1
            lost_frames = (wc - self._read_cursor) - RING_FRAMES
            lost_seconds = lost_frames * FRAME_MS / 1000.0
            logger.warning(
                f"[RING] Overrun on consumer '{getattr(self, '_name', '?')}': "
                f"reader was {lost_frames} frames ({lost_seconds:.1f}s) behind write head. "
                f"Ring capacity is {RING_FRAMES * FRAME_MS / 1000:.0f}s — "
                f"utterances longer than ~{(RING_FRAMES - PREBUFFER_FRAMES) * FRAME_MS / 1000:.1f}s "
                f"will be truncated to the last {PREBUFFER_FRAMES * FRAME_MS / 1000:.1f}s."
            )
            self._read_cursor = max(0, wc - PREBUFFER_FRAMES)

            # Defense-in-depth against the stale-slot TOCTOU race (MA-1, ACE-02):
            # Between repositioning and the actual pcm read below, a fast writer
            # could advance write_cursor by another RING_FRAMES and overwrite the
            # very slot we just repositioned to, producing a torn frame
            # (seq from one write cycle, pcm from another).
            #
            # The concurrent stress test (test_ring_concurrent.py) does NOT fire
            # this race at realistic capture rates because RING_FRAMES (100) writes
            # take ~1s at 10ms/frame — far longer than the nanoseconds needed to
            # read one slot. However, at synthetic tight-loop write rates the race
            # is theoretically possible.
            #
            # Fix: re-read write_cursor after repositioning and step forward again
            # if we were lapped a second time. Bounded by RING_FRAMES iterations;
            # does NOT add a lock and cannot stall the writer.
            _guard = 0
            while (self._bus._write_cursor - self._read_cursor) > RING_FRAMES:
                self._read_cursor = max(0, self._bus._write_cursor - PREBUFFER_FRAMES)
                _guard += 1
                if _guard >= RING_FRAMES:
                    break   # safety valve — impossible at real audio rates

        slot = self._read_cursor % RING_FRAMES
        frame = Frame(
            seq          = int(self._bus._seq[slot]),
            t_capture    = float(self._bus._t_capture[slot]),
            pcm          = self._bus._pcm[slot],   # view, not copy
            device_epoch = int(self._bus._epoch[slot]),
        )
        self._read_cursor += 1
        return frame

    def rewind(self, n_frames: int) -> None:
        """Move the read cursor back by n_frames to access pre-trigger history.

        This is the [LOCKED] prebuffer mechanism. "Drain prebuffer on speech
        onset" is rewind(PREBUFFER_FRAMES). The cursor is clamped so it
        cannot move behind the oldest slot still in the ring
        (write_cursor - RING_FRAMES) or below zero.

        Args:
            n_frames: number of frames to rewind. Values larger than the
                      available ring history are clamped silently.

        Raises:
            RuntimeError: if this Reader has been invalidated.
        """
        if self._invalidated:
            raise RuntimeError("Reader is invalidated.")

        wc = self._bus._write_cursor
        min_cursor = max(0, wc - RING_FRAMES)
        self._read_cursor = max(self._read_cursor - n_frames, min_cursor)

    def snap_to_head(self) -> None:
        """Move read cursor to the current write head (skip buffered backlog)."""
        if self._invalidated:
            raise RuntimeError("Reader is invalidated.")
        self._read_cursor = self._bus._write_cursor

    def invalidate(self) -> None:
        """Mark this Reader as dead. Called by AudioCaptureEngine.unregister_consumer()."""
        self._invalidated = True

    def __repr__(self) -> str:
        return (
            f"Reader(cursor={self._read_cursor}, "
            f"overruns={self.overrun_count}, "
            f"invalidated={self._invalidated})"
        )


# ── FrameBus ──────────────────────────────────────────────────────────────────

class FrameBus:
    """Pre-allocated lock-free ring buffer for 16kHz int16 audio Frames.

    Memory layout (pre-allocated at construction, never resized):
        _pcm      : int16[RING_FRAMES, FRAME_SIZE]  — audio payload
        _seq      : int64[RING_FRAMES]              — monotonic write counter
        _t_capture: float64[RING_FRAMES]            — perf_counter at capture
        _epoch    : int64[RING_FRAMES]              — device_epoch at write

    The write_cursor is a plain Python int. See the module docstring for
    the atomicity argument (GIL word-sized assignment, ACE-00 cleared).
    """

    def __init__(self) -> None:
        # Pre-allocate all ring storage — never resized or re-allocated
        self._pcm       = np.zeros((RING_FRAMES, FRAME_SIZE), dtype=np.int16)
        self._seq       = np.zeros(RING_FRAMES,               dtype=np.int64)
        self._t_capture = np.zeros(RING_FRAMES,               dtype=np.float64)
        self._epoch     = np.zeros(RING_FRAMES,               dtype=np.int64)

        # write_cursor: single writer, many readers.
        # GIL guarantees atomic reference replacement on += 1.
        # See module docstring — atomicity claim backed by ACE-00.
        self._write_cursor: int = 0
        self._device_epoch: int = 0

    # ── Writer API (called from capture thread; NO locks, NO reader inspection) ──

    def write(self, pcm: np.ndarray, t_capture: float, device_epoch: int) -> None:
        """Write one frame into the ring. NEVER inspects reader cursors.

        All array writes complete BEFORE write_cursor is incremented.
        This ordering is the visibility fence: a reader that observes the
        new write_cursor value is guaranteed to see complete frame data
        in the corresponding slot.

        Args:
            pcm:          int16 array of exactly FRAME_SIZE samples (1600).
            t_capture:    time.perf_counter() from the capture callback.
            device_epoch: current device epoch (from bump_device_epoch()).
        """
        slot = self._write_cursor % RING_FRAMES
        self._pcm[slot]       = pcm          # in-place copy; releases GIL
        self._seq[slot]       = self._write_cursor
        self._t_capture[slot] = t_capture
        self._epoch[slot]     = device_epoch
        self._write_cursor   += 1            # commit / visibility fence

    # ── Reader factory ────────────────────────────────────────────────────────

    def new_reader(self, name: str | None = None) -> Reader:
        """Return a new Reader positioned at the current write head.

        The reader starts with no buffered history (read_cursor ==
        write_cursor). To access pre-trigger history, call
        reader.rewind(PREBUFFER_FRAMES) immediately after obtaining the
        reader — this positions it at the start of the prebuffer window
        already captured in the ring.

        Args:
            name: optional consumer label; shows in overrun log messages.
        """
        reader = Reader(self, self._write_cursor)
        reader._name = name
        return reader

    # ── Epoch management ──────────────────────────────────────────────────────

    def bump_device_epoch(self) -> int:
        """Increment device_epoch and return the new value.

        [LOCKED] DISCONTINUITY RULE: an epoch change invalidates all active
        utterances. Any DictationSession spanning an epoch boundary MUST
        abort the in-flight utterance rather than stitching audio across
        a stream discontinuity (device switch, BT reconnect, recovery).

        The engine (AudioCaptureEngine.bump_device_epoch()) calls this and
        then passes the new epoch value to every subsequent write(). The
        epoch change is visible to consumers via Frame.device_epoch — the
        boundary frame is the first frame whose epoch differs from
        the previous frame's epoch.

        Returns:
            The new device_epoch value (starts at 0, increments by 1 each
            call, never wraps in practice).
        """
        self._device_epoch += 1
        return self._device_epoch

    @property
    def write_cursor(self) -> int:
        """Current write cursor (number of frames written). Read-only."""
        return self._write_cursor

    @property
    def device_epoch(self) -> int:
        """Current device epoch. Incremented by bump_device_epoch()."""
        return self._device_epoch

    def __repr__(self) -> str:
        return (
            f"FrameBus(write_cursor={self._write_cursor}, "
            f"device_epoch={self._device_epoch}, "
            f"ring_frames={RING_FRAMES})"
        )
