"""Coverage of the deterministic intent grammar over the generated corpus.

tests/fixtures/intent_eval.jsonl (tools/gen_intent_eval.py): 40 lines per
canonical_id -- 37 phrasings that should resolve to it, 3 near-misses that
must not.

Two coverage numbers since queue 93 put execution rules in front of the tiers,
because "the gate understood you" and "the gate is allowed to act" stopped
being the same question:

  IDENTIFIED  the gate named the right command, resolved OR suggested. This
              is what the grammar is for, it is what the tiers are measured
              on, and it keeps the original COVERAGE_FLOOR of 0.85.
  EXECUTABLE  the gate was allowed to RUN it. Rules 1 (no single-word command
              executes) and 2 (only an exact match executes) demote the rest
              to a suggestion or to dictation. Pinned at EXECUTABLE_FLOOR,
              which is deliberately BELOW identified coverage: the gap is the
              accessibility cost the Move A tribunal asked to have measured,
              and the floor is here so the gap cannot widen unnoticed.

Queue 127 splits both numbers over two corpora, because two thirds of the
positives are the `filler` kind -- the canonical phrase with a courtesy word
in front of it ("please scroll down", "could you scroll down") -- and those
resolve almost for free:

  FULL CORPUS   every positive line. This is the number queue 93 published and
                the one the floors were set against, so it stays. It answers
                "of everything a user might say to reach a command, how much
                works", with the corpus's own mix of easy and hard.
  NON-FILLER    the same measure with the filler lines removed, leaving the
                phrasings that actually stress the grammar: aliases, synonyms,
                reorderings, mishearings, numerals, slots, punctuation,
                determiners. It answers "when the user does NOT say the
                command the way the catalog spells it, how much works". It is
                the harder and more honest number, and it is much lower.

Both are published. Neither replaces the other: the full number is what the
floors and queue 93's history are denominated in, and the non-filler number is
what a decision about turning the rules on should be read from.

Also asserts ZERO dictated sentences resolved to any command, and prints the
report (both coverages over both corpora, by phrasing kind, the 20 worst
commands with an example failure, negatives, tier 1+2 latency).
"""

import contextlib
import importlib.util
import io
import json
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from samsara.intent import resolve as rs
from samsara.command_scope import MatchContext

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "intent_eval.jsonl"
COVERAGE_FLOOR = 0.85
#: Queue 93: measured at 0.7716 the day the execution rules landed (16,961 ->
#: 13,733 of 17,797 corpus phrasings still execute). The floor is set just
#: under that so a further erosion fails rather than passing quietly.
EXECUTABLE_FLOOR = 0.76
#: Queue 127. The same two measures over the non-filler positives only
#: (5,946 of 18,167). Measured 0.9310 identified / 0.6140 executable against
#: the 491-command catalog; floors set just under. These are SEPARATE floors,
#: not replacements -- see the module docstring.
NONFILLER_COVERAGE_FLOOR = 0.92
NONFILLER_EXECUTABLE_FLOOR = 0.60
#: The phrasing kind that is the canonical phrase plus a courtesy word. It is
#: two thirds of the corpus and it is the easiest kind, so it is what the
#: non-filler split removes.
FILLER_KIND = "filler"


def _live_context(record):
    """Synthetic context in which this record is eligible for grammar coverage.

    The corpus measures recognition of every catalog row. A focused-app row
    must therefore be evaluated with one of its declared apps focused rather
    than being counted as a grammar miss merely because this offline test has
    no foreground window.
    """
    scope = record.get("scope") or {}
    apps = scope.get("apps") or ()
    tags = set(scope.get("tags") or ()) | set(scope.get("argument_tags") or ())
    return MatchContext.for_app(apps[0], tags=tags) if apps else MatchContext.for_app("notepad.exe", tags=tags)
WORST = 20


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_intent_eval", REPO / "tools" / "gen_intent_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def catalog(gen):
    with contextlib.redirect_stdout(io.StringIO()):
        return gen.load_catalog()


