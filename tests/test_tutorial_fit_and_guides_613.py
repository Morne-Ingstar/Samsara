"""Scaled laptop layout and the tutorial's asynchronous guide handoff."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton

from samsara.ui import qt_runtime, tutorial_qt
from samsara.ui.tutorial_qt import TutorialWindow
from tests._theme_stub_app import StubApp


@pytest.fixture
def tutorial(qapp, monkeypatch):
    # Read-only catalogue data, with no plugin discovery or app bootstrap.
    monkeypatch.setattr(tutorial_qt, '_command_count', lambda app: 562)
    window = TutorialWindow(StubApp({'ui': {'live_surface': {'enabled': True}}}))
    window.show()
    QTest.qWait(20)
    yield window
    window.close()
    window.deleteLater()
    qapp.processEvents()


def settle(qapp):
    for _ in range(8):
        qapp.processEvents()


@pytest.mark.parametrize('scale', [1, 1.25, 1.5])
def test_every_step_frame_and_footer_fit_scaled_laptop(tutorial, qapp, scale, monkeypatch):
    area = QRect(40, 30, round(1366 / scale), round(728 / scale))
    monkeypatch.setattr(tutorial, 'screen', lambda: SimpleNamespace(availableGeometry=lambda: area))
    for step in range(len(tutorial._steps)):
        tutorial._step = step
        tutorial._show_step()
        tutorial._fit_available_screen()
        settle(qapp)
        assert area.contains(tutorial.frameGeometry()), (step, tutorial.frameGeometry())
        for button in (tutorial._back_btn, tutorial._next_btn):
            rect = QRect(button.mapTo(tutorial, QPoint()), button.size())
            assert tutorial.rect().contains(rect)
            assert button.height() >= 44


@pytest.mark.parametrize('scale', [1, 1.25, 1.5])
def test_example_keeps_its_natural_width_at_smallest_window(tutorial, qapp, scale, monkeypatch):
    area = QRect(0, 0, round(1366 / scale), round(728 / scale))
    monkeypatch.setattr(tutorial, 'screen', lambda: SimpleNamespace(availableGeometry=lambda: area))
    tutorial._step = next(i for i, (key, _) in enumerate(tutorial._steps) if key == 'try')
    tutorial._show_step()
    settle(qapp)
    tutorial.resize(tutorial.minimumSize())
    page = tutorial._try_page
    page.result.setText('Example complete.')
    page.refresh()
    settle(qapp)
    label = page.result
    assert label.isVisible()
    assert not label.wordWrap()
    assert label.width() >= label.fontMetrics().horizontalAdvance(label.text())
    assert label.height() >= label.fontMetrics().height()
    rect = QRect(label.mapTo(page.widget(), QPoint()), label.size())
    assert rect.right() < page.viewport().width()


def test_done_guides_fully_visible_without_scrolling_at_125(tutorial, qapp, monkeypatch):
    area = QRect(0, 0, 1093, 582)
    monkeypatch.setattr(tutorial, 'screen', lambda: SimpleNamespace(availableGeometry=lambda: area))
    tutorial._try_page.kept = {'level': 'Kept: Brief', 'voice': 'Kept: Local voice',
                               'pace': 'Kept: Standard'}
    tutorial._step = len(tutorial._steps) - 1
    tutorial._show_step()
    tutorial._fit_available_screen()
    settle(qapp)
    page = tutorial._current_page
    assert page.verticalScrollBar().maximum() == 0
    buttons = [b for b in page.findChildren(QPushButton) if b.text() == 'Open →']
    assert len(buttons) == 2
    for button in buttons:
        assert page.viewport().rect().contains(QRect(button.mapTo(page.viewport(), QPoint()), button.size()))
    for label in page.widget().findChildren(QLabel):
        if label.text() and label.isVisible():
            needed = label.heightForWidth(label.width()) if label.hasHeightForWidth() else label.fontMetrics().height()
            assert label.height() >= needed, label.text()


@pytest.mark.parametrize('method,attribute', [('open_mic_setup_guide', 'mic_setup_wizard'),
                                             ('open_ava_guide', 'ava_guide')])
@pytest.mark.parametrize('already_opened', [False, True])
def test_guide_becomes_active_and_close_returns_to_tutorial(tutorial, qapp, monkeypatch,
                                                           method, attribute, already_opened):
    monkeypatch.setattr(qt_runtime, 'post', lambda callback: QTimer.singleShot(0, qapp, callback))
    wrapper = SimpleNamespace(window=None)

    def open_window():
        if wrapper.window is None:
            wrapper.window = QDialog()
        wrapper.window.show()

    setattr(tutorial._app, attribute, wrapper)
    setattr(tutorial._app, method, lambda: qt_runtime.post(open_window))
    if already_opened:
        open_window()
        wrapper.window.hide()
    tutorial._step = len(tutorial._steps) - 1
    tutorial._show_step()
    settle(qapp)
    tutorial.activateWindow()
    opens = [b for b in tutorial._current_page.findChildren(QPushButton) if b.text() == 'Open →']
    opens[0 if attribute == 'mic_setup_wizard' else 1].click()
    settle(qapp)
    guide = wrapper.window
    try:
        assert QApplication.activeWindow() is guide
        assert tutorial.isVisible()
        assert guide.windowHandle().transientParent() is tutorial.windowHandle()
        guide.close()
        settle(qapp)
        assert QApplication.activeWindow() is tutorial
    finally:
        guide.close()
        guide.deleteLater()
