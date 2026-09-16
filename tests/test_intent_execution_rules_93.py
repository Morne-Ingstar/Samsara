"""Queue 93: the two execution rules the Move A tribunal's HALT asked for.

The tiers in samsara/intent/resolve.py decide WHICH command an utterance
looks like. These rules decide whether looking like it may RUN it:

  rule 1  a one-word command never executes, and a one-word utterance never
          executes -- it is the user's text. Blocked -> dictation.
  rule 2  only a literal match executes: tier 1, or a tier-2 parse whose
          command words carry no stem/confusion/synonym penalty at all.
          Blocked -> suggest.

The escape hatch (intent.command_prefix, empty = off) waives rule 1 and only
rule 1: a spoken prefix says "this is a command", never "trust a homophone".

Nothing here imports dictation.py, and the package is still not wired into
dispatch -- these rules are what makes wiring it in arguable later.
"""

import contextlib
import io

import pytest

from samsara.intent import resolve as rs

APPS = ("chrome", "spotify", "discord", "notepad", "vs code", "firefox", "obsidian", "steam", "terminal")
PREFIX = "samsara"


@pytest.fixture(scope="module")
def records():
    with contextlib.redirect_stdout(io.StringIO()):
        return rs.load_records()


@pytest.fixture(scope="module")
def resolver(records):
    return rs.IntentResolver(records, app_names=APPS)


# ---------------------------------------------------------------------------
# Rule 1: a single word is text
# ---------------------------------------------------------------------------

#: The six the brief names. "To, um..." is the utterance that executed
#: window_cube.two on 2026-09-14 and convened the tribunal; the rest are the
#: words a user with no keyboard has to be able to type by saying them.
@pytest.mark.parametrize("text", ["To, um...", "to", "two", "copy", "yes", "Claude"])
def test_a_single_word_is_dictation_whatever_the_confidence(resolver, text):
    res = resolver.resolve(text)
    assert res.kind == rs.DICTATION, (text, res.canonical_id, res.tier, res.confidence)
    assert res.canonical_id is None
    assert res.blocked in (rs.BLOCK_ONE_WORD_UTTERANCE, rs.BLOCK_ONE_WORD_COMMAND), text


def test_the_demoted_command_is_still_recorded(resolver):
    """Dictation, but the log can still count what the gate would have run --
    otherwise a week of shadow data cannot say what rule 1 cost."""
    res = resolver.resolve("copy")
    assert res.kind == rs.DICTATION and res.canonical_id is None
    assert "builtin.copy" in res.suggestions
    assert res.tier == "exact"                      # which tier matched is still on the row


def test_a_suggestion_never_claims_a_single_word_either(resolver):
    """The Move A auditor's "~8% silent data loss": a `suggest` that claims
    the utterance loses the user's word as surely as an execution would.
    Rule 1 applies to suggestions too, so a one-word suggest cannot exist."""
    for text in ("Claude.", "Check.", "and", "That."):
        res = resolver.resolve(text)
        assert res.kind == rs.DICTATION, (text, res.kind, res.canonical_id)


def test_a_one_word_command_does_not_execute_through_a_longer_utterance(resolver):
    """Reaching windows.bring through its ONE-WORD alias "get" in "Get rid of
    the demo." is a single word triggering a command. Logged as executing on
    2026-09-15. This is rule 1 doing exactly what it was built for, and it is
    unchanged by queue 127: the evidence is the form the user spoke."""
    res = resolver.resolve("Get rid of the demo.")
    assert res.kind != rs.RESOLVED
    assert res.blocked == rs.BLOCK_ONE_WORD_COMMAND


