"""Tab-number slots must not swallow reversed browser commands (queue 433)."""

import contextlib
import io

import pytest

from samsara.command_scope import MatchContext
from samsara.intent import grammar as gr
from samsara.intent import normalize as nz
from samsara.intent import resolve as rs


@pytest.fixture(scope="module")
def records():
    with contextlib.redirect_stdout(io.StringIO()):
        return rs.load_records()


@pytest.fixture(scope="module")
def resolver(records):
    return rs.IntentResolver(records)


@pytest.fixture(scope="module")
def number_grammar(records):
    # Isolate the parameterized command from the numbered-tab builtin rules.
    record = next(r for r in records if r["canonical_id"] == "tab_finder.tab_number")
    return gr.Grammar([record])


@pytest.mark.parametrize("text, cid", [
    ("tab next", "builtin.next_tab"),
    ("tab close", "builtin.close_tab"),
])
@pytest.mark.parametrize("prefix", ["", "command"])
def test_reversed_tab_phrases_only_suggest(resolver, text, cid, prefix):
    spoken = f"{prefix} {text}" if prefix else text
    result = resolver.resolve(spoken, context=MatchContext.for_app("chrome.exe"), prefix=prefix)
    assert (result.kind, result.tier, result.canonical_id) == (rs.SUGGEST, "similarity", cid)
    assert cid in result.suggestions
    assert result.args == {}


@pytest.mark.parametrize("text, kind, tier", [
    ("close tab", rs.RESOLVED, "exact"),
    ("clothes tab", rs.SUGGEST, "grammar"),
    ("shut tab", rs.SUGGEST, "grammar"),
    ("tab close", rs.SUGGEST, "similarity"),
])
def test_destructive_tab_phrases_keep_the_safety_boundary(resolver, text, kind, tier):
    result = resolver.resolve(text, context=MatchContext.for_app("chrome.exe"))
    assert (result.kind, result.tier, result.canonical_id) == (kind, tier, "builtin.close_tab")
    if kind == rs.SUGGEST:
        assert "builtin.close_tab" in result.suggestions


@pytest.mark.parametrize("alias", ["find tab", "switch to tab"])
@pytest.mark.parametrize("name", ["next steps doc", "close reading", "next", "close"])
def test_explicit_tab_search_keeps_names_containing_command_words(resolver, alias, name):
    result = resolver.resolve(f"{alias} {name}", context=MatchContext.for_app("chrome.exe"))
    assert (result.kind, result.canonical_id, result.args) == (
        rs.RESOLVED, "tab_finder.find_tab", {"query": name},
    )


@pytest.mark.parametrize("alias", ["tab", "tab number"])
@pytest.mark.parametrize("number", range(1, 10))
def test_tab_number_slot_keeps_valid_numbers_as_voice_remainders(number_grammar, alias, number):
    result = number_grammar.parse(nz.tokens(f"{alias} {number}"))
    assert result is not None
    assert result.canonical_id == "tab_finder.tab_number"
    assert result.args == {"remainder": str(number)}


@pytest.mark.parametrize("alias", ["tab", "tab number"])
@pytest.mark.parametrize("remainder", ["0", "10", "zero", "ten", "next", "close"])
def test_tab_number_slot_rejects_out_of_range_and_command_words(number_grammar, alias, remainder):
    assert number_grammar.parse(nz.tokens(f"{alias} {remainder}")) is None


@pytest.mark.parametrize("alias", ["tab", "tab number"])
def test_tab_number_slot_accepts_a_spoken_number_word(number_grammar, alias):
    result = number_grammar.parse(nz.tokens(f"{alias} seven"))
    assert result is not None
    assert result.canonical_id == "tab_finder.tab_number"
    assert result.args == {"remainder": "seven"}
