"""Visible labels, their actual click routes and whole-utterance safety (1150)."""
import ast
from dataclasses import replace
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import button_voice as voice
from samsara.session_modes import (
    CLEAR_DRAFT_PHRASES, CommandDispatchResult, SessionMode, SessionModeManager,
    UtteranceSignals,
)

ROOT = Path(__file__).resolve().parents[1]
UI_SOURCE_FILES = (
    "samsara/streaming.py", "samsara/ui/live_surface_qt.py",
    "samsara/ui/correction_picker_qt.py", "samsara/ui/ava_consent_qt.py",
    "samsara/ui/ava_search_panel_qt.py", "samsara/bug_report/compact_qt.py",
    "samsara/bug_report/preview_qt.py", "samsara/ui/tray_qt.py",
    "samsara/ui/tray_actions.py", "samsara/clipboard_recovery.py",
    "samsara/ui/listening_indicator.py",
    "samsara/ui/tray_microphones.py", "samsara/game_mode.py",
    "samsara/ui/settings/general_qt.py", "samsara/ui/settings/modes_qt.py",
    "samsara/ui/settings/sounds_qt.py",
)


def definition_labels():
    """Read constructors, label swaps and literal loop tables, never a label list.

    Dynamic word/device choices are exercised through the runtime tests below;
    they are not frozen into a catalogue of the user's text or hardware.
    """
    found = set()
    for path in UI_SOURCE_FILES:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        bindings = {}

        def values(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return {node.value}
            if isinstance(node, ast.Name):
                return bindings.get(node.id, set())
            if isinstance(node, ast.IfExp):
                return values(node.body) | values(node.orelse)
            return set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bindings.setdefault(target.id, set()).update(values(node.value))
            if isinstance(node, ast.For) and isinstance(node.iter, (ast.List, ast.Tuple)):
                targets = node.target.elts if isinstance(node.target, ast.Tuple) else [node.target]
                for row in node.iter.elts:
                    cells = row.elts if isinstance(row, (ast.List, ast.Tuple)) else [row]
                    for target, cell in zip(targets, cells):
                        if isinstance(target, ast.Name):
                            bindings.setdefault(target.id, set()).update(values(cell))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if "/settings/" in path and name not in {"QCheckBox", "QRadioButton"}:
                continue  # This inventory's Settings scope is quick toggles.
            index = None
            if name in {"QPushButton", "QToolButton", "QCheckBox", "QRadioButton", "QMenu", "addAction"}:
                index = 0
            elif name == "_button":
                index = 1 if path.endswith("live_surface_qt.py") else 0
            elif name in {"add", "_add"} and ("tray" in path or path.endswith("game_mode.py")):
                index = 1
            elif name == "setText" and isinstance(func, ast.Attribute):
                receiver = ast.unparse(func.value).casefold()
                if any(token in receiver for token in ("button", "_btn", "report", "dismiss", "_latest")):
                    index = 0
            if index is not None and len(node.args) > index:
                for label in values(node.args[index]):
                    if label.strip():
                        found.add((path, label))
    # Calm Settings moves the original checkboxes into rows. The visible row
    # title, rather than a stale legacy accessibility name, is their label.
    checkbox_fields = set()
    pages = ROOT / "samsara/ui/settings"
    for file in pages.glob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        variables = {target.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                     and isinstance(node.value, ast.Call)
                     and getattr(node.value.func, "id", "") == "QCheckBox"
                     for target in node.targets if isinstance(target, ast.Name)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id in variables:
                for target in node.targets:
                    if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Attribute)
                            and target.value.attr == "_widgets" and isinstance(target.slice, ast.Constant)):
                        checkbox_fields.add(target.slice.value)
    for file in pages.glob("*_page.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.List) and len(node.elts) >= 4
                    and isinstance(node.elts[0], ast.Constant) and isinstance(node.elts[0].value, str)
                    and isinstance(node.elts[2], ast.Constant) and isinstance(node.elts[2].value, str)
                    and checkbox_fields.intersection(node.elts[2].value.split(","))):
                found.add((file.relative_to(ROOT).as_posix(), node.elts[0].value))
    return sorted(found)


GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,),
                        transcript_confident=True)
NOISE = UtteranceSignals(has_contiguous_speech=False, compression_ratios=(50.0,))


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(voice, "prepare_visible_actions", lambda: None)
    monkeypatch.setattr(voice, "_bridge", None)
    result = SessionModeManager(
        abort_phrases=["cancel", "abort"], foreground_exe_resolver=lambda: "notepad.exe",
        foreground_hwnd_resolver=lambda: 4242, inject_fn=Mock(), remove_chars_fn=Mock(),
        command_dispatch_fn=lambda _: CommandDispatchResult(matched=False, phrase=None),
        agent_dispatch_fn=Mock(), buffer_dictate_until_commit=True, live_surface_enabled=True,
    )
    result.reset(SessionMode.DICTATE)
    yield result
    result._clear_draft_undo.drop()


@pytest.mark.parametrize("phrase", ["clear draft", "Clear the draft!", "CLEAR MY DRAFT."])
def test_clear_label_is_immediate_and_recoverable(manager, phrase):
    manager.dispatch_utterance("Synthetic first thought.", GOOD)
    before = manager.dictate_pending_buffer
    assert manager.dispatch_utterance(phrase, GOOD).kind == "dictate_draft_cleared"
    assert manager.dictate_pending_buffer == ""
    assert manager.dispatch_utterance("bring it back", GOOD).kind == "dictate_draft_recovered"
    assert manager.dictate_pending_buffer == before
    manager._inject_fn.assert_not_called()


@pytest.mark.parametrize("text", [
    "we should clear draft notes later", "please clear my draft notes later",
    "we should insert text tomorrow", "literal clear draft",
])
def test_containing_sentence_and_literal_do_not_clear(manager, text):
    manager._dictate_pending_buffer = "Synthetic draft."
    assert manager.dispatch_utterance(text, GOOD).kind == "dictate_staged"
    assert manager.dictate_pending_buffer.startswith("Synthetic draft.")


def test_clear_aliases_keep_audio_gate_and_catalogue(manager):
    manager._dictate_pending_buffer = "Synthetic draft."
    assert manager.dispatch_utterance("clear draft", NOISE).kind != "dictate_draft_cleared"
    assert manager.dictate_pending_buffer.startswith("Synthetic draft.")
    command = json.loads((ROOT / "commands.json").read_text(encoding="utf-8"))["commands"]["scratch everything"]
    assert set(CLEAR_DRAFT_PHRASES) == {"scratch everything", *command["aliases"]}


def test_quick_reference_covers_labels_read_from_ui_definitions(qapp):
    from PySide6.QtWidgets import QLabel
    from samsara.button_voice_reference import CONTROL_LABELS
    from samsara.ui.quick_reference_qt import _QuickReferenceWindow
    assert {label for _, label in definition_labels()} <= set(CONTROL_LABELS)
    window = _QuickReferenceWindow(SimpleNamespace(config={}))
    try:
        displayed = {label.text() for label in window.findChildren(QLabel)}
        for label in CONTROL_LABELS:
            assert label in displayed
            assert voice.phrases_for_label(label)[0] in displayed
        # 1150 n2: deriving guidance from its owners preserves the approved copy.
        assert {
            "bring it back (within 30 seconds of a voice clear)",
            "dismiss Ava captions", "open source one (or its number)",
            "click followed by the visible label",
        } <= displayed
    finally:
        window.close()
        window.deleteLater()


