"""Queue 127: the four repairs to queue 93's execution rules and its eval.

Queue 93 shipped two rules in front of the tiers and an eval to measure what
they cost. Both were wrong in ways that made the measurement flatter than the
truth, so the decision "turn the rules on?" was about to be taken on numbers
that were not true. This file pins the repairs.

  1. ZERO PENALTY IS NOT LITERAL. Rule 2 reads "only a literal match
     executes", and the code tested `word_penalty == 0`. Those are different
     questions: a hand-written grammar rule ("go page downwards" ->
     scroll.page_down) is in no alias and costs nothing, and a template
     swallows inserted determiners ("open THE memos") for nothing. Queue 127
     adds Resolution.literal, which answers the real question, and records it
     on the shadow row. NOTHING GATES ON IT -- rule 2 is unchanged in what it
     blocks, and rule 2's docstring no longer claims something it never did.

  2. RULE 1 WAS READING THE WRONG EVIDENCE. Its third leg blocked on the
     COMMAND's canonical word count -- catalog metadata, not evidence about
     what the user said. ask_ollama.yes has the canonical "yes", so the
     two-word alias "go ahead", spoken as two words, was demoted to
     dictation: the confirmation word for Ava was unusable by voice. Leg
     removed. The two legs that read the utterance stay.

  3. A CHAIN COULD SMUGGLE A ONE-WORD COMMAND PAST RULE 1. The chain's
     alias_words was the minimum over the members that HAD an alias, so a
     member with none -- which is every hand-written rule -- contributed
     nothing and the whole chain defaulted to 0, which rule 1 reads as "not
     alias-driven, nothing to check". _trigger_words falls back to the
     canonical phrase so every member counts.

  4. THE FIXTURE WAS STALE. tests/fixtures/intent_eval.jsonl was generated
     against a smaller catalog, so commands were measured at zero lines.
     Regenerated against the live 503.

Everything here is observer-only and stays that way: TestTheGateStillRunsNothing
is the test that fails if any of this ever reaches the live path.
"""

import ast
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from samsara.intent import grammar as gr
from samsara.intent import resolve as rs
from samsara.intent import shadow as sh

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "intent_eval.jsonl"
SHADOW_LOG_DIR = Path.home() / ".samsara" / "shadow"


@pytest.fixture(scope="module")
def records():
    with contextlib.redirect_stdout(io.StringIO()):
        return rs.load_records()


@pytest.fixture(scope="module")
def resolver(records):
    r = rs.IntentResolver(records)
    r.resolve("warm up")
    return r


# ---------------------------------------------------------------------------
# 1. Zero penalty is not literal
# ---------------------------------------------------------------------------

#: (utterance, canonical_id, tier). Astra's probe is the first row. Each of
#: these costs word_penalty 0.0 and NONE of them is a form the catalog
#: registers -- the three classes found by enumerating the whole corpus:
#: a hand-written rule, a template with an inserted determiner, and a
#: template reordering.
ZERO_PENALTY_NOT_LITERAL = [
    ("go page downwards", "scroll.page_down", "grammar"),     # rule source, no alias at all
    ("open the memos", "quick_memo.open_memos", "grammar"),   # inserted determiner
    ("go to the downloads page", "web_shortcuts.go_to", "grammar"),
]


@pytest.mark.parametrize("text,cid,tier", ZERO_PENALTY_NOT_LITERAL)
def test_a_zero_penalty_match_can_still_be_words_the_catalog_never_registered(resolver, text, cid, tier):
    res = resolver.resolve(text)
    assert res.canonical_id == cid and res.tier == tier
    assert res.word_penalty == 0.0, "the premise: rule 2 sees nothing wrong with this"
    assert res.literal is False, "and yet the user did not speak a registered form"


def test_a_form_the_catalog_does_register_is_literal(resolver):
    """The other half. Without this the field could just be False everywhere."""
    for text, cid in (("open memos", "quick_memo.open_memos"),
                      ("please scroll down", "scroll.scroll_down"),
                      ("scroll down a little", "scroll.scroll_down_a_little")):
        res = resolver.resolve(text)
        assert res.canonical_id == cid and res.literal is True, (text, res.tier)