# ---------------------------------------------------------------------------
# Rule 2: only a literal match executes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("heard, meant, cid", [
    ("snap write", "snap right", "builtin.snap_right"),     # right/write
    ("knew tab", "new tab", "builtin.new_tab"),             # new/knew
])
def test_a_homophone_of_a_two_word_command_suggests_but_never_executes(resolver, heard, meant, cid):
    """Sounds exactly like the canonical form, spelled differently. The gate
    still names it -- that is the grammar working -- but a confusion-key
    substitution is not an exact match, so it may not run."""
    hit = resolver.resolve(meant)
    assert (hit.kind, hit.canonical_id, hit.tier) == (rs.RESOLVED, cid, "exact")
    assert hit.word_penalty == 0.0

    res = resolver.resolve(heard)
    assert res.kind == rs.SUGGEST, (heard, res.canonical_id)
    assert res.canonical_id == cid and cid in res.suggestions
    assert res.blocked == rs.BLOCK_INEXACT
    assert res.word_penalty > 0.0


def test_a_synonym_is_not_an_exact_match(resolver):
    """"launch" is a synonym of "open", not the word the registry holds."""
    res = resolver.resolve("can you launch spotify for me")
    assert res.kind == rs.SUGGEST and res.canonical_id == "builtin.open_spotify"
    assert res.blocked == rs.BLOCK_INEXACT


def test_tier_three_can_never_execute(resolver):
    """Tier 3 is order-free token overlap over confusion keys: fuzzy by
    construction. It had one execution in the shadow log ("Could you copy
    that, please?" -> builtin.copy at confidence 1.0)."""
    res = resolver.resolve("Could you copy that, please?")
    assert res.kind != rs.RESOLVED
    assert all(r.tier != "similarity" for r in
               [resolver.resolve(t) for t in ("tab next", "Check.", "the light weight?")]
               if r.kind == rs.RESOLVED)


# ---------------------------------------------------------------------------
# What must still work
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, cid", [
    ("volume up", "volume.volume_up"),
    ("switch window", "builtin.switch_window"),
    ("close tab", "builtin.close_tab"),
    ("show numbers", "show_numbers.show_numbers"),
    ("open chrome", "builtin.open_chrome"),
    ("cube copy", "window_cube.cube_copy"),
    ("scroll down", "scroll.scroll_down"),
])
def test_an_exact_two_word_form_still_executes(resolver, text, cid):
    res = resolver.resolve(text)
    assert (res.kind, res.canonical_id) == (rs.RESOLVED, cid)
    assert res.confidence == 1.0 and res.tier == "exact"
    assert res.blocked is None and res.word_penalty == 0.0


def test_exact_still_beats_grammar_and_fillers_do_not_shrink_the_utterance(resolver):
    """Rule 1 counts the words of the READING THAT MATCHED, not of the most
    stripped one: "right now" is a filler, so counting the most-stripped
    reading would call "snap right now" a one-word utterance."""
    assert resolver.resolve("snap right now").canonical_id == "builtin.snap_right"
    assert resolver.resolve("go to window bravo").canonical_id == "window_switcher.window_switch"


# ---------------------------------------------------------------------------
# The escape hatch
# ---------------------------------------------------------------------------

def test_the_prefix_runs_a_one_word_command(resolver):
    assert resolver.resolve("copy", prefix=PREFIX).kind == rs.DICTATION      # no prefix spoken
    res = resolver.resolve(f"{PREFIX} copy", prefix=PREFIX)
    assert (res.kind, res.canonical_id) == (rs.RESOLVED, "builtin.copy")
    assert res.forced is True and res.blocked is None


def test_the_prefix_is_inert_when_it_is_not_configured(resolver):
    """Empty is the default and means OFF. No word is hard-coded: the owner
    picks one from shadow data, so until then the hatch cannot fire."""
    for prefix in ("", None):
        res = resolver.resolve(f"{PREFIX} copy", prefix=prefix)
        assert res.forced is False
        assert res.kind != rs.RESOLVED
    assert resolver.command_prefix == ""


def test_the_prefix_does_not_waive_rule_two(resolver):
    """A spoken prefix says "this is a command". It does not say "promote a
    homophone" -- that is the failure the tribunal was convened over."""
    res = resolver.resolve(f"{PREFIX} snap write", prefix=PREFIX)
    assert res.kind == rs.SUGGEST and res.blocked == rs.BLOCK_INEXACT
    assert res.forced is True


