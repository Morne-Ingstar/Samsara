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

Native device rate → 16kHz int16 via a numpy-only polyphase resample with
a pre-computed up/down ratio AND pre-designed filter, both initialized in
_open_stream() (non-RT). For 44100Hz input:
  up=160, down=441  →  4410 samples → exactly 1600 samples = FRAME_SIZE.
For 48000Hz input:
  up=1, down=3      →  4800 samples → exactly 1600 samples = FRAME_SIZE.
The output is guaranteed to be FRAME_SIZE via pad/truncate with a warning
flag if the resampler produces an unexpected count.

_polyphase_resample() below is a from-scratch, numpy-only implementation
that is numerically equivalent (see tests/audio_engine/
test_resample_equivalence.py, float32-noise-level tolerance) to
scipy.signal.resample_poly(x, up, down) with its default filter design
and padtype='constant' boundary -- it does NOT call resample_poly itself.

WHY: 2026-07-2x P0 incident. resample_poly calls scipy's array-API-compat
layer (array_namespace(x)) on EVERY invocation, which does an
unconditional getattr(torch, 'Tensor') whenever 'torch' is present in
sys.modules at all -- including while torch is still mid-import in
another thread (sys.modules['torch'] exists from the moment import
*starts*, well before the module body finishes executing and binds
`Tensor`). This crashed the PortAudio realtime callback
(_on_audio_block, below) on EVERY block while the async model loader's
background thread was importing torch, with AttributeError: "partially
initialized module 'torch' has no attribute 'Tensor'".

