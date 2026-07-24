"""run: phase/phase-POISON check with alternate offline resamplers."""

from __future__ import annotations

import argparse
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
    find_wasapi_loopback_device,
    pick_rate_for_device,
    run_repeating_playback,
)
from tools.aec_diag.utils import align_by_delay, compute_erle_db, estimate_delay_samples, resample_linear, resample_poly_or_soxr, write_results_json

from samsara.echo_cancel import AdaptiveEchoCanceller


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resampler A/B probe.")
    parser.add_argument("--duration", type=float, default=12.0, help="Capture duration in seconds.")
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=0,
        help="Capture sample rate. 0 selects the first supported common rate.",
    )
    parser.add_argument("--volume", type=float, default=0.15, help="Playback volume scalar [0..1].")
    parser.add_argument("--result-dir", type=Path, default=None, help="Optional results directory.")
    return parser.parse_args()


def _pick_rates(sd_module, default_in, loopback, default_out) -> int:
    candidates = (48000, 44100, 32000, 22050, 16000)
    if default_in is not None and default_out is not None and loopback is not None:
        for rate in candidates:
            try:
                sd_module.check_input_settings(device=default_in, channels=1, samplerate=rate, dtype="float32")
                sd_module.check_output_settings(device=default_out, channels=2, samplerate=rate, dtype="float32")
                sd_module.check_input_settings(device=loopback, channels=1, samplerate=rate, dtype="float32")
                return rate
            except Exception:
                continue
    return 16000


def _run_offline_aec(reference: np.ndarray, mic: np.ndarray) -> tuple[np.ndarray, float]:
    if mic.size == 0 or reference.size == 0:
        return mic.astype(np.float32), float("nan")
    n = min(reference.size, mic.size)
    reference = reference[:n]
    mic = mic[:n]
    aec = AdaptiveEchoCanceller()
    cleaned = aec.process(mic, reference).astype(np.float32)
    return cleaned, compute_erle_db(mic, cleaned)


def run_probe(args: argparse.Namespace) -> int:
    try:
        import sounddevice as sd
    except Exception as exc:
        print(f"[AEC-DIAG] sounddevice unavailable: {exc}")
        return 1

    default_in, default_out = find_default_devices()
    loopback_device, _ = find_wasapi_loopback_device(sd)
    if loopback_device is None:
        print("[AEC-DIAG] WASAPI loopback capture unavailable.")
        return 1

    target = _pick_rates(sd, default_in, loopback_device, default_out)
    if args.sample_rate and args.sample_rate > 0:
        target = int(args.sample_rate)
    if pick_rate_for_device(sd, default_in, (target,), is_input=True) is None:
        print(f"[AEC-DIAG] Default mic cannot open at {target}Hz.")
        return 1

    if default_out is None:
        default_out = sd.default.device[1] if sd.default.device else None

    wave = build_probe_waveform(target, args.duration, symbol_seconds=0.25)
    mic = RingInputRecorder(sample_rate=target, device=default_in, max_seconds=args.duration + 8.0, name="mic")
    loopback = LoopbackRecorder(sample_rate=target, device=loopback_device, max_seconds=args.duration + 8.0)

    if not mic.start(sd):
        return 1
    if not loopback.start(sd):
        mic.stop()
        return 1

    import threading

    player = threading.Thread(
        target=run_repeating_playback,
        args=(sd, wave, target, default_out, args.volume),
        daemon=True,
    )
    player.start()

    target_t = time.perf_counter() + args.duration
    wait_for = target_t - time.perf_counter()
    if wait_for > 0:
        time.sleep(wait_for + 0.5)

    player.join(timeout=5.0)
    loopback.stop()
    mic.stop()

    mic_raw = mic.recent(args.duration)
    ref_raw = loopback.recent(args.duration)
    n = min(mic_raw.size, ref_raw.size)
    if n < max(1, int(target * 0.2)):
        print("[AEC-DIAG] Captured data too short for analysis.")
        return 1
    mic_raw = mic_raw[:n]
    ref_raw = ref_raw[:n]

    delay = estimate_delay_samples(mic_raw, ref_raw, sample_rate=target, max_lag_seconds=0.35)
    if delay is None:
        print("[AEC-DIAG] Could not estimate playback->mic delay.")
        return 1

    aligned_ref, aligned_mic = align_by_delay(ref_raw, mic_raw, delay.lag_samples)
    if aligned_ref.size < 16 or aligned_mic.size < 16:
        print("[AEC-DIAG] Alignment collapsed capture into too-short snippets.")
        return 1

    ref_linear = resample_linear(aligned_ref, source_rate=target, target_rate=16000)
    mic_linear = resample_linear(aligned_mic, source_rate=target, target_rate=16000)
    ref_poly = resample_poly_or_soxr(aligned_ref, source_rate=target, target_rate=16000)
    mic_poly = resample_poly_or_soxr(aligned_mic, source_rate=target, target_rate=16000)

    cleaned_linear, erle_linear = _run_offline_aec(ref_linear, mic_linear)
    cleaned_poly, erle_poly = _run_offline_aec(ref_poly, mic_poly)

    payload = {
        "tool": "resampler_ab",
        "source_sample_rate_hz": int(target),
        "target_sample_rate_hz": 16000,
        "delay_ms": delay.delay_ms,
        "delay_lag_samples": delay.lag_samples,
        "segment_seconds": n / float(target),
        "erle_db": {
            "linear": float(erle_linear),
            "poly": float(erle_poly),
        },
        "linear_len": int(len(ref_linear)),
        "poly_len": int(len(ref_poly)),
    }
    payload["erle_delta_db"] = payload["erle_db"]["poly"] - payload["erle_db"]["linear"]
    payload["linear_wins_over_poly"] = payload["erle_db"]["linear"] > payload["erle_db"]["poly"]

    out = write_results_json(payload, "resampler_ab", output_dir=args.result_dir)
    print("[AEC-DIAG] Wrote results:", out)
    print(
        f"[AEC-DIAG] ERLE linear={payload['erle_db']['linear']:.3f} dB, "
        f"poly={payload['erle_db']['poly']:.3f} dB, delta={payload['erle_delta_db']:.3f} dB"
    )
    return 0


def main() -> int:
    return run_probe(_parse_args())


if __name__ == "__main__":
    raise SystemExit(main())

