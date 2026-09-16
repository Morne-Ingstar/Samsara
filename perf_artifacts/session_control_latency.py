"""Queue 80, step 1: stage budget from speech offset to text delivered.

Measurement only -- no app code is touched or imported. Every number comes
from timestamps Samsara already writes to samsara.log (millisecond clock),
so the harness can be re-run against any log, before or after a change:

    python perf_artifacts/session_control_latency.py [LOG ...] [--out-prefix PATH]

With no LOG it reads ~/.samsara/logs/samsara.log and samsara.log.1.

Two event families are reconstructed.

A. Toggle DICTATE lane, commit by end word ("End." / "Over." ...). Lines used,
   in order, for one commit:
     [CMD-UTT] capture mode=dictate duration=D     endpoint decided (capture closed)
     [CMD-UTT] Transcribing Ds utterance           decode queued
     Processing audio with duration ...            decode started (model lock held)
     [CMD-UTT] "End."                              decode finished
     [GATE] pass: max contiguous speech Xms at Ys  gate finished
     Processing audio with duration S / Skipping commit re-decode   commit re-decode
     [SMART] ...                                   corrections (disabled or run)
     [PASTE] Ctrl+V sent                           injection sent
     [SESSION] ... outcome=dictate_committed       bookkeeping done
   Speech offset inside the buffer is Y + X (the gate's longest speech run).
   For a one-word end utterance the longest run IS the word, so
   endpoint_wait = D - (Y + X). Longer utterances are not used for that stage.

B. Every DICTATE-lane utterance (staged or committing): decode latency vs
   audio length, from "Transcribing" to the transcript line.

Stage names map to the brief:
  endpoint_wait      VAD/endpoint decision (silence after the word)
  queue_wait         buffer assembly + wait for the Whisper lock
  decode_word        Whisper decode of the end-word utterance itself
  gate               speech gate after decode
  commit_redecode    re-decode of the whole session audio before commit
  smart_to_paste     smart corrections + formatting tokens + clipboard + Ctrl+V
  paste_to_logged    post-paste bookkeeping (not user-visible)
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - (\w+) - (.*)$")
CAPTURE = re.compile(r"\[CMD-UTT\] capture mode=(\w+) duration=([\d.]+)s")
TRANSCRIBING = re.compile(r"\[CMD-UTT\] Transcribing ([\d.]+)s utterance")
PROCESSING = re.compile(r"Processing audio with duration (\d+):([\d.]+)")
TRANSCRIPT = re.compile(r'\[CMD-UTT\] "(.*)"$')
GATE = re.compile(r"\[GATE\] pass: max contiguous speech (\d+)ms at ([\d.]+)s \(buffer ([\d.]+)s\)")
SKIP_REDECODE = re.compile(r"Skipping commit re-decode")
SMART = re.compile(r"\[SMART\]")
PASTE = re.compile(r"\[PASTE\] Ctrl\+V sent chars=(\d+)")
OUTCOME = re.compile(r"\[SESSION\] mode=(\w+) outcome=(\w+)")

CONTROL_WORDS = {"end", "over", "done", "send", "cancel", "scratch that", "abort"}

# pyautogui.PAUSE default (0.1 s) + samsara.constants.CLIPBOARD_RESTORE_DELAY
# (0.139 s): both run AFTER Ctrl+V and before [PASTE] is logged.
POST_CTRL_V_MS = 100.0 + 139.0


def parse_ts(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f").timestamp()


def read_lines(paths: list[Path]) -> list[tuple[float, str, str]]:
    out = []
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                m = TS.match(raw.rstrip("\n"))
                if m:
                    out.append((parse_ts(m.group(1)), m.group(1), m.group(3)))
    out.sort(key=lambda r: r[0])
    return out


def norm_word(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def summarise(values: list[float]) -> dict:
    return {
        "n": len(values),
        "median_ms": None if not values else round(statistics.median(values), 1),
        "p95_ms": None if not values else round(pct(values, 0.95), 1),
        "min_ms": None if not values else round(min(values), 1),
        "max_ms": None if not values else round(max(values), 1),
    }


def utterances(lines):
    """Yield one dict per DICTATE-lane utterance, from capture to outcome."""
    i, n = 0, len(lines)
    while i < n:
        t, stamp, msg = lines[i]
        cap = CAPTURE.search(msg)
        if not cap:
            i += 1
            continue
        u = {"capture_ts": stamp, "t_capture": t, "mode": cap.group(1), "duration_s": float(cap.group(2))}
        j = i + 1
        # Stop at the next capture: anything after it belongs to that one.
        while j < n and not CAPTURE.search(lines[j][2]):
            tj, _, mj = lines[j]
            if "t_transcribing" not in u and TRANSCRIBING.search(mj):
                u["t_transcribing"] = tj
            elif "t_transcribing" in u and "t_decode_start" not in u and PROCESSING.search(mj):
                u["t_decode_start"] = tj
            elif "t_transcript" not in u and TRANSCRIPT.search(mj):
                u["t_transcript"] = tj
                u["text"] = TRANSCRIPT.search(mj).group(1)
            elif "t_gate" not in u and GATE.search(mj):
                g = GATE.search(mj)
                u["t_gate"] = tj
                u["gate_run_ms"] = int(g.group(1))
                u["gate_at_s"] = float(g.group(2))
            elif "t_gate" in u and "t_redecode_start" not in u and PROCESSING.search(mj):
                p = PROCESSING.search(mj)
                u["t_redecode_start"] = tj
                u["redecode_audio_s"] = int(p.group(1)) * 60 + float(p.group(2))
            elif "t_gate" in u and SKIP_REDECODE.search(mj):
                u["redecode_skipped"] = True
            elif "t_gate" in u and "t_smart" not in u and SMART.search(mj):
                u["t_smart"] = tj
            elif "t_paste" not in u and PASTE.search(mj):
                u["t_paste"] = tj
                u["paste_chars"] = int(PASTE.search(mj).group(1))
            elif OUTCOME.search(mj) and "outcome" not in u:
                u["t_outcome"] = tj
                u["outcome"] = OUTCOME.search(mj).group(2)
                break
            j += 1
        yield u
        i = j if j > i else i + 1


def ms(a, b):
    return None if a is None or b is None else (b - a) * 1000.0


def analyse(paths: list[Path]) -> dict:
    lines = read_lines(paths)
    utts = list(utterances(lines))
    dictate = [u for u in utts if u["mode"] == "dictate" and "t_transcript" in u]

    commits = [u for u in dictate if u.get("outcome") == "dictate_committed" and "t_paste" in u]
    rows = []
    for u in commits:
        word = norm_word(u.get("text", ""))
        one_word = word in CONTROL_WORDS
        speech_end_s = (u["gate_at_s"] + u["gate_run_ms"] / 1000.0) if "t_gate" in u else None
        row = {
            "capture_ts": u["capture_ts"],
            "text": u.get("text"),
            "duration_s": u["duration_s"],
            "endpoint_wait": (u["duration_s"] - speech_end_s) * 1000.0 if (one_word and speech_end_s is not None) else None,
            "queue_wait": ms(u["t_capture"], u.get("t_decode_start")),
            "decode_word": ms(u.get("t_decode_start"), u.get("t_transcript")),
            "gate": ms(u.get("t_transcript"), u.get("t_gate")),
            "commit_redecode": ms(u.get("t_gate"), u.get("t_smart")) if not u.get("redecode_skipped") else None,
            "redecode_audio_s": u.get("redecode_audio_s"),
            "redecode_skipped": bool(u.get("redecode_skipped")),
            "smart_to_paste": ms(u.get("t_smart"), u.get("t_paste")),
            "paste_to_logged": ms(u.get("t_paste"), u.get("t_outcome")),
            "capture_to_paste": ms(u["t_capture"], u.get("t_paste")),
        }
        if row["endpoint_wait"] is not None and row["capture_to_paste"] is not None:
            row["speech_offset_to_paste"] = row["endpoint_wait"] + row["capture_to_paste"]
            # [PASTE] is logged after clipboard.paste_with_preservation returns:
            # Ctrl+V, then pyautogui.PAUSE (0.1 s), then CLIPBOARD_RESTORE_DELAY
            # (0.139 s), then the restore. The text is on screen at Ctrl+V.
            row["speech_offset_to_ctrl_v_est"] = row["speech_offset_to_paste"] - POST_CTRL_V_MS
        rows.append(row)

    stages = ["endpoint_wait", "queue_wait", "decode_word", "gate", "commit_redecode",
              "smart_to_paste", "paste_to_logged", "capture_to_paste", "speech_offset_to_paste",
              "speech_offset_to_ctrl_v_est"]
    one_word_rows = [r for r in rows if r["endpoint_wait"] is not None]
    table = {s: summarise([r[s] for r in one_word_rows if r.get(s) is not None]) for s in stages}
    table_all_commits = {s: summarise([r[s] for r in rows if r.get(s) is not None]) for s in stages}

    # Endpoint wait on every staged one-run utterance too (short utterances
    # whose longest run is plausibly the last): gives the silence policy a
    # bigger sample than the commits alone.
    staged_tail = []
    for u in dictate:
        if "t_gate" not in u or u["duration_s"] > 2.5:
            continue
        tail = (u["duration_s"] - (u["gate_at_s"] + u["gate_run_ms"] / 1000.0)) * 1000.0
        if 0 <= tail <= 3000:
            staged_tail.append(tail)

    decode_buckets: dict[str, list[float]] = {"<=2s": [], "2-5s": [], "5-10s": [], ">10s": []}
    for u in dictate:
        d = ms(u.get("t_decode_start"), u.get("t_transcript"))
        if d is None:
            continue
        s = u["duration_s"]
        key = "<=2s" if s <= 2 else "2-5s" if s <= 5 else "5-10s" if s <= 10 else ">10s"
        decode_buckets[key].append(d)
    redecode = [(r["redecode_audio_s"], r["commit_redecode"]) for r in rows
                if r.get("commit_redecode") is not None and r.get("redecode_audio_s")]

    return {
        "logs": [str(p) for p in paths],
        "utterances_parsed": len(dictate),
        "commits": len(rows),
        "one_word_end_commits": len(one_word_rows),
        "stage_table_one_word_end": table,
        "stage_table_all_commits": table_all_commits,
        "endpoint_wait_short_utterances": summarise(staged_tail),
        "decode_by_audio_length": {k: summarise(v) for k, v in decode_buckets.items()},
        "commit_redecode_points": [{"audio_s": a, "ms": round(b, 1)} for a, b in redecode],
        "rows": rows,
    }


def render_md(result: dict) -> str:
    def fmt(v):
        return "-" if v is None else f"{v:.0f}"

    names = {
        "endpoint_wait": "VAD/endpoint wait (silence after the word)",
        "queue_wait": "buffer assembly + Whisper lock wait",
        "decode_word": "Whisper decode of the end word",
        "gate": "speech gate",
        "commit_redecode": "commit re-decode of whole session",
        "smart_to_paste": "smart corrections + formatting + paste",
        "paste_to_logged": "after paste (bookkeeping, not visible)",
        "capture_to_paste": "capture closed -> Ctrl+V",
        "speech_offset_to_paste": "speech offset -> [PASTE] logged (after clipboard restore)",
        "speech_offset_to_ctrl_v_est": "SPEECH OFFSET -> TEXT ON SCREEN (Ctrl+V, est. [PASTE] - 239 ms)",
    }
    out = ["| Stage | n | median ms | p95 ms | min | max |", "|---|---|---|---|---|---|"]
    for key, label in names.items():
        s = result["stage_table_one_word_end"][key]
        out.append(f"| {label} | {s['n']} | {fmt(s['median_ms'])} | {fmt(s['p95_ms'])} | {fmt(s['min_ms'])} | {fmt(s['max_ms'])} |")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="*")
    ap.add_argument("--out-prefix", default=None)
    args = ap.parse_args(argv)
    home = Path.home() / ".samsara" / "logs"
    paths = [Path(p) for p in args.logs] or [home / "samsara.log.1", home / "samsara.log"]
    result = analyse(paths)
    print(render_md(result))
    print(json.dumps({k: v for k, v in result.items() if k not in ("rows", "commit_redecode_points")}, indent=1))
    if args.out_prefix:
        Path(args.out_prefix + ".json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        Path(args.out_prefix + ".md").write_text(render_md(result) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
