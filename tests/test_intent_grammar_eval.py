"""Coverage of the deterministic intent grammar over the generated corpus.

tests/fixtures/intent_eval.jsonl (tools/gen_intent_eval.py): 40 lines per
canonical_id -- 37 phrasings that should resolve to it, 3 near-misses that
must not. Asserts overall resolved-correctly >= COVERAGE_FLOOR and ZERO
dictated sentences resolved to any command, and prints the report (overall,
by phrasing kind, the 20 worst commands with an example failure, negatives,
tier 1+2 latency).
"""

import contextlib
import importlib.util
import io
import json
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from samsara.intent import resolve as rs

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "intent_eval.jsonl"
COVERAGE_FLOOR = 0.85
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
def corpus():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(scope="module")
def report(gen, catalog, corpus):
    records, reserved = catalog
    resolver = rs.IntentResolver(records, app_names=gen.APP_NAMES, reserved=reserved)
    resolver.resolve("warm up")
    per = defaultdict(lambda: {"ok": 0, "n": 0, "fail": None})
    kinds = defaultdict(lambda: [0, 0])
    negatives = Counter()
    negative_hits = defaultdict(list)
    suggested_sentences = 0
    t12 = []
    for line in corpus:
        res = resolver.resolve(line["text"])
        t12.append(res.t12_ms)
        if line["polarity"] == "positive":
            ok = res.kind == rs.RESOLVED and res.canonical_id in line["accept"]
            row = per[line["id"]]
            row["n"] += 1
            row["ok"] += ok
            kinds[line["kind"]][0] += ok
            kinds[line["kind"]][1] += 1
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
    total = sum(r["n"] for r in per.values())
    return {
        "per": dict(per), "kinds": dict(kinds), "overall": total_ok / total, "ok": total_ok, "total": total,
        "negatives": negatives, "negative_hits": dict(negative_hits), "suggested_sentences": suggested_sentences,
        "t12": {"p50": t12[len(t12) // 2], "p95": t12[int(len(t12) * 0.95)], "p99": t12[int(len(t12) * 0.99)],
                "max": t12[-1]},
    }


def test_fixture_is_generated_and_current(gen, catalog):
    records, reserved = catalog
    expected = gen.render(gen.build_lines(records, reserved))
    assert FIXTURE.read_text(encoding="utf-8") == expected, "run tools/gen_intent_eval.py"


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
        f"INTENT EVAL: overall {report['ok']}/{report['total']} = {report['overall']:.4f} "
        f"(floor {COVERAGE_FLOOR}); excluding alias/filler/punct {hard_ok}/{hard_n} = {hard_ok / hard_n:.4f}",
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
    assert report["overall"] >= COVERAGE_FLOOR
    assert not report["negative_hits"].get("sentence"), report["negative_hits"].get("sentence")


def test_near_miss_commands_and_nonsense_do_not_resolve_to_the_target(report):
    assert not report["negative_hits"].get("other_command")
    assert not report["negative_hits"].get("nonsense")


def test_tiers_one_and_two_stay_inside_the_latency_budget(report):
    assert report["t12"]["p99"] < rs.LATENCY_BUDGET_MS
    assert report["t12"]["max"] < rs.LATENCY_BUDGET_MS
