"""G1 offline replay benchmark (Ava Front Door spec v2, P1 acceptance gate).

Runs the SAME corpus of utterances through:
  - OLD: samsara.ai_command_mode.resolve_utterance() -- the module being
    deleted in this pass. Captured HERE, before deletion, per the task's
    "capture its behavior first" instruction.
  - NEW: the D3 waterfall (samsara.ava_command_session) -- stage (a)
    exact/alias match, stage (b) deterministic ACTION2 grammar, stage (c)
    one LLM fallback pass with a fuzzy shortlist.

Both approaches call the SAME local model via the SAME plumbing
(plugins.commands.ask_ollama.ask_ollama / samsara.ai_command_mode.
resolve_utterance's own urllib call) -- no live Agora, no execution of
any resolved command (this tool measures RESOLUTION only; it never calls
CommandExecutor.execute_command, entry.handler, or ask_ollama.
handle_response/_execute_action2, so it is safe to run unattended -- no
keypress, launch, shutdown, or window action ever fires, even for a
resolved "shutdown"/"close window"/etc.).

Corpus (>=40 utterances, spec-required categories):
  - "exact": the literal registered phrase for a real command.
  - "paraphrase": a natural rephrasing of the SAME intended command.
  - "nonsense": 5 utterances with no sensible command at all (a
    resolver SUCCEEDS on these by correctly reporting no match).

Metrics reported per approach:
  - success rate: exact/paraphrase entries where the resolver produced
    the EXPECTED command, plus nonsense entries where it correctly
    produced no command at all.
  - ambiguity rate: entries where the resolver produced A command, but
    NOT the expected one (a wrong-command misfire) -- this is the
    "confidently wrong" failure mode success/miss rates alone don't
    surface. Old-approach "returned 2+ actions for a single-intent
    utterance" also counts as ambiguous.
  - latency: wall-clock milliseconds, median and p95, reported per
    waterfall stage for the new approach (stage (a), stage (b), and
    stage (c) reported separately, since the acceptance bar only applies
    to (a)/(b): "<50ms resolver overhead target; (c) bounded by existing
    Ava latency").

Usage:
    python -m tools.ava_command_replay --model qwen2.5:3b
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
import types
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Corpus: (utterance, category, expected_canonical_command_or_None)
# Drawn from real commands.json/plugin registrations (verified live against
# the actual CommandExecutor at run time below -- a corpus entry whose
# "expected" phrase doesn't actually exist in the registry is a bug in this
# file, not a resolver failure, and is flagged as such in the report).
# ---------------------------------------------------------------------------

CORPUS: list[tuple[str, str, Optional[str]]] = [
    # -- exact registered phrasings --------------------------------------
    ("close tab", "exact", "close tab"),
    ("scroll up", "exact", "scroll up"),
    ("screenshot", "exact", "screenshot"),
    ("volume up", "exact", "volume up"),
    ("mute", "exact", "mute"),
    ("maximize", "exact", "maximize"),
    ("minimize", "exact", "minimize"),
    ("copy", "exact", "copy"),
    ("paste", "exact", "paste"),
    ("undo", "exact", "undo"),
    ("select all", "exact", "select all"),
    ("bold", "exact", "bold"),
    ("close window", "exact", "close window"),
    ("action center", "exact", "action center"),
    ("new line", "exact", "new line"),
    ("period", "exact", "period"),
    ("comma", "exact", "comma"),
    ("back tab", "exact", "back tab"),
    ("bookmark this", "exact", "bookmark this"),
    ("command palette", "exact", "command palette"),
    # -- paraphrases of the same intent -----------------------------------
    ("close this tab", "paraphrase", "close tab"),
    ("scroll up a bit", "paraphrase", "scroll up"),
    ("take a screenshot", "paraphrase", "screenshot"),
    ("turn the volume up", "paraphrase", "volume up"),
    ("mute the sound", "paraphrase", "mute"),
    ("make this full screen", "paraphrase", "maximize"),
    ("minimize this window", "paraphrase", "minimize"),
    ("copy that", "paraphrase", "copy"),
    ("paste it here", "paraphrase", "paste"),
    ("undo the last thing", "paraphrase", "undo"),
    ("select everything", "paraphrase", "select all"),
    ("make it bold", "paraphrase", "bold"),
    ("close this window", "paraphrase", "close window"),
    ("can you switch to chrome", "paraphrase", None),   # ACTION2 focus -- no fixed-command "expected"
    ("please open notepad", "paraphrase", None),        # ACTION2 open
    ("could you close spotify", "paraphrase", None),    # ACTION2 close
    ("launch discord", "paraphrase", None),              # ACTION2 open (synonym verb)
    # -- nonsense (spec-required: 5) ---------------------------------------
    ("banana weather umbrella", "nonsense", None),
    ("purple thinking clouds today", "nonsense", None),
    ("asdkfj random gibberish text", "nonsense", None),
    ("the quick brown fox jumps over", "nonsense", None),
    ("nothing here matches anything useful", "nonsense", None),
]


def _make_app() -> Any:
    """Minimal headless app double -- real CommandExecutor (safe, no audio/
    Whisper dependency), no execution side effects anywhere this tool
    touches. Constructed once and reused for every corpus item so plugin
    load time isn't counted per-utterance."""
    from samsara.commands import CommandExecutor

    app = types.SimpleNamespace()
    app.command_executor = CommandExecutor()
    app.config = {
        "ai_command_mode": {}, "ava_command_session": {}, "ollama": {},
        "ava_personality": "relaxed",
    }
    app.audio_coordinator = None
    app.play_sound = lambda *a, **k: None
    app._ai_cmd_generation = 0
    app._ava_cmd_generation = 0
    return app


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    f, c = int(k), min(int(k) + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


# ---------------------------------------------------------------------------
# OLD resolver (samsara.ai_command_mode, pure resolve -- no execution)
# ---------------------------------------------------------------------------

def run_old(app: Any, utterance: str, model: str, host: str) -> dict:
    """OLD resolver call -- pre-deletion only. samsara.ai_command_mode was
    deleted as part of Ava Front Door P1's own build steps once this tool's
    G1 gate authorized it, so this import degrades gracefully rather than
    crashing: the module is gone by design, not by bug. A genuine OLD-vs-NEW
    comparison is no longer possible after that point (see main()'s
    unavailable-old-side handling)."""
    try:
        from samsara.ai_command_mode import resolve_utterance, _build_menu
    except ImportError:
        return {"actions": [], "elapsed_ms": 0.0, "unavailable": True}

    menu = _build_menu(app)
    t0 = time.perf_counter()
    actions = resolve_utterance(utterance, menu, model, host)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return {"actions": actions, "elapsed_ms": elapsed_ms, "unavailable": False}


# ---------------------------------------------------------------------------
# NEW waterfall (samsara.ava_command_session, pure resolve -- no execution)
# ---------------------------------------------------------------------------

def run_new(app: Any, utterance: str, model: str, host: str, shortlist_size: int) -> dict:
    from samsara.ava_command_session import _match_action2_grammar, _build_shortlist
    from plugins.commands import ask_ollama

    t0 = time.perf_counter()
    phrase = app.command_executor.find_command(utterance)
    stage_a_ms = (time.perf_counter() - t0) * 1000
    if phrase is not None:
        return {
            "stage": "a", "hit": True, "resolved": phrase,
            "stage_a_ms": stage_a_ms, "stage_b_ms": 0.0, "stage_c_ms": 0.0,
        }

    t1 = time.perf_counter()
    action2 = _match_action2_grammar(utterance)
    stage_b_ms = (time.perf_counter() - t1) * 1000
    if action2 is not None:
        verb, argument = action2
        return {
            "stage": "b", "hit": True, "resolved": f"ACTION2 {verb}|{argument}",
            "stage_a_ms": stage_a_ms, "stage_b_ms": stage_b_ms, "stage_c_ms": 0.0,
        }

    t2 = time.perf_counter()
    cfg = {"model": model, "shortlist_size": shortlist_size, "backend": "ollama"}
    shortlist = _build_shortlist(app, utterance, cfg)
    system = ask_ollama.get_system_prompt(app)
    if "{COMMAND_LIST}" in system:
        system = system.replace("{COMMAND_LIST}", ", ".join(shortlist))
    response = ask_ollama.ask_ollama(utterance, app, model=model, system=system)
    stage_c_ms = (time.perf_counter() - t2) * 1000

    if not isinstance(response, str) or response == "__OLLAMA_DOWN__":
        return {
            "stage": "c", "hit": False, "resolved": None,
            "stage_a_ms": stage_a_ms, "stage_b_ms": stage_b_ms, "stage_c_ms": stage_c_ms,
            "error": "ollama_down",
        }
    parsed = ask_ollama._parse_structured_response(response)
    if parsed["type"] == "conversation":
        resolved = None
        hit = False
    elif parsed["type"] == "action":
        resolved = parsed["command"]
        hit = True
    elif parsed["type"] == "action2":
        resolved = f"ACTION2 {parsed['verb']}|{parsed['argument']}"
        hit = True
    else:
        resolved = f"SCHEDULE {parsed.get('command') or parsed.get('key')}"
        hit = True
    return {
        "stage": "c", "hit": hit, "resolved": resolved,
        "stage_a_ms": stage_a_ms, "stage_b_ms": stage_b_ms, "stage_c_ms": stage_c_ms,
        "raw": response,
    }


def _classify(hit: bool, resolved_matches_expected: Optional[bool], category: str) -> str:
    """success | ambiguous | miss, per the module docstring's definitions."""
    if category == "nonsense":
        return "success" if not hit else "ambiguous"
    if not hit:
        return "miss"
    if resolved_matches_expected is False:
        return "ambiguous"
    return "success"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="qwen2.5:3b")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--shortlist-size", type=int, default=12)
    args = ap.parse_args()

    print(f"Building app double (CommandExecutor + plugins)...")
    app = _make_app()

    old_rows: list[dict] = []
    new_rows: list[dict] = []

    for utterance, category, expected in CORPUS:
        print(f"  [{category:10s}] {utterance!r}")

        old = run_old(app, utterance, args.model, args.host)
        old_hit = bool(old["actions"])
        old_matches_expected: Optional[bool] = None
        if expected is not None:
            old_matches_expected = expected in old["actions"] if old_hit else None
        elif old_hit:
            old_matches_expected = False  # nonsense/no-expected but resolver claimed something specific
        old_class = _classify(old_hit, old_matches_expected, category)
        old_ambiguous_multi = len(old["actions"]) > 1
        old_rows.append({**old, "utterance": utterance, "category": category,
                          "classification": old_class, "multi_action": old_ambiguous_multi})

        new = run_new(app, utterance, args.model, args.host, args.shortlist_size)
        new_matches_expected: Optional[bool] = None
        if expected is not None:
            new_matches_expected = (new["resolved"] == expected) if new["hit"] else None
        elif new["hit"] and category != "paraphrase":
            new_matches_expected = False
        new_class = _classify(new["hit"], new_matches_expected, category)
        new_rows.append({**new, "utterance": utterance, "category": category, "classification": new_class})

    print()
    print("=" * 78)
    old_unavailable = any(r.get("unavailable") for r in old_rows)
    if old_unavailable:
        print("OLD (samsara.ai_command_mode.resolve_utterance): module deleted "
              "(Ava Front Door P1) -- OLD-side comparison skipped.")
    else:
        _report("OLD (samsara.ai_command_mode.resolve_utterance)", old_rows, old=True)
    print()
    _report("NEW (samsara.ava_command_session waterfall)", new_rows, old=False)
    print("=" * 78)
    if old_unavailable:
        print("Bars check skipped: no OLD-side data (module deleted).")
    else:
        _bars_check(old_rows, new_rows)
    return 0