def test_quick_reference_refresh_reads_live_catalogue_and_missing_entry(qapp, monkeypatch):
    from PySide6.QtWidgets import QLabel
    from samsara import command_catalog
    from samsara.ui.quick_reference_qt import _QuickReferenceWindow

    rows = [{"phrase": "synthetic control"}]
    matcher = SimpleNamespace(list_commands=Mock(return_value=rows))
    catalogue = Mock(return_value=[{
        "canonical_id": "show_numbers.click", "phrase": "synthetic control",
        "aliases": ["synthetic control"],
    }])
    monkeypatch.setattr(command_catalog, "guidance_catalog", catalogue)
    window = _QuickReferenceWindow(SimpleNamespace(
        config={}, command_executor=SimpleNamespace(_matcher=matcher)))
    try:
        displayed = {label.text() for label in window.findChildren(QLabel)}
        assert "synthetic control followed by the visible label" in displayed
        assert any("use 'synthetic control'" in line for line in displayed)
        catalogue.assert_called_once_with(rows)
        catalogue.return_value = None
        window.refresh()
        displayed = {label.text() for label in window.findChildren(QLabel)}
        assert "unavailable  (disabled)" in displayed
        assert any("Command catalogue unavailable" in line for line in displayed)
        assert "click followed by the visible label" not in displayed
    finally:
        window.close()
        window.deleteLater()


@pytest.mark.parametrize("path,label", definition_labels())
def test_every_label_in_ui_definitions_has_an_exact_voice_route(path, label, qapp):
    """New labelled buttons automatically get the same actual click route."""
    from PySide6.QtWidgets import QPushButton
    from samsara.bug_report.preview_qt import ReportPreview
    from samsara.ui.button_voice_qt import VisibleActions
    window = ReportPreview("Synthetic report")
    # Use the label read from the UI source in a supported, real host. The
    # integration tests below also exercise unmodified production controls.
    for button in window.findChildren(QPushButton):
        button.hide()
    button = QPushButton(label, window)
    window.layout().addWidget(button)
    invoked = Mock()
    button.clicked.connect(invoked)
    window.show()
    qapp.processEvents()
    bridge = VisibleActions(qapp, lambda fn: fn())
    try:
        phrases = voice.phrases_for_label(button.text())
        assert phrases, (path, label)
        assert not bridge.dispatch("we should " + phrases[0] + " later")
        assert bridge.dispatch(phrases[0] + "."), (path, label)
        invoked.assert_called_once()
    finally:
        bridge.timer.stop()
        window.close()
        bridge.deleteLater()


def test_real_report_buttons_and_stale_activation(qapp):
    from samsara.bug_report.preview_qt import ReportPreview
    from samsara.ui.button_voice_qt import VisibleActions
    window = ReportPreview("Synthetic report")
    window.show()
    qapp.processEvents()
    queue = []
    bridge = VisibleActions(qapp, queue.append)
    try:
        assert not bridge.dispatch("copy")  # prose collision: explicit click
        assert bridge.dispatch("click Copy")
        queue.pop()()
        assert window.status.text() == "Report copied."
        assert bridge.dispatch("click Copy")
        window.copy.setDisabled(True)
        window.status.setText("")
        queue.pop()()
        assert window.status.text() == ""
        window.copy.setEnabled(True)
        bridge.refresh()
        assert bridge.dispatch("click Cancel")
        queue.pop()()
        assert not window.isVisible()
    finally:
        bridge.timer.stop()
        window.close()
        bridge.deleteLater()


def test_real_full_insert_button_and_duplicate_refusal(qapp):
    from PySide6.QtWidgets import QPushButton
    from samsara.live_surface.model import (CaptureState, DocumentState, Notice,
        NoticeKind, PresentationState, SurfaceView, VisibleForm)
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    from samsara.ui.button_voice_qt import VisibleActions
    view = SurfaceView(VisibleForm.REVIEW, CaptureState.OFF, None,
        DocumentState.PARKED_DRAFT, PresentationState.COMPACT,
        Notice(NoticeKind.NONE, "", None), "synthetic", 1, "Synthetic draft", "", False)
    window = LiveSurfaceWidget(view, reduced_motion=True)
    invoked = Mock()
    window.commit_requested.connect(invoked)
    window.show()
    qapp.processEvents()
    queue = []
    bridge = VisibleActions(qapp, queue.append)
    try:
        assert window._insert.isVisible()
        assert bridge.dispatch(window._insert.text())
        queue.pop()()
        invoked.assert_called_once()
        assert bridge.dispatch(window._insert.text())
        window._view = replace(view, revision=2)
        queue.pop()()
        invoked.assert_called_once()  # A newer draft cannot receive the queued click.
        duplicate = QPushButton(window._insert.text(), window)
        duplicate.setGeometry(5, 5, 130, 30)
        duplicate.show()
        bridge.refresh()
        assert not bridge.dispatch(window._insert.text())
    finally:
        bridge.timer.stop()
        window.close()
        bridge.deleteLater()


