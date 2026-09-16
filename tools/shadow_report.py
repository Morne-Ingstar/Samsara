"""Summarise the shadow intent log (samsara/intent/shadow.py) -- one table, no charts.

Reads ~/.samsara/shadow/intent-*.jsonl (or --dir / explicit files) and prints:

  1. count by would-have-done (dictate / command / suggest / miss)
  2. the top 20 utterances the gate would have claimed as commands -- the
     dangerous ones: dictated text that would have executed
  3. the top 20 misses (the gate produced no decision)
  4. latency percentiles, per tier, for BOTH measurements -- they are not the
     same thing and the 2026-09-15 Move A tribunal read one as the other:
       t12_us     tiers 1+2 only. This is what resolve.LATENCY_BUDGET_MS is
                  stated against, and the only number the budget applies to.
       elapsed_us wall clock around the whole shadow decision: the lazy
                  resolver build on the first utterance after the background
                  worker respawns (it exits after 30 s idle), plus tier 3,
                  plus whatever else that thread waited on. Rows in the tens
                  of milliseconds here are cold workers, not slow resolution.
  5. execution rules (queue 93): which rule demoted a decision, and the
     rules_version populations, so one log can hold before and after
  6. false-positive candidates: would-be-command utterances longer than 8 words

Read-only: never modifies or uploads the log.

Usage:
  F:\\envs\\sami\\python.exe tools\\shadow_report.py
  F:\\envs\\sami\\python.exe tools\\shadow_report.py --dir D:\\elsewhere --days 7
  F:\\envs\\sami\\python.exe tools\\shadow_report.py C:\\Users\\me\\.samsara\\shadow\\intent-2026-09-14.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

TOP = 20
LONG_WORDS = 8

from samsara.intent.resolve import LATENCY_BUDGET_MS  # noqa: E402


def default_dir() -> Path:
    from samsara.paths import samsara_home_dir
    return samsara_home_dir() / "shadow"


def log_files(folder: Path, days: int | None) -> list:
    files = sorted(folder.glob("intent-*.jsonl"))
    if days:
        cutoff = date.today() - timedelta(days=days - 1)
        keep = []
        for f in files:
            try:
                if date.fromisoformat(f.stem[len("intent-"):]) >= cutoff:
                    keep.append(f)
            except ValueError:
                continue
        files = keep
    return files


def read_entries(files) -> tuple:
    entries, bad = [], 0
    for path in files:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(entry, dict) and "would" in entry and "text" in entry:
                entries.append(entry)
            else:
                bad += 1
    return entries, bad


def _percentiles(values) -> str:
    values = sorted(v for v in values if isinstance(v, (int, float)))
    if not values:
        return "-"
    pick = lambda q: values[min(len(values) - 1, int(len(values) * q))]
    return f"p50 {pick(0.5):,} | p95 {pick(0.95):,} | p99 {pick(0.99):,} | max {values[-1]:,} us"


def _category(would: str) -> str:
    return would.split(":", 1)[0]


def build_report(entries, bad_lines: int = 0, sources=()) -> str:
    rows = []

    def section(title):
        rows.append("")
        rows.append(title)
        rows.append("-" * len(title))

    rows.append(f"SHADOW INTENT REPORT  {len(entries)} utterance(s) from {len(sources)} file(s)"
                + (f", {bad_lines} unreadable line(s) skipped" if bad_lines else ""))

    section("Would have done")
    counts = Counter(_category(e["would"]) for e in entries)
    total = max(1, len(entries))
    for cat in ("dictate", "command", "suggest", "miss"):
        rows.append(f"  {cat:<10} {counts.get(cat, 0):>7}  {100.0 * counts.get(cat, 0) / total:5.1f}%")

    section(f"Top {TOP} utterances the gate would have EXECUTED as commands")
    claimed = Counter((e["text"], e["would"]) for e in entries if _category(e["would"]) == "command")
    if not claimed:
        rows.append("  (none)")
    for (text, would), n in claimed.most_common(TOP):
        rows.append(f"  {n:>4}x  {would[len('command:'):]:<42}  {text!r}")

    section(f"Top {TOP} misses (no decision)")
    misses = Counter((e["text"], e.get("error") or "catalog unavailable")
                     for e in entries if _category(e["would"]) == "miss")
    if not misses:
        rows.append("  (none)")
    for (text, why), n in misses.most_common(TOP):
        rows.append(f"  {n:>4}x  {why:<24}  {text!r}")

    section("Latency (microseconds)")
    budget_us = int(LATENCY_BUDGET_MS * 1000)
    rows.append(f"  BUDGET: {budget_us:,} us, and it applies to t12 (tiers 1+2) ONLY.")
    rows.append("  elapsed = the whole shadow decision incl. tier 3 and the cold resolver")
    rows.append("  build after an idle worker respawn -- no budget is stated against it.")
    rows.append("")
    rows.append(f"  t12      ALL        {_percentiles(e.get('t12_us') for e in entries)}")
    rows.append(f"  elapsed  ALL        {_percentiles(e.get('elapsed_us') for e in entries)}")
    for tier in ("exact", "grammar", "similarity", None):
        subset = [e for e in entries if e.get("tier") == tier]
        if not subset:
            continue
        label = tier or "(dictate)"
        rows.append(f"  t12      {label:<11}{_percentiles(e.get('t12_us') for e in subset)}  (n={len(subset)})")
        rows.append(f"  elapsed  {label:<11}{_percentiles(e.get('elapsed_us') for e in subset)}  (n={len(subset)})")
    over = [e for e in entries if isinstance(e.get("t12_us"), (int, float)) and e["t12_us"] > budget_us]
    rows.append("")
    rows.append(f"  t12 rows OVER the {budget_us:,} us budget: {len(over)} of {len(entries)}")
    for e in over[:TOP]:
        rows.append(f"    {e.get('ts', '?')[:19]}  t12={e['t12_us']:,} us  "
                    f"elapsed={e.get('elapsed_us', 0):,} us  {e['text']!r}")

    section("Execution rules (queue 93)")
    versions = Counter(e.get("rules_version") for e in entries)
    for version, n in sorted(versions.items(), key=lambda kv: (kv[0] is None, kv[0])):
        rows.append(f"  rules_version {str(version):<8} {n:>7}  "
                    f"{100.0 * n / max(1, len(entries)):5.1f}%")
    blocked = Counter(e.get("blocked") for e in entries if e.get("blocked"))
    if not blocked:
        rows.append("  (no decision was demoted by an execution rule)")
    for reason, n in blocked.most_common():
        rows.append(f"  blocked {reason:<22} {n:>7}")
    forced = sum(1 for e in entries if e.get("forced"))
    rows.append(f"  spoken with the command prefix: {forced}")
    # Queue 127: rule 2 says "only an exact match executes", and the belief was
    # that zero word-penalty meant the user spoke a registered form. It does
    # not: a hand-written grammar rule and a template with an inserted
    # determiner both cost nothing. `literal` records the difference on the
    # row, and this is the line that reads it back. NOTHING GATES ON IT -- it
    # is here so the size of that gap is a number instead of an assumption.
    would_run = [e for e in entries if _category(e["would"]) == "command"]
    with_field = [e for e in would_run if "literal" in e]
    if not with_field:
        rows.append("  literal: no row carries the field (all rows predate queue 127)")
    else:
        not_literal = sum(1 for e in with_field if not e.get("literal"))
        rows.append(f"  would-execute rows whose words were NOT a registered form "
                    f"(literal=false): {not_literal} of {len(with_field)}"
                    + (f"  [{len(would_run) - len(with_field)} older row(s) have no field]"
                       if len(would_run) != len(with_field) else ""))
        by_tier = Counter(e.get("tier") for e in with_field if not e.get("literal"))
        for tier, n in by_tier.most_common():
            rows.append(f"    non-literal via tier {str(tier):<12} {n:>7}")

    section(f"False-positive candidates: would-be commands longer than {LONG_WORDS} words")
    long_ones = [e for e in entries
                 if _category(e["would"]) == "command" and len(str(e["text"]).split()) > LONG_WORDS]
    if not long_ones:
        rows.append("  (none)")
    for e in long_ones:
        rows.append(f"  {e.get('ts', '?')[:19]}  {e['would'][len('command:'):]:<36}  "
                    f"{e.get('tier') or '-':<10} {e.get('app') or '-':<16} {e['text']!r}")
    return "\n".join(rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=Path, help="explicit intent-*.jsonl files")
    parser.add_argument("--dir", type=Path, default=None, help="shadow folder (default ~/.samsara/shadow)")
    parser.add_argument("--days", type=int, default=None, help="only the last N days of files")
    args = parser.parse_args(argv)
    files = list(args.files) or log_files(args.dir or default_dir(), args.days)
    if not files:
        print(f"no shadow logs found in {args.dir or default_dir()}")
        return 1
    entries, bad = read_entries(files)
    print(build_report(entries, bad, files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
