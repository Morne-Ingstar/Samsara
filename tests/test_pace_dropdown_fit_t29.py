"""Focused speech-pace dropdown readability and persistence contract."""
import copy
import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from samsara.ui.settings_qt import _SettingsWindow
from tests.test_settings_pages import _StubApp, _schema_defaults


@pytest.fixture
def settings(qapp, qt_widget_scope):
    config = copy.deepcopy(_schema_defaults())
    config['accessibility'] = {'speech_pace': 'custom', 'ava_captions': True, 'ava_mute_audio': False,
                               'speech_pace_custom': {'silence_threshold': 9.0}}
    app = _StubApp(config)
    with qt_widget_scope(_SettingsWindow(app)) as window:
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        for index in range(window._sidebar.count()):
            if window._sidebar.item(index).text() == 'Dictation':
                window._sidebar.setCurrentRow(index)
                break
        window.resize(900, 700)
        window.show()
        qapp.processEvents()
        yield window, app


def test_pace_labels_fit_in_closed_field_and_open_popup(settings, qapp, tmp_path):
    window, _ = settings
    combo = window._widgets['speech_pace']
    assert [combo.itemText(i) for i in range(combo.count())] == [
        'Standard', 'Relaxed', 'Unhurried', 'Custom']
    proof = Path(os.environ.get('PACE_PROOF_DIR', str(tmp_path)))
    proof.mkdir(parents=True, exist_ok=True)
    assert window._rows_by_widget['speech_pace'].grab().save(str(proof / 'pace_row.png'))
    combo.showPopup()
    qapp.processEvents()
    try:
        for index in range(combo.count()):
            rect = combo.view().visualRect(combo.model().index(index, 0))
            needed = combo.fontMetrics().horizontalAdvance(combo.itemText(index))
            assert needed + 36 <= rect.width()
            assert needed + 36 <= combo.width()
        assert combo.view().window().grab().save(str(proof / 'pace_popup.png'))
    finally:
        combo.hidePopup()


def test_pace_explanation_is_complete_visible_and_keyboard_driven(settings, qapp):
    window, app = settings
    combo = window._widgets['speech_pace']
    row = window._rows_by_widget['speech_pace']
    detail = row.findChild(QLabel, 'speechPaceExplanation')
    assert detail is not None
    assert detail.isVisible() and detail.wordWrap()
    assert detail.text() == 'Custom — uses the advanced timing values you choose.'
    assert combo.accessibleDescription() == detail.text()
    combo.setCurrentIndex(0)
    combo.setFocus()
    QTest.keyClick(combo, Qt.Key.Key_Down)
    qapp.processEvents()
    assert combo.currentText() == 'Relaxed'
    assert detail.text() == 'Relaxed — gives pauses between phrases more room.'
    assert combo.accessibleDescription() == detail.text()
    assert app.config['accessibility']['speech_pace'] == 'custom'
    assert detail.height() >= detail.heightForWidth(detail.width())


@pytest.mark.parametrize('preset', ['standard', 'relaxed', 'unhurried', 'custom'])
def test_pace_save_retains_keys_and_other_accessibility_values(settings, preset):
    window, app = settings
    combo = window._widgets['speech_pace']
    index = combo.findData(preset)
    assert index >= 0
    combo.setCurrentIndex(index)
    window._apply_and_close()
    assert app.config['accessibility'] == {
        'speech_pace': preset, 'ava_captions': True, 'ava_mute_audio': False,
        'speech_pace_custom': {'silence_threshold': 9.0}}
