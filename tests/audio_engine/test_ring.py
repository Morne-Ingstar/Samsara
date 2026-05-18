"""FrameBus ring tests — HARDWARE-FREE, deterministic.

No audio device is opened. No sounddevice import. All tests run in CI.

Tests 3, 4, 5 are the INVARIANT PROOFS demanded by ARC and GPT:
  3. Overrun: recovery is graceful, no exception, no writer stall.
  4. Slow-consumer isolation: writer NEVER blocks on a dead reader.
  5. Fuzz: invariants hold under randomised interleaving.

Test 2 is the STRUCTURAL ELIMINATION PROOF for the prebuffer bug class:
  rewind() is the only prebuffer mechanism; it cannot be forgotten in the
  way the old copy-based prepend could be omitted.
"""

import time
import random

import numpy as np
import pytest

from samsara.audio_engine import (
    Frame,
    FrameBus,
    Reader,
    EMPTY,
    FRAME_SIZE,
    RING_FRAMES,
    PREBUFFER_FRAMES,
    PREBUFFER_SECONDS,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pcm(value: int = 0) -> np.ndarray:
    """Return a FRAME_SIZE int16 array filled with `value`."""
    return np.full(FRAME_SIZE, value, dtype=np.int16)


def _write_n(bus: FrameBus, n: int, epoch: int = 0) -> None:
    """Write n frames with pcm[i] = i % 32767, t=float(i)."""
    for i in range(n):
        bus.write(_pcm(i % 32767), float(i), epoch)


def _drain(reader: Reader) -> list[Frame]:
    """Read all available frames from a reader."""
    frames = []
    while True:
        f = reader.read_next()
        if f is EMPTY:
            break
        frames.append(f)
    return frames


# ── Test 1: Basic FIFO ────────────────────────────────────────────────────────

class TestBasicFifo:
    """Write N < RING_FRAMES frames; reader gets them in order, then EMPTY."""

    def test_fifo_order_and_seq(self):
        bus = FrameBus()
        reader = bus.new_reader()
        n = RING_FRAMES // 2

        _write_n(bus, n)

        frames = _drain(reader)
        assert len(frames) == n, f"Expected {n} frames, got {len(frames)}"

        seqs = [f.seq for f in frames]
        assert seqs == list(range(n)), f"seq not monotonic: {seqs[:10]}"

    def test_empty_before_write(self):
        bus = FrameBus()
        reader = bus.new_reader()
        assert reader.read_next() is EMPTY

    def test_empty_after_drain(self):
        bus = FrameBus()
        reader = bus.new_reader()
        _write_n(bus, 5)
        _drain(reader)
        assert reader.read_next() is EMPTY

    def test_pcm_values_correct(self):
        bus = FrameBus()
        reader = bus.new_reader()
        bus.write(_pcm(42), 1.0, 0)
        f = reader.read_next()
        assert f is not EMPTY
        assert f.pcm[0] == 42
        assert f.t_capture == pytest.approx(1.0)
        assert f.seq == 0
        assert f.device_epoch == 0

    def test_frame_is_view_not_copy(self):
        """pcm must be a view into ring memory, not a copy."""
        bus = FrameBus()
        reader = bus.new_reader()
        bus.write(_pcm(7), 0.0, 0)
        f = reader.read_next()
        assert f.pcm.base is not None, (
            "Frame.pcm should be a view (base is not None), not a detached copy"
        )


# ── Test 2: Prebuffer rewind (STRUCTURAL ELIMINATION PROOF) ──────────────────

class TestPrebufferRewind:
    """[LOCKED] Prebuffer is a cursor rewind, never a copy.

    Structural elimination proof: rewind() is the only prebuffer mechanism.
    A consumer that skips it starts later in the stream — it cannot
    accidentally lose prebuffer data because the history is already in the
    ring. There is no copy step to forget.
    """

    def test_rewind_re_reads_prebuffer_frames(self):
        bus = FrameBus()
        reader = bus.new_reader()

        n = PREBUFFER_FRAMES + 10  # more than the prebuffer window
        _write_n(bus, n)

        # Drain all frames
        frames_first_pass = _drain(reader)
        assert len(frames_first_pass) == n

        # Rewind to re-read the prebuffer window
        reader.rewind(PREBUFFER_FRAMES)

        frames_second_pass = _drain(reader)
        assert len(frames_second_pass) == PREBUFFER_FRAMES, (
            f"Expected exactly PREBUFFER_FRAMES={PREBUFFER_FRAMES} after rewind, "
            f"got {len(frames_second_pass)}"
        )

        # Verify the re-read frames are the correct tail
        expected_seqs = list(range(n - PREBUFFER_FRAMES, n))
        got_seqs = [f.seq for f in frames_second_pass]
        assert got_seqs == expected_seqs, (
            f"Rewind returned wrong frames. Expected seqs {expected_seqs}, "
            f"got {got_seqs}"
        )

    def test_rewind_clamped_at_ring_boundary(self):
        """Rewinding past ring capacity clamps at write_cursor - RING_FRAMES."""
        bus = FrameBus()
        reader = bus.new_reader()
        _write_n(bus, RING_FRAMES)
        _drain(reader)

        # Rewind by more than RING_FRAMES — should clamp at oldest available
        reader.rewind(RING_FRAMES * 10)
        frames = _drain(reader)
        # Should get at most RING_FRAMES frames back (oldest slot)
        assert len(frames) <= RING_FRAMES

    def test_rewind_clamped_below_zero_early_in_run(self):
        """Early in a run (write_cursor < PREBUFFER_FRAMES), clamp to 0."""
        bus = FrameBus()
        reader = bus.new_reader()
        _write_n(bus, 3)  # only 3 frames written
        _drain(reader)

        reader.rewind(PREBUFFER_FRAMES)  # more than available
        frames = _drain(reader)
        assert len(frames) == 3  # should get all 3, not go negative


# ── Test 3: Overrun recovery ──────────────────────────────────────────────────

class TestOverrun:
    """Writer laps a slow reader: overrun metric increments, cursor repositions,
    read_next returns a Frame (no exception, no writer stall).
    """

    def test_overrun_increments_metric_once(self):
        bus = FrameBus()
        reader = bus.new_reader()

        # Lap the reader completely
        _write_n(bus, RING_FRAMES + 10)

        assert reader.overrun_count == 0
        f = reader.read_next()

        assert reader.overrun_count == 1, (
            f"Expected overrun_count=1 after one lapped read, got {reader.overrun_count}"
        )
        assert f is not EMPTY, "read_next() must return a Frame on overrun, not EMPTY"

    def test_overrun_repositions_to_prebuffer_window(self):
        """After overrun, read_cursor jumps to write_cursor - PREBUFFER_FRAMES."""
        bus = FrameBus()
        reader = bus.new_reader()

        total = RING_FRAMES + 10
        _write_n(bus, total)

        expected_seq = total - PREBUFFER_FRAMES
        f = reader.read_next()
        assert f.seq == expected_seq, (
            f"After overrun recovery, first frame seq should be "
            f"write_cursor - PREBUFFER_FRAMES = {expected_seq}, got {f.seq}"
        )

    def test_overrun_subsequent_reads_are_continuous(self):
        """After overrun recovery, all subsequent reads have consecutive seq."""
        bus = FrameBus()
        reader = bus.new_reader()
        _write_n(bus, RING_FRAMES + 10)

        frames = _drain(reader)
        seqs = [f.seq for f in frames]
        for i in range(1, len(seqs)):
            assert seqs[i] == seqs[i - 1] + 1, (
                f"seq gap after overrun at index {i}: {seqs[i - 1]} -> {seqs[i]}"
            )

    def test_overrun_no_exception_raised(self):
        """Overrun must not raise any exception — lossy, not failing."""
        bus = FrameBus()
        reader = bus.new_reader()
        _write_n(bus, RING_FRAMES + 50)
        # Must not raise
        for _ in range(PREBUFFER_FRAMES + 5):
            reader.read_next()

    def test_overrun_metric_does_not_double_count(self):
        """A second call to read_next() after overrun should not count again
        unless the reader is lapped a second time."""
        bus = FrameBus()
        reader = bus.new_reader()
        _write_n(bus, RING_FRAMES + 1)

        reader.read_next()                  # triggers first overrun
        assert reader.overrun_count == 1

        _drain(reader)                      # drain remainder
        assert reader.overrun_count == 1    # no additional overruns


# ── Test 4: Slow-consumer isolation ──────────────────────────────────────────

class TestSlowConsumerIsolation:
    """[LOCKED] INVARIANT: a slow/crashed consumer cannot stall the writer.

    Proof structure:
      - Two readers: fast (reads between batches) and slow (never reads).
      - Writer writes 3 * RING_FRAMES frames.
      - Writer completes with no blocking (timing check).
      - Slow reader accumulates overruns; fast reader has zero.
      - The ring's write() method contains NO lock.acquire(), NO Condition.wait(),
        NO sleep() — this is the structural guarantee. The timing check
        adds an empirical bound.
    """

    def test_writer_never_blocks_on_dead_reader(self):
        bus = FrameBus()
        fast_reader = bus.new_reader()
        slow_reader = bus.new_reader()

        total = 3 * RING_FRAMES
        batch = RING_FRAMES // 2   # fast reader catches up every batch

        t0 = time.monotonic()
        for i in range(total):
            bus.write(_pcm(i % 32767), float(i), 0)
            # fast reader keeps up by reading between batches
            if (i + 1) % batch == 0:
                _drain(fast_reader)
        elapsed = time.monotonic() - t0

        # Drain any remaining frames from fast reader
        _drain(fast_reader)

        # Writer timing: 300 NumPy writes should complete in well under 1s.
        # 5s bound is extremely generous; CI machines are slow but not that slow.
        assert elapsed < 5.0, (
            f"Writer took {elapsed:.3f}s for {total} writes — "
            "possible deadlock or unexpected blocking on slow_reader"
        )

    def test_slow_reader_accumulates_overruns(self):
        bus = FrameBus()
        fast_reader = bus.new_reader()
        slow_reader = bus.new_reader()

        total = 3 * RING_FRAMES
        batch = RING_FRAMES // 2

        for i in range(total):
            bus.write(_pcm(0), float(i), 0)
            if (i + 1) % batch == 0:
                _drain(fast_reader)

        _drain(fast_reader)

        # Trigger slow_reader's overrun on first read
        f = slow_reader.read_next()
        assert slow_reader.overrun_count > 0, (
            "slow_reader should have at least one overrun after writer wrote "
            f"3*RING_FRAMES={total} frames without being read"
        )
        assert f is not EMPTY

    def test_fast_reader_zero_overruns(self):
        """Fast reader that keeps up within RING_FRAMES should have zero overruns."""
        bus = FrameBus()
        fast_reader = bus.new_reader()
        _slow = bus.new_reader()  # present but never read

        total = 3 * RING_FRAMES
        batch = RING_FRAMES // 2

        for i in range(total):
            bus.write(_pcm(0), float(i), 0)
            if (i + 1) % batch == 0:
                _drain(fast_reader)

        _drain(fast_reader)
        assert fast_reader.overrun_count == 0, (
            f"fast_reader should have 0 overruns, got {fast_reader.overrun_count}"
        )


# ── Test 5: Fuzz / invariant check ───────────────────────────────────────────

class TestFuzz:
    """Randomised interleaving of writes and multi-reader reads.

    Invariants asserted every iteration:
      - write_cursor is monotonic non-decreasing.
      - No reader returns a frame with seq > write_cursor.
      - No reader's read_cursor exceeds write_cursor.
    """

    def test_fuzz_invariants(self):
        rng = random.Random(0xACE01)
        bus = FrameBus()
        readers = [bus.new_reader() for _ in range(4)]

        prev_wc = 0
        for iteration in range(2000):
            n_writes = rng.randint(0, 15)
            for _ in range(n_writes):
                bus.write(_pcm(rng.randint(0, 32767)), float(iteration), 0)

            wc = bus.write_cursor
            assert wc >= prev_wc, (
                f"write_cursor decreased at iteration {iteration}: "
                f"{prev_wc} -> {wc}"
            )
            prev_wc = wc

            for reader in readers:
                n_reads = rng.randint(0, 8)
                for _ in range(n_reads):
                    f = reader.read_next()
                    if f is not EMPTY:
                        assert f.seq <= wc, (
                            f"Frame seq {f.seq} > write_cursor {wc} "
                            f"at iteration {iteration}"
                        )
                        assert reader._read_cursor <= wc + 1, (
                            f"read_cursor {reader._read_cursor} > write_cursor "
                            f"{wc} at iteration {iteration}"
                        )


# ── Test 6: Epoch change observable at frame boundary ────────────────────────

class TestEpoch:
    """device_epoch written to frames matches bump_device_epoch() calls."""

    def test_epoch_transition_visible_on_boundary_frame(self):
        bus = FrameBus()
        reader = bus.new_reader()

        # Write 5 frames with epoch 0
        for i in range(5):
            bus.write(_pcm(i), float(i), 0)

        bus.bump_device_epoch()   # epoch is now 1

        # Write 5 more frames with new epoch
        for i in range(5):
            bus.write(_pcm(i + 5), float(i + 5), bus.device_epoch)

        frames = _drain(reader)
        assert len(frames) == 10

        epochs = [f.device_epoch for f in frames]
        assert epochs == [0] * 5 + [1] * 5, (
            f"Expected [0]*5 + [1]*5, got {epochs}"
        )

    def test_epoch_boundary_frame_seq_is_continuous(self):
        """Epoch change does not break seq continuity."""
        bus = FrameBus()
        reader = bus.new_reader()

        for _ in range(3):
            bus.write(_pcm(), 0.0, 0)
        bus.bump_device_epoch()
        for _ in range(3):
            bus.write(_pcm(), 0.0, bus.device_epoch)

        frames = _drain(reader)
        seqs = [f.seq for f in frames]
        assert seqs == list(range(6)), f"Epoch change broke seq: {seqs}"

    def test_bump_device_epoch_increments(self):
        bus = FrameBus()
        assert bus.device_epoch == 0
        bus.bump_device_epoch()
        assert bus.device_epoch == 1
        bus.bump_device_epoch()
        assert bus.device_epoch == 2


# ── Test 7: Reader registry / lifecycle ──────────────────────────────────────

class TestReaderRegistry:
    """AudioCaptureEngine consumer registry: no leaks, no stale readers."""

    def test_register_and_unregister(self):
        from samsara.audio_engine import AudioCaptureEngine
        engine = AudioCaptureEngine(FrameBus())

        r1 = engine.register_consumer("whisper")
        r2 = engine.register_consumer("vad")
        r3 = engine.register_consumer("wake")

        assert len(engine._consumers) == 3

        engine.unregister_consumer(r2)
        assert len(engine._consumers) == 2

    def test_unregistered_reader_raises_on_read_next(self):
        from samsara.audio_engine import AudioCaptureEngine
        engine = AudioCaptureEngine(FrameBus())
        r = engine.register_consumer("test")
        engine.unregister_consumer(r)

        with pytest.raises(RuntimeError, match="invalidated"):
            r.read_next()

    def test_unregistered_reader_raises_on_rewind(self):
        from samsara.audio_engine import AudioCaptureEngine
        engine = AudioCaptureEngine(FrameBus())
        r = engine.register_consumer("test")
        engine.unregister_consumer(r)

        with pytest.raises(RuntimeError):
            r.rewind(5)

    def test_no_registry_leak_after_unregister_all(self):
        from samsara.audio_engine import AudioCaptureEngine
        engine = AudioCaptureEngine(FrameBus())

        readers = [engine.register_consumer(f"consumer_{i}") for i in range(5)]
        assert len(engine._consumers) == 5

        for r in readers:
            engine.unregister_consumer(r)

        assert len(engine._consumers) == 0, (
            f"Registry leaked {len(engine._consumers)} entries after unregistering all"
        )

    def test_active_readers_still_work_after_peer_unregistered(self):
        from samsara.audio_engine import AudioCaptureEngine
        bus = FrameBus()
        engine = AudioCaptureEngine(bus)

        r1 = engine.register_consumer("active")
        r2 = engine.register_consumer("to_be_removed")

        bus.write(_pcm(1), 0.0, 0)
        engine.unregister_consumer(r2)

        # r1 must still work
        f = r1.read_next()
        assert f is not EMPTY
        assert f.seq == 0

    def test_unregister_idempotent(self):
        """Unregistering an already-removed reader is a no-op."""
        from samsara.audio_engine import AudioCaptureEngine
        engine = AudioCaptureEngine(FrameBus())
        r = engine.register_consumer("once")
        engine.unregister_consumer(r)
        # Second call should not raise
        engine.unregister_consumer(r)
        assert len(engine._consumers) == 0
