"""Setup/tutorial geometry on scaled laptop work areas, without app bootstrap."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QScrollArea

from samsara.ui import first_run_wizard_qt as wizard, tutorial_qt


@pytest.mark.parametrize('kind', ['setup', 'tutorial'])
@pytest.mark.parametrize('width,height', [(1366, 728), (1093, 582), (1280, 720), (911, 485)])
def test_all_pages_fit_work_area(kind, width, height, qapp, tmp_path, monkeypatch):
    # Coordinates are logical pixels: laptop at 100/125/150%, desktop at 150%.
    available = QRect(150, 80, width, height)
    screen = SimpleNamespace(availableGeometry=lambda: available, devicePixelRatio=lambda: 1.0)
    monkeypatch.setattr(QApplication, 'primaryScreen', lambda: screen)
    monkeypatch.setattr(wizard.thread_registry, 'spawn', lambda *a, **k: None)
    cls = wizard._WizardWindow if kind == 'setup' else tutorial_qt.TutorialWindow
    monkeypatch.setattr(cls, 'screen', lambda self: screen)
    monkeypatch.setattr(wizard._WizardWindow, '_start_meter', lambda self: None)
    window = (cls(tmp_path / 'config.json') if kind == 'setup'
              else cls(SimpleNamespace(config={})))
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    try:
        window.show()
        qapp.processEvents()
        for index in range(len(window._pages)):
            window._step = index
            window._show_step()
            for _ in range(4):
                qapp.processEvents()
            assert available.contains(window.frameGeometry()), (kind, index, window.frameGeometry())
            assert isinstance(window._current_page, QScrollArea), (kind, index)
            assert window._current_page.viewport().height() > 40
            buttons = [window._back_btn, window._next_btn]
            if kind == 'tutorial':
                buttons += [window._skip_all_btn, window._skip_step_btn]
            for button in buttons:
                if button.isVisible():
                    rect = QRect(button.mapTo(window, QPoint()), button.size())
                    assert window.rect().contains(rect), (kind, index, button.text(), rect)
                    assert button.height() >= 44
        # A later show must respect a different monitor's origin and work area.
        window.hide()
        available.setRect(-1100, 0, 1093, 582)
        window.show()
        qapp.processEvents()
        assert available.contains(window.frameGeometry())
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