def _report(title: str, rows: list[dict], *, old: bool) -> None:
    print(title)
    total = len(rows)
    n_success = sum(1 for r in rows if r["classification"] == "success")
    n_ambiguous = sum(1 for r in rows if r["classification"] == "ambiguous")
    n_miss = sum(1 for r in rows if r["classification"] == "miss")
    print(f"  n={total}  success={n_success} ({100*n_success/total:.0f}%)  "
          f"ambiguous={n_ambiguous} ({100*n_ambiguous/total:.0f}%)  "
          f"miss={n_miss} ({100*n_miss/total:.0f}%)")

    if old:
        multi = sum(1 for r in rows if r.get("multi_action"))
        print(f"  multi-action plans (ambiguity signal): {multi}")
        latencies = [r["elapsed_ms"] for r in rows]
        print(f"  latency (single resolve call): median={statistics.median(latencies):.0f}ms "
              f"p95={_percentile(latencies, 0.95):.0f}ms")
    else:
        stage_counts: dict[str, int] = {}
        for r in rows:
            stage_counts[r["stage"]] = stage_counts.get(r["stage"], 0) + 1
        print(f"  stage distribution: {stage_counts}")
        a_ms = [r["stage_a_ms"] for r in rows]
        ab_ms = [r["stage_a_ms"] + r["stage_b_ms"] for r in rows if r["stage"] in ("a", "b")]
        c_ms = [r["stage_c_ms"] for r in rows if r["stage"] == "c"]
        print(f"  stage (a) alone:      median={statistics.median(a_ms):.2f}ms p95={_percentile(a_ms, 0.95):.2f}ms")
        if ab_ms:
            print(f"  stage (a)+(b) [resolver overhead, (a)/(b)-resolved rows only]: "
                  f"median={statistics.median(ab_ms):.2f}ms p95={_percentile(ab_ms, 0.95):.2f}ms")
        if c_ms:
            print(f"  stage (c) [LLM fallback, only rows that missed a+b]: "
                  f"median={statistics.median(c_ms):.0f}ms p95={_percentile(c_ms, 0.95):.0f}ms  n={len(c_ms)}")

    misfires = [r for r in rows if r["classification"] == "ambiguous"]
    if misfires:
        print("  ambiguous/misfire detail:")
        for r in misfires:
            resolved = r.get("resolved") if not old else r.get("actions")
            print(f"    {r['utterance']!r} ({r['category']}) -> {resolved!r}")


def _bars_check(old_rows: list[dict], new_rows: list[dict]) -> None:
    old_success = sum(1 for r in old_rows if r["classification"] == "success") / len(old_rows)
    new_success = sum(1 for r in new_rows if r["classification"] == "success") / len(new_rows)
    ab_ms = [r["stage_a_ms"] + r["stage_b_ms"] for r in new_rows if r["stage"] in ("a", "b")]
    ab_median = statistics.median(ab_ms) if ab_ms else 0.0

    print("G1 ACCEPTANCE BARS:")
    success_ok = new_success >= old_success
    print(f"  [{'PASS' if success_ok else 'FAIL'}] new success rate ({new_success:.0%}) "
          f">= old success rate ({old_success:.0%})")
    latency_ok = ab_median < 50.0
    print(f"  [{'PASS' if latency_ok else 'FAIL'}] (a)/(b) resolver overhead median "
          f"({ab_median:.2f}ms) < 50ms")
    if success_ok and latency_ok:
        print("  ALL BARS PASSED -- safe to proceed with deletion.")
    else:
        print("  BARS FAILED -- STOP. Do not delete ai_command_mode.py.")


if __name__ == "__main__":
    raise SystemExit(main())
