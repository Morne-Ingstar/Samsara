"""samsara/intent: normalisation, grammar, tier order (queue 25, Move B tier 2).

The resolver is built from the LIVE registry (the queue-15 path); nothing
here imports dictation.py, and the package is not wired into dispatch.
"""

import contextlib
import io
import json
import re
import time
from pathlib import Path

import pytest

from samsara.intent import grammar as gr
from samsara.intent import normalize as nz
from samsara.intent import resolve as rs

REPO = Path(__file__).resolve().parent.parent
APPS = ("chrome", "spotify", "discord", "notepad", "vs code", "firefox", "obsidian", "steam", "terminal")


@pytest.fixture(scope="module")
def records():
    with contextlib.redirect_stdout(io.StringIO()):
        return rs.load_records()


@pytest.fixture(scope="module")
def resolver(records):
    return rs.IntentResolver(records, app_names=APPS)


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Open Chrome.", ["open", "chrome"]),
    ("X-Ray, please!", ["xray", "please"]),
    ("today's note", ["todays", "note"]),
    ("  hey,   ava ", ["hey", "ava"]),
    ("", []),
])
def test_tokens_are_the_registry_view(text, expected):
    assert nz.tokens(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("can you please open chrome for me", "open chrome"),
    ("could you just scroll down please", "scroll down"),
    ("would you mind closing it", "closing it"),
    ("hey um open uh chrome", "open chrome"),
    ("go ahead and snap left thanks", "snap left"),
    ("i need you to take a memo right now", "take a memo"),
    ("okay so next tab now", "next tab"),
    ("scroll up quickly", "scroll up quickly"),    # "quickly" can be the command's own word
    ("so so please mute", "mute"),
    ("please", "please"),                          # only fillers: kept, never emptied
    ("um", "um"),
    ("open chrome", "open chrome"),                # nothing to strip
    ("remind me to buy milk please", "remind me to buy milk"),
])
def test_normalize_strips_fillers_and_politeness(text, expected):
    assert nz.normalize(text) == expected


def test_fillers_only_at_the_edges():
    assert nz.normalize("tell claude please be quick") == "tell claude please be quick"
    assert nz.normalize("i said hey to him") == "i said hey to him"


def test_filler_variants_try_the_ambiguous_trailing_phrase_both_ways():
    assert nz.filler_variants(["snap", "right", "now"]) == [["snap", "right"], ["snap"]]
    assert nz.filler_variants(["open", "chrome", "please"]) == [["open", "chrome"]]
    assert nz.AMBIGUOUS_TRAILING == ("right now",)


@pytest.mark.parametrize("group", [
    ("tab", "tap"), ("close", "clothes"), ("right", "write"), ("one", "won", "1"),
    ("to", "two", "too", "2"), ("for", "four", "4"), ("mute", "moot"), ("cube", "queue"),
    ("eight", "ate", "8"), ("new", "knew"), ("pause", "paws"),
])
def test_whisper_confusions_share_one_key(group):
    assert len({nz.confusion_key(t) for t in group}) == 1


def test_confusions_extend_the_catalog_table_rather_than_copy_it():
    from samsara.command_catalog import WHISPER_CONFUSIONS
    for group in WHISPER_CONFUSIONS:
        assert len({nz.confusion_key(t) for t in group}) == 1, group
    assert not set(map(tuple, WHISPER_CONFUSIONS)) & set(nz.EXTRA_CONFUSIONS)


def test_unrelated_tokens_keep_their_own_key():
    assert nz.confusion_key("chrome") == "chrome"
    assert nz.confusion_key("tab") != nz.confusion_key("close")
    assert nz.collapse_confusions(["clothes", "tap"]) == [nz.confusion_key("close"), nz.confusion_key("tab")]
    assert nz.normalize("please clothes the tap", collapse=True) == \
        " ".join([nz.confusion_key("close"), "the", nz.confusion_key("tab")])


@pytest.mark.parametrize("n, words", [(0, "zero"), (7, "seven"), (13, "thirteen"), (20, "twenty"),
                                      (37, "thirty seven"), (99, "ninety nine"), (100, "100")])
def test_number_to_words(n, words):
    assert nz.number_to_words(n) == words


