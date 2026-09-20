"""Summarize queue 290 per-utterance stage timing JSONL."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys

from samsara.paths import samsara_home_dir


STAGES = (
    "capture_finalize_ms",
    "pre_transcribe_ms",
    "transcribe_ms",
    "post_transcribe_ms",
    "action_ms",
    "feedback_ms",
)
ROUTE_ORDER = {"hold": 0, "handsfree": 1, "wake": 2}


def load_records(path: str | Path) -> list[dict]:
    source = Path(path)
    records = []
    with source.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{source}:{line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(record, dict) or not isinstance(record.get("deltas_ms"), dict):
                raise ValueError(f"{source}:{line_number}: expected an object with deltas_ms")
            records.append(record)
    return records


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.90 * len(ordered)) - 1)]


def render_report(records: list[dict]) -> str:
    if not records:
        return "No stage timing records."
    routes = sorted(
        {str(row.get("route", "unknown")) for row in records},
        key=lambda route: (ROUTE_ORDER.get(route, 99), route),
    )
    lines = ["route      stage                 n   median ms    p90 ms"]
    for route in routes:
        route_rows = [row for row in records if str(row.get("route", "unknown")) == route]
        for stage in (*STAGES, "total_ms"):
            values = []
            for row in route_rows:
                value = row["deltas_ms"].get(stage)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    values.append(float(value))
            if not values:
                continue
            lines.append(
                f"{route:<10} {stage:<21} {len(values):>3} {statistics.median(values):>11.1f} {_p90(values):>10.1f}"
            )

    all_total = sum(
        float(row["deltas_ms"]["total_ms"])
        for row in records
        if isinstance(row["deltas_ms"].get("total_ms"), (int, float))
        and math.isfinite(row["deltas_ms"]["total_ms"])
    )
    stage_totals = {
        stage: sum(
            float(row["deltas_ms"][stage])
            for row in records
            if isinstance(row["deltas_ms"].get(stage), (int, float))
            and math.isfinite(row["deltas_ms"][stage])
        )
        for stage in STAGES
    }
    top = sorted(stage_totals.items(), key=lambda item: item[1], reverse=True)[:3]
    lines.extend(["", "Top stages by share of total observed latency:"])
    if all_total <= 0:
        lines.append("  No complete total_ms values.")
    else:
        for stage, value in top:
            lines.append(f"  {stage}: {value / all_total * 100:.1f}% ({value:.1f} / {all_total:.1f} ms)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=samsara_home_dir() / "perf" / "stage_timing.jsonl",
        help="JSONL file (default: <SAMSARA_HOME_DIR>/perf/stage_timing.jsonl)",
    )
    args = parser.parse_args(argv)
    try:
        records = load_records(args.path)
    except (OSError, ValueError) as exc:
        print(f"stage_timing_report: {exc}", file=sys.stderr)
        return 2
    print(render_report(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
