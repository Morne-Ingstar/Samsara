"""T25: compact Ways keeps real calibration reachable outside closed cards."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton, QWidget

from samsara.ui import mic_setup_wizard_qt, tutorial_qt, wake_setup_qt


class Calibration(QObject):
    finished = Signal(int)


@pytest.fixture
def review(qapp, monkeypatch):
    app = SimpleNamespace(config={'wake_word_enabled': True, 'microphone': 7,
        'command_mode': {'enabled': True, 'mode': 'toggle'}})
    window = tutorial_qt.TutorialWindow(app)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    calls = []

    def open_calibration(actual_app, parent=None):
        assert actual_app is app and parent is window
        guide = Calibration(window)
        calls.append(guide)
        return guide

    monkeypatch.setattr(mic_setup_wizard_qt, 'open_wake_calibration', open_calibration)
    window._open_optional('ways')
    window.show()
    qapp.processEvents()
    yield window, app, calls
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_calibration_footer_keyboard_route_and_readiness(review, qapp):
    window, app, calls = review
    button = window._wake_calibration_button
    frame = window.findChild(QWidget, 'tutorialWay_wake')
    assert button.isVisible() and button.isEnabled()
    assert not frame.isAncestorOf(button)
    assert button.width() >= 44 and button.height() >= 44
    instruction = frame.findChild(QWidget, 'tutorialWakeInstruction')
    assert instruction.text() == wake_setup_qt.CALIBRATION_NOTE
    button.setFocus()
    QTest.keyClick(button, Qt.Key.Key_Space)
    assert len(calls) == 1
    app.config['wake_word_config'] = {'audio': {
        'measured_noise_floor': 0.01, 'calibrated_microphone': 7}}
    calls[0].finished.emit(0)
    qapp.processEvents()
    assert window._wake_calibration_button is button
    assert not button.isVisible()
    assert instruction.text() == instruction.property('wakeInstruction')
    window._return_to_done()
    window._open_optional('ways')
    qapp.processEvents()
    assert not button.isVisible()
    assert window.findChildren(QPushButton, 'tutorialWakeCalibrate') == [button]


def test_cancelled_calibration_after_navigation_stays_contextual(review, qapp):
    window, app, calls = review
    button = window._wake_calibration_button
    button.click()
    assert len(calls) == 1
    window._return_to_done()
    assert not button.isVisible()
    calls[0].finished.emit(-1)
    qapp.processEvents()
    assert not button.isVisible()
    window._open_optional('ways')
    qapp.processEvents()
    assert button.isVisible()
    assert window.findChildren(QPushButton, 'tutorialWakeCalibrate') == [button]
    assert window.height() <= window._default_window_height
    assert window._current_page.verticalScrollBar().maximum() == 0