@pytest.fixture(scope="module")
def corpus(gen, catalog):
    """Evaluate the current live schemas; the checked-in corpus is historical.

    Queue 134 deliberately changes declared slots while a later queue owns
    regenerating catalog-derived artifacts, including this fixture.
    """
    records, reserved = catalog
    return gen.build_lines(records, reserved)


@pytest.fixture(scope="module")
def report(gen, catalog, corpus):
    records, reserved = catalog
    resolver = rs.IntentResolver(records, app_names=gen.APP_NAMES, reserved=reserved)
    resolver.resolve("warm up")
    per = defaultdict(lambda: {"ok": 0, "n": 0, "fail": None})
    kinds = defaultdict(lambda: [0, 0])
    negatives = Counter()
    blocked = Counter()
    negative_hits = defaultdict(list)
    suggested_sentences = 0
    #: [ok, named, n] over the positives whose kind is not FILLER_KIND.
    nonfiller = [0, 0, 0]
    t12 = []
    by_id = {record["canonical_id"]: record for record in records}
    for line in corpus:
        # Scope-aware rows intentionally do not execute in an unknown app;
        # evaluate their language here in an eligible synthetic context.
        res = resolver.resolve(line["text"], context=_live_context(by_id[line["id"]]))
        t12.append(res.t12_ms)
        if line["polarity"] == "positive":
            named = res.canonical_id in line["accept"] or any(
                s in line["accept"] for s in (res.suggestions or ()))
            ok = res.kind == rs.RESOLVED and res.canonical_id in line["accept"]
            row = per[line["id"]]
            row["n"] += 1
            row["ok"] += ok
            row["named"] = row.get("named", 0) + named
            if res.blocked and named:
                blocked[res.blocked] += 1
            kinds[line["kind"]][0] += ok
            kinds[line["kind"]][1] += 1
            if line["kind"] != FILLER_KIND:
                nonfiller[0] += ok
                nonfiller[1] += named
                nonfiller[2] += 1
            if not ok and row["fail"] is None:
                row["fail"] = (line["text"], res.kind, res.canonical_id)
        else:
            negatives[line["kind"]] += 1
            if line["kind"] == "sentence":
                bad = res.kind == rs.RESOLVED
                suggested_sentences += res.kind == rs.SUGGEST
            else:
                bad = res.kind == rs.RESOLVED and res.canonical_id == line["reject"]
            if bad:
                negative_hits[line["kind"]].append((line["text"], res.canonical_id))
    t12.sort()
    total_ok = sum(r["ok"] for r in per.values())
    total_named = sum(r.get("named", 0) for r in per.values())
    total = sum(r["n"] for r in per.values())
    return {
        "per": dict(per), "kinds": dict(kinds), "overall": total_ok / total, "ok": total_ok, "total": total,
        "identified": total_named / total, "named": total_named, "blocked": blocked,
        "nf_ok": nonfiller[0], "nf_named": nonfiller[1], "nf_total": nonfiller[2],
        "nf_overall": nonfiller[0] / nonfiller[2], "nf_identified": nonfiller[1] / nonfiller[2],
        "negatives": negatives, "negative_hits": dict(negative_hits), "suggested_sentences": suggested_sentences,
        "t12": {"p50": t12[len(t12) // 2], "p95": t12[int(len(t12) * 0.95)], "p99": t12[int(len(t12) * 0.99)],
                "max": t12[-1]},
    }


def test_fixture_is_generated_and_current(gen, catalog):
    records, reserved = catalog
    historical = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
    assert historical
    assert {line["id"] for line in historical} == {record["canonical_id"] for record in records}


def test_corpus_covers_every_command_forty_times(catalog, corpus):
    records, _reserved = catalog
    counts = Counter(line["id"] for line in corpus)
    assert set(counts) == {r["canonical_id"] for r in records}
    assert set(counts.values()) == {40}
    per_kind = Counter((line["id"], line["polarity"]) for line in corpus)
    for cid in counts:
        assert per_kind[(cid, "positive")] == 37 and per_kind[(cid, "negative")] == 3, cid
    assert {line["kind"] for line in corpus if line["polarity"] == "negative"} == \
        {"sentence", "other_command", "nonsense"}


def test_coverage_and_no_sentence_ever_executes(report, capsys):
    per = report["per"]
    worst = sorted(per.items(), key=lambda kv: (kv[1]["ok"] / kv[1]["n"], kv[0]))[:WORST]
    trivial = {"alias", "filler", "punct"}
    hard_ok = sum(v[0] for k, v in report["kinds"].items() if k not in trivial)
    hard_n = sum(v[1] for k, v in report["kinds"].items() if k not in trivial)
    lines = [
        "",
        f"INTENT EVAL  FULL CORPUS ({report['total']} positives, filler included -- the number the "
        f"floors and queue 93 are denominated in):",
        f"  identified {report['named']}/{report['total']} = {report['identified']:.4f} "
        f"(floor {COVERAGE_FLOOR}) | executable {report['ok']}/{report['total']} = "
        f"{report['overall']:.4f} (floor {EXECUTABLE_FLOOR})",
        f"INTENT EVAL  NON-FILLER ({report['nf_total']} positives, the phrasings that stress the "
        f"grammar -- read a rules decision from this one):",
        f"  identified {report['nf_named']}/{report['nf_total']} = {report['nf_identified']:.4f} "
        f"(floor {NONFILLER_COVERAGE_FLOOR}) | executable {report['nf_ok']}/{report['nf_total']} = "
        f"{report['nf_overall']:.4f} (floor {NONFILLER_EXECUTABLE_FLOOR})",
        "demoted by execution rule: " + (", ".join(f"{k} {n}" for k, n in report["blocked"].most_common())
                                         or "(none)"),
        f"excluding alias/filler/punct {hard_ok}/{hard_n} = {hard_ok / hard_n:.4f}",
        "by kind: " + ", ".join(f"{k} {ok}/{n}={ok / n:.2f}" for k, (ok, n) in sorted(report["kinds"].items())),
        f"commands at 100%: {sum(1 for r in per.values() if r['ok'] == r['n'])}/{len(per)}",
        f"negatives: {dict(report['negatives'])}; resolved: "
        + ", ".join(f"{k}={len(v)}" for k, v in sorted(report["negative_hits"].items())) + " (none if empty); "
        f"sentences suggested (not executed): {report['suggested_sentences']}",
        "tier 1+2 ms: " + ", ".join(f"{k} {v:.3f}" for k, v in report["t12"].items())
        + f" (budget {rs.LATENCY_BUDGET_MS})",
        f"{WORST} worst commands:",
    ]
    for cid, row in worst:
        text, kind, got = row["fail"] or ("-", "-", "-")
        lines.append(f"  {row['ok']:>2}/{row['n']} {cid:45} e.g. {text!r} -> {kind} {got}")
    with capsys.disabled():
        print("\n".join(lines))
    assert report["identified"] >= COVERAGE_FLOOR
    assert report["overall"] >= EXECUTABLE_FLOOR
    assert report["overall"] <= report["identified"]     # executing is a subset of understanding
    # Queue 127: both published numbers are floored. The non-filler floors are
    # separate values, not a replacement -- removing either assert loses half
    # the picture.
    assert report["nf_identified"] >= NONFILLER_COVERAGE_FLOOR
    assert report["nf_overall"] >= NONFILLER_EXECUTABLE_FLOOR
    assert report["nf_overall"] <= report["nf_identified"]
    # The split is real: filler is the easy kind, so dropping it must not
    # leave the same number. If this ever fails the corpus has changed shape
    # and the two numbers have stopped meaning different things.
    assert report["nf_overall"] < report["overall"]
    assert not report["negative_hits"].get("sentence"), report["negative_hits"].get("sentence")


def test_near_miss_commands_and_nonsense_do_not_resolve_to_the_target(report):
    assert not report["negative_hits"].get("other_command")
    assert not report["negative_hits"].get("nonsense")


def test_tiers_one_and_two_stay_inside_the_latency_budget(report):
    assert report["t12"]["p99"] < rs.LATENCY_BUDGET_MS
    assert report["t12"]["max"] < rs.LATENCY_BUDGET_MS
