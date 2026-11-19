"""Read-only hands-free short/rejected capture chronology; never emits words/PCM.

Usage: python -m tools.diag.handsfree_deaf_1286 --logs DIR --flight DIR
Flight-only days are explicitly unscored: a short close is not proof of rejection.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re


STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})")
FIELDS = re.compile(r"\b(duration_s|peak|verdict)=([\w.\-]+)")
DROP = re.compile(r"\[CMD-UTT\] dropped reason=([a-z_]+)")


def read_logs(root, offset_hours=-8):
    clips, context, boundaries, coverage = [], [], [], []
    zone = timezone(timedelta(hours=offset_hours))
    for path in [root / "samsara.log", *(root / f"samsara.log.{i}" for i in range(1, 4))]:
        if not path.is_file():
            continue
        first = last = None
        pending = None
        with path.open(encoding="utf-8", errors="replace") as source:
            for line in source:
                match = STAMP.match(line)
                if not match:
                    continue
                at = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=zone).timestamp()
                first = at if first is None else first
                last = at
                label = None
                if "[HOTKEY] Main hotkey" in line and "pressed" in line:
                    label = "hold_press"
                elif "suppression RELEASED" in line:
                    label = "suppression_release"
                elif "[DUCK] capture duck restored" in line:
                    label = "duck_restore"
                elif "AudioCoordinator" in line or "[TTS]" in line or "Ava lane suppression" in line:
                    label = "tts_ava_event"
                elif "[CMD MODE] Entering command mode" in line:
                    label = "mode_enter"
                elif "[CMD MODE] Exiting command mode" in line:
                    label = "mode_exit"
                elif "[SESSION] mode change " in line:
                    mode = re.search(r"\[SESSION\] mode change [a-z_]+ -> ([a-z_]+)", line)
                    if mode:
                        label = "mode_" + mode[1]
                elif "[SESSION] mode=" in line:
                    mode = re.search(r"\[SESSION\] mode=([a-z_]+)", line)
                    if mode and "outcome=mode_" in line:
                        label = "mode_" + mode[1]
                if label:
                    context.append((at, label))
                    if label.startswith("mode_"):
                        boundaries.append(at)
                if "[GATE] speech_presence path=handsfree " in line:
                    fields = dict(FIELDS.findall(line))
                    if "duration_s" not in fields or "verdict" not in fields:
                        continue
                    pending = {"at": at, "duration_s": float(fields["duration_s"]),
                               "peak_probability": float(fields.get("peak", -1)),
                               "outcome": fields["verdict"]}
                    clips.append(pending)
                drop = DROP.search(line)
                if drop and pending is not None and 0 <= at - pending["at"] < 30:
                    pending["outcome"] = drop[1]
        coverage.append({"file": path.name, "first": first, "last": last})
    # Rotations can overlap; identical gate timestamp is the same capture.
    clips = sorted({c["at"]: c for c in clips}.values(), key=lambda c: c["at"])
    return clips, sorted(set(context)), sorted(set(boundaries)), coverage


def read_flights(root):
    closes, context, coverage = [], [], []
    for path in sorted(root.glob("events-*.jsonl")):
        opened = {}
        first = last = None
        malformed = 0
        with path.open(encoding="utf-8", errors="replace") as source:
            for line in source:
                try:
                    row = json.loads(line)
                    at = float(row["t_wall"])
                except (ValueError, KeyError, TypeError):
                    malformed += 1
                    continue
                first = at if first is None else first
                last = at
                event = row.get("event")
                thread = row.get("thread")
                if event == "wake.session_open":
                    opened[thread] = at
                elif event == "wake.session_close":
                    start = opened.pop(thread, None)
                    if row.get("reason") == "silence_flush":
                        closes.append({"at": at, "opened_at": start,
                                       "duration_s": row.get("speech_s")})
                elif event == "hold_recording.start":
                    context.append((at, "hold_press"))
        coverage.append({"file": path.name, "first": first, "last": last,
                         "malformed_rows": malformed})
    return sorted(closes, key=lambda c: c["at"]), context, coverage


def bad(clip):
    return clip["duration_s"] <= 1.500001 and clip["outcome"] in {
        "reject", "no_speech_in_audio", "empty_transcription", "signoff_hallucination"}


def analyse(log_root, flight_root, offset_hours=-8):
    clips, context, boundaries, log_coverage = read_logs(log_root, offset_hours)
    closes, flight_context, flight_coverage = read_flights(flight_root)
    context = sorted(set(context + flight_context))
    close_times = [c["at"] for c in closes]
    for clip in clips:
        i = bisect_right(close_times, clip["at"]) - 1
        if i >= 0 and clip["at"] - closes[i]["at"] < 10:
            close = closes[i]
            if abs(float(close["duration_s"]) - clip["duration_s"]) < .11:
                clip["opened_at"] = close["opened_at"]
                clip["closed_at"] = close["at"]
    indices = {}
    for at, label in context:
        indices.setdefault(label, []).append(at)

    def before(at):
        result = {}
        for label, times in indices.items():
            i = bisect_right(times, at) - 1
            if i >= 0:
                result[label] = {"at": times[i], "seconds_before": round(at - times[i], 3)}
        return result

    runs, run = [], []

    def finish(next_clip):
        if len(run) < 3:
            return
        start = run[0].get("opened_at") or run[0]["at"]
        prior = before(start)
        hold = prior.get("hold_press")
        recovery = None
        if next_clip is not None:
            recovery = {**next_clip, "preceding": before(next_clip.get("opened_at") or next_clip["at"])}
        runs.append({"start": start, "end": run[-1]["at"], "clips": len(run),
                     "hold_within_60s": bool(hold and hold["seconds_before"] <= 60),
                     "preceding": prior, "captures": list(run),
                     "next_nonmatching_capture": recovery})

    previous = None
    for clip in clips:
        boundary = previous is not None and bisect_right(boundaries, previous) != bisect_right(boundaries, clip["at"])
        # A log gap/day boundary is not continuous evidence of a bad stretch.
        if previous is not None and (boundary or clip["at"] - previous > 1800):
            finish(None)
            run = []
        if bad(clip):
            run.append(clip)
        else:
            finish(clip)
            run = []
        previous = clip["at"]
    finish(None)
    return {"definition": ">=3 consecutive logged handsfree gates <=1.5s rejected/empty/signoff; mode changes or >30min gap split runs",
            "recovery_caveat": "Next nonmatching capture is not proof of recovery: inspect its duration/outcome. A passed short clip breaks the strict run.",
            "time_format": "Unix seconds UTC; log timestamps converted using explicit utc_offset_hours",
            "utc_offset_hours": offset_hours, "log_coverage": log_coverage,
            "flight_coverage": flight_coverage, "logged_handsfree_clips": len(clips),
            "flight_silence_closes": len(closes),
            "flight_only_caveat": "No rejection verdict or spoken-audio evidence on flight-only days; excluded from stretch counts",
            "stretch_count": len(runs), "follow_hold_within_60s": sum(r["hold_within_60s"] for r in runs),
            "not_follow_hold_within_60s": sum(not r["hold_within_60s"] for r in runs),
            "stretches": runs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", required=True, type=Path)
    parser.add_argument("--flight", required=True, type=Path)
    parser.add_argument("--utc-offset-hours", type=float, default=-8)
    args = parser.parse_args()
    print(json.dumps(analyse(args.logs, args.flight, args.utc_offset_hours), indent=2))


if __name__ == "__main__":
    main()