@pytest.mark.parametrize("toks, value", [
    (["7"], 7), (["37"], 37), (["seven"], 7), (["twelve"], 12), (["thirty", "seven"], 37),
    (["number", "nine"], 9), (["no", "5"], 5), (["won"], 1), (["to"], 2), (["for"], 4),
    (["chrome"], None), ([], None), (["thirty", "thirty"], None), (["seven", "thirty"], None),
    (["one", "two", "three"], None), (["number"], None), (["3", "7"], None),
])
def test_parse_number_needs_the_whole_token_list(toks, value):
    assert nz.parse_number(toks) == value


@pytest.mark.parametrize("tok, value", [("first", 1), ("second", 2), ("sixth", 6), ("3rd", 3),
                                        ("seventh", None), ("tab", None)])
def test_parse_ordinal(tok, value):
    assert nz.parse_ordinal(tok) == value


@pytest.mark.parametrize("text, words, numerals", [
    ("heading 1", "heading one", "heading 1"),
    ("window thirty seven", "window thirty seven", "window 37"),
    ("tab 3 and 12", "tab three and twelve", "tab 3 and 12"),
    ("open chrome", "open chrome", "open chrome"),
])
def test_numerals_and_number_words_round_trip(text, words, numerals):
    assert nz.numerals_to_words(text) == words
    assert nz.words_to_numerals(words) == numerals


@pytest.mark.parametrize("tok, letter", [("bravo", "B"), ("b", "B"), ("xray", "X"), ("zulu", "Z"),
                                         ("bee", "B"), ("chrome", None), ("ab", None)])
def test_nato_letter(tok, letter):
    assert nz.nato_letter(tok) == letter


@pytest.mark.parametrize("text, letters", [
    ("bravo", ["B"]), ("bravo and charlie", ["B", "C"]), ("b c", ["B", "C"]),
    ("double you", ["W"]), ("x-ray", ["X"]), ("alpha then zulu", ["A", "Z"]),
    ("bravo chrome", None), ("and", None), ("", None),
])
def test_parse_letters_needs_the_whole_token_list(text, letters):
    assert nz.parse_letters(nz.tokens(text)) == letters


def test_normalize_is_pure():
    text = "Could you just MOVE chrome to the LEFT screen, please?"
    assert nz.normalize(text) == nz.normalize(text) == "move chrome to the left screen"


# ---------------------------------------------------------------------------
# Reuse, not a fourth parser
# ---------------------------------------------------------------------------

def test_slot_parsers_reuse_the_existing_tables():
    from plugins.commands import show_numbers, window_switcher, windows
    assert nz._word_to_num() is show_numbers._WORD_TO_NUM
    assert nz.ordinals() is windows._ORDINALS
    assert set(nz._phonetic_view().values()) == set(window_switcher.PHONETIC.values())


def test_intent_package_carries_no_number_or_letter_table_of_its_own():
    for path in (REPO / "samsara" / "intent").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        for word in ("twenty", "thirty", "foxtrot", "juliet", "november"):
            assert f'"{word}"' not in src and f"'{word}'" not in src, (path.name, word)


def test_app_names_use_the_shared_app_index_score(monkeypatch, records):
    import samsara.app_index as ai
    calls = []
    real = ai.score_name_match
    monkeypatch.setattr(ai, "score_name_match", lambda q, c: calls.append((q, c)) or real(q, c))
    g = gr.Grammar(records, ("chrome",), frozenset())
    assert g.parse(["focus", "chrome"]).args == {"app_name": "chrome"}
    assert ("chrome", "chrome") in calls


def test_not_wired_into_dispatch():
    targets = [REPO / "samsara" / "session_modes.py", REPO / "samsara" / "commands.py",
               REPO / "samsara" / "command_registry.py", *sorted((REPO / "plugins").rglob("*.py"))]
    for path in targets:
        assert "samsara.intent" not in path.read_text(encoding="utf-8", errors="replace"), path
    # 36: dictation.py may use the gate ONLY as the shadow observer -- both
    # imports live inside _intent_shadow_observe, nothing dispatches on it.
    import ast
    tree = ast.parse((REPO / "dictation.py").read_text(encoding="utf-8-sig"))
    users = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.ImportFrom) and (inner.module or "").startswith("samsara.intent"):
                    users.add((node.name, inner.module))
    top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
                 and "samsara.intent" in ast.dump(n)]
    assert top_level == []
    assert {name for name, _m in users} <= {"_intent_shadow_observe", "_resolver_factory"}
    assert {m for _n, m in users} == {"samsara.intent.shadow", "samsara.intent.resolve"}


