"""First-use layout and loss-safe quit decision regressions; no app bootstrap."""
from samsara.log import describe_text, safe_exception
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest


@pytest.mark.parametrize("line_count", [40, 41, 44, 180])
def test_full_long_draft_follows_true_end_and_has_right_gutter(qapp, line_count):
    from PySide6.QtGui import QTextCursor
    from samsara.live_surface.model import (CaptureState, DocumentState, Notice,
        NoticeKind, PresentationState, SurfaceView, VisibleForm)
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    view = SurfaceView(VisibleForm.LIVE, CaptureState.RECORDING, None,
        DocumentState.EDITABLE_DRAFT, PresentationState.COMPACT,
        Notice(NoticeKind.NONE, "", None), "d1", 1,
        "\n".join(f"Line {n}: a long draft with several words to wrap across the card." for n in range(line_count)), "", True)
    widget = LiveSurfaceWidget(view, reduced_motion=True)
    widget.show()
    for _ in range(8):
        qapp.processEvents()
    text = widget._transcript
    cursor = text.cursorForPosition(text.viewport().rect().topLeft())
    line = cursor.block().layout().lineForTextPosition(cursor.positionInBlock())
    bar = text.verticalScrollBar()
    # FINISH IT 2: an incomplete top line is allowed when following. The last
    # line and follow position must never be sacrificed for top alignment.
    assert bar.value() == bar.maximum()
    assert widget._follow_latest
    end = text.textCursor()
    end.movePosition(QTextCursor.MoveOperation.End)
    assert text.viewport().rect().contains(text.cursorRect(end))
    assert line.isValid()
    assert line.x() + line.naturalTextWidth() <= text.viewport().width() - 8
    assert "QScrollBar::handle:vertical" in text.verticalScrollBar().styleSheet()
    widget.hide()


def test_tasks_empty_line_has_room_for_font_and_padding(qapp, monkeypatch):
    from samsara.ui import task_overlay
    monkeypatch.setattr(task_overlay.tasks_store, "get_all", lambda: [])
    widget = task_overlay._TaskWindow()
    widget.show()
    qapp.processEvents()
    item = widget._list.item(0)
    label = widget._list.itemWidget(item)
    assert item.sizeHint().height() >= label.fontMetrics().height() + 24
    assert label.contentsRect().height() >= label.fontMetrics().height()
    widget.hide()


def _quit_method():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "dictation.py").read_text(encoding="utf-8-sig"))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "_resolve_parked_draft_before_quit")
    namespace = {"logger": Mock(), "describe_text": describe_text, "safe_exception": safe_exception}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<quit decision>", "exec"), namespace)
    return namespace[method.name]


@pytest.mark.parametrize("choice,expected,commits,discards", [
    ("send", True, 1, 0), ("discard", True, 0, 1), ("keep", False, 0, 0)])
def test_quit_only_discards_on_discard_choice(monkeypatch, choice, expected, commits, discards):
    from samsara.ui import quit_draft_dialog_qt as dialog
    monkeypatch.setattr(dialog, "ask_quit_draft", lambda: choice)
    manager = SimpleNamespace(has_parked_hold=True, dictate_pending_buffer="Synthetic draft.",
        commit_pending_dictation=Mock(return_value=SimpleNamespace(kind="dictate_committed")),
        confirm_clear_draft=Mock())
    app = SimpleNamespace(_live_surface_hold_parking_enabled=lambda: True,
                          _session_mode_manager=manager)
    assert _quit_method()(app) is expected
    assert manager.commit_pending_dictation.call_count == commits
    assert manager.confirm_clear_draft.call_count == discards


def test_quit_failed_send_preserves_draft(monkeypatch):
    from samsara.ui import quit_draft_dialog_qt as dialog
    monkeypatch.setattr(dialog, "ask_quit_draft", lambda: "send")
    manager = SimpleNamespace(has_parked_hold=True, dictate_pending_buffer="Synthetic draft.",
        commit_pending_dictation=Mock(return_value=SimpleNamespace(kind="failed")),
        confirm_clear_draft=Mock())
    app = SimpleNamespace(_live_surface_hold_parking_enabled=lambda: True, _session_mode_manager=manager)
    assert not _quit_method()(app)
    manager.confirm_clear_draft.assert_not_called()


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Escape])
def test_quit_buttons_name_actions_and_keyboard_keeps_app_open(qapp, key):
    from samsara.ui.quit_draft_dialog_qt import QuitDraftDialog
    dialog = QuitDraftDialog()
    dialog.show()
    qapp.processEvents()
    assert [b.text() for b in dialog.action_buttons] == ["Send it", "Discard it", "Keep Samsara open"]
    QTest.keyClick(dialog, key)
    assert dialog.choice == "keep"


@pytest.mark.parametrize("index,choice", [(0, "send"), (1, "discard"), (2, "keep")])
def test_quit_button_click_selects_only_its_named_action(qapp, index, choice):
    from samsara.ui.quit_draft_dialog_qt import QuitDraftDialog
    dialog = QuitDraftDialog()
    dialog.show()
    qapp.processEvents()
    dialog.action_buttons[index].click()
    assert dialog.choice == choice