def test_the_prefix_alone_is_not_a_command(resolver):
    res = resolver.resolve(PREFIX, prefix=PREFIX)
    assert res.kind == rs.DICTATION and res.canonical_id is None


def test_the_prefix_comes_from_one_config_key():
    from samsara.config_schema import SETTINGS_SCHEMA
    from samsara.intent import shadow

    spec = SETTINGS_SCHEMA["intent.command_prefix"]
    assert spec["type"] == "str" and spec["default"] == "" and spec["tab"] == "advanced"
    assert shadow.command_prefix({}) == ""
    assert shadow.command_prefix({"intent": {}}) == ""
    assert shadow.command_prefix({"intent": {"command_prefix": "samsara"}}) == "samsara"


# ---------------------------------------------------------------------------
# The rules are versioned, and the log says which ones decided a row
# ---------------------------------------------------------------------------

def test_every_resolution_is_stamped_with_the_rules_that_made_it(resolver):
    for text in ("copy", "volume up", "snap write", "the quick brown fox"):
        assert resolver.resolve(text).rules_version == rs.EXECUTION_RULES_VERSION
    assert rs.EXECUTION_RULES_VERSION >= 2


def test_the_shadow_row_carries_the_rules_version_and_the_block(resolver):
    from datetime import datetime

    from samsara.intent import shadow

    res = resolver.resolve("copy")
    entry = shadow.build_entry("copy", "staged", res, elapsed_us=12, app=None, when=datetime.now())
    assert entry["v"] == shadow.SCHEMA_VERSION == 2
    assert entry["rules_version"] == rs.EXECUTION_RULES_VERSION
    assert entry["blocked"] == res.blocked and entry["blocked"] is not None
    assert entry["would"] == "dictate"              # blocked says what it would have been
    assert entry["forced"] is False

    miss = shadow.build_entry("x", "staged", None, elapsed_us=1, app=None, when=datetime.now())
    assert miss["rules_version"] is None and miss["blocked"] is None and miss["would"] == "miss"


def test_the_rules_are_one_chokepoint_not_a_tier_special_case(resolver):
    """block_reason is a public, testable predicate over a Resolution: there
    is one place that decides, and it does not care which tier produced the
    decision."""
    exact = rs.Resolution(rs.RESOLVED, "builtin.open_chrome", tier="exact")
    assert resolver.block_reason(exact, 2) is None
    assert resolver.block_reason(exact, 1) == rs.BLOCK_ONE_WORD_UTTERANCE
    assert resolver.block_reason(exact, 1, forced=True) is None

    fuzzy = rs.Resolution(rs.RESOLVED, "builtin.open_chrome", tier="grammar", word_penalty=0.05)
    assert resolver.block_reason(fuzzy, 5) == rs.BLOCK_INEXACT
    assert resolver.block_reason(fuzzy, 5, forced=True) == rs.BLOCK_INEXACT   # rule 2 is unconditional

    # Queue 127: built the way tier_grammar now builds it. builtin.copy's
    # only spoken form is the one-word "copy", so a chain member that reaches
    # it contributes 1 (_trigger_words falls back to the canonical phrase
    # when a parse carries no alias). Before 127 this field was left at 0 for
    # a rule-source member and rule 1 read 0 as "nothing to check" -- see
    # tests/test_intent_execution_rules_127.py.
    one_word_cmd = rs.Resolution(rs.RESOLVED, "builtin.copy", tier="grammar", alias_words=1)
    assert resolver.block_reason(one_word_cmd, 5) == rs.BLOCK_ONE_WORD_COMMAND
    one_word_alias = rs.Resolution(rs.RESOLVED, "ask_ollama.hey_ava", tier="grammar", alias_words=1)
    assert resolver.block_reason(one_word_alias, 5) == rs.BLOCK_ONE_WORD_COMMAND

    assert resolver.block_reason(rs.Resolution(rs.DICTATION), 1) is None
