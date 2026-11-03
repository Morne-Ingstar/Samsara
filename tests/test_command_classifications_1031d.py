"""Actual plugin declarations and policy, with inert devices and synthetic data."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import command_catalog as cc, effect_policy as effects, execution_policy as ep, plugin_commands
from samsara.command_registry import DispatchState
from samsara.command_recovery import recovery_hint


@pytest.fixture(scope="module")
def catalog():
    from tools.dump_command_metadata import build_executor
    executor = build_executor()
    return executor, dict(plugin_commands._REGISTRY), cc.build_catalog(executor)


@pytest.fixture
def harness(catalog, monkeypatch):
    executor, registry, specs = catalog
    monkeypatch.setattr(executor._matcher, "_enabled_packs", None)
    plugin_commands._REGISTRY.update(registry)
    app = SimpleNamespace(config={}, _ava_cmd_generation=7, play_sound=Mock(),
                          audio_coordinator=Mock(), _show_outcome_chip=Mock(),
                          command_executor=executor)
    monkeypatch.setattr(ep, "foreground_window_identity", lambda: {
        "hwnd": 42, "pid": 99, "process": "notepad.exe", "title_hash": "synthetic"})
    monkeypatch.setattr(ep, "listen_for_confirmation", Mock())
    return app, executor, {s.canonical_id: s for s in specs}


@pytest.mark.parametrize("phrase,tier,risk", [
    ("start print", "T0", "ui"), ("resume print", "T0", "ui"),
    ("pause print", "T0", "ui"), ("cancel print", "T0", "ui"),
    ("stop recording", "T0", "ui"), ("stop capture", "T0", "ui"),
    ("cancel memo", "T0", "ui"), ("select word", "T0", "ui"),
    ("select paragraph in word", "T0", "ui"), ("word count", "T0", "read"),
    ("read selection", "T0", "read"), ("discard", "T0", "ui"),
    ("bold that", "T1", "write"), ("undo in word", "T1", "write"),
    ("apply", "T1", "write"),
    ("clear health log", "T3", "destructive"), ("undo health log", "T3", "destructive"),
    ("cancel reminder", "T3", "destructive"), ("cancel all reminders", "T3", "destructive"),
    ("remove task", "T3", "destructive"), ("clear completed", "T3", "destructive"),
    ("delete layout", "T3", "destructive"), ("close", "T3", "destructive"),
])
@pytest.mark.parametrize("route", sorted(ep.USER_ROUTES, key=lambda route: route.value))
def test_real_declarations_select_tiers_without_timers(harness, phrase, tier, risk, route):
    app, executor, _ = harness
    inv = ep.Invocation(phrase, route=route, generation=7)
    assert ep.classify(phrase, executor=executor)[0] == risk
    handling = effects.handling_for(inv, app=app, executor=executor)
    assert handling.tier == tier and handling.wait_s == 0
    assert ep.cancel_window_for(phrase, app, executor=executor) == 0
    decision = ep.authorize(inv, app=app, executor=executor)
    assert isinstance(decision, ep.NeedsConfirmation if tier == "T3" else ep.Allowed)


@pytest.mark.parametrize("phrase,code", [
    ("continue printing", "M24"),
    ("hold the print", "M25"),
    ("abort print", "M26"),
])
def test_printer_alias_dispatch_is_immediate_and_not_undoable(harness, monkeypatch, phrase, code):
    from plugins.commands import flashforge_printer
    app, executor, _ = harness
    send = Mock(return_value="ok")
    monkeypatch.setattr(flashforge_printer, "_send", send)
    result = executor.execute_canonical(phrase, app)
    assert result.state is DispatchState.COMPLETED
    send.assert_called_once_with(app, code)
    assert ep.pending_operation() is None
    assert ep.classify(result.phrase, executor=executor)[1] is False


def test_memo_disarm_dispatch_has_no_question(harness):
    from samsara import voice_memo
    app, executor, _ = harness
    voice_memo.arm(app)
    assert executor.execute_canonical("cancel memo", app).state is DispatchState.COMPLETED
    assert voice_memo.is_armed(app.config) is False
    assert ep.pending_operation() is None


@pytest.mark.parametrize("keys", [["alt", "f4"], ["ctrl", "w"], ["ctrl", "f4"], ["ctrl", "shift", "w"]])
def test_generic_close_has_no_default_undo_and_requires_one_question(harness, keys):
    app, executor, _ = harness
    raw = "key:" + "+".join(keys)
    assert ep.classify(raw, executor=executor)[:2] == ("destructive", False)
    inv = ep.Invocation(raw, generation=7)
    assert effects.handling_for(inv, app=app, executor=executor) == effects.CONFIRM
    assert isinstance(ep.authorize(inv, app=app, executor=executor), ep.NeedsConfirmation)
    assert isinstance(ep.authorize(inv, app=app, executor=executor, confirmed=True), ep.Allowed)
    assert ep.cancel_window_for(raw, app, executor=executor) == 0


def test_registered_close_hotkey_has_no_default_undo(harness):
    _, executor, _ = harness
    assert ep.classify("close window", executor=executor)[:2] == ("destructive", False)


def test_immediate_printer_still_respects_pack_and_stale_guards(harness, monkeypatch):
    app, executor, _ = harness
    inv = ep.Invocation("resume print", generation=6)
    assert isinstance(ep.authorize(inv, app=app, executor=executor), ep.Denied)
    monkeypatch.setattr(executor._matcher, "_enabled_packs", {"core"})
    decision = ep.authorize(replace(inv, generation=7), app=app, executor=executor)
    assert isinstance(decision, ep.Denied) and decision.reason == "pack_disabled"


@pytest.mark.parametrize("risk,phrase,metadata,expected,source", [
    ("ui", "switch window", {}, False, "undeclared"),
    ("write", "add task", {}, False, "undeclared"),
    ("write", "type text", {}, False, "undeclared"),
    ("read", "read health log", {"reversible": True}, False, "not needed"),
    ("destructive", "close window", {"reversible": True}, False, "conditional"),
    ("write", "bold that", {"reversible": True}, True, "declared"),
    ("destructive", "remove task", {"reversible": False}, False, "declared"),
])
def test_recovery_provenance_does_not_invent_undo(risk, phrase, metadata, expected, source):
    hint, provenance = recovery_hint(risk, phrase, metadata)
    assert hint is expected and provenance.startswith(source)
    assert cc.guess_undoable(risk, phrase, metadata) is expected


def test_generated_and_runtime_catalog_expose_declaration_provenance(harness):
    _, executor, specs = harness
    assert specs["builtin.close_window"].undoable is False
    assert specs["app_verbs.close"].undoable_source.startswith("conditional")
    assert specs["word.bold_that"].undoable_source == "declared: reversible (not verified rollback)"
    assert specs["word.word_count"].undoable_source == "not needed: read only"
    assert specs["flashforge_printer.resume_print"].undoable is False
    assert cc.validate_catalog(cc.to_document(list(specs.values()))) == []
    rows = cc.catalog_from_registry_rows(executor._matcher.list_commands(), executor.commands)
    assert next(r for r in rows if r["canonical_id"] == "word.word_count")["undoable_source"].startswith("not needed")
    assert "not verified rollback" in cc.render_markdown(list(specs.values()))


def test_catalog_validator_rejects_invalid_provenance(harness):
    _, _, specs = harness
    spec = replace(specs["word.word_count"], undoable_source="")
    assert any("undoable_source" in error for error in cc.validate_catalog(cc.to_document([spec])))
