"""519 layout regressions, without app bootstrap or child processes."""
from types import SimpleNamespace
import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QLabel, QFrame
from samsara.ui import first_run_wizard_qt as wizard, theme
from samsara.ui.profile_manager_qt import _ProfileManagerWindow
from samsara.ui.tutorial_display_qt import DisplayPlacementPage
from samsara.ui.widgets.indicator_demo import CAPTIONS


def test_profile_labels_are_plain(qapp):
    pm = SimpleNamespace(get_active_profile_names=lambda: {}, list_dictionary_profiles=lambda: ['Example'], list_command_profiles=lambda: ['Default'])
    window = _ProfileManagerWindow(pm)
    window.show()
    qapp.processEvents()
    labels = [label for label in window.findChildren(QLabel) if label.text().startswith('Active:') or label.text() == 'Profile:']
    assert len(labels) == 4
    for label in labels:
        assert label.frameShape() == QFrame.Shape.NoFrame
        assert label.frameWidth() == 0
        assert 'border: none' in window.styleSheet()
        assert 'QFrame#profileCard' in label.parentWidget().styleSheet()
    window.hide()
    window.deleteLater()


@pytest.mark.parametrize('setup', [True, False])
def test_placement_explanation_once(qapp, setup):
    page = DisplayPlacementPage({}, setup=setup)
    page.show()
    page.demo.timer.stop() if page.demo.timer else None
    page.demo.set_frame(0)
    qapp.processEvents()
    assert sum(label.isVisibleTo(page) and label.text() == CAPTIONS[0] for label in page.findChildren(QLabel)) == 1
    assert page.reason.y() < page.demo.y()
    page.demo.set_frame(1)
    assert page.demo.caption.isVisibleTo(page)
    page.demo.set_frame(0)
    assert not page.demo.caption.isVisibleTo(page)
    page.hide()
    page.deleteLater()


@pytest.mark.parametrize('physical', [(1920,1080), (1366,768), (2560,1440)])
@pytest.mark.parametrize('scale', [1, 1.25, 1.5])
def test_setup_scrolls_only_when_work_area_requires_it(qapp, tmp_path, monkeypatch, physical, scale):
    area = QRect(0, 0, int(physical[0]/scale), int(physical[1]/scale))
    screen = SimpleNamespace(availableGeometry=lambda: area)
    monkeypatch.setattr(wizard._WizardWindow, 'screen', lambda self: screen)
    monkeypatch.setattr(wizard.thread_registry, 'spawn', lambda *a, **k: None)
    monkeypatch.setattr(wizard._WizardWindow, '_start_meter', lambda self: None)
    window = wizard._WizardWindow(tmp_path / 'config.json')
    window.show()
    for step in range(len(window._pages)):
        window._step = step
        window._show_step()
        for _ in range(10):
            qapp.processEvents()
        page = window._current_page
        content = page.widget().layout()
        required = max(content.minimumSize().height(), content.totalHeightForWidth(page.viewport().width()))
        chrome = window.height() - page.viewport().height()
        margins = window.windowHandle().frameMargins()
        available = area.height() - margins.top() - margins.bottom() - chrome
        assert not page.verticalScrollBar().maximum() or required > available, (physical, scale, step, required, available)
        assert area.contains(window.frameGeometry())
    window.hide()
    window.deleteLater()


def test_content_revealed_after_opening_grows_window(qapp, tmp_path, monkeypatch):
    screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1920, 1080))
    monkeypatch.setattr(wizard._WizardWindow, 'screen', lambda self: screen)
    monkeypatch.setattr(wizard.thread_registry, 'spawn', lambda *a, **k: None)
    window = wizard._WizardWindow(tmp_path / 'config.json')
    window.show()
    for _ in range(10):
        qapp.processEvents()
    initial_height = window.height()
    page = window._current_page
    extra = QLabel('Expanded explanation', page.widget())
    extra.setFixedHeight(400)
    page.widget().layout().addWidget(extra)
    for _ in range(20):
        qapp.processEvents()
    assert window.height() > initial_height
    assert page.verticalScrollBar().maximum() == 0
    window.hide()
    window.deleteLater()
