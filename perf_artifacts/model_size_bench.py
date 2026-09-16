"""Queue 80: what `small` costs in accuracy and saves in latency vs `medium`,
on the owner's own recordings. Standalone: loads faster-whisper directly,
never imports the app.

    python perf_artifacts/model_size_bench.py [--hotkey-sample 40] [--repeats 3]

Clip sets
  A. labelled: every wake-lane dump that has a [HEAR] line in the logs
     (~/.samsara-daily and ~/.samsara). The owner was reading a script
     ("Jarvis dictate", "over", "cancel", "done", "this is round one, over"),
     so the true text is known -- including where the live medium decode got
     it wrong ("Jarvis stick tape"). Scored: exact normalised match, WER, and
     whether the control word is recognised as the whole utterance / suffix.
  B. unlabelled: hold-to-dictate dumps (~/.samsara/debug/hotkey_*.wav), a
     spread of lengths. No ground truth exists, so this reports latency and
     small's word disagreement with medium -- NOT accuracy.

Decode params are the hands-free DICTATE lane's (performance_mode accurate +
_handle_command_mode_utterance overrides): language en, beam 5,
vad_filter False, condition_on_previous_text True, no initial_prompt. Both
models run float16 on CUDA like the app. The live app shares the GPU: runs are
interleaved medium/small per clip so contention hits both equally.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
HOME = Path.home()

LANE = dict(language="en", beam_size=5, vad_filter=False, condition_on_previous_text=True,
            without_timestamps=False, no_speech_threshold=0.6, log_prob_threshold=-1.0)

CONTROL = {"over", "cancel", "done", "end", "scratch that"}


def truth_for(heard: str) -> str:
    """The scripted phrase behind a live [HEAR] line."""
    n = norm(heard)
    if n.startswith("jarvis"):
        return "jarvis dictate"  # every jarvis utterance in these sessions was the wake+dictate script
    return n


def norm(text: str) -> str:
    text = (text or "").lower().replace("-", " ")
    text = re.sub(r"[^a-z0-9' ]", " ", text)
    return " ".join(text.split())


def wer(ref: str, hyp: str) -> float:
    r, h = ref.split(), hyp.split()
    if not r:
        return 0.0 if not h else 1.0
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
            prev, d[j] = d[j], cur
    return d[len(h)] / len(r)


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path}: {width * 8}-bit")
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        idx = np.linspace(0, len(audio) - 1, int(len(audio) * 16000 / sr))
        audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
    return audio


def wake_pairs() -> list[dict]:
    pairs, seen = [], set()
    for log in [HOME / ".samsara-daily/logs/samsara.log", HOME / ".samsara/logs/samsara.log.1",
                HOME / ".samsara/logs/samsara.log"]:
        if not log.exists():
            continue
        pending = None
        for line in log.open(encoding="utf-8", errors="replace"):
            m = re.search(r"Dumped wake audio -> (.+\.wav) \(", line)
            if m:
                pending = m.group(1)
                continue
            h = re.search(r'\[HEAR\] "(.*)"', line)
            if h and pending:
                if os.path.exists(pending) and pending not in seen:
                    seen.add(pending)
                    pairs.append({"wav": pending, "live": h.group(1), "truth": truth_for(h.group(1))})
                pending = None
    return pairs


def hotkey_sample(n: int, seed: int = 80) -> list[dict]:
    files = sorted((HOME / ".samsara/debug").glob("hotkey_2026091*.wav"))
    files = [f for f in files if f.stat().st_size > 60_000]  # > ~1.9 s at 16 kHz int16
    rng = random.Random(seed)
    buckets = {"2-5s": [], "5-15s": [], ">15s": []}
    for f in files:
        secs = (f.stat().st_size - 44) / 32000
        key = "2-5s" if secs <= 5 else "5-15s" if secs <= 15 else ">15s"
        buckets[key].append(f)
    chosen = []
    per = max(1, n // 3)
    for key, group in buckets.items():
        chosen += rng.sample(group, min(per, len(group)))
    return [{"wav": str(f), "seconds": round((f.stat().st_size - 44) / 32000, 1)} for f in chosen]


def decode(model, audio) -> tuple[str, float]:
    t0 = time.perf_counter()
    segs, _ = model.transcribe(audio, **LANE)
    text = "".join(s.text for s in segs).strip()
    return text, (time.perf_counter() - t0) * 1000.0


def pct(values, q):
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hotkey-sample", type=int, default=30)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--out-prefix", default=str(HERE / "model_size_bench"))
    args = ap.parse_args(argv)

    from faster_whisper import WhisperModel

    models = {}
    load_ms = {}
    for size in ("medium", "small"):
        t0 = time.perf_counter()
        models[size] = WhisperModel(size, device="cuda", compute_type="float16", local_files_only=True)
        load_ms[size] = (time.perf_counter() - t0) * 1000.0
    warm = np.zeros(16000, dtype=np.float32)
    for m in models.values():
        for _ in range(2):
            decode(m, warm)

    labelled = wake_pairs()
    unlabelled = hotkey_sample(args.hotkey_sample)
    results = {"params": LANE, "load_ms": load_ms, "labelled": [], "unlabelled": []}

    def run(clip):
        audio = load_wav(clip["wav"])
        clip["seconds"] = round(len(audio) / 16000, 2)
        for size, model in (("medium", models["medium"]), ("small", models["small"])):
            times, text = [], ""
            for _ in range(args.repeats):
                text, ms = decode(model, audio)
                times.append(ms)
            clip[size] = {"text": text, "ms": round(statistics.median(times), 1)}
        return clip

    for i, clip in enumerate(labelled):
        run(clip)
        for size in ("medium", "small"):
            hyp = norm(clip[size]["text"])
            clip[size]["exact"] = hyp == clip["truth"]
            clip[size]["wer"] = round(wer(clip["truth"], hyp), 3)
            last = clip["truth"].split()[-1] if clip["truth"] else ""
            if last in CONTROL:
                clip[size]["control_ok"] = (hyp.split()[-1:] == [last])
        print(f"L{i:02d} {clip['seconds']:5.2f}s truth={clip['truth']!r:40} "
              f"medium={clip['medium']['text']!r:36} {clip['medium']['ms']:6.0f}ms  "
              f"small={clip['small']['text']!r:36} {clip['small']['ms']:6.0f}ms", flush=True)
        results["labelled"].append(clip)

    for i, clip in enumerate(unlabelled):
        run(clip)
        clip["small_vs_medium_wer"] = round(wer(norm(clip["medium"]["text"]), norm(clip["small"]["text"])), 3)
        print(f"U{i:02d} {clip['seconds']:5.1f}s medium {clip['medium']['ms']:6.0f}ms small {clip['small']['ms']:6.0f}ms "
              f"disagree={clip['small_vs_medium_wer']:.2f}", flush=True)
        results["unlabelled"].append(clip)

    summary = {}
    L = results["labelled"]
    for size in ("medium", "small"):
        ctrl = [c[size]["control_ok"] for c in L if "control_ok" in c[size]]
        summary[size] = {
            "labelled_n": len(L),
            "labelled_exact": sum(c[size]["exact"] for c in L),
            "labelled_mean_wer": round(statistics.mean(c[size]["wer"] for c in L), 3) if L else None,
            "control_word_n": len(ctrl),
            "control_word_ok": sum(ctrl),
            "labelled_ms_median": round(statistics.median(c[size]["ms"] for c in L), 1) if L else None,
            "labelled_ms_p95": round(pct([c[size]["ms"] for c in L], 0.95), 1) if L else None,
        }
        for key, lo, hi in (("short_<=3s", 0, 3), ("mid_3-10s", 3, 10), ("long_>10s", 10, 1e9)):
            vals = [c[size]["ms"] for c in L + results["unlabelled"] if lo < c["seconds"] <= hi]
            summary[size][key] = None if not vals else {
                "n": len(vals), "median_ms": round(statistics.median(vals), 1), "p95_ms": round(pct(vals, 0.95), 1)}
    U = results["unlabelled"]
    summary["small_vs_medium_disagreement_wer_mean"] = round(statistics.mean(c["small_vs_medium_wer"] for c in U), 3) if U else None
    results["summary"] = summary
    Path(args.out_prefix + ".json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
