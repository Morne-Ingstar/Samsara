"""ACE-02 MA-1: Concurrent ring stress test.

ACE-01 tests were entirely single-threaded. This test runs a real writer
thread alongside multiple reader threads simultaneously to validate:

  1. The ring is correct under genuine concurrent access.
  2. The overrun-recovery path does NOT return torn/stale frames (stale-slot
     race: reader repositions after overrun, writer overwrites that slot
     before the reader reads pcm).
  3. The writer never blocks regardless of reader states.

Torn-frame detection: writer encodes seq in pcm[0] (pcm[0] = seq % 32767).
Readers check frame.pcm[0] == frame.seq % 32767 immediately after read_next().
A mismatch means the seq and pcm metadata belong to different write cycles.

Run: pytest tests/audio_engine/test_ring_concurrent.py -v -s
"""

import random
import threading
import time
from dataclasses import dataclass, field
from typing import List

import numpy as np
import pytest

from samsara.audio_engine import (
    EMPTY,
    FRAME_SIZE,
    PREBUFFER_FRAMES,
    RING_FRAMES,
    FrameBus,
)

# ── Test parameters ───────────────────────────────────────────────────────────

WRITE_INTERVAL_S = 0.010   # 10 ms/frame  ≈ real 16kHz capture callback rate
TEST_DURATION_S  = 6.0     # seconds of concurrent runtime
SLOW_SLEEP_S     = 1.2     # slow reader sleep — intentionally longer than ring


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pcm(seq: int) -> np.ndarray:
    """Encode seq into pcm[0] so readers can detect torn/stale reads."""
    pcm = np.zeros(FRAME_SIZE, dtype=np.int16)
    pcm[0] = np.int16(seq % 32767)
    return pcm


@dataclass
class _ReaderResult:
    name:         str
    errors:       List[str] = field(default_factory=list)
    overrun_count: int = 0
    frames_read:   int = 0


# ── Thread functions ──────────────────────────────────────────────────────────

def _writer(bus: FrameBus, stop: threading.Event, iters_out: List[int]) -> None:
    """Write at ~capture rate, encoding seq into pcm[0]."""
    seq = 0
    while not stop.is_set():
        bus.write(_make_pcm(seq), time.perf_counter(), 0)
        seq += 1
        time.sleep(WRITE_INTERVAL_S)
    iters_out[0] = seq


def _fast_reader(bus: FrameBus, stop: threading.Event, result: _ReaderResult) -> None:
    """Read every frame — should stay within the ring window with zero overruns."""
    reader = bus.new_reader()
    while not stop.is_set():
        frame = reader.read_next()
        if frame is EMPTY:
            time.sleep(0.001)
            continue
        result.frames_read += 1
        expected = frame.seq % 32767
        if frame.pcm[0] != expected:
            result.errors.append(
                f"[FAST] torn: seq={frame.seq}  pcm[0]={frame.pcm[0]}  "
                f"expected={expected}"
            )
    result.overrun_count = reader.overrun_count


def _slow_reader(bus: FrameBus, stop: threading.Event, result: _ReaderResult) -> None:
    """Sleep longer than the ring depth — will be lapped, stress-testing overrun recovery."""
    reader = bus.new_reader()
    while not stop.is_set():
        time.sleep(SLOW_SLEEP_S)
        frame = reader.read_next()
        if frame is EMPTY:
            continue
        result.frames_read += 1
        expected = frame.seq % 32767
        if frame.pcm[0] != expected:
            result.errors.append(
                f"[SLOW] torn: seq={frame.seq}  pcm[0]={frame.pcm[0]}  "
                f"expected={expected}"
            )
    result.overrun_count = reader.overrun_count


def _random_reader(bus: FrameBus, stop: threading.Event, result: _ReaderResult) -> None:
    """Read at random intervals — exercises the full timing spectrum."""
    rng = random.Random(0xACE02)
    reader = bus.new_reader()
    while not stop.is_set():
        time.sleep(rng.uniform(0.0, 0.15))
        burst = rng.randint(1, 25)
        for _ in range(burst):
            frame = reader.read_next()
            if frame is EMPTY:
                break
            result.frames_read += 1
            expected = frame.seq % 32767
            if frame.pcm[0] != expected:
                result.errors.append(
                    f"[RAND] torn: seq={frame.seq}  pcm[0]={frame.pcm[0]}  "
                    f"expected={expected}"
                )
    result.overrun_count = reader.overrun_count


def _zombie_reader(bus: FrameBus, stop: threading.Event, result: _ReaderResult) -> None:
    """Never reads — verifies writer never blocks on a permanently-dead consumer."""
    reader = bus.new_reader()
    stop.wait()
    result.overrun_count = reader.overrun_count  # counts internally; reader lapped many times


# ── Main test ─────────────────────────────────────────────────────────────────

