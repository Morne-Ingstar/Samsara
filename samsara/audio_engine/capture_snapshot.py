"""Committed, generation-owned PCM and discontinuity receipts (1249).

This is a same-interpreter publication journal, not an interprocess transport.
Each link owns immutable PCM; sealing reads the last published link without
waiting behind an accumulator lock or a reader stalled inside read_next().
"""
from dataclasses import dataclass

import numpy as np

@dataclass(frozen=True)
class Loss:
    reason: str
    before_seq: int | None
    after_seq: int | None
    missing_samples: int | None = None


@dataclass(frozen=True)
class CommittedFrame:
    seq: int
    epoch: int
    captured_at: float
    pcm: bytes
    boundary: bool


@dataclass(frozen=True)
class CaptureSnapshot:
    generation: int
    frames: tuple
    losses: tuple
    drain_incomplete: bool
    requested_boundary: float | None
    # A callback timestamp is not a writer acknowledgement of that boundary.
    start_coverage: str = 'unknown'

    @property
    def final_sequence(self):
        return self.frames[-1].seq if self.frames else None

    @property
    def sample_count(self):
        return sum(len(frame.pcm) // 2 for frame in self.frames)

    def audio(self):
        if not self.frames:
            return None
        pcm = np.frombuffer(b''.join(frame.pcm for frame in self.frames), dtype=np.int16)
        floats = pcm.astype(np.float32) / 32767.0
        # bytes backing prevents callers from re-enabling WRITEABLE.
        result = np.frombuffer(floats.tobytes(), dtype=np.float32).view(CaptureAudio)
        result.capture_snapshot = self
        result.capture_start = 0
        return result

    def receipt(self):
        offset, rows = 0, []
        for frame in self.frames:
            size = len(frame.pcm) // 2
            rows.append(dict(start_sample=offset, end_sample=offset + size,
                             seq=frame.seq, epoch=frame.epoch,
                             captured_at=frame.captured_at, boundary=frame.boundary))
            offset += size
        return dict(generation=self.generation, final_sequence=self.final_sequence,
                    drain_incomplete=self.drain_incomplete, start_coverage=self.start_coverage,
                    requested_boundary=self.requested_boundary, sample_count=offset,
                    frames=rows, losses=[vars(loss).copy() for loss in self.losses])


class CaptureAudio(np.ndarray):
    """PCM with source coordinates preserved through ordinary copies/slices."""
    def __array_finalize__(self, source):
        self.capture_snapshot = getattr(source, 'capture_snapshot', None)
        self.capture_start = getattr(source, 'capture_start', 0)

    def __getitem__(self, key):
        result = super().__getitem__(key)
        if isinstance(result, CaptureAudio):
            if isinstance(key, slice) and key.indices(len(self))[2] == 1:
                result.capture_start = self.capture_start + key.indices(len(self))[0]
            else:
                result.capture_snapshot = None
        return result


def copy_audio(audio):
    """Retention must keep the receipt as well as PCM, including after retry."""
    return audio.copy() if isinstance(audio, CaptureAudio) else np.array(audio, copy=True)


def fade_capture(audio, rate, milliseconds):
    """Keep the existing fade algorithm and this take's discontinuity map."""
    from samsara.audio_tools import _fade_edges
    result = _fade_edges(audio, rate, milliseconds)
    if isinstance(audio, CaptureAudio):
        result = result.view(CaptureAudio)
        result.capture_snapshot = audio.capture_snapshot
        result.capture_start = audio.capture_start
    return result


def spans(audio):
    """Packed-sample ranges never bridge a known device/sequence/overflow gap.

    Original sequence/epoch/callback times remain in the snapshot. Unknown
    device loss duration is never fabricated as samples or silence.
    """
    snapshot = getattr(audio, 'capture_snapshot', None)
    if snapshot is None:
        return [(0, len(audio))]
    origin = audio.capture_start
    end = origin + len(audio)
    offset, cuts = 0, [0]
    for frame in snapshot.frames:
        if frame.boundary and origin < offset < end:
            cuts.append(offset - origin)
        offset += len(frame.pcm) // 2
    return list(zip(cuts, cuts[1:] + [len(audio)]))


class CaptureJournal:
    """One writer under the existing accumulator lock; atomic link publication."""
    def __init__(self, generation, boundary):
        self.generation, self.boundary = generation, boundary
        self.head = None
        self.losses = ()
        self.pending_boundary = False
        self.closed = False
        self.snapshot = None

    def loss(self, reason, before=None, after=None, samples=None):
        if not self.closed:
            self.losses += (Loss(reason, before, after, samples),)
            self.pending_boundary = True

    def append(self, frame, pcm):
        if self.closed:
            return False
        committed = CommittedFrame(frame.seq, frame.device_epoch, frame.t_capture,
                                   np.asarray(pcm, dtype=np.int16).tobytes(),
                                   self.pending_boundary)
        # The PCM copy can release the GIL. Recheck after it and publish with
        # no blocking/native operation between the check and assignment.
        if self.closed:
            return False
        self.head = (committed, self.head)
        self.pending_boundary = False
        return True

    def read(self, *, seal=False, incomplete=False):
        if self.snapshot is not None:
            return self.snapshot
        if seal:
            self.closed = True
        head, losses = self.head, self.losses
        frames = []
        while head is not None:
            frame, head = head
            frames.append(frame)
        result = CaptureSnapshot(self.generation, tuple(reversed(frames)), losses,
                                 incomplete, self.boundary)
        if seal:
            self.snapshot = result
        return result


def begin(consumer):
    consumer._capture_generation = getattr(consumer, '_capture_generation', 0) + 1
    consumer._capture_journal = CaptureJournal(consumer._capture_generation,
                                                getattr(consumer, '_capture_not_before', None))
    consumer.capture_snapshot = None


def commit(consumer, frame, pcm):
    journal = getattr(consumer, '_capture_journal', None)
    return journal is None or journal.append(frame, pcm)


def finish(consumer, *, incomplete=False):
    journal = getattr(consumer, '_capture_journal', None)
    if journal is None or consumer.capture_snapshot is not None:
        return None
    if incomplete:
        import time
        from .hold_capture import observe_stop
        observe_stop(consumer)
        journal.loss('drain_incomplete', consumer._last_capture_seq)
        consumer._previous_capture_end = consumer._capture_not_after or time.perf_counter()
    snapshot = journal.read(seal=True, incomplete=incomplete)
    consumer.capture_snapshot = snapshot
    return snapshot.audio()


def loss(consumer, reason, before=None, after=None, samples=None):
    journal = getattr(consumer, '_capture_journal', None)
    if journal is not None:
        journal.loss(reason, before, after, samples)
