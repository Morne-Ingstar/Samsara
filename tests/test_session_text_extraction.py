"""Synthetic pre-move results and real-manager traces for the text extraction."""
import ast
from dataclasses import asdict
from enum import Enum
import json
from pathlib import Path

import pytest

from samsara import session_modes as facade


FIXTURE = Path(__file__).parent / "fixtures/session_text_pre_move.json"


def _plain(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _manager_trace(recipe):
    injections, formatting, redecodes, removals = [], [], [], []

    def inject(text, *guards):
        injections.append({"text": text, "guards": [guard() for guard in guards]})
        return text

    def format_text(text):
        formatting.append(text)
        return text

    def redecode(text, audio):
        redecodes.append({"text": text, "audio": list(audio)})
        return recipe["redecoded"]

    manager = facade.SessionModeManager(
        abort_phrases=["cancel"], foreground_exe_resolver=lambda: "synthetic.exe",
        foreground_hwnd_resolver=lambda: 653, inject_fn=inject,
        remove_chars_fn=lambda count: removals.append(count) or True,
        command_dispatch_fn=lambda text: None, agent_dispatch_fn=lambda *args: None,
        buffer_dictate_until_commit=recipe.get("buffered", True),
        format_dictate_fn=format_text, clock=lambda: 100.0,
        commit_redecode_fn=redecode if "redecoded" in recipe else None,
    )
    manager.reset(facade.SessionMode.DICTATE)
    document, stack, lock = manager.draft_document, manager._stack, manager._dispatch_lock
    snapshots = []
    for index, operation in enumerate(recipe["operations"]):
        action = operation[0]
        if action == "say":
            result = manager._dispatch_dictate(operation[1], audio_ref=f"audio-{index}")
            result = {"kind": result.kind, "detail": _plain(result.detail)}
        elif action == "scratch":
            result = manager._do_scratch_that()
        elif action == "suffix":
            manager._dictate_pending_buffer += operation[1]
            result = None
        elif action == "commit":
            result = manager.commit_pending_dictation()
            result = {"kind": result.kind, "detail": _plain(result.detail)}
        else:
            raise AssertionError(action)
        assert manager.draft_document is document
        assert manager._stack is stack
        assert manager._dispatch_lock is lock
        snapshots.append({
            "result": result, "pending": manager.dictate_pending_buffer,
            "stack": [_plain(asdict(item)) for item in stack._items],
            "audio": list(manager._dictate_pending_audio),
            "terminal": manager._last_dictate_ended_terminal,
            "stage": manager._stage_buffer,
        })
    return {"snapshots": snapshots, "injections": injections, "formatting": formatting,
            "redecodes": redecodes, "removals": removals}


def _golden():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_every_original_definition_is_the_same_export():
    from samsara import session_text as text

    names = _golden()["facade_names"]
    for name in names:
        assert getattr(facade, name) is getattr(text, name), name
    tree = ast.parse(Path(text.__file__).read_text(encoding="utf-8"))
    definitions = {node.name for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    definitions.update(target.id for node in tree.body if isinstance(node, ast.Assign)
                       for target in node.targets if isinstance(target, ast.Name))
    assert definitions == set(names)
    assert text.EllipsisSeam.__module__ == "samsara.session_text"
    # Keep the helper's dependency direction pure and import-time inert.
    imports = {node.module for node in tree.body if isinstance(node, ast.ImportFrom)}
    imports.update(alias.name for node in tree.body if isinstance(node, ast.Import)
                   for alias in node.names)
    assert imports <= {"__future__", "re", "string", "dataclasses", "typing"}
    assert text.REDECODE_RUN_ON_WORDS_PER_MARK == 40.0
    assert text.REDECODE_STAGED_ADVANTAGE == 2.0
    assert text.REDECODE_MIN_STAGED_MARKS == 2


@pytest.mark.parametrize("section", ["seam_join", "continuation", "ellipsis", "terminal",
                                     "density", "quality"])
def test_synthetic_results_match_pre_move_bytes_and_fields(section):
    from samsara import session_text as text

    functions = {"continuation": "_is_continuation_chunk", "ellipsis": "decide_ellipsis_seam",
                 "terminal": "chunk_ends_terminal", "density": "sentence_mark_density",
                 "quality": "redecode_is_poorer"}
    function = getattr(text, functions.get(section, section))
    for case in _golden()[section]:
        actual = function(*case["args"])
        if section == "ellipsis" and actual is not None:
            assert isinstance(actual, facade.EllipsisSeam)
            actual = asdict(actual)
        # UTF-8 preserves punctuation/whitespace and distinguishes None from
        # a join=False result; expected bytes were captured before the move.
        assert json.dumps(actual, ensure_ascii=False).encode("utf-8") == json.dumps(
            case["expected"], ensure_ascii=False).encode("utf-8"), case["id"]


def test_matrix_pins_negative_cases_and_inclusive_thresholds():
    golden = _golden()
    outcomes = [case["expected"] for case in golden["ellipsis"]]
    assert None in outcomes
    assert {result["clause"] for result in outcomes if result is not None} == {
        "empty", "ellipsis_only", "ellipsis_lowercase", "ellipsis_determiner",
        "ellipsis_conjunction_i", "ellipsis_hesitation_only", "ellipsis_new_sentence",
    }
    quality = {case["id"]: case["expected"] for case in golden["quality"]}
    assert quality["40-words-per-mark"] is False
    assert quality["41-words-per-mark"] is True
    assert quality["advantage-equal"] is True
    assert quality["advantage-below"] is False
    assert quality["one-staged-mark"] is False
    assert quality["two-staged-marks"] is True


def test_real_manager_matches_pre_move_state_scratch_and_injection():
    for case in _golden()["manager"]:
        assert _manager_trace(case["recipe"]) == case["expected"], case["id"]


def test_manager_keeps_dynamic_facade_seam_override(monkeypatch):
    calls = []

    def patched(previous, raw):
        calls.append((previous, raw))
        return "Synthetic facade override."

    monkeypatch.setattr(facade, "seam_join", patched)
    trace = _manager_trace({"operations": [["say", "Synthetic first."],
                                              ["say", "Synthetic second."], ["commit"]]})
    assert calls == [(True, "Synthetic second.")]
    assert trace["injections"][0]["text"] == "Synthetic first. Synthetic facade override."


def test_hands_free_delivery_retains_its_import_time_binding(monkeypatch):
    # Import before rebinding the facade, just as production composition does.
    from samsara import hands_free_delivery as delivery
    from samsara import session_text as text

    original = delivery.seam_join
    assert original is text.seam_join
    monkeypatch.setattr(facade, "seam_join", lambda *args: "Synthetic override.")
    assert delivery.seam_join is original
    assert delivery.seam_join(False, "Synthetic fragment") == "synthetic fragment"