def test_every_hand_written_rule_is_non_literal_by_construction():
    """Not a sample -- the property. All four rules in grammar.RULES build a
    Parse with source "rule" and alias "", and _parse_is_literal returns
    False for any parse with no alias. So EVERY rule hit is non-literal, and
    every one of them costs zero penalty. That is the whole class."""
    assert gr.RULES, "if the rules moved, this proof moved with them"
    src = Path(gr.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    rule_names = {r.__name__ for r in gr.RULES}
    seen = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in rule_names):
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "Parse":
                seen += 1
                kw = {k.arg: k.value for k in call.keywords}
                alias = kw.get("alias")
                assert alias is None or (isinstance(alias, ast.Constant) and not alias.value), \
                    f"{node.name} now passes an alias -- re-check the literal claim"
                pen = kw.get("word_penalty")
                assert pen is None or (isinstance(pen, ast.Constant) and pen.value == 0), node.name
    assert seen >= len(rule_names), (seen, rule_names)


def test_the_corpus_agrees_that_the_two_are_different_questions(resolver):
    """Over the generated corpus, zero-penalty and literal are measurably not
    the same set. If this ever hits zero the field has stopped meaning
    anything and rule 2's old wording would have been right after all."""
    lines = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
    counts = Counter()
    for line in lines:
        if line["polarity"] != "positive":
            continue
        res = resolver.resolve(line["text"])
        if res.kind == rs.RESOLVED and res.word_penalty == 0.0:
            counts["literal" if res.literal else "not_literal"] += 1
    assert counts["not_literal"] > 0, counts
    assert counts["literal"] > counts["not_literal"], "sanity: most zero-cost matches ARE literal"


