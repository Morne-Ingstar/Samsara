# Track B Code Review — Audio Engine, Wake Word, Echo Cancellation

**Files:** B1 (frame + ring) · B2 (engine + debug_recorder) · B3 (dictation_consumer + continuous_consumer) · B4 (wake_consumer + wake_word_matcher) · B5 (wake_detector + wake_corrections) · B6 (echo_cancel + calibration + audio_switch)

**Pre-read:** `C:\Users\Morne\Documents\Claude\aec_investigation.md` — documents 3–8% actual AEC suppression vs 60–80% expected; root-causes latency_ms=30 as wrong for WASAPI; warns against raising step_size while misaligned.

---

## HIGH — Fix before next release

---

### B-H1 · `EchoCanceller.process` — latency offset fetched but discarded
**File:** `samsara/echo_cancel.py:479–487`

```python
ref = self._loopback.get_recent(n + self._latency_samples)

if len(ref) >= n + self._latency_samples:
    ref = ref[:n]          # <-- discards the offset window, returns freshest n samples
elif len(ref) >= n:
    ref = ref[:n]          # <-- same, both branches identical
else:
    return mic_audio
```

`get_recent(n + latency_samples)` is called specifically to obtain enough history to shift by `latency_samples`. The full-length branch should return `ref[latency_samples : latency_samples + n]` — the window that was playing through the speaker `latency_ms` ago. Instead both branches return `ref[:n]` (most recent). The latency offset is requested, then thrown away. The NLMS filter correlates mic audio against reference audio from the *wrong* time window; with the signals ~100ms out of phase at real WASAPI latencies, the filter cannot converge and actively adapts in the wrong direction.

This is the primary code-level cause of the 3–8% suppression documented in the investigation. Even with correct `latency_ms`, this bug alone produces near-zero cancellation.

**Fix:**
```python
ref = self._loopback.get_recent(n + self._latency_samples)

if len(ref) >= n + self._latency_samples:
    ref = ref[self._latency_samples : self._latency_samples + n]
elif len(ref) >= n:
    ref = ref[:n]   # not enough history yet — best effort, filter may be noisy
else:
    return mic_audio
```

---

## MEDIUM — Address soon, not blocking

---

### B-M1 · `EchoCanceller` — default `latency_ms=30` is wrong for Windows WASAPI
**File:** `samsara/echo_cancel.py:383`

```python
def __init__(
    self,
    ...
    latency_ms: float = 30.0,   # <-- wrong default for WASAPI shared mode
):
```

Windows WASAPI shared-mode output latency is 80–180 ms (empirically measured in `aec_investigation.md`). At 16 kHz, 30 ms = 480 samples; 100 ms = 1600 samples. Even after fixing B-H1, the filter receives a reference window displaced by 70–150 ms from the actual echo — still sufficient to prevent convergence. `calibrate_lag()` measures the real delay via chirp cross-correlation and `set_latency()` applies it, but neither is called automatically. The system ships with a broken default and no self-healing path.

Note: the investigation explicitly warns that raising `step_size` is not the fix — it causes divergence on the misaligned signal. The only correct path is accurate latency.

**Fix (code):** Change the default to a value known to be within the WASAPI range and add a startup call:

```python
latency_ms: float = 120.0,   # WASAPI shared mode typical; replaced by calibrate_lag()
```

```python
# At app startup, after EchoCanceller.start():
if echo_canceller.is_active:
    measured_ms = echo_canceller.calibrate_lag()
    if measured_ms is not None:
        echo_canceller.set_latency(measured_ms)
```

**ARC flag:** Whether calibration runs at every startup, once per device (persisted to config), or on-demand is an architectural decision. Routing to ARC before implementing.

---

### B-M2 · `DictationSessionConsumer.cancel` — `_frames.clear()` races after join timeout
**File:** `samsara/audio_engine/dictation_consumer.py:130–138`

```python
if thread is not None:
    thread.join(timeout=2.0)
    self._drain_thread = None
self._frames.clear()          # <-- no lock; thread may still be running
self._active = False
```

If `_hold_drain_loop` does not return within 2 seconds (a frozen transcription call elsewhere on the GIL, GC pause, etc.), `join()` returns while the thread is alive. The subsequent `self._frames.clear()` runs concurrently with `_hold_drain_loop`'s `self._frames.append(...)`, which is a list size-change race. The practical risk is low (2 s is a generous timeout for an audio-only loop that sleeps 5 ms between polls), but it is a real race.

