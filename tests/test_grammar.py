"""Focused contract tests for the unwired top-level tier-2 grammar."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from samsara import grammar


ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "commands_catalog.json").read_text(encoding="utf-8"))
RECORDS = CATALOG["commands"]


def _record(canonical_id: str) -> dict:
    return next(record for record in RECORDS if record["canonical_id"] == canonical_id)


def _positive(record: dict) -> str:
    phrase = record["aliases"][0]
    args = record.get("args", [])
    if len(args) == 2:
        kinds = [arg["type"] for arg in args]
        if kinds == ["app_name", "monitor"]:
            return f"{phrase} obsidian to left screen"
        if kinds == ["nato_letter", "monitor"]:
            return f"{phrase} bravo to left screen"
    if not args:
        return phrase
    arg = args[0]
    kind = arg["type"]
    if kind == "int":
        value = "two into four" if arg["name"].endswith("s") else "twenty five"
    elif kind == "nato_letter":
        value = "bravo and charlie" if arg["name"].endswith("s") else "bravo"
    elif kind == "app_name":
        value = "obsidian"
    elif kind == "monitor":
        value = "left screen"
    elif kind == "side":
        value = "left"
    else:
        value = "sample value"
    return f"{phrase} {value}"


def _negative(record: dict) -> str:
    return f"i said {record['aliases'][0]}"


@pytest.fixture(autouse=True)
def _stub_live_window_resolution(monkeypatch):
    """Keep generated tests deterministic while still exercising the import hook."""
    calls = []

    def resolve(name):
        calls.append(name)
        return None

    monkeypatch.setattr("plugins.commands.app_verbs.resolve_window", resolve)
    return calls


def test_numbers_digits_words_compounds_and_ordinals():
    assert grammar.parse("click 2", CATALOG)[0][1] == {"label": 2}
    assert grammar.parse("click twenty five", CATALOG)[0][1] == {"label": 25}
    assert grammar.parse("click second", CATALOG)[0][1] == {"label": 2}
    tiny = {"commands": [{"canonical_id": "demo.pick", "aliases": ["pick"],
                           "args": [{"name": "n", "type": "int", "required": True}]}]}
    assert grammar.parse("pick twenty five", tiny)[0][1] == {"n": 25}


def test_nato_and_plain_letters_are_lowercase_and_shared():
    assert grammar.parse("window switch bravo", CATALOG)[0][1] == {"label": "b"}
    assert grammar.parse("window switch b", CATALOG)[0][1] == {"label": "b"}
    assert grammar.parse("cube copy two into four", CATALOG)[0][1] == {"numbers": [2, 4]}
    assert grammar.parse("switch window bravo", CATALOG)[0][1] == {"label": "b"}
    tiny = {"commands": [{"canonical_id": "demo.focus", "aliases": ["focus"],
                           "args": [{"name": "letter", "type": "nato_letter", "required": True}]}]}
    assert grammar.parse("focus bravo", tiny)[0][1] == {"letter": "b"}


def test_repetition_is_an_additional_canonical_argument():
    assert grammar.parse("volume down twice", CATALOG)[0][1] == {"repeat": 2}
    assert grammar.parse("volume up three times", CATALOG)[0][1] == {"repeat": 3}
    assert grammar.parse("volume down again", CATALOG)[0][1] == {"repeat": 2}


def test_app_resolver_is_reused_and_name_is_harmless_text(monkeypatch):
    calls = []
    monkeypatch.setattr("plugins.commands.app_verbs.resolve_window", lambda name: calls.append(name) or None)
    assert grammar.parse("open obsidian", CATALOG)[0][1] == {"app_name": "obsidian"}
    assert calls == ["obsidian"]


def test_chaining_is_ordered_and_prose_is_not_a_command():
    result = grammar.parse("open obsidian then snap left", CATALOG)
    assert [item[0] for item in result] == ["app_verbs.open", "windows.snap"]
    assert result[0][1] == {"app_name": "obsidian"}
    assert result[1][1] == {"side": "left"}
    assert result[0][2] == (0, 2)
    assert result[1][2] == (3, 5)
    assert grammar.parse("I told him to switch window", CATALOG) == []
    assert grammar.parse("open obsidian then maybe", CATALOG) == []


def test_every_argument_bearing_canonical_form_has_generated_positive_and_negative():
    with_args = [record for record in RECORDS if record.get("args")]
    assert len(with_args) == 72
    positives = [_positive(record) for record in with_args]
    negatives = [_negative(record) for record in with_args]
    assert len(positives) == len(with_args)
    assert len(negatives) == len(with_args)
    assert all(grammar.parse(utterance, CATALOG) for utterance in positives)
    assert all(grammar.parse(utterance, CATALOG) == [] for utterance in negatives)
    assert {grammar.parse(utterance, CATALOG)[0][0] for utterance in positives} >= {
        record["canonical_id"] for record in with_args
    }


def test_parser_is_fast_for_one_thousand_repeated_utterances():
    utterances = ["volume down twice", "click twenty five", "window switch bravo", "snap left"] * 250
    start = time.perf_counter()
    for utterance in utterances:
        grammar.parse(utterance, CATALOG)
    elapsed_ms = (time.perf_counter() - start) * 1000
    print(f"grammar 1,000 utterances: {elapsed_ms:.3f} ms")
    assert elapsed_ms < 30, f"grammar took {elapsed_ms:.3f} ms"


def test_module_does_not_import_dispatch_or_dictation():
    source = (ROOT / "samsara" / "grammar.py").read_text(encoding="utf-8")
    assert "import dictation" not in source
    assert "execution_policy" not in source
    assert "command_registry" not in source
