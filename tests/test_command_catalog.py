"""samsara/command_catalog.py + tools/gen_command_catalog.py: the canonical
command table built from the live registry.

  * every commands.json phrase and every registered plugin phrase resolves
    to exactly one canonical_id -- except the collisions frozen in
    tests/command_catalog_known_collisions.txt (a new collision, or one that
    disappeared, fails until that file is updated on purpose)
  * no canonical_id without an alias; risk never empty; JSON validates
    against CATALOG_SCHEMA (jsonschema) and the module's own validator
  * generation is deterministic and the committed files are fresh
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samsara import command_catalog as cc  # noqa: E402
from tools import gen_command_catalog as gen  # noqa: E402

COLLISIONS_FILE = ROOT / "tests" / "command_catalog_known_collisions.txt"


@pytest.fixture(scope="module")
def registry():
    from tools.dump_command_metadata import build_executor
    executor = build_executor()
    specs = cc.build_catalog(executor)
    claims = cc.raw_phrase_claims(executor)
    return executor, specs, claims


@pytest.fixture(scope="module")
def specs(registry):
    return registry[1]


@pytest.fixture(scope="module")
def claims(registry):
    return registry[2]


def _known():
    """(collisions, orphans) frozen in tests/command_catalog_known_collisions.txt."""
    return cc.parse_collisions(COLLISIONS_FILE.read_text(encoding="utf-8"))


def _known_phrases():
    colls, orphaned = _known()
    return {p for p, _ids in colls} | {p for p, _ids in orphaned}


class TestResolution:
    def test_every_builtin_phrase_resolves_to_exactly_one_id(self, specs, claims):
        by_alias = {}
        for s in specs:
            for a in s.aliases:
                by_alias.setdefault(a, set()).add(s.canonical_id)
        known = _known_phrases()
        keys = json.loads((ROOT / "commands.json").read_text(encoding="utf-8"))["commands"]
        bad = {}
        for key in keys:
            phrase = cc.normalize_phrase(key)
            ids = by_alias.get(phrase, set())
            if len(ids) != 1 and phrase not in known:
                bad[phrase] = ids
        assert not bad, f"builtin phrases not resolving to exactly one id: {bad}"

    def test_every_plugin_phrase_resolves_to_exactly_one_id(self, specs, claims):
        by_alias = {}
        for s in specs:
            for a in s.aliases:
                by_alias.setdefault(a, set()).add(s.canonical_id)
        known = _known_phrases()
        bad = {}
        for phrase, ids in claims.items():
            resolved = by_alias.get(phrase, set())
            if len(resolved) != 1 and phrase not in known:
                bad[phrase] = (ids, resolved)
        assert not bad, f"phrases not resolving to exactly one id: {bad}"

    def test_collisions_are_frozen(self, specs, claims):
        """The phrases claimed by more than one command, and the phrases the
        registry drops because of those collisions, must equal the committed
        list -- visible, and changed only on purpose."""
        known_colls, known_orphans = _known()
        assert cc.collisions(claims) == known_colls
        assert cc.orphans(claims, specs) == known_orphans

    def test_orphans_really_resolve_to_nothing(self, specs):
        """Documents the registry behaviour the orphan list records."""
        live = {a for s in specs for a in s.aliases}
        _colls, known_orphans = _known()
        assert known_orphans, "the tree currently has orphaned aliases; see the frozen file"
        for phrase, _ids in known_orphans:
            assert phrase not in live, phrase

    def test_catalog_aliases_are_registry_normalised(self, specs):
        for s in specs:
            for a in s.aliases:
                assert a == cc.normalize_phrase(a) and a


class TestRecords:
    def test_no_canonical_id_without_alias(self, specs):
        assert all(s.aliases for s in specs)

    def test_canonical_ids_unique_and_stable_shape(self, specs):
        ids = [s.canonical_id for s in specs]
        assert len(ids) == len(set(ids))
        for s in specs:
            phrase = s.verb + ("_" + s.object if s.object else "")
            assert s.canonical_id == f"{s.plugin}.{phrase}"
            assert s.plugin != "plugin", "plugin stem must be the module name, not the registry's 'plugin' tag"

    def test_risk_never_empty_and_in_range(self, specs):
        assert all(s.risk in cc.RISKS for s in specs)
        assert {s.risk for s in specs} >= {"ui", "write", "destructive"}

    def test_arg_types_in_range(self, specs):
        for s in specs:
            for a in s.args:
                assert a.type in cc.ARG_TYPES

    def test_builtins_have_no_args_and_json_source(self, specs):
        for s in specs:
            if s.kind == "builtin":
                assert s.args == [] and s.source.startswith("commands.json:")

    def test_whole_utterance_flags_reserved_words(self, specs):
        reserved = cc.reserved_whole_utterances()
        flagged = {s.canonical_id for s in specs if s.whole_utterance}
        assert "builtin.scratch_that" in flagged
        for s in specs:
            phrase = " ".join([s.verb, *s.object.split("_")]).strip()
            assert s.whole_utterance == (phrase in reserved), s.canonical_id

    def test_known_specs_look_right(self, specs):
        by_id = {s.canonical_id: s for s in specs}
        send = by_id["windows.send"]
        assert {a.type for a in send.args} == {"app_name", "monitor"}
        assert "put" in send.aliases and send.source.startswith("plugins/commands/windows.py:")
        assert by_id["builtin.close_window"].risk == "destructive"
        assert by_id["builtin.switch_window"].risk == "ui"
        assert by_id["builtin.scratch_that"].whole_utterance is True


class TestHeuristics:
    @pytest.mark.parametrize("phrase,expected", [
        ("show numbers", "read"), ("list layouts", "read"), ("switch window", "ui"), ("snap left", "ui"),
        ("volume up", "ui"), ("type hello", "write"), ("paste", "write"), ("send chrome to tv", "write"),
        ("save layout", "write"), ("close window", "destructive"), ("delete file", "destructive"),
        ("kill process", "destructive"), ("quit app", "destructive"), ("shutdown", "destructive"),
        ("frobnicate", "ui"),
    ])
    def test_verb_heuristic(self, phrase, expected):
        assert cc.classify_risk("plugin", phrase, None, None) == expected

    def test_declared_risk_wins(self):
        assert cc.classify_risk("plugin", "close window", "safe", None) == "ui"
        assert cc.classify_risk("plugin", "show x", "destructive", None) == "destructive"

    def test_builtin_keys_decide(self):
        assert cc.classify_risk("builtin", "whatever", None, {"type": "hotkey", "keys": ["alt", "f4"]}) == "destructive"
        assert cc.classify_risk("builtin", "whatever", None, {"type": "press", "key": "enter"}) == "write"

    def test_undoable_guess(self):
        assert cc.guess_undoable("ui", "switch window") is True
        assert cc.guess_undoable("write", "type hello") is True
        assert cc.guess_undoable("write", "send it") is False
        assert cc.guess_undoable("destructive", "close window") is True
        assert cc.guess_undoable("destructive", "delete file") is False

    def test_near_collisions_detects_whisper_pairs(self):
        a = cc.CommandSpec("x.tab_one", "x", "tab", "one", aliases=["tab one"])
        b = cc.CommandSpec("y.tap_one", "y", "tap", "one", aliases=["tap one"])
        c = cc.CommandSpec("z.tab_won", "z", "tab", "won", aliases=["tab won"])
        found = cc.near_collisions([a, b, c])
        pairs = {(p, q) for p, q, _x, _y in found}
        assert ("tab one", "tap one") in pairs and ("tab one", "tab won") in pairs

    def test_normalisation_matches_registry(self):
        assert cc.normalize_phrase("  Close  Window! ") == "close window"
        assert cc.slug("close window") == "close_window"


class TestFiles:
    def test_json_validates_against_schema(self, specs):
        import jsonschema
        doc = cc.to_document(specs)
        jsonschema.validate(doc, cc.CATALOG_SCHEMA)
        assert cc.validate_catalog(doc) == []

    def test_validator_catches_problems(self):
        bad = {"version": 1, "commands": [{"canonical_id": "Bad Id", "plugin": "x", "verb": "v", "object": "",
               "args": [{"name": "n", "type": "nope", "required": True}], "aliases": [], "description": "",
               "risk": "", "undoable": "yes", "source": "s", "whole_utterance": False, "pack": "core", "kind": "plugin"}]}
        problems = cc.validate_catalog(bad)
        assert any("bad canonical_id" in p for p in problems)
        assert any("no alias" in p for p in problems) and any("risk" in p for p in problems)

    def test_generation_is_deterministic(self, registry):
        executor = registry[0]
        one = cc.dumps(cc.to_document(cc.build_catalog(executor)))
        two = cc.dumps(cc.to_document(cc.build_catalog(executor)))
        assert one == two
        assert cc.render_markdown(cc.build_catalog(executor)) == cc.render_markdown(cc.build_catalog(executor))

    def test_committed_catalog_is_fresh(self, specs):
        """commands_catalog.json and docs/COMMAND_CATALOG.md must match a
        fresh generation -- re-run tools/gen_command_catalog.py after any
        command change."""
        assert gen.JSON_PATH.read_text(encoding="utf-8") == cc.dumps(cc.to_document(specs))
        assert gen.MD_PATH.read_text(encoding="utf-8") == cc.render_markdown(specs)

    def test_check_mode_reports_fresh_in_a_fresh_interpreter(self):
        """--check as the owner runs it. In-process, conftest's registry
        save/clear/restore around tests changes which duplicate wins, so
        the real check is a fresh interpreter."""
        import subprocess
        out = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_command_catalog.py"), "--check"],
                             capture_output=True, text=True, timeout=80)
        assert out.returncode == 0, out.stdout + out.stderr[-500:]
        assert "up to date" in out.stdout

    def test_dump_tool_shares_the_loader(self):
        from tools import dump_command_metadata as dump
        assert callable(dump.build_executor)
