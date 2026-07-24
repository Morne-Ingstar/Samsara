"""Targeted tests for first-run wizard custom choice indicator widget."""

from PySide6.QtWidgets import QButtonGroup

from samsara.ui.first_run_wizard_qt import _WizardChoiceIndicator


def test_choice_indicator_preserves_checked_state_api(qapp):
    indicator = _WizardChoiceIndicator()

    states: list[bool] = []
    indicator.toggled.connect(lambda checked: states.append(checked))

    assert indicator.isChecked() is False
    indicator.setChecked(True)
    assert indicator.isChecked() is True
    indicator.setChecked(False)
    assert indicator.isChecked() is False
    assert states == [True, False]


def test_choice_indicator_works_with_qbuttongroup_exclusive(qapp):
    group = QButtonGroup()
    first = _WizardChoiceIndicator()
    second = _WizardChoiceIndicator()
    first.setProperty("_value", "first")
    second.setProperty("_value", "second")
    group.addButton(first, 1)
    group.addButton(second, 2)

    first.setChecked(True)
    second.setChecked(True)

    assert first.isChecked() is False
    assert second.isChecked() is True
