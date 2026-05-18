"""AudioCaptureEngine — interface definition (ACE-01, no PortAudio yet).

The engine is the single owner of the PortAudio stream (added in ACE-02).
It resamples native-rate audio to 16kHz int16 (ACE-02), writes the FrameBus,
and manages consumer registration/lifecycle.

This file defines the full method surface of AudioCaptureEngine so that:
  - Consumers can be coded against a stable interface before ACE-02.
  - The consumer lifecycle contract is formally defined now, not retrofitted.
  - Tests can prove the interface seam exists (start/stop raise ACE-02 stubs).

PortAudio (sounddevice) is NOT imported here. It will be added in ACE-02
alongside the real capture implementation.

== Consumer lifecycle contract ==

register_consumer(name) -> Reader:
    Records a (name, Reader) pair in the engine's consumer registry.
    Returns a Reader positioned at the current write head with no history.
    To access pre-trigger audio, call reader.rewind(PREBUFFER_FRAMES) once
    before starting your read loop.

unregister_consumer(reader) -> None:
    Removes the (name, reader) record from the registry and calls
    reader.invalidate(). Any further call to reader.read_next() or
    reader.rewind() on an unregistered Reader raises RuntimeError.

Registry does NOT leak: unregister_consumer guarantees the Reader object
is dropped from the registry dict and invalidated atomically under the
registry lock. The FrameBus's lossy design handles the zombie case: if a
consumer vanishes without calling unregister_consumer, its Reader simply
gets lapped (overrun_count increments) without stalling capture. The
registry will hold a reference until an explicit unregister; this is the
only memory-leak risk and it is bounded by the number of registered
consumers (typically 5-10).

== [LOCKED] Epoch / discontinuity contract ==

bump_device_epoch() delegates to FrameBus.bump_device_epoch(). The engine
passes the new epoch to every subsequent ring.write() call (ACE-02). The
epoch change is the transport-level signal that the audio stream is not
contiguous. Consumer policy (abort utterance, reset VAD state) is
consumer-side; the epoch value on each Frame is the observable trigger.
No per-consumer epoch policy is permitted — all consumers see the same
epoch change at the same frame boundary.

== Observability ==

metrics() returns a dict with all required fields populated as placeholders
in ACE-01. Real values are populated in ACE-02+ as the capture path,
histogram, and lag tracking are implemented.
"""

import threading
from typing import Any

from .frame import PREBUFFER_FRAMES
from .ring import FrameBus, Reader