def test_session_routes_visible_controls_before_cancel_and_preserves_literal(manager, monkeypatch):
    bridge = SimpleNamespace(dispatch=Mock(return_value=True))
    monkeypatch.setattr(voice, "_bridge", bridge)
    assert manager.dispatch_utterance("click Cancel", GOOD).kind == "surface_review_action"
    bridge.dispatch.assert_called_once_with("click Cancel")
    bridge.dispatch.reset_mock()
    assert manager.dispatch_utterance("literal click Cancel", GOOD).kind == "dictate_staged"
    bridge.dispatch.assert_not_called()
    manager.dispatch_utterance("click Copy", NOISE)
    bridge.dispatch.assert_not_called()


def test_visible_menu_and_settings_switch_use_existing_handlers(qapp):
    from PySide6.QtWidgets import QMenu, QCheckBox
    from samsara.ui.settings.presentation import SettingRow
    from samsara.bug_report.preview_qt import ReportPreview
    from samsara.ui.button_voice_qt import VisibleActions
    window = ReportPreview("Synthetic report")
    switch = QCheckBox()
    switch.setAccessibleName("Legacy switch name")
    row = SettingRow("Wake activation", "Synthetic help", [switch])
    window.layout().addWidget(row)
    window.show()
    qapp.processEvents()
    bridge = VisibleActions(qapp, lambda fn: fn())
    menu = QMenu()
    invoked = Mock()
    action = menu.addAction("Reset dictation preview")
    action.triggered.connect(invoked)
    try:
        assert bridge.dispatch("click Wake activation")
        assert switch.isChecked()
        menu.show()
        qapp.processEvents()
        bridge.refresh()
        assert bridge.dispatch(action.text())
        invoked.assert_called_once()
        assert not menu.isVisible()
    finally:
        bridge.timer.stop()
        menu.close()
        window.close()
        bridge.deleteLater()


