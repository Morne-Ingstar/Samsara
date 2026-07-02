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

from scipy.signal import resample_poly
from .frame import FRAME_SIZE, PREBUFFER_FRAMES, SAMPLE_RATE
from .ring import FrameBus, Reader
from samsara.log import get_logger

logger = get_logger(__name__)

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

    def __init__(
        self,
        ring: FrameBus,
        config: dict | None = None,
        on_stream_death: "Any" = None,
        on_recovery_success: "Any" = None,
        on_give_up: "Any" = None,
    ) -> None:
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

        # ── Disconnect/reconnect recovery (device unplugged mid-session) ──
        # device_name is the STABLE identifier recovery re-resolves against
        # (PortAudio indices shift on re-enumeration; names don't). Optional
        # callables let the caller (DictationApp) earcon/log/pause-timer
        # without this engine knowing anything about the app -- same
        # constructor-injected-callable pattern as SessionModeManager.
        self._device_name: "str | None" = None
        self._recovering  = False
        self._on_stream_death     = on_stream_death
        self._on_recovery_success = on_recovery_success
        self._on_give_up          = on_give_up

    # ── Stream lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Open a sounddevice InputStream and begin writing to the ring."""
        if self._running:
            return
        # Stashed for recovery only -- the initial open below is UNCHANGED
        # from before (still opens by config['microphone'], the already-
        # reconciled index/None) so first-boot behavior is untouched.
        self._device_name = self._config.get('microphone_name')
        device = self._config.get('microphone', None)
        self._open_stream(device)
        self._running = True

    def _open_stream(self, device) -> None:
        """Build the polyphase resample ratio for `device` and open the
        InputStream. Used by both start() and the recovery loop -- a
        recovered device may have a different native rate than the one
        that just died, so the ratio is always recomputed here, never
        cached from a prior open.

        Computes the polyphase resample ratio (native_rate → SAMPLE_RATE)
        once here so the callback never recomputes it. The resampler
        (scipy.signal.resample_poly) is stateless per-call, so no persistent
        filter state needs to be carried across callbacks.
        """
        import sounddevice as sd

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
                logger.exception(f"[ACE] device query failed ({exc}), using default")
                self._native_rate = SAMPLE_RATE

        # Pre-compute resample ratio (GCD reduction)
        g = _gcd(self._native_rate, SAMPLE_RATE)
        self._up   = SAMPLE_RATE          // g
        self._down = self._native_rate    // g

        # blocksize: native_rate * FRAME_MS/1000  (e.g. 44100*0.1 = 4410)
        from .frame import FRAME_MS
        self._blocksize = int(self._native_rate * FRAME_MS // 1000)

        # The function reference is stored on self; the callback uses it directly.
        if self._up != 1 or self._down != 1:
            self._resample_poly = resample_poly
        else:
            self._resample_poly = None

        logger.info(
            f"[ACE] Opening stream: device={device!r}  "
            f"native={self._native_rate}Hz  "
            f"resample={self._up}/{self._down}  "
            f"blocksize={self._blocksize}"
        )

        self._stream = sd.InputStream(
            samplerate       = self._native_rate,
            channels         = 1,
            dtype            = np.float32,
            blocksize        = self._blocksize,
            device           = device,
            callback         = self._on_audio_block,
            finished_callback= self._on_stream_finished,
        )
        self._stream.start()

    def stop(self) -> None:
        """Stop and close the PortAudio stream (deliberate, external stop --
        e.g. app shutdown or a user-initiated mic switch). Clears _running
        BEFORE tearing down the stream so _on_stream_finished can tell this
        apart from an unexpected death (which fires with _running still
        True) and skip triggering recovery."""
        self._running   = False
        self._recovering = False
        self._teardown_stream_only()
        logger.info("[ACE] Engine stopped.")

    def _teardown_stream_only(self) -> None:
        """Close self._stream without touching _running -- used both by
        stop() and internally between recovery retry attempts, where the
        engine must stay "supposed to be running" throughout."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                logger.debug(f"[ACE] stream teardown error: {exc}")
            self._stream = None

    # ── Disconnect/reconnect recovery ─────────────────────────────────────────

    def _on_stream_finished(self) -> None:
        """sounddevice finished_callback -- fires whenever the stream stops,
        for ANY reason (deliberate stop() or an unexpected device death).

        Runs on a PortAudio-managed thread, near-instantly after the stream
        actually stops -- this is the detection mechanism (no separate
        healthy-state polling thread; requirement is "no polling while
        healthy" and this is purely event-driven).
        """
        if not self._running:
            return  # deliberate stop() already cleared this -- not a death
        if self._recovering:
            return  # already handling a prior death; avoid a second thread
        logger.error("[ACE] Input stream died unexpectedly -- entering recovery")
        self._recovering = True
        if self._on_stream_death:
            try:
                self._on_stream_death()
            except Exception:
                logger.exception("[ACE] on_stream_death callback failed")
        threading.Thread(
            target=self._recovery_loop, daemon=True, name="ace-recovery",
        ).start()

    def _recovery_loop(self) -> None:
        """Poll for the configured device every 2s for up to 60s.

        Re-resolves by NAME (self._device_name, captured at start()) so a
        BT device reappearing under a different PortAudio index is still
        recognized as the same device -- sounddevice accepts a device name
        string directly and re-resolves it fresh on every InputStream()
        call, so no index caching/matching is needed here at all. Falls
        back to the originally configured id/None (system default) only
        when no name was stored (older config, or "auto" device selection,
        where None always means "whatever the OS default is right now").
        """
        device = self._device_name or self._config.get('microphone', None)
        deadline = time.monotonic() + 60.0
        try:
            while self._running and time.monotonic() < deadline:
                time.sleep(2.0)
                if not self._running:
                    return  # a deliberate stop() happened while we waited
                try:
                    self._teardown_stream_only()  # never two streams alive at once
                    self._open_stream(device)
                    self.bump_device_epoch()  # signal the discontinuity to consumers
                    logger.info("[ACE] Recovery succeeded -- stream rebuilt")
                    self._recovering = False
                    if self._on_recovery_success:
                        try:
                            self._on_recovery_success()
                        except Exception:
                            logger.exception("[ACE] on_recovery_success callback failed")
                    return
                except Exception as exc:
                    logger.debug(f"[ACE] Recovery attempt failed, retrying in 2s: {exc}")
            if self._running:
                logger.error(
                    "[ACE] Recovery gave up after 60s -- device never reappeared"
                )
                if self._on_give_up:
                    try:
                        self._on_give_up()
                    except Exception:
                        logger.exception("[ACE] on_give_up callback failed")
        finally:
            self._recovering = False

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
            logger.warning("[ACE] WARNING: resampler produced unexpected frame size — "
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