# ---------------------------------------------------------------------------
# Catalog: the live registry path
# ---------------------------------------------------------------------------

def test_records_come_from_the_live_registry_and_match_the_fixture(records):
    fixture = {c["canonical_id"]: c for c in
               json.loads((REPO / "commands_catalog.json").read_text(encoding="utf-8"))["commands"]}
    live = {r["canonical_id"]: r for r in records}
    assert set(live) == set(fixture)
    for cid, rec in live.items():
        assert rec["aliases"] == fixture[cid]["aliases"], cid
        assert rec["args"] == fixture[cid]["args"], cid          # remainder-reading handlers re-inferred
        assert rec["risk"] == fixture[cid]["risk"], cid


def test_catalog_shape(records):
    assert len(records) >= 400
    assert sum(r["risk"] == "destructive" for r in records) >= 10
    assert any(r["whole_utterance"] for r in records)


# ---------------------------------------------------------------------------
# grammar
# ---------------------------------------------------------------------------

def _parse(resolver, text):
    return resolver.grammar.parse(nz.strip_fillers(nz.tokens(text)))


@pytest.mark.parametrize("text, cid, args", [
    ("move chrome to the left screen", "windows.send", {"app_name": "chrome", "monitor": "left screen"}),
    ("throw spotify to monitor 2", "windows.send", {"app_name": "spotify", "monitor": "monitor 2"}),
    ("drag discord onto the right monitor", "windows.send", {"app_name": "discord", "monitor": "right monitor"}),
    ("move to the second display notepad", "windows.send", {"app_name": "notepad", "monitor": "the second display"}),
    ("cursor to the main screen", "windows.cursor_to", {"monitor": "main screen"}),
    ("snap to the left side", "windows.snap", {"side": "left"}),
    ("click twelve", "show_numbers.click", {"label": 12}),
    ("press number 7", "show_numbers.click", {"label": 7}),
    ("window switch bravo", "window_switcher.window_switch", {"label": "B"}),
    ("tile windows alpha and charlie", "window_switcher.window_tile", {"labels": ["A", "C"]}),
    ("remind me to buy milk", "reminders.remind_me_to", {"text": "buy milk"}),
    ("launch firefox", "builtin.open_firefox", {}),
    ("switch to obsidian", "app_verbs.focus", {"app_name": "obsidian"}),
    ("close the tab", "builtin.close_tab", {}),
    ("show my tasks", "tasks.show_tasks", {}),
])
def test_grammar_parses_verb_object_and_slots(resolver, text, cid, args):
    p = _parse(resolver, text)
    assert p is not None and p.canonical_id == cid and p.args == args
    assert rs.GRAMMAR_RESOLVE <= p.confidence <= gr.GRAMMAR_BASE
    assert p.span == (0, len(nz.strip_fillers(nz.tokens(text))))


def test_the_other_one_is_an_unresolved_monitor(resolver):
    p = _parse(resolver, "put discord on the other one")
    assert p.canonical_id == "windows.send"
    assert p.args["monitor"] == gr.Unresolved("other one")


@pytest.mark.parametrize("text, cid", [
    ("turn the volume up a bit", "volume.volume_up"), ("make it louder", "volume.volume_up"),
    ("crank the sound down", "volume.volume_down"), ("scroll down a bit", "scroll.scroll_down_a_little"),
    ("scroll up quickly", "scroll.scroll_up_fast"), ("scroll down one page", "scroll.page_down"),
    ("scroll to the top", "builtin.scroll_to_top"), ("go to tab three", "builtin.tab_three"),
    ("switch to the second tab", "builtin.tab_two"), ("tab 1", "builtin.tab_one"),
])
def test_rules_cover_families_that_are_not_alias_shaped(resolver, text, cid):
    p = _parse(resolver, text)
    assert p is not None and p.canonical_id == cid


def test_dictate_to_claude_rule_keeps_the_message(resolver):
    p = _parse(resolver, "send a message to claude saying the build is green")
    assert (p.canonical_id, p.args) == ("message_claude.claude_message_prepare", {"text": "the build is green"})