def test_nothing_gates_on_literal():
    """The field is a measurement. If block_reason ever reads it, this test
    is the one that says so -- turning the rules on is a product decision the
    owner has not made, and a new gate is not something to slip in under a
    measurement repair."""
    src = ast.parse(Path(rs.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(src)
              if isinstance(n, ast.FunctionDef) and n.name == "block_reason")
    reads = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert "literal" not in reads, "block_reason now gates on `literal`"


# ---------------------------------------------------------------------------
# 2. Rule 1's third leg was the wrong evidence
# ---------------------------------------------------------------------------

def test_the_confirmation_word_for_ava_is_usable_again(resolver):
    """"go ahead" is a two-word alias of ask_ollama.yes, spoken as two words.
    Only the CANONICAL phrase ("yes") is one word, and that is a fact about
    the catalog, not about the user. Before queue 127 this came back as
    dictation / one_word_command."""
    res = resolver.resolve("go ahead")
    assert res.kind == rs.RESOLVED
    assert res.canonical_id == "ask_ollama.yes"
    assert res.blocked is None
    assert res.literal is True and res.alias_words == 2


@pytest.mark.parametrize("text", ["yes", "ava", "copy", "two", "to"])
def test_removing_that_leg_did_not_unblock_any_single_word(resolver, text):
    """The protection queue 93 was actually built for is untouched: the two
    legs that read the UTTERANCE still demote every one of these."""
    res = resolver.resolve(text)
    assert res.kind == rs.DICTATION and res.canonical_id is None, (text, res.tier)
    assert res.blocked in (rs.BLOCK_ONE_WORD_UTTERANCE, rs.BLOCK_ONE_WORD_COMMAND)


def test_what_removing_that_leg_costs_is_pinned_not_silent(resolver):
    """The other side of the ledger, kept as a test so it cannot drift.

    "I feel it, baby." reached health_tracker.symptom and the canonical leg
    blocked it. Without that leg it resolves, and the honest reading is that
    the leg was blocking the right utterance for a false reason: the user
    said four words and "i feel" is a REGISTERED two-word alias of that
    command. What makes it unsafe is a weak two-word alias paired with a
    free-text slot, which is the identical shape to "search for cats" -- a
    command the user wants. No rule in this file can separate them, so the
    separation is a catalog decision and the owner's to make. It is recorded
    here, with the number, instead of being papered over."""
    res = resolver.resolve("I feel it, baby.")
    assert res.kind == rs.RESOLVED and res.canonical_id == "health_tracker.symptom"
    assert res.literal is False, "and THAT is the fact the owner should decide on"
    assert res.word_penalty == 0.0, "rule 2 sees nothing wrong with it, which is the point"
    # Same shape, and this one is wanted. Any fix that kills the first must
    # be checked against the second.
    wanted = resolver.resolve("search for cats")
    assert wanted.kind == rs.RESOLVED and wanted.canonical_id == "web_shortcuts.search_for"
    assert wanted.literal is False and wanted.word_penalty == 0.0


def test_rule_one_no_longer_reads_the_canonical_word_count():
    """The leg is gone from the code, not merely stepped around."""
    src = ast.parse(Path(rs.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(src)
              if isinstance(n, ast.FunctionDef) and n.name == "block_reason")
    body = ast.unparse(fn)
    assert "_canonical_words" not in body, "block_reason is reading catalog metadata again"


# ---------------------------------------------------------------------------
# 3. A chain must not smuggle a one-word command past rule 1
# ---------------------------------------------------------------------------

def test_a_chain_member_with_no_alias_still_counts_its_words(resolver):
    """The defect: alias_words was min() over the members that HAD an alias,
    so a rule-source member (alias "" by construction) was skipped and an
    empty min defaulted to 0 -- which rule 1 reads as "nothing to check".
    _trigger_words falls back to the canonical phrase, so it counts.

    Built synthetically because no rule in grammar.RULES currently targets a
    one-word canonical. That is luck, not design: the hole is closed before
    the next rule walks into it."""
    one_word = gr.Parse("builtin.copy", {}, 1.0, (0, 1), "rule")     # canonical "copy"
    assert one_word.alias == "", "premise: rule parses carry no alias"
    assert resolver._trigger_words(one_word) == 1, "a one-word command must count as one word"
    assert resolver._parse_is_literal(one_word, ["copy"]) is False

    two_word = gr.Parse("builtin.next_tab", {}, 1.0, (0, 2), "rule")
    assert resolver._trigger_words(two_word) == 2
    # The chain takes the WEAKEST member, so a chain holding both is caught.
    assert min(resolver._trigger_words(p) for p in (two_word, one_word)) < rs.MIN_COMMAND_WORDS


def test_the_chain_minimum_is_taken_over_every_member_not_just_aliased_ones():
    """Pins the shape of the fix, so a future edit cannot quietly reinstate
    the `if p.alias` filter that caused it."""
    src = ast.parse(Path(rs.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(src)
              if isinstance(n, ast.FunctionDef) and n.name == "tier_grammar")
    gens = [n.args[0] for n in ast.walk(fn)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "min"
            and n.args and isinstance(n.args[0], ast.GeneratorExp)
            and ast.unparse(n.args[0].elt) == "self._trigger_words(p)"]
    assert len(gens) == 1, ast.unparse(fn)[-600:]
    gen = gens[0]
    assert len(gen.generators) == 1 and ast.unparse(gen.generators[0].iter) == "chain"
    assert not gen.generators[0].ifs, \
        "the chain minimum is filtered again -- an unfiltered member contributes 0 and " \
        "rule 1 reads 0 as 'not alias-driven, nothing to check'"


def test_an_ordinary_chain_still_runs(resolver):
    """The counter-test. Rule 1 is not allowed to eat legitimate chains --
    both members here are two-word forms the user actually spoke."""
    # Browsers are now focused-app commands (265), so test this chain where
    # its first member is deliberately eligible.
    from samsara.command_scope import MatchContext
    res = resolver.resolve("next tab and go ahead", context=MatchContext.for_app("brave.exe"))
    assert res.kind == rs.RESOLVED and res.blocked is None
    assert [p.canonical_id for p in res.chain] == ["builtin.next_tab", "ask_ollama.yes"]
    assert res.alias_words >= rs.MIN_COMMAND_WORDS and res.literal is True


# ---------------------------------------------------------------------------
# 4. The fixture measures the catalog that exists
# ---------------------------------------------------------------------------

def test_the_fixture_covers_exactly_the_live_catalog(records):
    """Ten commands were measured at 0 lines because the fixture predated
    them. A command absent from the corpus is not a command that scores
    badly -- it is a command nobody measured, and it flattered the average."""
    ids = {json.loads(line)["id"] for line in FIXTURE.read_text(encoding="utf-8").splitlines()}
    live = {r["canonical_id"] for r in records}
    assert ids == live, {"missing from fixture": sorted(live - ids),
                         "stale in fixture": sorted(ids - live)}


def test_both_coverage_numbers_are_published_and_floored():
    """Queue 127 publishes coverage over the full corpus AND over the
    non-filler subset. Neither replaces the other: the full number is what
    queue 93's floors are denominated in, the non-filler number is the one a
    decision about turning the rules on should be read from. Deleting either
    floor deletes half the picture."""
    spec = importlib.util.spec_from_file_location(
        "eval_mod", REPO / "tests" / "test_intent_grammar_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("COVERAGE_FLOOR", "EXECUTABLE_FLOOR",
                 "NONFILLER_COVERAGE_FLOOR", "NONFILLER_EXECUTABLE_FLOOR"):
        assert isinstance(getattr(mod, name), float), name
    # The non-filler corpus is the harder one, so its executable floor must
    # sit below the full-corpus floor. If they ever converge, the split has
    # stopped measuring anything.
    assert mod.NONFILLER_EXECUTABLE_FLOOR < mod.EXECUTABLE_FLOOR


# ---------------------------------------------------------------------------
# 5. It is still an observer. This is the section that must never go green
#    by accident.
# ---------------------------------------------------------------------------

#: Modules that can make something happen. If any of these ever appears in
#: the import closure of samsara.intent, the gate has stopped being a gate
#: that only watches.
FORBIDDEN_IMPORTS = (
    "dictation", "samsara.command_executor", "samsara.execution_policy",
    "samsara.text_injector", "samsara.plugin_commands", "plugins",
    "keyboard", "pyautogui", "pynput",
)


class TestTheGateStillRunsNothing:

    def test_the_gate_adds_no_module_that_can_act(self):
        """Measured in a clean interpreter, as a DELTA against `import
        samsara` alone. The baseline matters: samsara/__init__.py imports
        CommandExecutor at package level, so anything under samsara.* drags
        the executor and pynput in whether it wants them or not. That is the
        package root's doing, not the gate's. What this asserts is the thing
        the gate controls -- importing resolve, grammar and shadow adds
        nothing beyond the baseline that could make something happen.

        The catalog LOAD (load_records) does reach the plugin registry; that
        is reading metadata, it happens inside a function, and it is not part
        of importing the gate."""
        code = (
            "import sys, json;"
            "import samsara;"
            "base = set(sys.modules);"
            "import samsara.intent.resolve, samsara.intent.shadow, samsara.intent.grammar;"
            "print(json.dumps(sorted(set(sys.modules) - base)))"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        added = set(json.loads(out.stdout.strip().splitlines()[-1]))
        hits = sorted(m for m in added
                      if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_IMPORTS))
        assert not hits, f"samsara.intent now imports something that can act: {hits}"

    def test_the_only_app_call_site_ignores_what_the_gate_decided(self):
        """dictation.py hands the gate a COPY of the text and the outcome
        kind, strictly after dispatch, as a bare statement. Nothing branches
        on the return value, so no decision of the gate's can change what the
        app did with the utterance."""
        tree = ast.parse((REPO / "dictation.py").read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_intent_shadow_observe")
        nested = {id(n) for d in ast.walk(fn) if isinstance(d, ast.FunctionDef) and d is not fn
                  for n in ast.walk(d)}
        own_returns = [n for n in ast.walk(fn)
                       if isinstance(n, ast.Return) and id(n) not in nested]
        assert own_returns and all(n.value is None for n in own_returns), \
            "_intent_shadow_observe now returns something a caller could act on"
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) == "_intent_shadow_observe"]
        assert len(calls) == 1, f"expected exactly one call site, found {len(calls)}"
        used = [n for n in ast.walk(tree)
                if isinstance(n, ast.Expr) and n.value in calls]
        assert len(used) == 1, "the call site's value is being consumed by something"

    def test_only_dictation_outcomes_are_even_looked_at(self):
        """A command outcome is never observed at all, so the gate cannot
        form a second opinion about anything the app already ran."""
        assert set(sh.OBSERVED_OUTCOMES) == {"dictate_staged", "dictate_injected"}
        shadow = sh.IntentShadow(lambda: pytest.fail("resolver built for a command outcome"),
                                 lambda: {}, spawn=lambda *a, **k: pytest.fail("worker spawned"))
        assert shadow.observe("delete everything", "command_executed") is False
        assert shadow.observed == 0

    def test_a_decision_to_execute_writes_a_line_and_does_nothing_else(self, tmp_path, resolver):
        """The end-to-end shape: give the worker an utterance that the gate
        WOULD run, on the real resolver, and check that the total effect is
        one JSONL line. No spawn, no executor, no injected text."""
        config = {"intent": {"shadow_dir": str(tmp_path)}}
        shadow = sh.IntentShadow(lambda: resolver, lambda: config,
                                 spawn=lambda *a, **k: pytest.fail("worker spawned"))
        entry = shadow.record("open memos", "injected", datetime(2026, 9, 16, 10, 0), None)
        assert entry["would"] == "command:quick_memo.open_memos"
        assert entry["literal"] is True and entry["rules_version"] == rs.EXECUTION_RULES_VERSION
        written = sorted(p.name for p in tmp_path.rglob("*") if p.is_file())
        assert written == ["intent-2026-09-16.jsonl"], written
        rows = [json.loads(line) for line in
                (tmp_path / "intent-2026-09-16.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1 and rows[0]["would"] == entry["would"]

    def test_the_gate_never_suppresses_the_dictation_it_watched(self, tmp_path, resolver):
        """The utterance is already staged or injected by the time the gate
        sees it -- `delivery` is the receipt. record() returns the row it
        wrote and has no channel back to the text."""
        config = {"intent": {"shadow_dir": str(tmp_path)}}
        shadow = sh.IntentShadow(lambda: resolver, lambda: config,
                                 spawn=lambda *a, **k: pytest.fail("worker spawned"))
        for text in ("open memos", "go ahead", "go page downwards", "copy"):
            entry = shadow.record(text, "staged", datetime(2026, 9, 16, 10, 0), None)
            assert entry["delivery"] == "staged"
            assert entry["text"] == text, "the text is recorded verbatim, never rewritten"
        # And the module calls nothing that could act. Checked on the AST
        # rather than the text, because the prose necessarily says words like
        # "dictate_injected" -- that is the name of an outcome it reads, not
        # something it does.
        tree = ast.parse(Path(sh.__file__).read_text(encoding="utf-8"))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called.add(getattr(node.func, "attr", None) or getattr(node.func, "id", None))
        acting = {"execute", "execute_canonical", "execute_command", "run_command", "dispatch",
                  "dispatch_utterance", "inject", "inject_text", "type_text", "send_keys",
                  "press", "authorize", "ava_menu"}
        assert not (called & acting), sorted(called & acting)


# ---------------------------------------------------------------------------
# 6. The replay still runs
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shadow_report():
    spec = importlib.util.spec_from_file_location("shadow_report", REPO / "tools" / "shadow_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_replay_reports_the_literal_split(shadow_report):
    """The reason `literal` is recorded at all: something has to read it
    back, or it is not a measurement. Two rows that both cost zero penalty,
    one of them a form the catalog registers and one of them not."""
    entries = [
        {"v": 2, "ts": "2026-09-16T10:00:00", "text": "open memos", "delivery": "staged",
         "would": "command:quick_memo.open_memos", "tier": "exact", "literal": True,
         "rules_version": 2, "elapsed_us": 900, "t12_us": 400},
        {"v": 2, "ts": "2026-09-16T10:01:00", "text": "go page downwards", "delivery": "staged",
         "would": "command:scroll.page_down", "tier": "grammar", "literal": False,
         "rules_version": 2, "elapsed_us": 900, "t12_us": 400},
    ]
    out = shadow_report.build_report(entries, 0, ["synthetic"])
    assert "literal=false): 1 of 2" in out
    assert "non-literal via tier grammar" in out


def test_the_replay_still_runs_over_the_real_log(shadow_report):
    """Whatever is in ~/.samsara/shadow right now. v1 rows have no `literal`
    field at all and the report must degrade to saying so rather than
    crashing -- a week of collected data is not re-collectable."""
    files = shadow_report.log_files(SHADOW_LOG_DIR, None) if SHADOW_LOG_DIR.is_dir() else []
    if not files:
        pytest.skip("no shadow log on this machine")
    entries, bad = shadow_report.read_entries(files)
    out = shadow_report.build_report(entries, bad, files)
    assert "Would have done" in out and "Execution rules" in out
    assert "literal" in out


def test_replaying_the_real_log_through_the_repaired_rules(resolver):
    """Re-DECIDE the logged utterances rather than reading back what the old
    gate decided, since 98% of the rows carry rules_version None. This is the
    number the owner's decision is actually about."""
    files = shadow_report_files()
    if not files:
        pytest.skip("no shadow log on this machine")
    kinds, literal = Counter(), Counter()
    total = 0
    for path in files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                text = json.loads(line)["text"]
            except Exception:
                continue
            total += 1
            res = resolver.resolve(text)
            kinds[res.kind] += 1
            if res.kind == rs.RESOLVED:
                literal["literal" if res.literal else "not_literal"] += 1
    assert total > 0
    # Real dictation, so the overwhelming majority must stay dictation. This
    # is the safety property the whole of queue 93 exists for.
    assert kinds[rs.DICTATION] / total > 0.9, dict(kinds)
    assert kinds[rs.RESOLVED] / total < 0.01, dict(kinds)


def shadow_report_files():
    return sorted(SHADOW_LOG_DIR.glob("intent-*.jsonl")) if SHADOW_LOG_DIR.is_dir() else []
