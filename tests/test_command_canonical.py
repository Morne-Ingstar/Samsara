"""Queue 45: the canonical-command views over samsara/command_catalog.py.

  * every phrase resolves to exactly one command, except the phrases 09a
    froze in tests/command_catalog_known_collisions.txt -- and those are
    exactly the rows resolution_rows() reports (nothing silently picked)
  * undo is the DECLARED `reversible` value or "unknown", never guessed
  * destructive follows the catalog risk; canonical forms carry the slots
  * the sound-alike detector finds Whisper look-alikes across commands
  * queue 93: every command whose canonical spoken phrase is ONE WORD is in
    tests/command_catalog_one_word_canonical.txt -- those can never execute
    (intent execution rule 1), so adding one is a deliberate act
  * none of this changes commands_catalog.json
Never imports dictation.py.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samsara import command_catalog as cc  # noqa: E402

KNOWN = ROOT / "tests" / "command_catalog_known_collisions.txt"
ONE_WORD = ROOT / "tests" / "command_catalog_one_word_canonical.txt"


@pytest.fixture(scope="module")
def registry():
    from tools.dump_command_metadata import build_executor
    executor = build_executor()
    return (cc.build_catalog(executor), cc.raw_phrase_claims(executor), cc.registry_metadata(executor))


def test_every_phrase_resolves_to_exactly_one_command_or_is_a_reported_collision(registry):
    specs, claims, _meta = registry
    rows = cc.resolution_rows(specs, claims)
    colls, orphans = cc.parse_collisions(KNOWN.read_text(encoding="utf-8"))
    frozen = {p for p, _ids in colls} | {p for p, _ids in orphans}
    assert {r["phrase"] for r in rows} == frozen
    for r in rows:
        assert r["status"] in ("zero", "many")
        if r["status"] == "zero":
            assert r["resolves_to"] == () and len(r["claimed_by"]) == 1
        else:
            assert len(r["claimed_by"]) > 1 or len(r["resolves_to"]) > 1


def test_resolution_rows_never_pick_a_winner():
    specs = [cc.CommandSpec("a.x", "a", "x", "", aliases=["x", "shared"]),
             cc.CommandSpec("b.y", "b", "y", "", aliases=["y", "shared"])]
    claims = {"x": {"a.x"}, "y": {"b.y"}, "shared": {"a.x", "b.y"}, "gone": {"c.gone"}}
    rows = {r["phrase"]: r for r in cc.resolution_rows(specs, claims)}
    assert set(rows) == {"shared", "gone"}
    assert rows["shared"]["resolves_to"] == ("a.x", "b.y") and rows["shared"]["status"] == "many"
    assert rows["gone"]["status"] == "zero" and rows["gone"]["resolves_to"] == ()


def test_undo_is_declared_never_guessed(registry):
    specs, _claims, meta = registry
    values = {s.canonical_id: cc.declared_undo(meta.get(s.canonical_id)) for s in specs}
    for s in specs:
        declared = meta.get(s.canonical_id, {}).get("reversible")
        expected = declared if isinstance(declared, bool) else "unknown"
        assert values[s.canonical_id] == expected, s.canonical_id
    assert "unknown" in values.values()
    assert cc.declared_undo(None) == "unknown"
    assert cc.declared_undo({"reversible": "unknown"}) == "unknown"
    assert cc.declared_undo({"reversible": True}) is True
    assert cc.declared_undo({"reversible": False}) is False


def test_destructive_follows_risk(registry):
    specs = registry[0]
    by_id = {s.canonical_id: s for s in specs}
    assert cc.is_destructive(by_id["builtin.close_window"]) is True
    assert cc.is_destructive(by_id["builtin.switch_window"]) is False
    assert all(cc.is_destructive(s) == (s.risk == "destructive") for s in specs)


def test_canonical_form_carries_slots(registry):
    by_id = {s.canonical_id: s for s in registry[0]}
    send = cc.canonical_form(by_id["windows.send"])
    assert send.startswith("send ") and "[app_name:app_name]" in send and "<monitor:monitor>" in send
    assert cc.canonical_form(by_id["builtin.close_window"]) == "close window"
    spec = cc.CommandSpec("x.set_volume", "x", "set", "volume", args=[cc.ArgSpec("level", "int", True)])
    assert cc.canonical_form(spec) == "set volume <level:int>"


def _frozen_one_word() -> set:
    return {line.strip() for line in ONE_WORD.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")}


def test_one_word_canonical_commands_are_the_frozen_inventory(registry):
    """Queue 93 rule 1: a command whose canonical spoken phrase is a single
    word never executes -- a single word is the user's text, and a user with
    no keyboard has to be able to say "copy", "yes" or "Claude" and have them
    typed. Each id here is reachable only through a two-or-more-word alias or
    the command prefix, and each wants a two-word canonical form.

    Frozen rather than counted: a NEW one-word command is a real decision
    (it ships unexecutable), so it should fail here and be added on purpose."""
    records = cc.to_document(registry[0])["commands"]
    found = {r["canonical_id"] for r in records
             if len(cc.canonical_phrase(r).split()) == 1}
    frozen = _frozen_one_word()
    assert found - frozen == set(), (
        "new one-word canonical command(s): they cannot execute under intent "
        "execution rule 1. Give them a two-word form, or add them to "
        f"{ONE_WORD.name} on purpose")
    assert frozen - found == set(), (
        f"{ONE_WORD.name} lists command(s) that no longer exist or are no "
        "longer one word")
    assert len(frozen) > 40                          # the pin is real, not vacuous


def test_the_resolver_reads_rule_one_off_the_same_canonical_phrase(registry):
    """The inventory and the live gate must not drift: the resolver's own
    word counts are cc.canonical_phrase, not a second opinion."""
    from samsara.intent.resolve import MIN_COMMAND_WORDS, IntentResolver

    records = cc.to_document(registry[0])["commands"]
    resolver = IntentResolver(records, reserved=frozenset())
    blocked = {cid for cid, n in resolver._canonical_words.items() if n < MIN_COMMAND_WORDS}
    assert blocked == _frozen_one_word()


def test_sound_alike_pairs_find_whisper_look_alikes():
    specs = [
        cc.CommandSpec("x.tab_one", "x", "tab", "one", aliases=["tab one"]),
        cc.CommandSpec("y.tap_won", "y", "tap", "won", aliases=["tap won"]),
        cc.CommandSpec("z.snap_left", "z", "snap", "left", aliases=["snap left", "snip left"]),
        cc.CommandSpec("w.snip_left", "w", "snip", "left", aliases=["snip left now"]),
        cc.CommandSpec("v.slap_left", "v", "slap", "left", aliases=["slap left"]),
    ]
    found = {(a, b, rule) for a, b, _x, _y, rule in cc.sound_alike_pairs(specs)}
    assert ("tab one", "tap won", "confusion") in found
    assert ("slap left", "snap left", "one_letter") in found
    # aliases of the same command are not a hazard
    assert not any({a, b} == {"snap left", "snip left"} for a, b, _r in found)


def test_sound_alike_pairs_on_live_registry_are_cross_command(registry):
    for a, b, ia, ib, rule in cc.sound_alike_pairs(registry[0]):
        assert a < b and ia != ib and rule in ("confusion", "one_letter")


def test_views_do_not_change_the_committed_catalog(registry):
    """The canonical view is read-only: commands_catalog.json is still exactly
    09a's generation."""
    specs = registry[0]
    assert (ROOT / "commands_catalog.json").read_text(encoding="utf-8") == cc.dumps(cc.to_document(specs))
