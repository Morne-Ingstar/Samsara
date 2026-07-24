"""run: long-horizon delay drift probe (10 s windows over 20 min)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


if str(_repo_root()) not in sys.path:
    sys.path.insert(0, str(_repo_root()))

from tools.aec_diag.capture import (
    LoopbackRecorder,
    RingInputRecorder,
    build_probe_waveform,
    find_default_devices,
    pick_rate_for_device,
    run_repeating_playback,
)
from tools.aec_diag.utils import estimate_delay_samples, write_results_json


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drift probe over a 20-minute window.")
    parser.add_argument("--duration", type=float, default=20 * 60.0, help="Total probe run time in seconds.")
    parser.add_argument("--window", type=float, default=10.0, help="Delay measurement window in seconds.")
    parser.add_argument("--sample-rate", type=int, default=16000, help="Capture and playback sample rate.")
    parser.add_argument("--volume", type=float, default=0.20, help="Playback volume scalar [0..1].")
    parser.add_argument("--result-dir", type=Path, default=None, help="Optional results directory.")
    return parser.parse_args()


def _detect_discontinuities(delay_estimates: list[float], times_min: np.ndarray) -> list[dict[str, Any]]:
    if len(delay_estimates) < 2:
        return []

    d = np.asarray(delay_estimates, dtype=np.float64)
    delta = np.abs(np.diff(d))
    if delta.size == 0:
        return []

    med = np.median(delta)
    base = max(8.0, med * 6.0 if med > 1e-9 else 8.0)
    out = []
    for i, step in enumerate(delta):
        if float(step) > base:
            out.append(
                {
                    "index": i,
                    "from_ms": float(d[i]),
                    "to_ms": float(d[i + 1]),
                    "delta_ms": float(step),
                    "time_min": float(times_min[i]),
                }
            )
    return out


def run_probe(args: argparse.Namespace) -> int:
    try:
        import sounddevice as sd
    except Exception as exc:
        print(f"[AEC-DIAG] sounddevice unavailable: {exc}")
        return 1

    default_in, default_out = find_default_devices()
    requested_rate = int(args.sample_rate)
    if requested_rate <= 0:
        requested_rate = 16000
    rate = pick_rate_for_device(sd, default_in, tuple([requested_rate]), is_input=True)
    if rate is None:
        print(f"[AEC-DIAG] Default mic does not support {requested_rate}Hz.")
        return 1
    if default_out is None:
        default_out = sd.default.device[1] if sd.default.device else None

    loopback_device = LoopbackRecorder.select_device(sd)
    if loopback_device is None:
        print("[AEC-DIAG] WASAPI loopback capture unavailable.")
        print("        Ensure PyAudioWPatch is installed or expose a WASAPI loopback endpoint.")
        return 1

    wave = build_probe_waveform(rate, args.duration, symbol_seconds=0.25)

    mic = RingInputRecorder(sample_rate=rate, device=default_in, max_seconds=args.duration + 12.0, name="mic")
    loopback = LoopbackRecorder(sample_rate=rate, device=loopback_device, max_seconds=args.duration + 12.0)

    if not mic.start(sd):
        return 1
    if not loopback.start(sd):
        mic.stop()
        return 1

    start = time.perf_counter()
    import threading

    player = threading.Thread(
        target=run_repeating_playback,
        args=(sd, wave, rate, default_out, args.volume),
        daemon=True,
    )
    player.start()

    window_ms: list[float] = []
    timestamps_min: list[float] = []
    window_count = max(1, int(args.duration / max(args.window, 0.5)))

    for i in range(window_count):
        deadline = start + (i + 1) * max(args.window, 0.5)
        wait = deadline - time.perf_counter()
        if wait > 0:
            time.sleep(wait)

        mic_snip = mic.recent(args.window)
        ref_snip = loopback.recent(args.window)
        expected = int(rate * args.window)
        if mic_snip.size < expected // 2 or ref_snip.size < expected // 2:
            continue

        est = estimate_delay_samples(mic_snip, ref_snip, sample_rate=rate, max_lag_seconds=0.25)
        if est is None:
            continue

        window_ms.append(float(est.delay_ms))
        timestamps_min.append((i + 1) * args.window / 60.0)

    player.join(timeout=5.0)
    loopback.stop()
    mic.stop()

    if len(window_ms) < 2:
        print("[AEC-DIAG] Not enough delay samples were collected.")
        return 1

    delays = np.asarray(window_ms, dtype=np.float64)
    times = np.asarray(timestamps_min, dtype=np.float64)
    slope_ms_per_min = float(np.polyfit(times, delays, 1)[0]) if times.size >= 2 else 0.0
    jitter_ms = float(np.std(delays))
    discontinuities = _detect_discontinuities(window_ms, times)

    payload = {
        "tool": "drift_probe",
        "sample_rate_hz": rate,
        "duration_s": args.duration,
        "window_s": args.window,
        "sample_count": len(window_ms),
        "mean_delay_ms": float(np.mean(delays)),
        "jitter_ms": jitter_ms,
        "slope_ms_per_minute": slope_ms_per_min,
        "discontinuities": discontinuities,
        "device_events": {
            "mic_status_events": int(mic.buffer.status_events),
            "loopback_status_events": int(loopback.buffer.status_events),
        },
    }

    out = write_results_json(payload, "drift_probe", output_dir=args.result_dir)
    print("[AEC-DIAG] Wrote results:", out)
    print(
        "[AEC-DIAG] slope_ms_per_minute=",
        f"{payload['slope_ms_per_minute']:.6f}",
        " jitter_ms=",
        f"{payload['jitter_ms']:.6f}",
    )
    return 0


def main() -> int:
    return run_probe(_parse_args())


if __name__ == "__main__":
    raise SystemExit(main())

