"""1279: the first microphone scan offers focus without taking it back."""
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from samsara.ui import first_run_wizard_qt as wizard


@pytest.fixture
def mic_setup(qapp, monkeypatch, tmp_path):
    devices = [{"id": 1, "name": "Desk microphone"}, {"id": 2, "name": "Headset"}]
    monkeypatch.setattr(wizard, "list_microphones", lambda: list(devices))
    monkeypatch.setattr(wizard.thread_registry, "spawn", Mock())
    monkeypatch.setattr(wizard, "register_notifications", lambda callback: None)
    monkeypatch.setattr(wizard, "capture_endpoints", frozenset)
    monkeypatch.setattr(wizard._WizardWindow, "_start_meter", lambda self: None)

    window = wizard._WizardWindow(tmp_path / "config.json")
    window.show()
    qapp.setActiveWindow(window)
    qapp.processEvents()
    yield window, devices
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_initial_scan_names_and_focuses_picker(mic_setup):
    window, _ = mic_setup
    window._next_btn.click()
    assert not window._mic_combo.isEnabled()

    window._load_mics()

    assert window._mic_combo.accessibleName() == "Microphone"
    assert window._mic_combo.hasFocus()
    QTest.keyClick(window._mic_combo, Qt.Key.Key_Down)
    assert window._mic_combo.currentText() == "Desk microphone"


def test_scan_on_welcome_waits_to_focus_until_microphone_step(mic_setup):
    window, _ = mic_setup
    assert window._next_btn.hasFocus()

    window._load_mics()
    assert window._next_btn.hasFocus()
    window._next_btn.click()

    assert window._mic_combo.hasFocus()


@pytest.mark.parametrize("return_to_start", [False, True])
def test_scan_respects_keyboard_navigation(mic_setup, return_to_start):
    window, _ = mic_setup
    window._next_btn.click()
    starting_focus = window.focusWidget()
    QTest.keyClick(starting_focus, Qt.Key.Key_Tab)
    assert window.focusWidget() is not starting_focus
    if return_to_start:
        QTest.keyClick(window.focusWidget(), Qt.Key.Key_Backtab)
        assert window.focusWidget() is starting_focus
    chosen_focus = window.focusWidget()

    window._load_mics()

    assert window.focusWidget() is chosen_focus


def test_scan_after_leaving_microphone_does_not_move_focus(mic_setup):
    window, _ = mic_setup
    window._next_btn.click()
    window._back_btn.click()
    assert window._next_btn.hasFocus()

    window._load_mics()

    assert window._next_btn.hasFocus()


def test_empty_scan_and_later_refresh_do_not_move_focus(mic_setup):
    window, devices = mic_setup
    window._next_btn.click()
    starting_focus = window.focusWidget()
    devices.clear()

    window._load_mics()
    assert not window._mic_combo.isEnabled()
    assert window.focusWidget() is starting_focus

    refresh = next(button for button in window._mic_page.findChildren(QPushButton)
                   if button.text() == "Refresh")
    refresh.setFocus()
    refresh.click()
    window._mics = [{"id": 1, "name": "Desk microphone"}]
    window._mic_result.emit("_refresh_done_", "")

    assert window._mic_combo.isEnabled()
    assert refresh.hasFocus()
