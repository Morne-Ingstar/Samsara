from copy import deepcopy

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from tests.test_shortcuts_help_order_1075 import window


PICKER_KEYS = {"Ava": "ava_key_picker", "Shortcuts": "ava_mode_key"}


def capture(button, key):
    button.click()
    assert button._capturing
    QTest.keyClick(button, key)
    assert not button._capturing


@pytest.mark.parametrize("first, second", [("Ava", "Shortcuts"), ("Shortcuts", "Ava")])
def test_ava_key_pick_is_shared_before_apply(window, first, second):
    before = deepcopy(window.app.config)
    window.show_tab(first)
    first_picker = window._widgets[PICKER_KEYS[first]]
    capture(first_picker, Qt.Key.Key_F9)

    window.show_tab(second)
    second_picker = window._widgets[PICKER_KEYS[second]]
    assert second_picker is not first_picker
    assert second_picker.combo == "f9"
    assert second_picker.text() == first_picker.text() == "F9"

    capture(second_picker, Qt.Key.Key_F11)
    assert first_picker.combo == second_picker.combo == "f11"
    assert first_picker.text() == second_picker.text() == "F11"
    assert window.app.config == before
    window.app.save_config.assert_not_called()

    window._apply_button.click()
    window.app.save_config.assert_called_once()
    assert window.app.config["ava_mode_key"] == "f11"


def test_ava_key_conflict_uses_the_same_pending_shortcuts_on_both_pages(window):
    window.show_tab("Shortcuts")
    cancel = window._widgets["cancel_hotkey"]
    capture(cancel, Qt.Key.Key_F10)
    assert cancel.combo == "f10"
    warning = "Already used by Cancel recording; choose another key."

    for page in PICKER_KEYS:
        window.show_tab(page)
        picker = window._widgets[PICKER_KEYS[page]]
        row = window._rows_by_widget[PICKER_KEYS[page]]
        capture(picker, Qt.Key.Key_F10)
        label = picker._hold_reason_label
        assert row.isAncestorOf(label) and label.isVisible()
        assert label.text() == warning
        assert picker.combo == "right_alt"
        assert picker.text() == "Right Alt"

    # A pending edit from Ava is also visible to other shortcut validators.
    window.show_tab("Ava")
    capture(window._widgets["ava_key_picker"], Qt.Key.Key_F9)
    window.show_tab("Shortcuts")
    capture(cancel, Qt.Key.Key_F9)
    assert cancel._hold_reason_label.isVisible()
    assert cancel._hold_reason_label.text() == "Already used by Ava; choose another key."
    assert cancel.combo == "f10"
