"""Compatibility and independent, synthetic pre-extraction feedback evidence."""
import ast
from copy import deepcopy
import json
from pathlib import Path

from samsara import session_feedback as feedback
from samsara import session_modes as facade
from tests.test_outcome_chip import _dispatch_outcome_kinds


GOLDEN = json.loads((Path(__file__).parent / "fixtures/session_feedback_pre_move.json")
                    .read_text(encoding="utf-8"))
# Preserve every historical case; 658/695 added this producer after extraction.
GOLDEN["producer_kinds"].append("ava_edit_handled")
GOLDEN["cases"].append({"id": "ava-edit-handled", "kind": "ava_edit_handled",
                      "detail": {}, "expected": None, "mapped": True,
                      "canonical_id": "", "ttl": 0})


def test_every_original_name_is_the_same_export():
    for name in GOLDEN["facade_names"]:
        assert getattr(facade, name) is getattr(feedback, name), name
    tree = ast.parse(Path(feedback.__file__).read_text(encoding="utf-8"))
    definitions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    definitions.update(target.id for node in tree.body if isinstance(node, ast.Assign)
                       for target in node.targets if isinstance(target, ast.Name))
    assert definitions == set(GOLDEN["facade_names"])
    assert facade._COMMAND_SUGGESTION_TIMEOUT_S == 8.0
    assert not hasattr(feedback, "_COMMAND_SUGGESTION_TIMEOUT_S")


def test_golden_matrix_covers_every_current_producer():
    producers = _dispatch_outcome_kinds()
    assert producers == set(GOLDEN["producer_kinds"])
    assert producers <= {case["kind"] for case in GOLDEN["cases"]}


def test_outcomes_match_pre_move_results_without_mutating_inputs():
    # Expected labels, severity, catalog identity and TTL were captured from
    # unchanged HEAD, rather than computed with either candidate import path.
    for case in GOLDEN["cases"]:
        detail = deepcopy(case["detail"])
        before = deepcopy(detail)
        actual = feedback.outcome_chip(case["kind"], detail)
        expected = tuple(case["expected"]) if case["expected"] is not None else None
        assert actual == expected, case["id"]
        assert detail == before, case["id"]
        assert feedback.is_mapped_outcome(case["kind"]) == case["mapped"], case["id"]
        if actual is not None:
            assert getattr(actual[0], "canonical_id", "") == case["canonical_id"], case["id"]
            assert feedback.chip_ttl_ms(case["kind"], actual[1]) == case["ttl"], case["id"]


def test_private_helpers_match_pre_move_results_without_mutating_inputs():
    for case in GOLDEN["helpers"]:
        args, kwargs = deepcopy(case["args"]), deepcopy(case["kwargs"])
        before = deepcopy((args, kwargs))
        actual = getattr(feedback, case["name"])(*args, **kwargs)
        expected = case["expected"]
        if isinstance(expected, list):
            expected = tuple(expected)
        assert actual == expected, case["name"]
        assert (args, kwargs) == before, case["name"]
        assert getattr(actual, "canonical_id", "") == case["canonical_id"], case["name"]


def test_ttl_matrix_preserves_pending_live_and_overrides():
    for case in GOLDEN["ttl_cases"]:
        assert feedback.chip_ttl_ms(case["outcome"], case["chip_kind"]) == case["expected"], case


def test_mode_enum_remains_usable_without_a_feedback_enum_dependency():
    for mode in facade.SessionMode:
        assert feedback._mode_label(mode) == mode.value.upper()
        assert feedback.outcome_chip("mode_switch", {"mode": mode}) == (
            f"{facade.CHIP_ARROW} {mode.value.upper()}", "accent")


def test_shared_mutable_tables_still_drive_both_import_paths(monkeypatch):
    monkeypatch.setitem(facade.COMMAND_CHIP_COPY, "miss", "Synthetic {heard}")
    monkeypatch.setitem(facade._CHIP_TTL_OVERRIDES, "command_miss", 1234)
    monkeypatch.setitem(facade._INTERACT_CHIPS, "interact_action", ("Synthetic action", "accent"))
    for module in (facade, feedback):
        assert module.outcome_chip("command_miss", {"heard": "input"}) == ("Synthetic input", "error")
        assert module.chip_ttl_ms("command_miss", "error") == 1234
        assert module.outcome_chip("interact_action") == ("Synthetic action", "accent")


def test_feedback_imports_stay_independent_and_catalog_lookup_stays_lazy():
    tree = ast.parse(Path(feedback.__file__).read_text(encoding="utf-8"))
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    modules = [node.module for node in imports if isinstance(node, ast.ImportFrom)]
    modules.extend(alias.name for node in imports if isinstance(node, ast.Import) for alias in node.names)
    assert set(modules) <= {"__future__", "typing", "samsara.outcome_ring"}
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_command_chip_label")
    catalog_imports = [node for node in imports if isinstance(node, ast.ImportFrom)
                       and node.module == "samsara.outcome_ring"]
    assert len(catalog_imports) == 1
    assert catalog_imports[0] in list(ast.walk(helper))
