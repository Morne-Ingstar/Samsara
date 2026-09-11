"""Decode owner-over-media corpus rows with the whole WAV scaled by a gain.

Sanity bound only: the owner and the media are attenuated together, which is
not the live mix (ducking attenuates only the PC media stream).  It answers
the narrower question of whether the owner's voice still transcribes when it
arrives at the level a ducked capture would carry.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import hf_bench  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=hf_bench.DEFAULT_CORPUS)
    parser.add_argument("--label", default="speech_owner_over_media")
    parser.add_argument("--gain", type=float, default=0.15)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--stock-config", action="store_true")
    args = parser.parse_args(argv)

    config = hf_bench.load_config(None, use_live=not args.stock_config)
    rows = [row for row in json.loads((args.corpus / "manifest.json").read_text(encoding="utf-8"))
            if row["label"] == args.label]
    pipe = hf_bench.RealPipeline(config)
    scored = []
    for row in rows:
        audio, rate = hf_bench.read_wav(args.corpus / row["file"])
        scaled = hf_bench.scale_int16(audio, args.gain)
        started = time.perf_counter()
        text, segs, wall = pipe.run(scaled, rate)
        if wall == 0:
            wall = (time.perf_counter() - started) * 1000.0
        score = hf_bench.score_row(hf_bench.BenchResult(
            row, text, segs, pipe.voiced_ms(scaled, rate), wall), config)
        score["gain"] = args.gain
        score["rms_dbfs"] = round(hf_bench.rms_dbfs(scaled), 2)
        scored.append(score)
    result = {"config": config, "gain": args.gain, "label": args.label,
              "rows": scored, "summary": hf_bench.summarize(scored)}
    if args.out:
        # A gain in the stem ("..._0.15") makes with_suffix eat the ".15".
        out = args.out if args.out.suffix == ".json" else Path(str(args.out) + ".json")
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(out)
    for score in scored:
        print(score["file"], "wer=%.4f" % score["wer"], "rms=%s" % score["rms_dbfs"],
              "|", score["text"] or "(empty)")
    print(args.label, result["summary"][args.label])
    return 0


if __name__ == "__main__":
    sys.exit(main())
