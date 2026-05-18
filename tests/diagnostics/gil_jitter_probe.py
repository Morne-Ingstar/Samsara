"""ACE-00 GIL / Callback-Jitter Empirical Test

Measures PortAudio inter-callback timing jitter while faster-whisper,
Silero VAD, and OpenWakeWord run concurrently — reproducing the real
GIL pressure the AudioCaptureEngine will face in Model A.

Usage:
    F:\\envs\\sami\\python.exe tests/diagnostics/gil_jitter_probe.py

Run 3 times in different audio environments and paste all outputs.
This is a THROWAWAY diagnostic. It is not imported by anything and
ships with no production code changes.

Pass conditions (ARC-defined, do not relax):
  1. p99 inter-callback delta within 5 ms of expected chunk period
  2. max delta does not exceed 2x the expected chunk period
  3. Zero sounddevice input overflows/underflows over the 30 s run
"""

import sys
import time
import threading

import numpy as np
import sounddevice as sd

# ── Configuration ─────────────────────────────────────────────────────────────

CAPTURE_SECONDS    = 30       # seconds to hold the audio stream open
CHUNK_MS           = 100      # target chunk duration; blocksize = rate * 0.1
WHISPER_MODEL_SIZE = 'base'   # actual app uses small.en; 'base' for speed
WHISPER_DEVICE     = 'cuda'   # set to 'cpu' if CUDA unavailable
WHISPER_COMPUTE    = 'float16'
WHISPER_DUMMY_SEC  = 5        # length of dummy audio clip fed to Whisper


# ── Callback state (no allocations in the hot path) ───────────────────────────

class _CaptureState:
    """All mutable state accessed by the audio callback, pre-allocated."""

    def __init__(self, max_callbacks: int):
        self.timestamps     = np.zeros(max_callbacks, dtype=np.float64)
        self.status_flags   = np.zeros(max_callbacks, dtype=np.int32)
        self.idx            = 0
        self.overflow_count = 0

    def callback(self, indata, frames, time_info, status):
        idx = self.idx
        if idx < len(self.timestamps):
            # time.perf_counter() is the only call here; no allocation.
            self.timestamps[idx] = time.perf_counter()
            self.status_flags[idx] = 0 if not status else 1
        self.idx = idx + 1
        if status:
            self.overflow_count += 1


# ── Helpers ───────────────────────────────────────────────────────────────────

def _percentile_report(deltas_ms, expected_ms):
    p50  = float(np.percentile(deltas_ms, 50))
    p95  = float(np.percentile(deltas_ms, 95))
    p99  = float(np.percentile(deltas_ms, 99))
    dmax = float(deltas_ms.max())
    dstd = float(deltas_ms.std())
    dmean = float(deltas_ms.mean())
    return dict(
        mean=dmean, p50=p50, p95=p95, p99=p99, max=dmax, std=dstd,
        p99_margin=p99 - expected_ms,
        max_ratio=dmax / expected_ms,
    )


def _load_whisper():
    from faster_whisper import WhisperModel
    print(f"  Loading faster-whisper '{WHISPER_MODEL_SIZE}' on {WHISPER_DEVICE}/{WHISPER_COMPUTE}...",
          end=' ', flush=True)
    model = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE,
                         compute_type=WHISPER_COMPUTE)
    dummy = np.zeros(WHISPER_DUMMY_SEC * 16000, dtype=np.float32)
    # Warm-up: JIT / CUDA kernel compilation must not pollute timing.
    segs, _ = model.transcribe(dummy, vad_filter=False)
    list(segs)
    print("ready.")
    return model, dummy


def _load_silero():
    import torch
    print("  Loading Silero VAD...", end=' ', flush=True)
    vad, _ = torch.hub.load(
        'snakers4/silero-vad', 'silero_vad',
        trust_repo=True, verbose=False,
    )
    vad.eval()
    frame = torch.zeros(512)  # 512 samples @ 16 kHz = 32 ms per Silero spec
    with torch.no_grad():
        vad(frame, 16000)     # warm-up
    print("ready.")
    return vad, frame


def _load_oww():
    print("  Loading OpenWakeWord...", end=' ', flush=True)
    from openwakeword.model import Model as OWWModel
    model = OWWModel(inference_framework='onnx')
    dummy = np.zeros(1280, dtype=np.int16)  # 80 ms @ 16 kHz
    model.predict(dummy)                     # warm-up
    print("ready.")
    return model, dummy


# ── Main ──────────────────────────────────────────────────────────────────────

