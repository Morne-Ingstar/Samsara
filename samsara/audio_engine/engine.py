"""AudioCaptureEngine — ACE-02: real PortAudio capture alongside the legacy path.

The engine owns exactly one sounddevice InputStream. It resamples native-rate
audio to 16kHz int16 once at the head, writes the FrameBus, and exposes a
consumer registry so multiple downstream readers can process the same stream.

The legacy capture path in dictation.py is UNCHANGED. The engine runs in
parallel behind the `ace_debug_capture: true` config flag and is a passive
observer until ACE-03 begins migrating individual modes.

== Concurrency (Model A, ACE-00 cleared) ==

Single writer (the PortAudio callback thread), many readers (registered
consumers). No locks on the read/write path. See ring.py for the full
atomicity argument.

== Resampling ==

Native device rate → 16kHz int16 via scipy.signal.resample_poly with a
pre-computed up/down ratio initialized in start(). For 44100Hz input:
  up=160, down=441  →  4410 samples → exactly 1600 samples = FRAME_SIZE.
For 48000Hz input:
  up=1, down=3      →  4800 samples → exactly 1600 samples = FRAME_SIZE.
The output is guaranteed to be FRAME_SIZE via pad/truncate with a warning
flag if the resampler produces an unexpected count.

== Consumer lifecycle ==

See ACE-01 engine.py docstring — contract is unchanged.
"""

import collections
import math
import threading
import time
from typing import Any

import numpy as np

from .frame import FRAME_SIZE, PREBUFFER_FRAMES, SAMPLE_RATE
from .ring import FrameBus, Reader

# sounddevice is imported lazily in start() so the module can be imported
# in test environments without a microphone.


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