**Fix:**
```python
if thread is not None:
    thread.join(timeout=2.0)
    self._drain_thread = None
lock = getattr(self, '_frames_lock', None)
if lock is not None:
    with lock:
        self._frames.clear()
else:
    self._frames.clear()
self._active = False
```

The `getattr` guard is needed because `_frames_lock` is created in `activate()` — `cancel()` is documented as safe to call without a prior `activate()`.

---

### B-M3 · `LoopbackCapture._stream_callback` — lock held across resampling in audio callback
**File:** `samsara/echo_cancel.py:200–210`

```python
with self._lock:
    n = len(audio)
    ...
    if self._device_rate != self.target_rate:
        audio = self._resample(audio, ...)   # <-- inside the lock
    ...
    self._write_pos = end % buf_len
```

`_resample()` allocates a NumPy output array and does float arithmetic, releasing the GIL internally. While the GIL is released, `get_recent()` on the processing thread can acquire `self._lock` — but `_stream_callback` holds it and is waiting for the GIL back. This creates lock-ordering contention between the PortAudio realtime thread and the processing thread. With a 50 ms PyAudio buffer and a slow `get_recent()` caller (e.g., during AEC with large `latency_samples`), the callback can miss its deadline and cause a dropout.

**Fix:** Move resampling outside the lock; only hold it for the ring-buffer write:
```python
def _stream_callback(self, in_data, frame_count, time_info, status):
    if not self._running:
        return (None, pyaudio.paComplete)
    try:
        audio = np.frombuffer(in_data, dtype=np.float32)
        if self._device_channels > 1:
            audio = audio.reshape(-1, self._device_channels).mean(axis=1)
        if self._device_rate != self.target_rate:
            audio = self._resample(audio, self._device_rate, self.target_rate)
        with self._lock:   # lock only for the write
            n = len(audio)
            buf_len = len(self._buffer)
            end = self._write_pos + n
            if end <= buf_len:
                self._buffer[self._write_pos:end] = audio
            else:
                first = buf_len - self._write_pos
                self._buffer[self._write_pos:] = audio[:first]
                self._buffer[:n - first] = audio[first:]
            self._write_pos = end % buf_len
    except Exception:
        pass
    return (None, pyaudio.paContinue)
```

---

### B-M4 · `LoopbackCapture._resample` — linear interpolation accumulates clock drift
**File:** `samsara/echo_cancel.py:217–229`

```python
n_out = int(len(audio) * ratio)   # truncates — drops or repeats 1 sample per chunk
indices = np.arange(n_out) / ratio
...
return audio[idx_floor] * (1 - frac) + audio[idx_ceil] * frac
```

`int(...)` truncates rather than rounds. At 48 kHz → 16 kHz the ratio is exactly 1/3 (clean case), but at 44.1 kHz → 16 kHz the ratio is 0.3628...; each 50 ms callback produces `int(2205 * 0.3628) = 800` samples where 800.988... is exact. Over time this drifts ~1 sample/callback = ~20 samples/second = 1.25 ms/second. After 30 seconds the reference is ~37 ms misaligned beyond the configured `latency_ms`. Even with B-H1 and B-M1 fixed, the NLMS filter will decorrelate as drift accumulates.

Additionally, linear interpolation introduces aliasing at Nyquist/3 frequencies, degrading the reference signal quality the filter sees.

**Fix:** Replace with `scipy.signal.resample_poly` using integer ratio approximation:
```python
from math import gcd

@staticmethod
def _resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return audio
    from scipy.signal import resample_poly
    g = gcd(src_rate, dst_rate)
    return resample_poly(audio, dst_rate // g, src_rate // g).astype(np.float32)
```

`resample_poly` uses a polyphase FIR filter (phase-correct, anti-aliased) and produces exactly `ceil(len * up/down)` samples with no drift. The additional scipy import is a one-time cost.

**ARC flag:** `scipy.signal` in the audio callback. In practice `resample_poly` releases the GIL during the C extension; whether this is acceptable in the callback context should be confirmed.

---

### B-M5 · `wake_consumer`, `dictation_consumer` — direct private `_read_cursor` mutation
**File:** `samsara/audio_engine/wake_consumer.py:78`, `samsara/audio_engine/dictation_consumer.py:89`

