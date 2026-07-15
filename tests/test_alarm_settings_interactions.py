from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
)

import dictation
import samsara.ui.reminder_toast as toast_module
import samsara.ui.settings_qt as settings_module
from samsara.ui.reminder_toast import _ToastWindow
from samsara.ui.settings_qt import _AlarmHotkeyButton, _SettingsWindow


class _FakeAlarmManager:
    def __init__(self, config):
        self.config = config
        self.nagging_alarm_id = None
        self.played = []

    @property
    def items(self):
        return self.config["alarms"]["items"]

    def get_alarm(self, alarm_id):
        return next(
            (
                alarm for alarm in self.items
                if alarm.get("id") == alarm_id or alarm.get("name") == alarm_id
            ),
            None,
        )

    def get_next_trigger_at(self, _alarm_id):
        return None

    def get_stats(self, _alarm_id):
        return {"current_streak": 0, "best_streak": 0}

    def toggle_alarm(self, alarm_id):
        alarm = self.get_alarm(alarm_id)
        alarm["enabled"] = not alarm["enabled"]
        return alarm["enabled"]

    def update_alarm(self, alarm_id, **updates):
        self.get_alarm(alarm_id).update(updates)
        return True

    def play_sound(self, alarm):
        self.played.append(alarm["id"])


class _AlarmSettingsHarness:
    _selected_alarm_id = _SettingsWindow._selected_alarm_id
    _populate_alarms_table = _SettingsWindow._populate_alarms_table
    _toggle_selected_alarm = _SettingsWindow._toggle_selected_alarm
    _test_selected_alarm = _SettingsWindow._test_selected_alarm
    _alarm_dialog_save = _SettingsWindow._alarm_dialog_save

    def __init__(self):
        config = {
            "alarms": {
                "items": [
                    {
                        "id": "water",
                        "name": "Water",
                        "interval_minutes": 20,
                        "sound": "alarm",
                        "enabled": True,
                    }
                ]
            }
        }
        self.app = SimpleNamespace(config=config)
        self.app.alarm_manager = _FakeAlarmManager(config)


def _alarm_table():
    return QTableWidget(0, 5)


def test_toggle_refresh_keeps_same_alarm_selected_for_repeated_toggle(qapp):
    harness = _AlarmSettingsHarness()
    table = _alarm_table()
    harness._populate_alarms_table(table)
    table.selectRow(0)

    harness._toggle_selected_alarm(table)
    assert harness._selected_alarm_id(table) == "water"
    assert harness.app.alarm_manager.get_alarm("water")["enabled"] is False

    harness._toggle_selected_alarm(table)
    assert harness._selected_alarm_id(table) == "water"
    assert harness.app.alarm_manager.get_alarm("water")["enabled"] is True


def test_edit_save_keeps_alarm_selected_so_test_works_next(
    qapp, monkeypatch
):
    harness = _AlarmSettingsHarness()
    table = _alarm_table()
    harness._populate_alarms_table(table)
    table.selectRow(0)

    name = QLineEdit("Drink water")
    interval = QSpinBox()
    interval.setRange(1, 480)
    interval.setValue(30)
    sound = QComboBox()
    sound.addItem("alarm")
    enabled = QCheckBox()
    enabled.setChecked(True)
    dialog = Mock()
    monkeypatch.setattr(settings_module.QMessageBox, "information", Mock())

    harness._alarm_dialog_save(
        dialog, "water", harness.app.alarm_manager, table,
        name, interval, sound, enabled,
    )

    assert harness._selected_alarm_id(table) == "water"
    assert table.item(table.currentRow(), 1).text() == "Drink water"
    monkeypatch.setattr(
        settings_module.thread_registry,
        "spawn",
        lambda _name, callback, daemon=True: callback(),
    )
    harness._test_selected_alarm(table)
    assert harness.app.alarm_manager.played == ["water"]


def test_alarm_hotkeys_are_uppercase_action_labelled_and_larger(qapp):
    button = _AlarmHotkeyButton("f7", "Complete alarm")
    assert button.text() == "F7 — Complete alarm"
    assert button.minimumHeight() >= 38
    assert "Complete alarm shortcut: F7" == button.accessibleName()

    button._held = {"f8"}
    button._finish_capture()
    assert button.combo == "f8"
    assert button.text() == "F8 — Complete alarm"


def test_actionable_toast_has_clickable_buttons_without_keyboard_focus(qapp):
    dismissed = Mock()
    completed = Mock()
    window = _ToastWindow()
    try:
        window.add_row("Alarm: Water", "Choose an action", dismissed, completed)
        qapp.processEvents()
        buttons = {button.text(): button for button in window.findChildren(QPushButton)}

        assert set(buttons) == {"Dismiss", "Complete"}
        assert buttons["Dismiss"].focusPolicy() == Qt.FocusPolicy.NoFocus
        assert buttons["Complete"].focusPolicy() == Qt.FocusPolicy.NoFocus
        assert not bool(window.windowFlags() & Qt.WindowTransparentForInput)

        buttons["Complete"].click()
        qapp.processEvents()
        completed.assert_called_once_with()
        dismissed.assert_not_called()
        assert window.isHidden()
    finally:
        window.stop()


def test_generic_toast_remains_click_through(qapp):
    window = _ToastWindow()
    try:
        window.add_row("Reminder", "No action required")
        assert bool(window.windowFlags() & Qt.WindowTransparentForInput)
        assert window.findChildren(QPushButton) == []
    finally:
        window.stop()

def test_toast_rebuild_does_not_duplicate_action_buttons(qapp):
    window = _ToastWindow()
    try:
        window.add_row("Alarm: Water", "Choose an action", Mock(), Mock())
        window.add_row("Reminder", "A second row")
        qapp.processEvents()
        qapp.sendPostedEvents(None, 0)
        qapp.processEvents()

        buttons = [
            button.text() for button in window.findChildren(QPushButton)
            if button.text() in {"Dismiss", "Complete"}
        ]
        assert sorted(buttons) == ["Complete", "Dismiss"]
    finally:
        window.stop()



def test_alarm_notification_wires_to_manager_actions(monkeypatch):
    toast = Mock()
    toast.show.return_value = True
    monkeypatch.setattr(toast_module, "get_toast", lambda: toast)
    manager = SimpleNamespace(complete=Mock(), dismiss=Mock())

    dictation.DictationApp._show_alarm_notification(
        SimpleNamespace(alarm_manager=manager), {"name": "Water"}
    )

    kwargs = toast.show.call_args.kwargs
    assert kwargs["on_complete"] is manager.complete
    assert kwargs["on_dismiss"] is manager.dismiss
