"""Read-only digest CLI over samsara/flight_recorder.py's JSONL event log.

The flight recorder (samsara/flight_recorder.py) appends one JSON line per
event to ``<home>/flight/events-YYYYMMDD.jsonl``, rotated daily. Every line
has at least ``event``, ``t_wall`` (unix epoch seconds), ``t_wall_iso``,
``t_mono``, and ``thread``, plus whatever event-specific fields the call
site passed. There is no schema registry anywhere in the repo -- the field
set below was derived by grepping every ``flight_recorder.record(`` call
site (dictation.py, samsara/audio_ducking.py, samsara/audio_engine/
wake_consumer.py, tools/fallback_dictation.py) rather than read from any
single source of truth.

This tool never writes into the recorder's storage location -- it only
opens files under ``<home>/flight`` for reading.

Privacy: dictation content can appear in these logs (``hold_recording.
decoded`` carries a 12-char ``text_preview``). Nothing in this module ever
prints a raw record. Every record is passed through ``_redact()``, an
explicit field ALLOWLIST, before it reaches any output path -- an unlisted
field (``text``, ``text_preview``, or any future field nobody allowlisted)
is dropped even if present, rather than trusting a blocklist to keep up
with every call site that might one day pass a content-bearing field.

Usage:
    python tools\\flight_digest.py [--days N] [--home PATH] [--out PATH.md]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from samsara.paths import samsara_home_dir  # noqa: E402

# ---------------------------------------------------------------------------
# Privacy allowlist -- the ONLY fields this tool will ever put in its output.
# Deliberately excludes: 'text', 'text_preview' (hold_recording.decoded),
# 'raw_bits' (key_state.not_pressed -- raw VK-state map, not content but not
# needed here either), and 'last_error' (free-text exception strings from
# the ducking subsystem -- not dictated content, but not allowlisted since
# nothing in this digest needs it).
# ---------------------------------------------------------------------------
SAFE_FIELDS = {
    "event", "t_wall", "t_wall_iso", "thread",
    "path", "why", "result", "reason", "op", "kind", "exc",
    "chars", "text_len", "audio_duration_ms", "audio_samples", "audio_s",
    "target_process", "trigger", "key_combo", "key", "mode",
    "streaming", "command_mode_recording", "ava_mode_recording",
    "low_confidence", "suspected_data_loss", "retried",
    "app_state", "wake_word_triggered", "oww_confirmed",
    "buffer_s", "variance", "speech_s",
    "pending_op", "generation", "duck_level", "noop",
    "sessions_seen", "sessions_ducked", "sessions_failed", "elapsed_ms",
    "session_id", "audio_duration_ms",
}

# 'inject' event's result values, bucketed for the outcome matrix.
_OK_RESULTS = {"ok"}
_FAILED_RESULTS = {"failed", "failed_falling_back_to_clipboard", "paste_failed"}
_CANCELLED_RESULTS = {"cancelled_target_changed"}

# Event names that are inherently failure/warning signals regardless of
# their fields (audio-ducking child-process trouble).
_ERROR_EVENT_NAMES = {"ducker_child_busy", "ducker_child_restart", "ducker_restore_skipped"}

# hold_recording.start's 'trigger' field values, bucketed hotkey vs hands-free.
_HOTKEY_TRIGGERS = {"hotkey_batch", "fallback_hotkey"}
_HANDS_FREE_TRIGGERS = {"capslock_stream"}


def _redact(rec: dict) -> dict:
    """Return only allowlisted fields. See module docstring -- this is the
    single choke point every output path in this module goes through."""
    return {k: v for k, v in rec.items() if k in SAFE_FIELDS}


def result_bucket(result) -> str:
    if result in _OK_RESULTS:
        return "ok"
    if result in _FAILED_RESULTS:
        return "failed"
    if result in _CANCELLED_RESULTS:
        return "cancelled"
    return "other" if result is not None else "unknown"


def is_error_or_warn(rec: dict) -> bool:
    """Heuristic classifier -- the recorder has no explicit severity field,
    so this treats a record as error/warn when: its event name is itself a
    failure signal, an explicit exception flag is set, or an outcome field
    resolves to a non-'ok' bucket. Documented here rather than silently
    assumed, since a future call site could use different field names."""
    event = rec.get("event")
    if event in _ERROR_EVENT_NAMES:
        return True
    if rec.get("exc") is True:
        return True
    if event == "inject" and result_bucket(rec.get("result")) in ("failed", "cancelled"):
        return True
    if event == "wake.fallback_decode_end" and rec.get("result") not in (None, "ok"):
        return True
    return False


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def iter_records(flight_dir: Path, days: int, now_ts: "float | None" = None):
    """Yield raw record dicts within the last `days` days, oldest file
    first. Malformed lines are skipped silently -- this is a best-effort
    digest over a best-effort append-only log (flight_recorder.record()
    itself never raises, so a torn/partial line is an expected possibility,
    not a bug to surface here)."""
    if not flight_dir.is_dir():
        return
    now_ts = time.time() if now_ts is None else now_ts
    cutoff_ts = now_ts - (days * 86400)
    for path in sorted(flight_dir.glob("events-*.jsonl")):
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                t_wall = rec.get("t_wall")
                if isinstance(t_wall, (int, float)) and t_wall < cutoff_ts:
                    continue
                yield rec


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def build_digest(records: list) -> dict:
    total_by_event = Counter()
    inject_matrix = defaultdict(Counter)  # (path, bucket) -> Counter(why)
    inject_totals = Counter()  # (path, bucket) -> count
    dispatch_by_kind_op = defaultdict(Counter)  # kind -> Counter(op)
    dispatch_exceptions = Counter()  # kind -> count where exc is True
    wake_close_reasons = Counter()
    oww_confirm_count = 0
    trigger_counts = Counter()
    error_warn_records = []
    day_totals = Counter()
    day_error_warn = Counter()

    for rec in records:
        event = rec.get("event", "<missing>")
        total_by_event[event] += 1

        t_wall_iso = rec.get("t_wall_iso") or ""
        day = t_wall_iso[:10] if len(t_wall_iso) >= 10 else "unknown"
        day_totals[day] += 1

        if event == "inject":
            bucket = result_bucket(rec.get("result"))
            path = rec.get("path", "unknown")
            inject_totals[(path, bucket)] += 1
            inject_matrix[(path, bucket)][rec.get("why", "unknown")] += 1

        if event == "session.dispatch":
            kind = rec.get("kind", "unknown")
            op = rec.get("op", "unknown")
            dispatch_by_kind_op[kind][op] += 1
            if op == "completed" and rec.get("exc") is True:
                dispatch_exceptions[kind] += 1

        if event == "wake.session_close":
            wake_close_reasons[rec.get("reason", "unknown")] += 1

        if event == "wake.oww_confirm":
            oww_confirm_count += 1

        if event == "hold_recording.start":
            trigger = rec.get("trigger", "unknown")
            if trigger in _HOTKEY_TRIGGERS:
                trigger_counts["hotkey"] += 1
            elif trigger in _HANDS_FREE_TRIGGERS:
                trigger_counts["hands_free"] += 1
            else:
                trigger_counts["unknown"] += 1

        if is_error_or_warn(rec):
            error_warn_records.append(_redact(rec))
            day_error_warn[day] += 1

    error_warn_records.sort(key=lambda r: r.get("t_wall", 0), reverse=True)

    return {
        "total_by_event": total_by_event,
        "inject_totals": inject_totals,
        "inject_matrix": inject_matrix,
        "dispatch_by_kind_op": dispatch_by_kind_op,
        "dispatch_exceptions": dispatch_exceptions,
        "wake_close_reasons": wake_close_reasons,
        "oww_confirm_count": oww_confirm_count,
        "trigger_counts": trigger_counts,
        "error_warn_records": error_warn_records,
        "day_totals": day_totals,
        "day_error_warn": day_error_warn,
        "record_count": sum(total_by_event.values()),
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _pct(n: int, total: int) -> str:
    if total <= 0:
        return "0.0%"
    return f"{(100.0 * n / total):.1f}%"


def render_markdown(digest: dict, days: int) -> str:
    lines = []
    lines.append(f"# Flight recorder digest -- last {days} day(s)")
    lines.append("")
    total = digest["record_count"]
    lines.append(f"Total records: **{total}**")
    lines.append("")

    lines.append("## Records by event type")
    lines.append("")
    lines.append("| Event | Count | % |")
    lines.append("|---|---|---|")
    for event, count in digest["total_by_event"].most_common():
        lines.append(f"| `{event}` | {count} | {_pct(count, total)} |")
    lines.append("")

    lines.append("## Injection outcomes")
    lines.append("")
    inject_total = sum(digest["inject_totals"].values())
    if inject_total == 0:
        lines.append("No `inject` records in this window.")
    else:
        lines.append(f"Total injection attempts: **{inject_total}**")
        lines.append("")
        lines.append("| Path | Result | Count | % of injections | Why breakdown |")
        lines.append("|---|---|---|---|---|")
        for (path, bucket), count in sorted(
            digest["inject_totals"].items(), key=lambda kv: (-kv[1], kv[0])
        ):
            why_counter = digest["inject_matrix"][(path, bucket)]
            why_str = ", ".join(f"{why}={n}" for why, n in why_counter.most_common())
            lines.append(
                f"| {path} | {bucket} | {count} | {_pct(count, inject_total)} | {why_str} |"
            )
    lines.append("")

    lines.append("## Hotkey vs. hands-free dictation counts")
    lines.append("")
    trig = digest["trigger_counts"]
    trig_total = sum(trig.values())
    if trig_total == 0:
        lines.append("No `hold_recording.start` records in this window.")
    else:
        lines.append(
            "Derived from `hold_recording.start`'s `trigger` field "
            "(`hotkey_batch`/`fallback_hotkey` = hotkey, `capslock_stream` = hands-free)."
        )
        lines.append("")
        lines.append("| Trigger class | Count | % |")
        lines.append("|---|---|---|")
        for cls in ("hotkey", "hands_free", "unknown"):
            n = trig.get(cls, 0)
            if n:
                lines.append(f"| {cls} | {n} | {_pct(n, trig_total)} |")
    lines.append("")

    lines.append("## Hands-free session metrics")
    lines.append("")
    lines.append(
        "**Not available as an explicit session concept.** The recorder has no "
        "`session started` / `session ended` event and no `exit_reason` field "
        "for hands-free (wake-word) mode as a whole. The closest signals are "
        "per-utterance, not per-session:"
    )
    lines.append("")
    lines.append(
        "- `session.dispatch` -- one command-utterance's dispatch lifecycle "
        "(`op`: started/enqueued/completed/dropped, `kind`: toggle_command/"
        "ava_command/wake_buffer/wake_buffer_tracked)."
    )
    lines.append(
        "- `wake.session_open` / `wake.session_close` -- one speech-capture "
        "window opening/closing within the always-on listener, not the "
        "hands-free mode being turned on/off."
    )
    lines.append(
        "- `wake.oww_confirm` -- one wake-word confirmation. "
        f"Count in this window: **{digest['oww_confirm_count']}**."
    )
    lines.append("")
    lines.append("Dispatch outcomes by kind (closest proxy for commits/misses):")
    lines.append("")
    lines.append("| Kind | started | enqueued | completed | dropped | completed-with-exception |")
    lines.append("|---|---|---|---|---|---|")
    for kind, ops in sorted(digest["dispatch_by_kind_op"].items()):
        lines.append(
            f"| {kind} | {ops.get('started', 0)} | {ops.get('enqueued', 0)} | "
            f"{ops.get('completed', 0)} | {ops.get('dropped', 0)} | "
            f"{digest['dispatch_exceptions'].get(kind, 0)} |"
        )
    if not digest["dispatch_by_kind_op"]:
        lines.append("| *(none in this window)* | | | | | |")
    lines.append("")
    lines.append("Capture-window close reasons (`wake.session_close`):")
    lines.append("")
    if digest["wake_close_reasons"]:
        lines.append("| Reason | Count |")
        lines.append("|---|---|")
        for reason, n in digest["wake_close_reasons"].most_common():
            lines.append(f"| {reason} | {n} |")
    else:
        lines.append("*(none in this window)*")
    lines.append("")

    lines.append("## Top error/warn records")
    lines.append("")
    lines.append(
        "Classification heuristic (no explicit severity field exists): a "
        "record counts as error/warn when its event name is itself a "
        "failure signal (`ducker_child_busy`/`ducker_child_restart`/"
        "`ducker_restore_skipped`), `exc` is `true`, or an outcome field "
        "resolves to `failed`/`cancelled`."
    )
    lines.append("")
    top = digest["error_warn_records"][:10]
    if not top:
        lines.append("None found in this window.")
    else:
        lines.append(f"Showing {len(top)} of {len(digest['error_warn_records'])} found.")
        lines.append("")
        lines.append("| Time (UTC) | Event | Detail |")
        lines.append("|---|---|---|")
        for rec in top:
            when = rec.get("t_wall_iso", "?")
            event = rec.get("event", "?")
            detail_fields = {
                k: v for k, v in rec.items()
                if k not in ("event", "t_wall", "t_wall_iso", "thread")
            }
            detail = ", ".join(f"{k}={v}" for k, v in sorted(detail_fields.items()))
            lines.append(f"| {when} | `{event}` | {detail} |")
    lines.append("")

    lines.append("## Day-by-day")
    lines.append("")
    lines.append("| Day (UTC) | Total records | Error/warn |")
    lines.append("|---|---|---|")
    for day in sorted(digest["day_totals"]):
        lines.append(
            f"| {day} | {digest['day_totals'][day]} | {digest['day_error_warn'].get(day, 0)} |"
        )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="Look-back window in days (default: 7)")
    parser.add_argument(
        "--home", type=str, default=None,
        help="Samsara home dir (default: samsara.paths.samsara_home_dir(), honors SAMSARA_HOME_DIR)",
    )
    parser.add_argument("--out", type=str, default=None, help="Write digest markdown here instead of stdout")
    args = parser.parse_args(argv)

    home = Path(args.home) if args.home else samsara_home_dir()
    flight_dir = home / "flight"

    records = list(iter_records(flight_dir, args.days))
    digest = build_digest(records)
    markdown = render_markdown(digest, args.days)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
        print(f"Digest written to {out_path}")
    else:
        print(markdown)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