UNDOCUMENTED ORDERING DEPENDENCY (now documented, was implicit): before
boot-fix commit 7faf4e2, this module's `from scipy.signal import
resample_poly` was a LAZY import inside dictation.py's
_start_ace_engine(), taking ~11.4s (cold scipy.signal import). That
accidentally serialized engine start behind the async model loader's own
torch import in most real boots -- by the time the slow scipy import
finished, torch had usually already finished importing too, so the race
never fired. 7faf4e2 hoisted that import to module load (correctly, for
boot-time reasons unrelated to this), which starts the engine much
earlier and reliably exposed the always-latent race: the engine can now
easily start (and produce its first callback) WHILE torch is still
mid-import in the background thread. Do not "fix" this by re-introducing
a lazy/delayed import anywhere in the ACE startup path -- fix the actual
hazard, which is calling anything array-API-probing from the RT callback
thread at all. firwin (used below, at stream-open only, never per block)
does NOT trigger this probing -- verified empirically; see
_design_polyphase_filter's docstring.

== Consumer lifecycle ==

See ACE-01 engine.py docstring — contract is unchanged.
"""

import collections
import math
import threading
import time
from typing import Any

import numpy as np

from . import device_resolver
from .frame import FRAME_SIZE, PREBUFFER_FRAMES, SAMPLE_RATE
from .ring import FrameBus, Reader
from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)

# sounddevice is imported lazily in start() so the module can be imported
# in test environments without a microphone.


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def _design_polyphase_filter(up: int, down: int) -> tuple[np.ndarray, int, int]:
    """Precompute (h_padded, taps_per_phase, n_pre_remove) for a given
    up/down ratio (already GCD-reduced, coprime). Replicates scipy.signal.
    resample_poly's own default filter design exactly (firwin, window=
    ('kaiser', 5.0), padtype='constant'/cval=0 boundary) -- see
    _polyphase_resample for how these three values are used, and
    tests/audio_engine/test_resample_equivalence.py for the numerical
    verification against the real resample_poly.

    Safe to call while another thread has torch mid-import: firwin does
    NOT go through scipy's array-API-compat probing the way resample_poly
    does (verified empirically -- calling firwin with a broken/partially-
    initialized torch module already present in sys.modules does not
    raise, where calling resample_poly the same way does). Still, this is
    only ever called from _open_stream(), once per stream open -- never
    from the RT callback.
    """
    from scipy.signal import firwin  # noqa: PLC0415 -- stream-open only, see docstring

    max_rate = max(up, down)
    f_c = 1.0 / max_rate
    half_len = 10 * max_rate
    h = firwin(2 * half_len + 1, f_c, window=('kaiser', 5.0)).astype(np.float64)
    h = h * up

    n_pre_pad = down - (half_len % down)
    h = np.concatenate([np.zeros(n_pre_pad), h])
    n_pre_remove = (half_len + n_pre_pad) // down

    # Pad h's length up to a multiple of `up` so every polyphase "phase"
    # (h[phase::up]) has exactly the same number of taps -- the trailing
    # zeros contribute nothing, they just keep _polyphase_resample's loop
    # below rectangular instead of ragged.
    taps_per_phase = -(-len(h) // up)  # ceil division
    h_padded = np.concatenate([h, np.zeros(taps_per_phase * up - len(h))])

    return h_padded, taps_per_phase, n_pre_remove


def _polyphase_resample(
    x: np.ndarray,
    up: int,
    down: int,
    h_padded: np.ndarray,
    taps_per_phase: int,
    n_pre_remove: int,
    n_out: int,
) -> np.ndarray:
    """Numpy-only polyphase resample -- upfirdn-equivalent to
    scipy.signal.resample_poly(x, up, down) with its default filter
    design and zero (padtype='constant') boundary. Verified within
    float32-noise tolerance of the real resample_poly -- see
    tests/audio_engine/test_resample_equivalence.py.

    Deliberately does NOT call scipy.signal.resample_poly or import scipy
    at all: see this module's docstring ("Resampling" / "WHY") for the
    2026-07-2x incident this exists to fix -- resample_poly is not safe
    to call from the RT callback thread while torch may be mid-import
    elsewhere. h_padded/taps_per_phase/n_pre_remove come from
    _design_polyphase_filter(up, down), computed once at stream open.

    Derivation: for output index m (0-indexed into the unsliced,
    conceptual upsample-then-filter-then-downsample result), position
    pos = m * down lands in the upsampled-filter domain; phase = pos % up
    selects which "polyphase branch" of h applies, and x_index = pos //
    up is the input sample the branch is centered on. The real output is
    the slice starting at n_pre_remove (scipy's own centering offset).
    """
    pad = taps_per_phase
    x64 = np.concatenate([
        np.zeros(pad, dtype=np.float64),
        x.astype(np.float64),
        np.zeros(pad, dtype=np.float64),
    ])

    m = np.arange(n_pre_remove, n_pre_remove + n_out)
    pos = m * down
    phase = pos % up
    x_index = pos // up

    y = np.zeros(n_out, dtype=np.float64)
    for j in range(taps_per_phase):
        y += h_padded[phase + j * up] * x64[x_index - j + pad]

    return y.astype(np.float32)


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
        self._resample_state  = None    # (h_padded, taps_per_phase, n_pre_remove, n_out);
                                         # bound in _open_stream(); None = no resampling needed

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
        # (name, hostapi_name) captured on every SUCCESSFUL open. Recovery
        # resolves this pair back to a live index instead of handing
        # sounddevice a bare name, which is unresolvable when the same name
        # exists under several host APIs -- see device_resolver's docstring
        # and the 2026-09-12 incident it records.
        self._device_identity: "tuple[str, str] | None" = None
        self._recovering  = False
        #: True once recovery has exhausted its fast window. The engine keeps
        #: probing in the background, but callers should treat capture as
        #: unavailable and say so exactly once -- see device_lost.
        self._device_lost = False
        self._reconnect_now = threading.Event()
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

        Computes the polyphase resample ratio AND filter (native_rate →
        SAMPLE_RATE) once here so the callback never recomputes them or
        touches scipy at all -- see _polyphase_resample's docstring and
        this module's docstring ("Resampling" / "WHY") for why the
        callback must not call scipy.signal.resample_poly. The resampler
        is stateless per-call (no persistent filter state carried across
        callbacks -- each block is treated as its own zero-padded-at-the-
        edges signal, matching resample_poly's own default behavior).
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

        # Precompute the polyphase filter once; the callback only does
        # array arithmetic against these precomputed values (see
        # _polyphase_resample), never filter design, never scipy.
        if self._up != 1 or self._down != 1:
            h_padded, taps_per_phase, n_pre_remove = _design_polyphase_filter(self._up, self._down)
            n_out = self._blocksize * self._up
            n_out = n_out // self._down + bool(n_out % self._down)
            self._resample_state = (h_padded, taps_per_phase, n_pre_remove, n_out)
        else:
            self._resample_state = None

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

        # Identity for recovery, recorded only after a successful open so a
        # failed attempt can never overwrite a good pair.
        try:
            identity = device_resolver.capture_identity(sd, device)
            if identity is not None:
                self._device_identity = identity
                self._device_name = identity[0]
        except Exception as exc:
            logger.debug(f"[ACE] identity capture skipped: {exc}")

    # ── Device-lost state ─────────────────────────────────────────────────────

    @property
    def device_lost(self) -> bool:
        """True while the input device is gone and the fast recovery window
        has already expired.

        The app layer uses this for the three things the engine must not do
        itself: show the persistent indicator, play the failure earcon ONCE
        per loss rather than per attempt, and refuse hold/wake capture with
        an explanation instead of a silent frames=0 recording. Cleared
        automatically by a successful reopen.
        """
        return self._device_lost

    def request_reconnect_now(self) -> None:
        """Force the background probe to retry immediately.

        Public so a tray action ("Reconnect microphone") can short-circuit
        the 10 s wait without knowing anything about the retry loop.
        """
        self._reconnect_now.set()

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
        thread_registry.spawn("ace-recovery", self._recovery_loop, daemon=True)

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
        # Moved here from _on_stream_finished (the PortAudio finished-
        # callback thread) so play_sound('error') -- which opens an output
        # stream -- never runs on the PortAudio thread during a device-death
        # event (deadlock/hang risk). Still runs before any recovery
        # attempt, just on this normal daemon thread instead.
        if self._on_stream_death:
            try:
                self._on_stream_death()
            except Exception:
                logger.exception("[ACE] on_stream_death callback failed")

        deadline = time.monotonic() + 60.0
        try:
            while self._running and time.monotonic() < deadline:
                time.sleep(2.0)
                if not self._running:
                    return  # a deliberate stop() happened while we waited
                if self._try_reopen(phase="recovery"):
                    return
            if self._running:
                logger.error(
                    "[ACE] Recovery gave up after 60s -- device never reappeared"
                )
                self._device_lost = True
                if self._on_give_up:
                    try:
                        self._on_give_up()
                    except Exception:
                        logger.exception("[ACE] on_give_up callback failed")
                # Keep looking. A device that comes back five minutes later
                # must reconnect on its own -- before this, giving up was
                # permanent until the user restarted or re-picked the mic
                # from the tray.
                self._slow_probe_loop()
        finally:
            self._recovering = False

    def _resolve_device(self):
        """The device argument for a reopen: an INDEX whenever possible.

        Returns (device, note) where note is a human-readable explanation for
        the log when the host API changed. Falls back to the configured
        id/None (system default) only when nothing was recorded -- never to a
        bare name, which is what could not be resolved in the first place.
        """
        import sounddevice as sd

        identity = self._device_identity
        name = identity[0] if identity else self._device_name
        recorded_api = identity[1] if identity else ""
        if not name:
            return (self._config.get('microphone', None), "")
        try:
            index, api, exact = device_resolver.resolve(sd, name, recorded_api)
        except device_resolver.DeviceNotFound as exc:
            raise RuntimeError(str(exc)) from exc
        if exact:
            return (index, "")
        return (index, f"host API changed {recorded_api or '(unrecorded)'} -> {api}")

    def _try_reopen(self, *, phase: str) -> bool:
        """One resolve-and-open attempt. True when the stream is live again."""
        try:
            device, note = self._resolve_device()
            self._teardown_stream_only()  # never two streams alive at once
            self._open_stream(device)
            self.bump_device_epoch()  # signal the discontinuity to consumers
        except Exception as exc:
            logger.debug(f"[ACE] {phase} attempt failed: {exc}")
            return False
        if note:
            logger.warning("[ACE] Recovered on a different backend -- %s", note)
        logger.info("[ACE] Recovery succeeded -- stream rebuilt")
        self._recovering = False
        self._device_lost = False
        if self._on_recovery_success:
            try:
                self._on_recovery_success()
            except Exception:
                logger.exception("[ACE] on_recovery_success callback failed")
        return True

    def _slow_probe_loop(self, interval_s: float = 10.0) -> None:
        """After give-up, probe every `interval_s` forever until it returns.

        Only enumerates (device_resolver.device_present) unless the name is
        actually back, so the steady-state cost of a permanently unplugged
        device is one query_devices() call every ten seconds. Runs on the
        same recovery daemon thread -- never on the Qt or boot thread.
        """
        import sounddevice as sd

        name = (self._device_identity or (self._device_name, ""))[0]
        while self._running:
            forced = self._reconnect_now.wait(timeout=interval_s)
            self._reconnect_now.clear()
            if not self._running:
                return
            try:
                present = device_resolver.device_present(sd, name) if name else True
            except Exception as exc:
                logger.debug(f"[ACE] probe failed: {exc}")
                present = False
            if not present and not forced:
                continue
            if self._try_reopen(phase="reconnect probe"):
                return

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
          - No dynamic allocation beyond what numpy internally does (numpy
            releases the GIL; GC pressure is acceptable per ACE-00).
          - No scipy calls of any kind -- see this module's docstring
            ("Resampling" / "WHY") for the 2026-07-2x incident where
            scipy.signal.resample_poly's array-API-compat probing crashed
            this exact callback while torch was mid-import elsewhere.
          - write_cursor increment is the commit fence (see ring.py).
        """
        t = time.perf_counter()

        if status:
            self._overflow_count += 1   # GIL-atomic plain int

        flat = indata[:, 0]   # mono float32, length = blocksize

        # Resample native rate → 16kHz int16. _resample_state is bound in
        # _open_stream() — no filter design, no scipy, no import cost here.
        if self._resample_state is not None:
            h_padded, taps_per_phase, n_pre_remove, n_out = self._resample_state
            resampled = _polyphase_resample(
                flat, self._up, self._down,
                h_padded, taps_per_phase, n_pre_remove, n_out,
            )
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