def test_rules_reject_mixed_directions_and_unknown_words(resolver):
    g = resolver.grammar
    assert gr.rule_volume(g, ["turn", "it", "up", "down"]) is None
    assert gr.rule_scroll(g, ["scroll", "down", "banana"]) is None
    assert gr.rule_numbered_tab(g, ["tab", "forty"]) is None      # no builtin.tab_forty


def test_synonym_and_confusion_cost_confidence(resolver):
    exactish = _parse(resolver, "close the tab")
    synonym = _parse(resolver, "shut the tab")
    lookalike = _parse(resolver, "clothes the tab")
    assert exactish.word_penalty == 0
    assert synonym.canonical_id == lookalike.canonical_id == "builtin.close_tab"
    assert exactish.confidence > synonym.confidence > lookalike.confidence
    assert synonym.word_penalty == gr.P_SYNONYM and lookalike.word_penalty == gr.P_CONFUSION


def test_chaining_needs_every_part_to_parse(resolver):
    chain = resolver.grammar.parse_chain(nz.tokens("next tab and then scroll down"))
    assert [p.canonical_id for p in chain] == ["builtin.next_tab", "scroll.scroll_down"]
    assert chain[0].span == (0, 2) and chain[1].span == (4, 6)
    assert resolver.grammar.parse_chain(nz.tokens("next tab and bake a cake")) is None
    three = resolver.grammar.parse_chain(nz.tokens("mute then next tab then scroll up"))
    assert [p.canonical_id for p in three] == ["music.mute", "builtin.next_tab", "scroll.scroll_up"]


def test_an_and_inside_one_command_is_not_a_split(resolver):
    chain = resolver.grammar.parse_chain(nz.tokens("tile windows bravo and charlie"))
    assert len(chain) == 1 and chain[0].args == {"labels": ["B", "C"]}


def test_unknown_app_is_heard_but_not_confident(resolver):
    p = _parse(resolver, "open the purple elephant")
    assert p.canonical_id == "app_verbs.open" and p.confidence < rs.GRAMMAR_RESOLVE


def test_whole_utterance_words_are_not_grammar_templates(resolver):
    assert not [t for t in resolver.grammar.templates if " ".join(t.alias) in resolver.reserved]


def test_every_verb_class_covers_real_catalog_verbs(records):
    first_words = {a.split()[0] for r in records for a in r["aliases"]}
    for vc in gr.VERB_CLASSES:
        assert vc.verbs & first_words, vc.name
        assert set(vc.verbs) <= set(vc.synonyms), vc.name


# ---------------------------------------------------------------------------
# resolve: tier order, thresholds, safety
# ---------------------------------------------------------------------------

def test_tier_order_and_thresholds_are_constants():
    assert rs.TIERS == ("exact", "grammar", "similarity")
    assert 0 < rs.SIMILARITY_SUGGEST < rs.SIMILARITY_RESOLVE <= 1.0
    assert 0 < rs.GRAMMAR_RESOLVE < gr.GRAMMAR_BASE < 1.0
    assert rs.SIMILARITY_MAX_EXTRA == 1 and rs.SUGGESTION_LIMIT == 3
    assert rs.LATENCY_BUDGET_MS == 30.0


@pytest.mark.parametrize("text, tier, cid, kind", [
    ("open chrome", "exact", "builtin.open_chrome", rs.RESOLVED),
    ("Please take a screenshot.", "exact", "builtin.take_a_screenshot", rs.RESOLVED),
    # Queue 93 rule 2: "launch" is a SYNONYM of "open", so this is not an
    # exact match and may only suggest. The tier order is unchanged -- the
    # grammar still names the right command -- but naming is not executing.
    ("can you launch spotify for me", "grammar", "builtin.open_spotify", rs.SUGGEST),
    # Queue 93 rule 2: tier 3 is order-free token overlap over confusion
    # keys. Fuzzy by construction, so it can never execute.
    ("tab next", "similarity", "builtin.next_tab", rs.SUGGEST),
])
def test_tiers_in_order(resolver, text, tier, cid, kind):
    res = resolver.resolve(text)
    assert (res.kind, res.tier, res.canonical_id) == (kind, tier, cid)
    if tier == "exact":
        assert res.confidence == 1.0
    if kind == rs.SUGGEST:
        assert res.blocked == rs.BLOCK_INEXACT
        assert cid in res.suggestions