def test_caption_and_web_source_use_the_existing_ui_handlers(qapp, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    from samsara.ui.ava_captions_qt import _CaptionsWindow
    from samsara.ui.ava_search_panel_qt import _build_window
    from samsara.ui.button_voice_qt import VisibleActions
    caption = _CaptionsWindow(SimpleNamespace(config={}))
    caption._timer.start(60000)
    caption.show()
    qapp.processEvents()
    bridge = VisibleActions(qapp, lambda fn: fn())
    panel = None
    try:
        assert bridge.dispatch("dismiss Ava captions")
        assert not caption.isVisible()
        assert not caption._timer.isActive()
        panel = _build_window()
        panel.set_content("Synthetic question", "Synthetic answer", [("Synthetic source", "https://example.com/")])
        panel.show()
        qapp.processEvents()
        bridge.refresh()
        opened = Mock(return_value=True)
        monkeypatch.setattr(QDesktopServices, "openUrl", opened)
        assert not bridge.dispatch(panel.sources.item(0).text())
        assert bridge.dispatch("open source one")
        opened.assert_called_once()
        assert opened.call_args.args[0].toString() == "https://example.com/"
    finally:
        bridge.timer.stop()
        caption.close()
        if panel is not None:
            panel.close()
        bridge.deleteLater()


@pytest.fixture
def pending_app(monkeypatch):
    from samsara import ava_pending, message_lifetime
    monkeypatch.setattr(message_lifetime, "consume_acknowledgement", lambda *a, **k: False)
    monkeypatch.setattr(ava_pending.policy, "pending_operation", lambda: None)
    runner = SimpleNamespace(eligible=Mock(return_value=True), run=Mock(), expired=Mock())
    question = ava_pending.Question(runner, "yes_no", "", (),
        opened=True, deadline=time.monotonic() + 30, answer_guidance_given=True)
    return SimpleNamespace(_ava_question=question)


def test_toggle_caller_checks_controls_before_pending_and_session_dispatch():
    """Pin the production call seam without bootstrapping dictation.py."""
    tree = ast.parse((ROOT / "dictation.py").read_text(encoding="utf-8"))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "_handle_command_mode_utterance")
    body = next(n.body for n in ast.walk(method) if isinstance(n, ast.Try)
                and any(isinstance(s, ast.Assign) and isinstance(s.value, ast.Call)
                        and ast.unparse(s.value.func) == "manager.dispatch_utterance"
                        for s in n.body))
    index = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign)
                 and isinstance(n.value, ast.Call)
                 and ast.unparse(n.value.func) == "manager.dispatch_utterance")
    binding, guard = body[index - 2:index]
    assert isinstance(binding, ast.ImportFrom) and binding.module == "samsara.button_voice"
    assert [(n.name, n.asname) for n in binding.names] == [("consume_before_pending", None)]
    assert isinstance(guard, ast.If) and isinstance(guard.body[0], ast.Return)
    assert ast.unparse(guard.test) == (
        "consume_before_pending(self, text, signals, self_hear=_self_hear, capture_path='toggle_command')")
    assert not any(isinstance(n, ast.ImportFrom) and n.module == "samsara.ava_pending"
                   for n in ast.walk(method))


@pytest.mark.parametrize("kind", ["yes_no", "choice"])
def test_pending_question_must_not_swallow_a_visible_control_phrase(pending_app, monkeypatch, kind):
    from samsara import ava_pending
    question = pending_app._ava_question
    question.kind = kind
    before = vars(question).copy()
    bridge = SimpleNamespace(dispatch=Mock(side_effect=lambda text: text == "click dismiss"))
    monkeypatch.setattr(voice, "_bridge", bridge)
    consume = Mock(wraps=ava_pending.consume)
    monkeypatch.setattr(ava_pending, "consume", consume)
    assert voice.consume_before_pending(pending_app, "click dismiss", GOOD,
                                        self_hear=False, capture_path="toggle_command")
    bridge.dispatch.assert_called_once_with("click dismiss")
    consume.assert_not_called()
    assert pending_app._ava_question is question
    assert vars(question) == before
    question.runner.run.assert_not_called()


@pytest.mark.parametrize("answer, runs", [("yes", 1), ("no", 0)])
def test_normal_yes_no_still_reaches_pending_question(pending_app, monkeypatch, answer, runs):
    from samsara import ava_pending
    question = pending_app._ava_question
    monkeypatch.setattr(voice, "_bridge", SimpleNamespace(dispatch=Mock(return_value=False)))
    consume = Mock(wraps=ava_pending.consume)
    monkeypatch.setattr(ava_pending, "consume", consume)
    assert voice.consume_before_pending(pending_app, answer, GOOD,
                                        self_hear=False, capture_path="toggle_command")
    consume.assert_called_once_with(pending_app, answer, capture_path="toggle_command")
    assert pending_app._ava_question is None
    assert question.runner.run.call_count == runs


