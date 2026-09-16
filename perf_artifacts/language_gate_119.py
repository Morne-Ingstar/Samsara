"""Queue 119: apply the PRODUCTION language gate to hf_bench rows.

tools/hf_bench.py summarises `language_probability` but never constructs a
LanguageConfidenceGate, so its `false_accept_rate` measures the decode and the
segment-quality gates only. That is why hf_bench_post_fix.md could report
media_only = 1.0 while the gate was present and committed: the bench never
exercised it.

This script closes that gap. It replays the bench's own rows through the real
samsara.languages.LanguageConfidenceGate and reports the false-accept rate
before and after the gate, per label.

    python perf_artifacts/language_gate_119.py <hf_bench.json> [--out FILE.md]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara.languages import (  # noqa: E402
    LANGUAGE_CONFIDENCE_FLOOR,
    LanguageConfidenceGate,
)

#: Labels whose clips must produce NO text. Anything they transcribe is a
#: false accept -- this is the TV-audio failure the gate exists to stop.
SILENT_LABELS = ("media_only", "nonspeech_body", "nonspeech_room", "silence")
#: Labels that are the owner speaking. Anything the gate rejects here is a
#: false REJECT, which is the cost side of the trade.
OWNER_LABELS = ("speech_owner", "speech_owner_over_media", "wake_over_media")


def evaluate_rows(rows, *, configured_language="en", floor=LANGUAGE_CONFIDENCE_FLOOR):
    out = []
    for row in rows:
        text = (row.get("text") or "").strip()
        gate = LanguageConfidenceGate()          # fresh: no cross-clip memory
        reason, _expected = gate.evaluate(
            text, row.get("language"), row.get("language_probability"),
            configured_language, floor,
            remember=False, duration_s=row.get("duration_s") or None,
        )
        out.append({
            "label": row.get("label"),
            "file": row.get("file"),
            "probability": row.get("language_probability"),
            "chars": len(text),
            "produced_text": bool(text),
            "rejected": reason is not None,
            "reason": reason,
        })
    return out


def report(results) -> str:
    labels = []
    for r in results:
        if r["label"] not in labels:
            labels.append(r["label"])

    lines = ["# Language gate applied to the hands-free bench (queue 119)", "",
             f"Floor {LANGUAGE_CONFIDENCE_FLOOR}. `before` is the bench's own number "
             "(decode + segment gates); `after` adds the production language gate.", "",
             "| label | n | before false-accept | after false-accept | rejected |",
             "|---|---:|---:|---:|---:|"]
    for label in labels:
        rows = [r for r in results if r["label"] == label]
        if label not in SILENT_LABELS:
            continue
        before = sum(1 for r in rows if r["produced_text"]) / len(rows)
        after = sum(1 for r in rows if r["produced_text"] and not r["rejected"]) / len(rows)
        rejected = sum(1 for r in rows if r["rejected"])
        lines.append(f"| {label} | {len(rows)} | {before:.2f} | **{after:.2f}** | {rejected} |")

    lines += ["", "| owner label | n | false REJECT rate | rejected clips |", "|---|---:|---:|---|"]
    for label in labels:
        if label not in OWNER_LABELS:
            continue
        rows = [r for r in results if r["label"] == label]
        spoke = [r for r in rows if r["produced_text"]]
        bad = [r for r in spoke if r["rejected"]]
        rate = (len(bad) / len(spoke)) if spoke else 0.0
        lines.append(f"| {label} | {len(rows)} | {rate:.2f} | "
                     f"{', '.join(r['file'] for r in bad) or 'none'} |")

    lines += ["", "## Per clip", "",
              "| label | file | probability | chars | text? | gate |", "|---|---|---:|---:|---|---|"]
    for r in results:
        lines.append(f"| {r['label']} | {r['file']} | "
                     f"{(r['probability'] or 0):.4f} | {r['chars']} | "
                     f"{'yes' if r['produced_text'] else 'no'} | "
                     f"{r['reason'] or 'passed'} |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bench_json")
    ap.add_argument("--out")
    ap.add_argument("--language", default="en")
    args = ap.parse_args()

    data = json.loads(Path(args.bench_json).read_text(encoding="utf-8"))
    results = evaluate_rows(data["rows"], configured_language=args.language)
    text = report(results)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written: {args.out}")


if __name__ == "__main__":
    main()