def test_exact_beats_grammar_even_when_grammar_would_parse(resolver):
    assert resolver.resolve("go to window bravo").canonical_id == "window_switcher.window_switch"
    assert resolver.resolve("snap right now").canonical_id == "builtin.snap_right"


def test_whole_utterance_control_words_only_match_as_heard(resolver):
    assert resolver.resolve("scratch that").canonical_id == "builtin.scratch_that"
    assert resolver.resolve("Scratch that.").kind == rs.RESOLVED
    assert resolver.resolve("please scratch that").canonical_id != "builtin.scratch_that"


def test_destructive_never_resolves_through_a_lookalike_synonym_or_similarity(resolver):
    assert resolver.resolve("close tab").kind == rs.RESOLVED
    for text in ("clothes tab", "shut tab", "tab close"):
        res = resolver.resolve(text)
        assert res.kind == rs.SUGGEST and res.canonical_id == "builtin.close_tab", text
        assert "builtin.close_tab" in res.suggestions


def test_destructive_with_a_text_slot_still_resolves_when_said_plainly(resolver):
    res = resolver.resolve("delete layout work setup")
    assert (res.kind, res.canonical_id, res.args) == (rs.RESOLVED, "windows.delete_layout", {"name": "work setup"})


def test_required_argument_missing_is_a_suggestion(resolver):
    res = resolver.resolve("window switch")
    assert res.kind == rs.RESOLVED and res.tier == "exact"          # the alias itself, as the registry has it
    # "please" is a filler, so this is the one-word utterance "focus", of the
    # one-word command app_verbs.focus. Queue 93 rule 1: it is dictation, and
    # says so -- twice over, which is the point of the rule.
    res = resolver.resolve("please focus")
    assert res.kind == rs.DICTATION
    assert res.blocked in (rs.BLOCK_ONE_WORD_UTTERANCE, rs.BLOCK_ONE_WORD_COMMAND)
    res = resolver.resolve("shift window")                          # grammar: window_move without a label
    assert res.kind == rs.SUGGEST and res.canonical_id == "window_switcher.window_move"


@pytest.mark.parametrize("text", [
    "yesterday i told my sister we should close tab before dinner",
    "honestly the whole point of the meeting was to open chrome and nobody did",
    "my grandmother never understood why people would scroll down on a sunday",
    "the quick brown fox jumps over the lazy dog",
])
def test_sentences_are_dictation(resolver, text):
    assert resolver.resolve(text).kind != rs.RESOLVED


def test_grey_zone_is_a_did_you_mean(resolver):
    res = resolver.resolve("open the purple elephant")
    assert res.kind == rs.SUGGEST and res.suggestions[0] == "app_verbs.open"
    assert len(res.suggestions) <= rs.SUGGESTION_LIMIT


def test_empty_and_noise_are_dictation(resolver):
    assert resolver.resolve("").kind == rs.DICTATION
    assert resolver.resolve("purple banana glimmer octopus velvet").kind == rs.DICTATION


def test_chain_resolution_reports_every_part(resolver):
    res = resolver.resolve("next tab and then close tab")
    assert res.kind == rs.RESOLVED and [p.canonical_id for p in res.chain] == ["builtin.next_tab", "builtin.close_tab"]


def test_module_level_resolve_accepts_a_resolver(resolver):
    assert rs.resolve("open chrome", resolver).canonical_id == "builtin.open_chrome"


def test_tiers_one_and_two_decide_within_budget_on_the_full_catalog(resolver):
    samples = ["open chrome", "can you please move spotify to the left screen for me",
               "scroll down a little bit please", "window switch bravo and then close tab",
               "yesterday i told my sister we should close tab before dinner",
               "tell claude that the build is green and the tests pass", "purple banana glimmer"] * 20
    resolver.resolve("warm up")
    worst = 0.0
    for text in samples:
        t0 = time.perf_counter()
        res = resolver.resolve(text)
        worst = max(worst, res.t12_ms)
        assert (time.perf_counter() - t0) * 1000 >= res.t12_ms - 1e-6
    assert worst < rs.LATENCY_BUDGET_MS, worst


def test_intent_sources_are_ascii():
    for path in [*(REPO / "samsara" / "intent").glob("*.py"), REPO / "tools" / "gen_intent_eval.py"]:
        assert not re.search(r"[^\x00-\x7f]", path.read_text(encoding="utf-8")), path