@pytest.mark.parametrize("case", [
    "noise", "missing_audio", "literal", "self_hear", "speaking", "echo", "model",
    "unopened", "expired", "ineligible", "operation_changed",
])
def test_pending_control_preserves_audio_source_and_question_guards(pending_app, monkeypatch, case):
    from samsara import ava_pending
    question = pending_app._ava_question
    bridge = SimpleNamespace(dispatch=Mock(return_value=True))
    monkeypatch.setattr(voice, "_bridge", bridge)
    text, signals, capture_path = "click dismiss", GOOD, "toggle_command"
    if case == "noise":
        signals = NOISE
    elif case == "missing_audio":
        signals = UtteranceSignals(has_contiguous_speech=None)
    elif case == "literal":
        text = "literal click dismiss"
    elif case == "speaking":
        pending_app.audio_coordinator = SimpleNamespace(is_speaking=True)
    elif case in {"echo", "model"}:
        capture_path = case
    elif case == "unopened":
        question.opened = False
    elif case == "expired":
        question.deadline = time.monotonic() - 1
    elif case == "ineligible":
        question.runner.eligible.return_value = False
    elif case == "operation_changed":
        monkeypatch.setattr(ava_pending.policy, "pending_operation", lambda: object())
    consume = Mock(wraps=ava_pending.consume)
    monkeypatch.setattr(ava_pending, "consume", consume)
    assert voice.consume_before_pending(pending_app, text, signals,
                                        self_hear=case == "self_hear", capture_path=capture_path)
    bridge.dispatch.assert_not_called()
    consume.assert_called_once_with(pending_app, text, capture_path=capture_path)
    question.runner.run.assert_not_called()
    if case in {"expired", "ineligible", "operation_changed"}:
        assert pending_app._ava_question is None
        question.runner.expired.assert_called_once()
    else:
        assert pending_app._ava_question is question


def test_pending_control_requires_exact_visible_enabled_action(pending_app, qapp, monkeypatch):
    from samsara.bug_report.preview_qt import ReportPreview
    from samsara.ui.button_voice_qt import VisibleActions
    window = ReportPreview("Synthetic report")
    window.show()
    qapp.processEvents()
    bridge = VisibleActions(qapp, lambda fn: fn())
    monkeypatch.setattr(voice, "_bridge", bridge)
    question = pending_app._ava_question
    before = vars(question).copy()
    try:
        for phrase in ("we should click cancel later", "click nonexistent control"):
            assert voice.consume_before_pending(pending_app, phrase, GOOD,
                                                self_hear=False, capture_path="toggle_command")
            assert window.isVisible()
        cancel = next(a.target() for a in bridge.snapshot if a.label == "Cancel")
        cancel.setEnabled(False)
        bridge.refresh()
        assert voice.consume_before_pending(pending_app, "click cancel", GOOD,
                                            self_hear=False, capture_path="toggle_command")
        assert window.isVisible()
        cancel.setEnabled(True)
        cancel.hide()
        bridge.refresh()
        assert voice.consume_before_pending(pending_app, "click cancel", GOOD,
                                            self_hear=False, capture_path="toggle_command")
        assert window.isVisible()
        cancel.show()
        qapp.processEvents()
        bridge.refresh()
        assert voice.consume_before_pending(pending_app, "click cancel", GOOD,
                                            self_hear=False, capture_path="toggle_command")
        assert not window.isVisible()
        assert pending_app._ava_question is question
        assert vars(question) == before
    finally:
        bridge.timer.stop()
        window.close()
        bridge.deleteLater()


def test_without_question_clear_draft_keeps_native_recovery(manager, monkeypatch):
    from samsara import message_lifetime
    monkeypatch.setattr(message_lifetime, "consume_acknowledgement", lambda *a, **k: False)
    bridge = SimpleNamespace(dispatch=Mock(return_value=True))
    monkeypatch.setattr(voice, "_bridge", bridge)
    manager.dispatch_utterance("literal Synthetic draft.", GOOD)
    before = manager.dictate_pending_buffer
    assert not voice.consume_before_pending(SimpleNamespace(), "clear draft", GOOD,
                                            self_hear=False, capture_path="toggle_command")
    bridge.dispatch.assert_not_called()
    assert manager.dispatch_utterance("clear draft", GOOD).kind == "dictate_draft_cleared"
    # Recovery is native only when there is no matching visible control.
    bridge.dispatch.return_value = False
    assert manager.dispatch_utterance("bring it back", GOOD).kind == "dictate_draft_recovered"
    assert manager.dictate_pending_buffer == before
