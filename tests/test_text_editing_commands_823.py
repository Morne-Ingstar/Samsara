"""823: deterministic routes, fake editor receipts and the real reference view."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json
import os

import pytest

from samsara import delivery_verify as dv, text_selection as ts
from samsara.ava_edit import intent, model, selection, session
from tests.test_clean_up_selection_632 import app, rig, executor, SOURCE, RESULT


@pytest.fixture
def global_executor(executor):
    from samsara.plugin_commands import _import_plugin
    from samsara.commands import CommandExecutor
    from samsara.command_scope import MatchContext
    root = Path(__file__).resolve().parents[1]
    for name in ("word", "text_selection"):
        _import_plugin(root / f"plugins/commands/{name}.py")
    result = CommandExecutor(root / "commands.json")
    result._matcher.set_context_provider(lambda: MatchContext.for_app("notepad.exe"))
    return result


@pytest.mark.parametrize("phrase", ["select all", "select word", "select line", "select sentence",
    "select last word", "select previous three words", "select that", "delete that",
    "unselect", "deselect", "copy that", "cut that", "paste"])
def test_selection_phrases_route_in_unscoped_editor(global_executor, phrase):
    entry, _ = global_executor._matcher.match(phrase)
    assert entry is not None and entry.scope is None


@pytest.mark.parametrize("unit,number", [("word", 2), ("line", 5), ("sentence", 3)])
def test_word_keeps_com_tier_and_other_editors_use_global_fallback(monkeypatch, app, unit, number):
    from plugins.commands import word
    called = []
    monkeypatch.setattr(word, "_word_is_focused", lambda: True)
    monkeypatch.setattr(word, "_selection_call", lambda app, fn:
                        (True, fn(SimpleNamespace(Expand=lambda n: called.append(n)))))
    assert getattr(word, "select_" + unit)(app)
    assert called == [number]
    monkeypatch.setattr(word, "_word_is_focused", lambda: False)
    monkeypatch.setattr(ts, "select_unit", lambda name: called.append(name) or True)
    monkeypatch.setattr(ts, "select_sentence", lambda app: called.append("sentence") or True)
    assert getattr(word, "select_" + unit)(app)
    assert called == [number, unit]


def test_bare_make_asks_before_capture_or_model(rig, app, executor, monkeypatch):
    monkeypatch.setattr(selection, "capture", lambda: pytest.fail("bare request captured text"))
    assert executor.process_text("make selection", app, force_commands=True).claimed
    assert app.spoken[-1] == "Make it how? For example: make the selection shorter."
    assert session.pending() is None


def test_complete_instruction_survives_canonical_policy_route(rig, app, executor, monkeypatch):
    seen = []
    monkeypatch.setattr(session, "enabled", lambda _: True)
    monkeypatch.setattr(model, "resolve_backend", lambda _: "ollama")
    monkeypatch.setattr(model, "rewrite", lambda original, instruction, app:
                        (seen.append(instruction) or RESULT, None))
    assert executor.process_text("make the selection shorter", app, force_commands=True).claimed
    assert seen == ["make the selection shorter"]
    assert session.pending().selection_target is not None
    assert executor.process_text("apply", app, force_commands=True).claimed
    assert rig.pasted == [RESULT]


def test_no_selection_explains_recovery(rig, app, executor):
    rig.selected = ""
    assert executor.process_text("make the selection shorter", app, force_commands=True).claimed
    assert app.spoken[-1] == "Select some text first"
    assert session.pending() is None


@pytest.mark.parametrize("enabled", [False, True])
def test_rewrite_without_authorized_backend_never_calls_model(rig, app, executor, monkeypatch, enabled):
    monkeypatch.setattr(session, "enabled", lambda _: enabled)
    monkeypatch.setattr(model, "resolve_backend", lambda _: None if enabled else
                        pytest.fail("disabled Ava probed a backend"))
    assert executor.process_text("make the selection shorter", app, force_commands=True).claimed
    assert app.spoken[-1] == "This edit needs Ava. Turn Ava on and choose a model first."
    assert session.pending() is None and not rig.pasted


@pytest.mark.parametrize("enabled", [False, True])
def test_selected_cleanup_uses_model_only_with_ava_enabled(rig, app, executor, monkeypatch, enabled):
    calls = []
    monkeypatch.setattr(session, "enabled", lambda _: enabled)
    monkeypatch.setattr(model, "rewrite", lambda original, instruction, app:
                        (calls.append((original, instruction)) or RESULT, None))
    assert session.source_text(app) == ""  # No staged draft is needed to edit selected text.
    assert executor.process_text("clean up selection", app, force_commands=True).claimed
    assert bool(calls) is enabled
    assert session.pending().proposal.rewritten == RESULT
    assert session.pending().selection_target is not None and not rig.pasted


@pytest.mark.parametrize("text", ['"make the selection shorter"',
    "I said make the selection shorter", "make me a coffee", "clean this up later"])
def test_whole_request_rejection_guard_is_kept(rig, app, executor, text):
    assert not executor.process_text(text, app, force_commands=True).claimed
    assert session.pending() is None


@pytest.mark.parametrize("phrase", ["capitalise selection", "capitalize selection",
                                    "formalise selection", "formalize selection"])
def test_spelling_variants_route_to_same_canonical(phrase):
    assert selection.command_for_utterance(phrase) == phrase.replace("ize", "ise")


def test_reference_has_one_record_per_edit_verb_with_instruction_help(global_executor):
    from samsara.ui.command_cheatsheet_qt import catalog_rows, row_text
    from samsara import command_catalog as cc
    rows = catalog_rows(global_executor._matcher.list_commands())
    edits = [r for r in rows if r["plugin"] == "selection_edit" and r.get("needs_ava")]
    verbs = intent.EDIT_VERBS - {"capitalize", "formalize"}
    assert {r["phrase"].split()[0] for r in edits} == verbs
    assert len(edits) == len(verbs)
    for row in edits:
        assert row["args"] and row["examples"]
        assert "Needs Ava" in row_text(row) and "cloud" in row_text(row)
        assert not row["shown_aliases"]
    make = next(r for r in edits if r["phrase"].startswith("make "))
    assert make["args"][0]["required"]
    assert make["examples"][0] == "make the selection shorter"
    settings = next(r for r in rows if r["canonical_id"] == "builtin.text_size")
    assert settings["plugin"] == "windows_accessibility"
    assert settings["description"].startswith("Opens Windows")
    assert not cc.collisions(cc.raw_phrase_claims(global_executor))


@pytest.fixture
def editor(monkeypatch):
    import pyautogui
    state = SimpleNamespace(text="Prefix. Delivered sentence.", start=27, end=27,
                            target=101, tick=7, shift=False, deletes=0)
    state.start = state.end = len(state.text)
    def proof():
        return ((state.target, (4, 9), state.text.encode("utf-16-le"),
                 state.start, state.end, state.tick), False)
    def press(key, presses=1, **kw):
        for _ in range(presses):
            state.tick += 1
            if key == "left":
                state.start = state.end = state.start - 1
            elif key == "right":
                state.end += 1
                if not state.shift:
                    state.start = state.end
            elif key == "delete":
                state.deletes += 1
                state.text = state.text[:state.start] + state.text[state.end:]
                state.end = state.start
    monkeypatch.setattr(dv, "snapshot", proof)
    monkeypatch.setattr(pyautogui, "press", press)
    monkeypatch.setattr(pyautogui, "keyDown", lambda k: setattr(state, "shift", k == "shift"))
    monkeypatch.setattr(pyautogui, "keyUp", lambda k: setattr(state, "shift", False))
    state.proof = proof
    return state


@pytest.mark.parametrize("delete", [False, True])
def test_uia_receipt_selects_or_deletes_only_last_delivery(editor, app, delete):
    app._last_delivery = (editor.proof(), "Delivered sentence.")
    assert ts.last_delivery(app, delete=delete)
    if delete:
        assert editor.text == "Prefix. " and editor.deletes == 1
    else:
        assert editor.text[editor.start:editor.end] == "Delivered sentence."
        assert editor.deletes == 0
    assert app._last_delivery is None


@pytest.mark.parametrize("change", ["caret", "away_and_back", "text", "target", "missing"])
def test_changed_or_unverified_receipt_never_deletes(editor, app, change):
    app._last_delivery = (editor.proof(), "Delivered sentence.")
    if change == "caret":
        editor.start = editor.end = 0
    elif change == "away_and_back":
        editor.tick += 1
    elif change == "text":
        editor.text = "X" + editor.text[1:]
    elif change == "target":
        editor.target = 202
    else:
        app._last_delivery = None
    assert ts.last_delivery(app, delete=True)
    assert editor.deletes == 0
    assert "Can't" in app.spoken[-1]


def test_sentence_fallback_selects_current_sentence(editor, app):
    assert ts.select_sentence(app)
    assert editor.text[editor.start:editor.end] == "Delivered sentence."


def test_verified_uia_snapshot_does_not_grant_native_undo(monkeypatch):
    before = ((101, (4, 9), b"", 0, 0, 7), False)
    after = ((101, (4, 9), "Typed.".encode("utf-16-le"), 6, 6, 8), False)
    monkeypatch.setattr(dv, "snapshot", lambda: after)
    assert dv.verified_snapshot(before, "Typed.", 101) == after
    assert dv.verify(before, "Typed.", 101) == (True, None)


@pytest.mark.parametrize("count,expected", [("three", 3), ("2", 2), ("zero", 0), ("101", 0)])
def test_previous_words_executes_bounded_count(global_executor, monkeypatch, app, count, expected):
    import pyautogui
    press = Mock()
    monkeypatch.setattr(pyautogui, "press", press)
    monkeypatch.setattr(pyautogui, "keyDown", Mock())
    monkeypatch.setattr(pyautogui, "keyUp", Mock())
    assert global_executor.process_text(f"select previous {count} words", app, force_commands=True).claimed
    if expected:
        press.assert_called_once_with("left", presses=expected, interval=0)
    else:
        press.assert_not_called()
        assert "1 to 100" in app.spoken[-1]


@pytest.mark.parametrize("verified", [True, False])
@pytest.mark.parametrize("typed", [True, False])
def test_typed_and_clipboard_delivery_retain_only_readback_proof(monkeypatch, typed, verified):
    from samsara import paste
    text = "Synthetic."
    before = ((101, (4, 9), b"", 0, 0, 7), False)
    after = ((101, (4, 9), text.encode("utf-16-le"), len(text), len(text), 8), False)
    current = [before]
    monkeypatch.setattr(dv, "snapshot", lambda: current[0])
    monkeypatch.setattr(paste, "_editor_snapshot", lambda: None)
    monkeypatch.setattr(paste.injection_safety, "window_integrity",
                        lambda: SimpleNamespace(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste.flight_recorder, "record", Mock())
    obj = SimpleNamespace(config={}, _foreground_wants_typed_injection=lambda: typed,
        _flight_foreground_process_name=lambda: "synthetic.exe", _record_undoable_paste=Mock(),
        adaptive_learner=Mock(), _undo_timer=None, _arm_undo_timer=Mock())
    def type_text(text, **kw):
        if verified:
            current[0] = after
        return True
    def central(text, **kw):
        assert kw["before_paste"]()
        type_text(text)
        kw["delivery_observer"](paste.DeliveryOutcome.SENT)
        return True
    assert paste.paste_preserving_clipboard(obj, text, return_delivery_confirmation=True,
        type_text_unicode_fn=type_text, paste_with_preservation_fn=central,
        get_foreground_hwnd_fn=lambda: 101)[0]
    assert obj._last_delivery == ((after, text) if verified else None)
    assert getattr(obj, "_undo_unit", None) is None  # UIA proof grants no native writer.


def test_new_failed_delivery_invalidates_previous_selection_receipt(monkeypatch, app):
    from samsara import paste
    app._last_delivery = ("older", "receipt")
    app.config = {}
    monkeypatch.setattr(paste.injection_safety, "window_integrity",
                        lambda: SimpleNamespace(blocked=True, describe=lambda: "synthetic"))
    assert not paste.paste_preserving_clipboard(app, "Synthetic.",
                                               return_delivery_confirmation=True)[0]
    assert app._last_delivery is None


def test_reference_picture_and_details(global_executor, qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from samsara.ui import command_cheatsheet_qt as cs
    from samsara import command_stats
    monkeypatch.setattr(cs, "_scope_context", lambda: None)
    monkeypatch.setattr(cs, "_disabled_packs", lambda: set())
    monkeypatch.setattr(command_stats, "get_top_commands", lambda n: [])
    win = cs._CheatSheetWindow(lambda _: None, global_executor._matcher.list_commands,
                              tmp_path / "palette.json")
    try:
        win.show()
        win._filter.setText("selection")
        qapp.processEvents()
        target = next(win._list.item(i) for i in range(win._list.count())
                      if win._list.item(i).data(Qt.ItemDataRole.UserRole) == "make selection")
        win._list.setCurrentItem(target)
        win._detail_card.open(next(r for r in win._all if r["phrase"] == "make selection"),
                              win._list, win._list.visualItemRect(target))
        qapp.processEvents()
        text = "\n".join(label.text() for label in win._detail_card.labels)
        assert "No argument required" not in text
        assert "make the selection shorter" in text and "cloud" in text
        if os.environ.get("SAMSARA_PROOF823_DIR"):
            path = Path(os.environ["SAMSARA_PROOF823_DIR"]) / "reference-selection.png"
            assert win.grab().save(str(path))
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
