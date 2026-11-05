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
    # 1040 replaces the dropdown with one bounded, labelled seconds slider.
    assert [mark.text() for mark in combo.stop_labels] == ['Standard', 'Relaxed', 'Unhurried']
    proof = Path(os.environ.get('PACE_PROOF_DIR', str(tmp_path)))
    proof.mkdir(parents=True, exist_ok=True)
    assert window._rows_by_widget['speech_pace'].grab().save(str(proof / 'pace_row.png'))
    for mark in combo.stop_labels:
        assert mark.fontMetrics().horizontalAdvance(mark.text()) <= mark.width()
    assert combo.slider.minimum() == 50 and combo.slider.maximum() == 800



def test_pace_explanation_is_complete_visible_and_keyboard_driven(settings, qapp):
    window, app = settings
    combo = window._widgets['speech_pace']
    row = window._rows_by_widget['speech_pace']
    detail = row.findChild(QLabel, 'speechPaceExplanation')
    assert detail is not None
    assert detail.isVisible() and detail.wordWrap()
    # 1040 keeps one scope explanation visible for every slider value.
    assert 'Held dictation' in detail.text() and 'recognizer' in detail.text()
    combo.setSeconds(2)
    combo.slider.setFocus()
    QTest.keyClick(combo.slider, Qt.Key.Key_Right)
    qapp.processEvents()
    assert combo.currentText() == '2.05 s · Custom'
    assert 'Held dictation' in detail.text()
    assert app.config['accessibility']['speech_pace'] == 'custom'
    assert detail.height() >= detail.heightForWidth(detail.width())



@pytest.mark.parametrize('preset', ['standard', 'relaxed', 'unhurried', 'custom'])
def test_pace_save_retains_keys_and_other_accessibility_values(settings, preset):
    window, app = settings
    combo = window._widgets['speech_pace']
    # 1040 presets are exact slider stops; preserve the old custom dictionary.
    seconds = {'standard': 2, 'relaxed': 5, 'unhurried': 8, 'custom': 3.75}[preset]
    combo.setSeconds(seconds)
    window._apply_and_close()
    acc = app.config['accessibility']
    assert acc['speech_pace'] == preset
    assert acc['speech_pause_s'] == seconds
    assert acc['ava_captions'] is True and acc['ava_mute_audio'] is False
    assert acc['speech_pace_custom'] == {'silence_threshold': 9.0}
