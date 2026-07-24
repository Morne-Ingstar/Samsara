"""run: causality check on captured loopback vs mic delay."""

from __future__ import annotations

import argparse
import threading
import sys
import time
from pathlib import Path

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
    parser = argparse.ArgumentParser(description="Lead-lag probe (5 s window delay).")
    parser.add_argument("--duration", type=float, default=60.0, help="Total probe run time in seconds.")
    parser.add_argument("--window", type=float, default=5.0, help="Window width in seconds.")
    parser.add_argument("--sample-rate", type=int, default=16000, help="Capture and playback sample rate.")
    parser.add_argument("--volume", type=float, default=0.35, help="Playback volume scalar [0..1].")
    parser.add_argument("--result-dir", type=Path, default=None, help="Optional results directory.")
    return parser.parse_args()


def _sign_name(delay_ms: float) -> str:
    if delay_ms < 0.0:
        return "negative"
    if delay_ms > 0.0:
        return "positive"
    return "zero"


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

    waveform = build_probe_waveform(rate, args.duration, symbol_seconds=0.25)

    mic = RingInputRecorder(sample_rate=rate, device=default_in, max_seconds=args.duration + 8.0, name="mic")
    loopback = LoopbackRecorder(sample_rate=rate, device=loopback_device, max_seconds=args.duration + 8.0)

    if not mic.start(sd):
        return 1
    if not loopback.start(sd):
        mic.stop()
        return 1

    start = time.perf_counter()
    playback = np.asarray(waveform, dtype=np.float32)
    playback_thread = threading.Thread(
        target=run_repeating_playback,
        args=(sd, playback, rate, default_out, args.volume),
        daemon=True,
    )
    playback_thread.start()

    window_count = max(1, int(args.duration / max(args.window, 0.1)))
    per_window = []

    for window_index in range(window_count):
        target_t = start + (window_index + 1) * max(args.window, 0.1)
        sleep_for = target_t - time.perf_counter()
        if sleep_for > 0:
            time.sleep(sleep_for)

        mic_snippet = mic.recent(args.window)
        ref_snippet = loopback.recent(args.window)
        expected = int(rate * args.window)
        if mic_snippet.size < expected // 2 or ref_snippet.size < expected // 2:
            continue

        estimate = estimate_delay_samples(mic_snippet, ref_snippet, sample_rate=rate, max_lag_seconds=0.5)
        if estimate is None:
            continue

        per_window.append(
            {
                "window_index": window_index,
                "start_s": round(window_index * args.window, 3),
                "lag_ms": round(estimate.delay_ms, 6),
                "lag_samples": estimate.lag_samples,
                "sign": _sign_name(estimate.delay_ms),
                "confidence": round(estimate.confidence, 6),
            }
        )

    playback_thread.join(timeout=5.0)
    loopback.stop()
    mic.stop()

    if not per_window:
        print("[AEC-DIAG] No usable windows were produced.")
        return 1

    delays = np.array([x["lag_ms"] for x in per_window], dtype=np.float64)
    result = {
        "tool": "leadlag_probe",
        "sample_rate_hz": rate,
        "window_s": args.window,
        "duration_s": args.duration,
        "window_count": len(per_window),
        "mean_delay_ms": float(np.mean(delays)),
        "spread_delay_ms": float(np.std(delays)),
        "mic_status_events": mic.buffer.status_events,
        "loopback_status_events": loopback.buffer.status_events,
        "windows": per_window,
    }

    out = write_results_json(result, "leadlag_probe", output_dir=args.result_dir)
    print("[AEC-DIAG] Wrote results:", out)
    print("[AEC-DIAG] mean_delay_ms=", f"{result['mean_delay_ms']:.4f}", "spread_ms=", f"{result['spread_delay_ms']:.4f}")
    return 0


def main() -> int:
    args = _parse_args()
    return run_probe(args)


if __name__ == "__main__":
    raise SystemExit(main())