```python
# wake_consumer.py:78
self._reader._read_cursor = self._engine._ring.write_cursor

# dictation_consumer.py:89
self._reader._read_cursor = self._engine._ring.write_cursor
```

Both consumers need to snap the cursor to the current write head before starting their drain loop (to skip stale ring history from the gap between recordings). They do this by mutating `Reader._read_cursor` directly. `Reader.__slots__` exposes this as an implementation detail; if the ring ever adds bounds validation or cursor versioning, these mutations bypass it silently.

**Fix:** Add a public method on `Reader`:
```python
def snap_to_head(self) -> None:
    """Advance read cursor to the current write head, discarding all buffered history."""
    if self._invalidated:
        raise RuntimeError("Reader is invalidated.")
    self._read_cursor = self._bus._write_cursor
```

Replace the direct mutations with `self._reader.snap_to_head()`.

---

### B-M6 · `ring.py` — overrun message always prints `'?'` for consumer name
**File:** `samsara/audio_engine/ring.py:174`

```python
print(
    f"[RING] Overrun on consumer '{getattr(self, '_name', '?')}': "
    ...
)
```

`Reader.__slots__ = ('_bus', '_read_cursor', 'overrun_count', '_invalidated')` — no `_name` slot. `getattr(self, '_name', '?')` therefore always returns `'?'`. The consumer name (e.g. `"dictation_hold"`, `"wake-consumer"`) is stored in `AudioCaptureEngine._consumers` as `(name, reader)` but is never set on the Reader itself. Overrun diagnostics are useless in practice.

**Fix:** Add `_name` to `Reader.__slots__`, thread the name from `AudioCaptureEngine.register_consumer` through `FrameBus.new_reader` to `Reader.__init__`:

```python
# ring.py
class Reader:
    __slots__ = ('_bus', '_read_cursor', 'overrun_count', '_invalidated', '_name')

    def __init__(self, bus, initial_cursor, name='?'):
        ...
        self._name = name

class FrameBus:
    def new_reader(self, name='?') -> Reader:
        return Reader(self, self._write_cursor, name=name)
```

```python
# engine.py
def register_consumer(self, name: str) -> Reader:
    reader = self._ring.new_reader(name=name)
    with self._registry_lock:
        self._consumers.append((name, reader))
    return reader
```

---

## LOW — Quality / coverage

---

### B-L1 · No tests for any consumer (wake, dictation, continuous, debug_recorder)
No test file covers `wake_consumer.py`, `dictation_consumer.py`, `continuous_consumer.py`, or `debug_recorder.py`. These files contain the highest-density concurrency logic in the codebase:

- MA-2 copy discipline (`frame.pcm.copy()` requirement — silent corruption if missed)
- Epoch-boundary abort (drain returns None on device change mid-utterance)
- The catch-up drain `while True` loop after join
- Cancel-during-drain thread interaction

A `FakeEngine` / `FakeReader` harness that drives a synthetic ring at wall-clock speed and advances `write_cursor` from a test thread would cover all of these without PortAudio. The catch-up drain test would verify that frames written between `stop.set()` and `join()` are captured (a subtle correctness guarantee that currently has no coverage).

---

### B-L2 · No tests for `echo_cancel.py`
No test covers:

- `AdaptiveEchoCanceller.process()` convergence: given N blocks of synthetic echo (mic = ref + noise), residual energy should decrease by >50% (a smoke test for B-H1 regression)
- `EchoCanceller.process()` reference window: after B-H1 is fixed, a test with known latency should verify the correct slice is passed to the NLMS filter
- `calibrate_lag()` returning a plausible value on a chirp-correlation basis
- Divergence detection and filter reset when `cleaned_energy / mic_energy > 2`

---

### B-L3 · `engine.py:269` — `metrics()` reads `Reader._read_cursor` directly
**File:** `samsara/audio_engine/engine.py:269`

```python
per_lag = {n: max(0, wc - r._read_cursor) for n, r in consumers_snapshot}
```

Reads the private `_read_cursor` from the metrics thread without synchronisation. Under the GIL this is a benign stale-read (metrics values are allowed to be slightly out of date), but it couples the engine to Reader internals. If B-M5 is fixed by adding `Reader.snap_to_head()`, a companion `Reader.lag` property would clean this up:

