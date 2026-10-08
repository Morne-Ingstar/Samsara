"""Synthetic Qt accessibility checks; no desktop bootstrap or live profile."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QLineEdit, QPushButton


def _accessible(widget, role, name=None):
    interface = QAccessible.queryAccessibleInterface(widget)
    assert interface is not None
    actual = interface.text(QAccessible.Text.Name)
    assert actual and actual != widget.metaObject().className()
    if name is not None:
        assert actual == name
    assert interface.role() == role
    return interface


@pytest.fixture
def announcements(monkeypatch):
    from samsara.ui.live_surface_accessibility import LiveSurfaceAnnouncer

    messages = []
    original = LiveSurfaceAnnouncer.announce_transition

    def record(self, transition, message):
        sent = original(self, transition, message)
        if sent:
            messages.append(message)
        return sent

    monkeypatch.setattr(LiveSurfaceAnnouncer, "announce_transition", record)
    return messages


@pytest.fixture
def task_store(monkeypatch):
    from samsara.ui import task_overlay

    task = dict(id="synthetic", text="Synthetic task", completed=False)
    monkeypatch.setattr(task_overlay.tasks_store, "get_all", lambda: [task])
    for method in ("add_task", "complete_task", "uncomplete_task", "remove_task"):
        monkeypatch.setattr(task_overlay.tasks_store, method, Mock())
    return task_overlay


def test_task_names_roles_and_checked_state(qapp, task_store):
    """F082/F083/F102: identify the field, checkbox and task-specific delete."""
    window = task_store._TaskWindow()
    row = window.findChild(task_store._TaskRow)
    try:
        _accessible(window._input, QAccessible.Role.EditableText, window._input.placeholderText())
        checkbox = _accessible(row._check, QAccessible.Role.CheckBox, row._label.text())
        _accessible(row._del, QAccessible.Role.Button, "Delete Synthetic task")
        assert not checkbox.state().checked
        # Use a standalone row so the callback records state without a list rebuild.
        standalone = task_store._TaskRow(row._task, Mock())
        standalone._check.click()
        assert _accessible(standalone._check, QAccessible.Role.CheckBox).state().checked
        task_store.tasks_store.complete_task.assert_called_once_with("synthetic")
        standalone.deleteLater()
    finally:
        window.hide()
        window.deleteLater()


@pytest.mark.parametrize("activation", ["click", Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space])
def test_task_delete_tab_walk_and_activation(qapp, task_store, activation):
    """F084: Tab reaches Delete; keys invoke the same store callback as click."""
    window = task_store._TaskWindow()
    try:
        window.show()
        window.activateWindow()
        qapp.processEvents()
        window._input.setFocus()
        row = window.findChild(task_store._TaskRow)
        visited = []
        for _ in range(12):
            QTest.keyClick(qapp.focusWidget(), Qt.Key_Tab)
            qapp.processEvents()
            visited.append(qapp.focusWidget())
            if qapp.focusWidget() is row._del:
                break
        assert row._check in visited
        assert qapp.focusWidget() is row._del
        if activation == "click":
            row._del.click()
        else:
            QTest.keyClick(row._del, activation)
        task_store.tasks_store.remove_task.assert_called_once_with("synthetic")
    finally:
        window.hide()
        window.deleteLater()


def _surface(kind, qapp, monkeypatch):
    if kind == "hint":
        from samsara.ui.hint_toast import HintToast
        return HintToast("Synthetic hint message"), lambda w: w.show(), QAccessible.Role.Client
    if kind == "reminder":
        from samsara.ui.reminder_toast import _ToastWindow
        return _ToastWindow(), lambda w: w.add_row("Synthetic reminder", "Synthetic message", Mock(), Mock()), QAccessible.Role.Client
    if kind == "status":
        from samsara.ui.status_overlay import _StatusWindow
        return _StatusWindow(None, None), lambda w: w.show(), QAccessible.Role.Window
    if kind == "indicator":
        from samsara.ui.listening_indicator import ListeningIndicator
        return ListeningIndicator(), lambda w: w.show_outcome("Synthetic result", "success"), QAccessible.Role.Client
    from samsara.ui import numbers_overlay_qt as numbers
    monkeypatch.setattr(numbers, "_ensure_dpi_thread_context", lambda: None)
    return numbers.NumbersOverlayWindow([[120, 110, 26, 22, "1"]], qapp.primaryScreen()), lambda w: w.show(), QAccessible.Role.Client


@pytest.mark.parametrize("kind", ["hint", "reminder", "status", "indicator", "numbers"])
def test_passive_show_preserves_editor_focus_and_exposes_message(qapp, monkeypatch, announcements, kind):
    """F096/F105/F106/F107/F111: named, announced and never focus stealing."""
    window, show, role = _surface(kind, qapp, monkeypatch)
    editor = QLineEdit()
    try:
        editor.show()
        editor.activateWindow()
        editor.setFocus()
        qapp.processEvents()
        assert qapp.focusWidget() is editor
        before = qapp.focusWidget()
        show(window)
        qapp.processEvents()
        assert qapp.focusWidget() is before
        assert window.windowFlags() & Qt.WindowDoesNotAcceptFocus
        interface = _accessible(window, role)
        assert interface.text(QAccessible.Text.Description)
        assert announcements
        for button in window.findChildren(QPushButton):
            _accessible(button, QAccessible.Role.Button)
            assert button.focusPolicy() == Qt.NoFocus
        for checkbox in window.findChildren(QCheckBox):
            _accessible(checkbox, QAccessible.Role.CheckBox)
            assert checkbox.focusPolicy() == Qt.NoFocus
    finally:
        window.hide()
        window.deleteLater()
        editor.hide()
        editor.deleteLater()


@pytest.mark.parametrize("kind,name", [("hint", "Dismiss hint"), ("status", "Close status overlay")])
def test_close_actions_have_meaningful_button_names(qapp, monkeypatch, kind, name):
    """F100/F101: the icon still looks identical, but speaks its purpose."""
    window, _show, _role = _surface(kind, qapp, monkeypatch)
    try:
        _accessible(window.findChild(QPushButton), QAccessible.Role.Button, name)
    finally:
        window.deleteLater()


@pytest.mark.parametrize("kind", ["hint", "reminder", "status"])
def test_passive_mouse_actions_still_work_without_activating(qapp, monkeypatch, kind):
    window, show, _role = _surface(kind, qapp, monkeypatch)
    editor = QLineEdit()
    clicked = Mock()
    try:
        editor.show()
        editor.activateWindow()
        editor.setFocus()
        qapp.processEvents()
        show(window)
        qapp.processEvents()
        button = window.findChild(QPushButton)
        button.clicked.connect(clicked)
        QTest.mouseClick(button, Qt.LeftButton)
        qapp.processEvents()
        clicked.assert_called_once()
        assert qapp.focusWidget() is editor
    finally:
        window.hide()
        window.deleteLater()
        editor.hide()
        editor.deleteLater()


def test_hint_announces_once_per_semantic_message(qapp, announcements):
    from samsara.ui.hint_toast import HintToast

    window = HintToast("Synthetic hint message")
    try:
        window.show()
        window.update()
        qapp.processEvents()
        assert announcements == ["Synthetic hint message"]
    finally:
        window.hide()
        window.deleteLater()


def test_reminder_batch_announces_only_new_rows_and_clears_description(qapp, announcements):
    from samsara.ui.reminder_toast import _ToastWindow

    window = _ToastWindow()
    try:
        window.add_row("First reminder", "First message")
        window.add_row("Second reminder", "Second message")
        window._rebuild_rows()
        assert announcements == ["First reminder: First message", "Second reminder: Second message"]
        assert "First message" in window.accessibleDescription()
        assert "Second message" in window.accessibleDescription()
        window._on_faded_out()
        assert not window.accessibleDescription()
    finally:
        window.stop()
        window.deleteLater()


def test_status_refresh_does_not_announce_countdown_or_timestamp(qapp, announcements, monkeypatch):
    from samsara.ui import status_overlay

    reminders = [dict(name="Synthetic reminder", schedule=dict(type="interval", minutes=30))]
    remaining = ["in 5 min"]
    nm = SimpleNamespace(get_all_reminders=lambda: reminders)
    am = SimpleNamespace(items=[dict(id="alarm", name="Synthetic alarm", enabled=True)],
                         is_nagging=lambda: False, nagging_alarm_id=None,
                         get_next_trigger_at=lambda _id: 12345, get_stats=lambda _id: {})
    monkeypatch.setattr(status_overlay, "_format_alarm_remaining", lambda *a, **kw: remaining[0])
    window = status_overlay._StatusWindow(nm, am)
    try:
        assert not announcements  # Constructing a hidden surface stays quiet.
        window.show()
        remaining[0] = "in 4 min"
        window._render()
        assert len(announcements) == 1
        assert "in 4 min" in window.accessibleDescription()
        reminders.append(dict(name="Another reminder", schedule={}))
        window._render()
        assert len(announcements) == 2
        window.hide()
        window.show()
        assert len(announcements) == 3
    finally:
        window.hide()
        window.deleteLater()


def test_indicator_state_outcome_and_device_loss_ignore_animation(qapp, announcements):
    from samsara.ui.listening_indicator import ListeningIndicator

    window = ListeningIndicator()
    try:
        window.show()
        window.set_listening(True)
        assert "recording" in window.accessibleDescription()
        window.show_outcome("Synthetic result", "success")
        assert "Synthetic result" in window.accessibleDescription()
        before = list(announcements)
        window._pulse_tick()
        window.update()
        assert announcements == before
        window.set_device_lost(True)
        assert "mic lost" in window.accessibleDescription()
        before = list(announcements)
        window.set_device_lost(True)
        assert announcements == before
        window.set_device_lost(False)
        assert "mic lost" not in window.accessibleDescription()
        assert announcements[-1] == "Microphone reconnected"
    finally:
        window.destroy()
        window.deleteLater()


def test_hidden_indicator_reports_device_reconnection(qapp, announcements):
    from samsara.ui.listening_indicator import ListeningIndicator

    window = ListeningIndicator()
    try:
        window.set_device_lost(True)
        window.set_device_lost(False)
        assert not window.isVisible()
        assert announcements == ["mic lost", "Microphone reconnected"]
    finally:
        window.destroy()
        window.deleteLater()


def test_numbers_mapping_cells_caption_and_repeat_updates(qapp, monkeypatch, announcements):
    from samsara.ui import numbers_overlay_qt as numbers

    monkeypatch.setattr(numbers, "_ensure_dpi_thread_context", lambda: None)
    labels = [[120, 110, 26, 22, "1"]]
    window = numbers.NumbersOverlayWindow(labels, qapp.primaryScreen())
    try:
        window.update_labels(labels, "Synthetic fallback", cells=[(0, 0, 200, 150)])
        window.show()
        assert "Synthetic fallback" in window.accessibleDescription()
        assert "1: target at 120, 110" in window.accessibleDescription()
        assert "1: cell 0, 0, 200, 150" in window.accessibleDescription()
        window.update_labels(labels, "Synthetic fallback", cells=[(0, 0, 200, 150)])
        window.update()
        assert len(announcements) == 1
        window.set_cells([(0, 0, 300, 200)])
        assert "1: cell 0, 0, 300, 200" in window.accessibleDescription()
        assert len(announcements) == 2
    finally:
        window.hide()
        window.deleteLater()