class AudioCaptureEngine:
    """Single owner of the PortAudio stream and FrameBus writer.

    Instantiate with a pre-constructed FrameBus. Multiple engine instances
    sharing a bus are not supported (single-writer invariant).

    Args:
        ring:   A FrameBus instance. The engine holds a reference but does
                not own the FrameBus's lifecycle — callers may inspect the
                bus directly (e.g. in tests).
        config: Optional app config dict. Reserved for ACE-02 (device
                selection, sample rate, blocksize). Ignored in ACE-01.
    """

    def __init__(self, ring: FrameBus, config: dict | None = None) -> None:
        self._ring    = ring
        self._config  = config or {}
        self._running = False

        # Consumer registry: list of (name, Reader) tuples.
        # Protected by _registry_lock for register/unregister operations.
        # The registry is NOT on the hot audio path; lock contention here
        # does not affect capture timing.
        self._consumers: list[tuple[str, Reader]] = []
        self._registry_lock = threading.Lock()

        # Metrics storage (populated progressively from ACE-02 onwards)
        self._dropped_frames: int = 0
        self._epoch_log: list[tuple[float, int]] = []   # (timestamp, epoch)
        # Callback duration histogram (p50/p95/p99/max) — ACE-02 populates
        self._cb_durations: list[float] = []

    # ── Stream lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Open the PortAudio stream and begin writing to the ring.

        NOT IMPLEMENTED — PortAudio capture is added in ACE-02.

        Raises:
            NotImplementedError: always, until ACE-02.
        """
        raise NotImplementedError(
            "AudioCaptureEngine.start() is not implemented yet. "
            "PortAudio stream setup lands in ACE-02."
        )

    def stop(self) -> None:
        """Stop the PortAudio stream.

        NOT IMPLEMENTED — PortAudio capture is added in ACE-02.

        Raises:
            NotImplementedError: always, until ACE-02.
        """
        raise NotImplementedError(
            "AudioCaptureEngine.stop() is not implemented yet. "
            "PortAudio stream teardown lands in ACE-02."
        )

    # ── Consumer management ───────────────────────────────────────────────────

    def register_consumer(self, name: str) -> Reader:
        """Register a named consumer and return its read cursor.

        The returned Reader is positioned at the current write head with
        no pre-trigger history. Call reader.rewind(PREBUFFER_FRAMES) to
        access the rolling prebuffer window before starting the read loop.

        Args:
            name: human-readable consumer identifier (e.g. "vad",
                  "wake_detector"). Used in metrics and log output.
                  Duplicate names are permitted (the registry stores all
                  (name, reader) pairs).

        Returns:
            A Reader owned by this engine's registry. Do not share Readers
            between threads without external synchronisation.
        """
        reader = self._ring.new_reader()
        with self._registry_lock:
            self._consumers.append((name, reader))
        return reader

    def unregister_consumer(self, reader: Reader) -> None:
        """Remove a consumer from the registry and invalidate its Reader.

        After this call, any attempt to call reader.read_next() or
        reader.rewind() raises RuntimeError. The caller must not retain
        a reference to the Reader beyond this call.

        Args:
            reader: the Reader previously returned by register_consumer().
                    If the reader is not found in the registry, this is a
                    no-op (idempotent; safe to call on already-removed readers).
        """
        with self._registry_lock:
            self._consumers = [
                (n, r) for (n, r) in self._consumers if r is not reader
            ]
        reader.invalidate()

    # ── PortAudio callback stub ───────────────────────────────────────────────

    def _on_audio_block(
        self,
        indata: Any,
        frames: int,
        time_info: Any,
        status: Any,
    ) -> None:
        """PortAudio stream callback — implemented in ACE-02.

        In ACE-02 this will:
          1. Measure callback entry time for the duration histogram.
          2. Check status for overflow/underflow; update metrics.
          3. Resample indata from native device rate to 16kHz int16.
          4. Call self._ring.write(pcm, t_capture, self._ring.device_epoch).

        Args:
            indata:    raw audio block from PortAudio (float32, native rate).
            frames:    number of frames in indata.
            time_info: PortAudio timing struct.
            status:    PortAudio status flags (overflow / underflow).
        """

    # ── Epoch management ──────────────────────────────────────────────────────

    def bump_device_epoch(self) -> int:
        """Increment the device epoch and record the change in the epoch log.

        [LOCKED] DISCONTINUITY RULE: call this whenever the audio stream
        is reopened (device switch, BT reconnect, sample-rate change,
        stream recovery). The new epoch is passed to all subsequent
        ring.write() calls so consumers see the boundary on the frame
        that follows.

        Returns:
            The new device_epoch value.
        """
        import time
        new_epoch = self._ring.bump_device_epoch()
        self._epoch_log.append((time.perf_counter(), new_epoch))
        return new_epoch

    # ── Metrics ───────────────────────────────────────────────────────────────

    def metrics(self) -> dict:
        """Return a snapshot of engine health metrics.

        All histogram fields are placeholder zeros in ACE-01. They will be
        populated by the real capture path in ACE-02. The dict shape is
        defined here so consumers can be coded against a stable schema.

        Returns:
            dict with keys:
                dropped_frames (int):     total frames where seq gap > 1
                                          (populated ACE-02).
                per_consumer_overruns (dict[str, int]):
                                          name -> overrun count for each
                                          registered consumer.
                per_consumer_lag (dict[str, int]):
                                          name -> frames behind write cursor.
                cb_duration_p50_ms (float):  callback duration histogram.
                cb_duration_p95_ms (float):  placeholder 0.0 until ACE-02.
                cb_duration_p99_ms (float):  placeholder 0.0 until ACE-02.
                cb_duration_max_ms (float):  placeholder 0.0 until ACE-02.
                device_epoch_log (list[tuple[float,int]]):
                                          [(perf_counter_ts, epoch), ...].
                write_cursor (int):       current ring write position.
        """
        with self._registry_lock:
            consumers_snapshot = list(self._consumers)

        wc = self._ring.write_cursor

        per_overruns = {n: r.overrun_count for n, r in consumers_snapshot}
        per_lag      = {n: max(0, wc - r._read_cursor) for n, r in consumers_snapshot}

        # Histogram: populated in ACE-02 from self._cb_durations
        if self._cb_durations:
            import numpy as _np
            a = _np.array(self._cb_durations) * 1000  # to ms
            cb_p50 = float(_np.percentile(a, 50))
            cb_p95 = float(_np.percentile(a, 95))
            cb_p99 = float(_np.percentile(a, 99))
            cb_max = float(a.max())
        else:
            cb_p50 = cb_p95 = cb_p99 = cb_max = 0.0

        return {
            'dropped_frames':         self._dropped_frames,
            'per_consumer_overruns':  per_overruns,
            'per_consumer_lag':       per_lag,
            'cb_duration_p50_ms':     cb_p50,
            'cb_duration_p95_ms':     cb_p95,
            'cb_duration_p99_ms':     cb_p99,
            'cb_duration_max_ms':     cb_max,
            'device_epoch_log':       list(self._epoch_log),
            'write_cursor':           wc,
        }

    def __repr__(self) -> str:
        n = len(self._consumers)
        return (
            f"AudioCaptureEngine(running={self._running}, "
            f"consumers={n}, "
            f"write_cursor={self._ring.write_cursor})"
        )