```python
@property
def lag(self) -> int:
    return max(0, self._bus._write_cursor - self._read_cursor)
```

Then: `per_lag = {n: r.lag for n, r in consumers_snapshot}`.

---

### B-L4 · `EchoCanceller._process_count` incremented without lock
**File:** `samsara/echo_cancel.py` (search `_process_count`)

`self._process_count += 1` is called from `process()` which runs on the processing thread. It is read (implicitly, via modulo check) in the same method. Under CPython this is a GIL-atomic plain-int increment — no torn read. Benign, but noting as a data race for any non-CPython runtime.

---

## ARC Candidates

These items raise architectural questions that should be resolved at the design level before committing a code fix.

---

### ARC-B1 · Ring GIL atomicity guarantee is CPython-only
**File:** `samsara/audio_engine/ring.py:16–34` (module docstring)

The lock-free ring relies on `write_cursor += 1` being an atomic reference replacement under CPython's GIL. The spec cross-references ACE-00 which empirically confirmed this. If Samsara is ever run under PyPy (which has a different GIL model) or a no-GIL CPython build (PEP 703, available as an opt-in since 3.13), the visibility ordering guarantee between NumPy slot writes and the `write_cursor` increment is not provided. This is an intentional CPython dependency; it should be stated in `CLAUDE.md` or the frozen spec so future porting work knows to address it.

**ARC question:** Should the spec document explicitly name CPython as a runtime requirement, or should the ring add a `sys.implementation.name == 'cpython'` guard that raises on import for other runtimes?

---

### ARC-B2 · AEC latency calibration is opt-in, but correctness requires it
**File:** `samsara/echo_cancel.py:383, ~540`

`calibrate_lag()` (chirp cross-correlation) can measure the actual speaker-to-mic round-trip delay. `set_latency()` applies it. Neither is called by the startup path. After B-H1 and B-M1 are fixed, the AEC will function correctly only if calibration has been performed. Without it the 120 ms default (or whatever is set) may be wrong by 50+ ms for any specific audio device.

**ARC question:** Should calibration run automatically once per audio device (result persisted to config), run silently in the background on startup and apply when complete, or require an explicit user action? The calibration plays a short chirp tone through the speakers — this affects whether it can be transparent.

---

## Summary Table

| ID | Severity | File | Line | Title |
|----|----------|------|------|-------|
| B-H1 | HIGH | echo_cancel.py | 482 | AEC alignment: latency offset discarded, filter gets wrong reference window |
| B-M1 | MEDIUM | echo_cancel.py | 383 | Default latency_ms=30 wrong for WASAPI; calibrate_lag() never called |
| B-M2 | MEDIUM | dictation_consumer.py | 137 | cancel() clears _frames without lock after join timeout |
| B-M3 | MEDIUM | echo_cancel.py | 200 | Lock held across resampling in audio callback — dropout risk |
| B-M4 | MEDIUM | echo_cancel.py | 217 | Linear interpolation resampler accumulates clock drift |
| B-M5 | MEDIUM | wake_consumer.py:78, dictation_consumer.py:89 | 78/89 | Direct _read_cursor mutation bypasses Reader API |
| B-M6 | MEDIUM | ring.py | 174 | Reader._name not in __slots__; overrun messages always show '?' |
| B-L1 | LOW | — | — | No tests for any consumer (dictation, wake, continuous, debug) |
| B-L2 | LOW | — | — | No tests for echo_cancel.py (alignment, convergence, calibration) |
| B-L3 | LOW | engine.py | 269 | metrics() reads Reader._read_cursor (private coupling, benign race) |
| B-L4 | LOW | echo_cancel.py | — | _process_count benign GIL race (CPython-only safety) |
| ARC-B1 | ARC | ring.py | 16 | GIL atomicity guarantee is CPython-specific; route if portability needed |
| ARC-B2 | ARC | echo_cancel.py | 383 | AEC correctness requires calibration; startup flow policy undefined |

**Fix priority:** B-H1 first (one-line correction to the slice, verified by B-L2 convergence test). Then B-M1 with an ARC decision on calibration flow. B-M2 through B-M6 are safe to batch. B-L1 and B-L2 are infrastructure — add them alongside any consumer or AEC changes to prevent regressions.