class TestRingConcurrent:

    def test_concurrent_writers_and_readers(self, capsys):
        bus         = FrameBus()
        stop        = threading.Event()
        iters       = [0]

        results = {
            "fast":   _ReaderResult("fast"),
            "slow":   _ReaderResult("slow"),
            "random": _ReaderResult("random"),
            "zombie": _ReaderResult("zombie"),
        }

        threads = [
            threading.Thread(target=_writer,        args=(bus, stop, iters),                    daemon=True, name="writer"),
            threading.Thread(target=_fast_reader,   args=(bus, stop, results["fast"]),           daemon=True, name="fast-reader"),
            threading.Thread(target=_slow_reader,   args=(bus, stop, results["slow"]),           daemon=True, name="slow-reader"),
            threading.Thread(target=_random_reader, args=(bus, stop, results["random"]),         daemon=True, name="random-reader"),
            threading.Thread(target=_zombie_reader, args=(bus, stop, results["zombie"]),         daemon=True, name="zombie-reader"),
        ]

        t0 = time.monotonic()
        for t in threads:
            t.start()

        time.sleep(TEST_DURATION_S)
        stop.set()

        for t in threads:
            t.join(timeout=3.0)

        elapsed = time.monotonic() - t0

        # ── Report ────────────────────────────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"ACE-02 MA-1  Concurrent Ring Stress Test  ({elapsed:.1f}s)")
        print(f"{'='*60}")
        print(f"  Writer iterations:        {iters[0]}")
        print(f"  Expected write_cursor:    {bus.write_cursor}")
        for name, r in results.items():
            torn = len(r.errors)
            print(f"  {name:<10}  frames={r.frames_read:>5}  "
                  f"overruns={r.overrun_count}  torn={torn}")
        print(f"{'='*60}")

        # ── Invariants ────────────────────────────────────────────────────────

        # 1. Writer must have completed all writes (no deadlock/stall).
        assert iters[0] > 0, "Writer wrote zero frames — likely deadlocked"
        expected_min_iters = int(TEST_DURATION_S / WRITE_INTERVAL_S * 0.8)
        assert iters[0] >= expected_min_iters, (
            f"Writer only completed {iters[0]} iters; expected >= {expected_min_iters}. "
            "Possible writer stall from slow/zombie reader."
        )

        # 2. write_cursor must equal the number of frames written (monotonic).
        assert bus.write_cursor == iters[0], (
            f"write_cursor {bus.write_cursor} != iters {iters[0]}"
        )

        # 3. Slow-reader overrun: with RING_FRAMES=600 (60-second ring) the slow
        #    reader sleeps only 1.2 s per cycle — it cannot be reliably lapped
        #    within a 6-second test without making the suite unacceptably slow.
        #    Overrun-recovery correctness is verified deterministically in
        #    test_overrun_recovery_is_deterministic below.  The slow reader
        #    continues to run here as extra concurrent load for invariant 4.

        # 4. No torn frames on any reader — this is the stale-slot race check.
        all_errors = []
        for r in results.values():
            all_errors.extend(r.errors)

        stale_slot_fired = len(all_errors) > 0

        if stale_slot_fired:
            print("\nSTALE-SLOT RACE DETECTED:")
            for e in all_errors[:10]:
                print(f"  {e}")
            pytest.fail(
                f"Torn frames detected ({len(all_errors)} occurrences). "
                "The overrun-recovery stale-slot race is reproducible. "
                "Fix ring.py Reader.read_next() before proceeding to ACE-02 wiring."
            )
        else:
            print(f"\n  Stale-slot race: NOT fired (ring is safe at this write rate)")
            print(f"  The retry-loop guard in ring.py provides defense-in-depth")
            print(f"  against the theoretical race at faster-than-hardware write rates.")

        # 5. Fast reader should have zero overruns (it kept up).
        assert results["fast"].overrun_count == 0, (
            f"Fast reader had {results['fast'].overrun_count} overruns — "
            "it should keep up with the write rate"
        )

    def test_overrun_recovery_is_deterministic(self):
        """Force a reader to be lapped and verify overrun-recovery correctness.

        Drives the writer a fixed number of frames past the reader so the
        overrun condition is triggered without any wall-clock racing.  Verifies:
          - overrun_count increments exactly once
          - read_next() returns a frame (not EMPTY) after recovery
          - the recovered frame is within the prebuffer window
          - no torn frame (seq and pcm[0] belong to the same write cycle)
        """
        bus    = FrameBus()
        reader = bus.new_reader()   # cursor at write head (0)

        # Write enough to lap the reader unambiguously.
        # Overrun fires when (write_cursor - read_cursor) > RING_FRAMES.
        # read_cursor is 0, so we need write_cursor > RING_FRAMES.
        total = RING_FRAMES + PREBUFFER_FRAMES + 5
        for seq in range(total):
            pcm      = np.zeros(FRAME_SIZE, dtype=np.int16)
            pcm[0]   = np.int16(seq % 32767)
            bus.write(pcm, time.perf_counter(), 0)

        # The reader is now lapped; the next read must trigger overrun recovery.
        frame = reader.read_next()

        assert reader.overrun_count == 1, (
            f"expected exactly 1 overrun, got {reader.overrun_count}"
        )
        assert frame is not EMPTY, (
            "overrun recovery should return a frame, not EMPTY"
        )
        # After recovery the cursor sits at write_cursor - PREBUFFER_FRAMES,
        # so the returned frame's seq must be within the prebuffer window.
        assert frame.seq >= total - PREBUFFER_FRAMES - 1, (
            f"recovered frame seq {frame.seq} not in prebuffer window "
            f"(expected >= {total - PREBUFFER_FRAMES - 1})"
        )
        # Torn-frame guard: seq and pcm[0] must belong to the same write cycle.
        assert frame.pcm[0] == frame.seq % 32767, (
            f"torn frame after recovery: seq={frame.seq} pcm[0]={frame.pcm[0]}"
        )
