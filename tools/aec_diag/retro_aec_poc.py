"""run: quick offline AEC proof-of-concept with operator key marker."""

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
    pick_rate_for_device,
    run_repeating_playback,
)
from tools.aec_diag.utils import align_by_delay, compute_erle_db, estimate_delay_samples, write_results_json

from scipy.io.wavfile import write as write_wav
from samsara.echo_cancel import AdaptiveEchoCanceller


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Retroactive offline AEC POC using key marker.")
    parser.add_argument("--timeout", type=float, default=600.0, help="Max waiting time for key marker, seconds.")
    parser.add_argument("--rate", type=int, default=16000, help="Capture and playback sample rate.")
    parser.add_argument("--duration", type=float, default=3.0, help="Each extracted segment length in seconds.")
    parser.add_argument("--volume", type=float, default=0.20, help="Playback volume scalar [0..1].")
    parser.add_argument("--result-dir", type=Path, default=None, help="Optional results directory.")
    return parser.parse_args()


def _wait_for_keypress(timeout_seconds: float) -> bool:
    try:
        import msvcrt
    except Exception:
        if timeout_seconds <= 0:
            input("Press Enter to capture the next segment...")
            return True
        end = time.perf_counter() + timeout_seconds
        while time.perf_counter() < end:
            time.sleep(0.05)
        return False

    print("[AEC-DIAG] Press any key to capture a 3-second aligned segment...")
    end = time.perf_counter() + timeout_seconds
    while True:
        if timeout_seconds > 0 and time.perf_counter() >= end:
            return False
        if msvcrt.kbhit():
            msvcrt.getwch()
            return True
        time.sleep(0.02)


def _write_pcm16(path: Path, sample_rate: int, audio: np.ndarray) -> None:
    pcm = np.clip(audio, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    write_wav(str(path), sample_rate, pcm16)


def run_probe(args: argparse.Namespace) -> int:
    try:
        import sounddevice as sd
    except Exception as exc:
        print(f"[AEC-DIAG] sounddevice unavailable: {exc}")
        return 1

    default_in, default_out = find_default_devices()
    rate = int(args.rate) if args.rate > 0 else 16000
    if pick_rate_for_device(sd, default_in, (rate,), is_input=True) is None:
        print(f"[AEC-DIAG] Mic does not support selected rate {rate}Hz.")
        return 1
    loopback_device = LoopbackRecorder.select_device(sd)
    if loopback_device is None:
        print("[AEC-DIAG] WASAPI loopback capture unavailable.")
        return 1
    if default_out is None:
        default_out = sd.default.device[1] if sd.default.device else None

    mic = RingInputRecorder(sample_rate=rate, device=default_in, max_seconds=20.0, name="mic")
    loopback = LoopbackRecorder(sample_rate=rate, device=loopback_device, max_seconds=20.0)
    if not mic.start(sd):
        return 1
    if not loopback.start(sd):
        mic.stop()
        return 1

    probe = build_probe_waveform(rate, max(args.timeout, args.duration + 2.0), symbol_seconds=0.25)
    import threading
    player = threading.Thread(
        target=run_repeating_playback,
        args=(sd, probe, rate, default_out, args.volume),
        daemon=True,
    )
    player.start()

    print("[AEC-DIAG] Capturing audio continuously. Press a key to run offline AEC on the latest 3 seconds.")
    if not _wait_for_keypress(args.timeout):
        mic.stop()
        loopback.stop()
        print("[AEC-DIAG] Timeout reached without marker keypress.")
        return 1

    player.join(timeout=5.0)

    sample_count = int(args.duration * rate)
    mic_tail = mic.recent(args.duration)
    loop_tail = loopback.recent(args.duration)

    if mic_tail.size < sample_count or loop_tail.size < sample_count:
        mic.stop()
        loopback.stop()
        print("[AEC-DIAG] Ring buffer does not have enough samples at keypress.")
        return 1

    mic_tail = mic_tail[-sample_count:]
    loop_tail = loop_tail[-sample_count:]
    est = estimate_delay_samples(mic_tail, loop_tail, sample_rate=rate, max_lag_seconds=0.5)
    if est is None:
        mic.stop()
        loopback.stop()
        print("[AEC-DIAG] Could not estimate delay for selected segment.")
        return 1

    ref_aligned, mic_aligned = align_by_delay(loop_tail, mic_tail, est.lag_samples)
    n = min(ref_aligned.size, mic_aligned.size)
    if n < 1:
        mic.stop()
        loopback.stop()
        print("[AEC-DIAG] Alignment produced an empty segment.")
        return 1

    ref_aligned = ref_aligned[:n]
    mic_aligned = mic_aligned[:n]
    aec = AdaptiveEchoCanceller()
    cleaned = aec.process(mic_aligned, ref_aligned).astype(np.float32)
    erle_before = compute_erle_db(mic_aligned, np.zeros_like(mic_aligned))
    erle_after = compute_erle_db(mic_aligned, cleaned)

    out_dir = args.result_dir or (Path(__file__).resolve().parent / "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(int(time.time()))
    before_path = out_dir / f"retro_aec_before_{stamp}.wav"
    after_path = out_dir / f"retro_aec_after_{stamp}.wav"
    _write_pcm16(before_path, rate, mic_aligned)
    _write_pcm16(after_path, rate, cleaned)

    payload = {
        "tool": "retro_aec_poc",
        "sample_rate_hz": int(rate),
        "segment_seconds": args.duration,
        "delay_ms": est.delay_ms,
        "delay_lag_samples": est.lag_samples,
        "erle_db_before": float(erle_before),
        "erle_db_after": float(erle_after),
        "before_wav": str(before_path),
        "after_wav": str(after_path),
    }

    loopback.stop()
    mic.stop()
    out = write_results_json(payload, "retro_aec_poc", output_dir=args.result_dir)
    print("[AEC-DIAG] Wrote results:", out)
    print(f"[AEC-DIAG] Offline ERLE improvement: {erle_after - erle_before:.3f} dB")
    return 0


def main() -> int:
    return run_probe(_parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