class AudioCaptureEngine:
    """Single owner of the PortAudio stream and FrameBus writer.

    Args:
        ring:   FrameBus instance. Engine is the sole writer.
        config: App config dict. Reads 'microphone' for device selection.
    """

    def __init__(self, ring: FrameBus, config: dict | None = None) -> None:
        self._ring    = ring
        self._config  = config or {}
        self._running = False

        self._stream          = None    # sounddevice.InputStream
        self._native_rate     = SAMPLE_RATE
        self._up:   int       = 1
        self._down: int       = 1
        self._blocksize:int   = 0
        self._size_warned     = False   # flag set in callback, logged on metrics()
        self._resample_poly   = None    # bound in start(); None = no resampling needed

        # Overflow counter — plain int, GIL-atomic (ACE-00)
        self._overflow_count: int = 0

        # Callback duration histogram (pre-allocated deque, no growth in callback)
        self._cb_durations: collections.deque = collections.deque(maxlen=10000)

        self._consumers: list[tuple[str, Reader]] = []
        self._registry_lock = threading.Lock()

        self._dropped_frames: int = 0
        self._epoch_log: list[tuple[float, int]] = []

    # ── Stream lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Open a sounddevice InputStream and begin writing to the ring.

        Computes the polyphase resample ratio (native_rate → SAMPLE_RATE)
        once here so the callback never recomputes it. The resampler
        (scipy.signal.resample_poly) is stateless per-call, so no persistent
        filter state needs to be carried across callbacks.
        """
        if self._running:
            return

        import sounddevice as sd

        device = self._config.get('microphone', None)

        # If the caller passed an explicit rate (e.g. to match a concurrent
        # stream on the same WASAPI device), use it directly; otherwise query.
        # WASAPI shared mode serves multiple streams through separate clients.
        # When two clients request different sample rates, Windows routes them
        # via different audio engines and only one gets callbacks reliably.
        # Forcing the ACE engine to the same rate as the app's other streams
        # (wake word, prebuffer) ensures they share a single WASAPI session.
        explicit_rate = self._config.get('_capture_rate')
        if explicit_rate:
            self._native_rate = int(explicit_rate)
        else:
            try:
                dev_info = sd.query_devices(device, kind='input')
                self._native_rate = int(dev_info['default_samplerate'])
            except Exception as exc:
                print(f"[ACE] device query failed ({exc}), using default")
                self._native_rate = SAMPLE_RATE

        # Pre-compute resample ratio (GCD reduction)
        g = _gcd(self._native_rate, SAMPLE_RATE)
        self._up   = SAMPLE_RATE          // g
        self._down = self._native_rate    // g

        # blocksize: native_rate * FRAME_MS/1000  (e.g. 44100*0.1 = 4410)
        from .frame import FRAME_MS
        self._blocksize = int(self._native_rate * FRAME_MS // 1000)

        # Pre-import resample_poly so the callback never pays the import cost.
        # The function reference is stored on self; the callback uses it directly.
        if self._up != 1 or self._down != 1:
            from scipy.signal import resample_poly as _rp
            self._resample_poly = _rp
        else:
            self._resample_poly = None

        print(
            f"[ACE] Starting engine: device={device!r}  "
            f"native={self._native_rate}Hz  "
            f"resample={self._up}/{self._down}  "
            f"blocksize={self._blocksize}"
        )

        self._stream = sd.InputStream(
            samplerate  = self._native_rate,
            channels    = 1,
            dtype       = np.float32,
            blocksize   = self._blocksize,
            device      = device,
            callback    = self._on_audio_block,
        )
        self._running = True
        self._stream.start()

    def stop(self) -> None:
        """Stop and close the PortAudio stream."""
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                print(f"[ACE] stream stop error: {exc}")
            self._stream = None
        print("[ACE] Engine stopped.")

    # ── Capture callback (HOT PATH — no locks, no logging, no allocation) ────

    def _on_audio_block(
        self,
        indata:    np.ndarray,
        frames:    int,
        time_info: Any,
        status:    Any,
    ) -> None:
        """PortAudio callback — runs on PortAudio's realtime thread.

        Rules (ACE-00 discipline):
          - No locks.
          - No logging / print.
          - No dynamic allocation beyond what numpy/scipy internally does
            (these release the GIL; GC pressure is acceptable per ACE-00).
          - write_cursor increment is the commit fence (see ring.py).
        """
        t = time.perf_counter()

        if status:
            self._overflow_count += 1   # GIL-atomic plain int

        flat = indata[:, 0]   # mono float32, length = blocksize

        # Resample native rate → 16kHz int16.
        # _resample_poly is bound in start() — no import cost in the callback.
        if self._resample_poly is not None:
            resampled = self._resample_poly(flat, self._up, self._down)
        else:
            resampled = flat

        n = len(resampled)
        if n == FRAME_SIZE:
            pcm_f32 = resampled
        elif n < FRAME_SIZE:
            # Pad with zeros — set flag for metric logging outside callback
            pcm_f32 = np.zeros(FRAME_SIZE, dtype=np.float32)
            pcm_f32[:n] = resampled
            self._size_warned = True
        else:
            # Truncate — set flag for metric logging outside callback
            pcm_f32 = resampled[:FRAME_SIZE]
            self._size_warned = True

        pcm = np.clip(pcm_f32 * 32767.0, -32768, 32767).astype(np.int16)

        self._ring.write(pcm, t, self._ring.device_epoch)

        self._cb_durations.append(time.perf_counter() - t)

    # ── Consumer management ───────────────────────────────────────────────────

    def register_consumer(self, name: str) -> Reader:
        """Register a named consumer and return its read cursor.

        The Reader starts at the current write head. Call
        reader.rewind(PREBUFFER_FRAMES) before the read loop to access
        rolling pre-trigger history.
        """
        reader = self._ring.new_reader(name)
        with self._registry_lock:
            self._consumers.append((name, reader))
        return reader

    def unregister_consumer(self, reader: Reader) -> None:
        """Remove a consumer and invalidate its Reader (idempotent)."""
        with self._registry_lock:
            self._consumers = [
                (n, r) for (n, r) in self._consumers if r is not reader
            ]
        reader.invalidate()

    # ── Epoch management ──────────────────────────────────────────────────────

    def bump_device_epoch(self) -> int:
        """Increment device_epoch. [LOCKED] discontinuity rule — see ring.py."""
        new_epoch = self._ring.bump_device_epoch()
        self._epoch_log.append((time.perf_counter(), new_epoch))
        return new_epoch

    # ── Metrics ───────────────────────────────────────────────────────────────

    def metrics(self) -> dict:
        """Return engine health metrics.

        Callback duration histogram is now populated from real capture data.
        All other fields match the ACE-01 schema (no consumers depend on
        previously-placeholder values changing).
        """
        if self._size_warned:
            print("[ACE] WARNING: resampler produced unexpected frame size — "
                  "check native_rate / FRAME_SIZE ratio")
            self._size_warned = False

        with self._registry_lock:
            consumers_snapshot = list(self._consumers)

        wc = self._ring.write_cursor

        per_overruns = {n: r.overrun_count  for n, r in consumers_snapshot}
        per_lag      = {n: max(0, wc - r._read_cursor) for n, r in consumers_snapshot}

        if self._cb_durations:
            a = np.array(self._cb_durations) * 1000.0   # ms
            cb_p50 = float(np.percentile(a, 50))
            cb_p95 = float(np.percentile(a, 95))
            cb_p99 = float(np.percentile(a, 99))
            cb_max = float(a.max())
        else:
            cb_p50 = cb_p95 = cb_p99 = cb_max = 0.0

        return {
            'dropped_frames':         self._dropped_frames,
            'overflow_count':         self._overflow_count,
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
        return (
            f"AudioCaptureEngine(running={self._running}, "
            f"consumers={len(self._consumers)}, "
            f"write_cursor={self._ring.write_cursor})"
        )