def run_probe():
    import torch

    # ── Device query ──────────────────────────────────────────────────────────
    dev_info    = sd.query_devices(kind='input')
    device_name = dev_info['name']
    native_rate = int(dev_info['default_samplerate'])
    blocksize   = int(native_rate * CHUNK_MS / 1000)
    expected_ms = blocksize / native_rate * 1000  # exactly CHUNK_MS if rate divides evenly
    max_cbs     = int(CAPTURE_SECONDS * 1000 / CHUNK_MS) + 100  # headroom

    print()
    print("=" * 62)
    print("ACE-00  GIL / Callback-Jitter Probe")
    print("=" * 62)
    print(f"Device:          {device_name}")
    print(f"Native rate:     {native_rate} Hz")
    print(f"Blocksize:       {blocksize} samples ({CHUNK_MS} ms)")
    print(f"Expected delta:  {expected_ms:.2f} ms")
    print(f"Capture:         {CAPTURE_SECONDS} s  ({max_cbs - 100} expected callbacks)")
    print(f"Whisper model:   {WHISPER_MODEL_SIZE}  device={WHISPER_DEVICE}/{WHISPER_COMPUTE}")
    print()

    # ── Load models ───────────────────────────────────────────────────────────
    print("Loading inference models (warm-up runs included):")
    whisper_model, whisper_dummy = _load_whisper()
    vad_model,     vad_frame     = _load_silero()

    oww_model, oww_dummy, oww_available = None, None, False
    try:
        oww_model, oww_dummy = _load_oww()
        oww_available = True
    except Exception as exc:
        print(f"  OpenWakeWord: not tested — {exc}")

    # ── Stressor threads ──────────────────────────────────────────────────────
    stop_event    = threading.Event()
    whisper_iters = [0]
    vad_iters     = [0]
    oww_iters     = [0]

    def _whisper_loop():
        while not stop_event.is_set():
            segs, _ = whisper_model.transcribe(whisper_dummy, vad_filter=False)
            list(segs)
            whisper_iters[0] += 1

    def _vad_loop():
        frame = vad_frame
        with torch.no_grad():
            while not stop_event.is_set():
                vad_model(frame, 16000)
                vad_iters[0] += 1

    def _oww_loop():
        chunk = oww_dummy
        while not stop_event.is_set():
            oww_model.predict(chunk)
            oww_iters[0] += 1

    stressors = [
        threading.Thread(target=_whisper_loop, daemon=True, name='stressor-whisper'),
        threading.Thread(target=_vad_loop,     daemon=True, name='stressor-vad'),
    ]
    if oww_available:
        stressors.append(
            threading.Thread(target=_oww_loop, daemon=True, name='stressor-oww')
        )

    # ── Capture ───────────────────────────────────────────────────────────────
    state = _CaptureState(max_cbs)

    print()
    print(f"Starting stressor threads and {CAPTURE_SECONDS}s capture...")
    for t in stressors:
        t.start()

    with sd.InputStream(
        samplerate=native_rate,
        channels=1,
        dtype=np.float32,
        blocksize=blocksize,
        callback=state.callback,
    ):
        time.sleep(CAPTURE_SECONDS)

    stop_event.set()
    print("Capture complete. Computing statistics...")

    # ── Statistics ────────────────────────────────────────────────────────────
    n  = min(state.idx, max_cbs)
    ts = state.timestamps[:n]

    if n < 10:
        print(f"ERROR: only {n} callbacks captured — something went wrong.")
        sys.exit(1)

    deltas_ms = np.diff(ts) * 1000.0
    stats     = _percentile_report(deltas_ms, expected_ms)

    # ── Report ────────────────────────────────────────────────────────────────
    print()
    print("=" * 62)
    print("RESULTS")
    print("=" * 62)
    print(f"  Callbacks captured:   {n}  (expected ~{CAPTURE_SECONDS*10})")
    print(f"  Expected delta:       {expected_ms:.2f} ms")
    print(f"  mean:                 {stats['mean']:.3f} ms")
    print(f"  p50:                  {stats['p50']:.3f} ms")
    print(f"  p95:                  {stats['p95']:.3f} ms")
    print(f"  p99:                  {stats['p99']:.3f} ms")
    print(f"  max:                  {stats['max']:.3f} ms")
    print(f"  stddev:               {stats['std']:.3f} ms")
    print(f"  Overflows/underflows: {state.overflow_count}")
    print(f"  Whisper iters:        {whisper_iters[0]}")
    print(f"  VAD iters:            {vad_iters[0]}")
    if oww_available:
        print(f"  OWW iters:            {oww_iters[0]}")
    else:
        print(f"  OWW iters:            (not tested)")

    print()
    print("PASS CONDITIONS:")
    p99_ok  = stats['p99_margin'] <= 5.0
    max_ok  = stats['max_ratio']  <= 2.0
    ovfl_ok = state.overflow_count == 0

    print(f"  1. p99 within 5 ms of expected:   {'PASS' if p99_ok  else 'FAIL'}"
          f"  (margin {stats['p99_margin']:+.3f} ms)")
    print(f"  2. max < 2x expected ({2*expected_ms:.1f} ms): {'PASS' if max_ok  else 'FAIL'}"
          f"  (max was {stats['max']:.3f} ms)")
    print(f"  3. Zero overflows:                {'PASS' if ovfl_ok else 'FAIL'}"
          f"  ({state.overflow_count} overflow(s))")

    verdict = 'PASS' if (p99_ok and max_ok and ovfl_ok) else 'FAIL'
    print()
    print(f"  OVERALL: {verdict}")
    print("=" * 62)

    if not (p99_ok and max_ok and ovfl_ok):
        print()
        print("STOP — Model A is at risk. Do not start ACE-01.")
        print("Capture this report and flag for re-review.")
        print("Fallback: Model C (Whisper in subprocess, capture + light")
        print("VAD/wake in-process).")

    return verdict


if __name__ == '__main__':
    run_probe()
