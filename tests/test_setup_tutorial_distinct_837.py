"""837: separate setup choices from learning and preserve their handoff."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox, QRadioButton

from samsara.onboarding.display_placement import placement_updates
from samsara.ui import first_run_wizard_qt as setup, tutorial_qt as tutorial
from samsara.ui.widgets.indicator_place import IndicatorPlacePicker


@pytest.fixture
def windows(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(setup.thread_registry, 'spawn', lambda *a, **k: None)
    monkeypatch.setattr(setup._WizardWindow, '_start_meter', lambda self: None)
    made = []

    def make(kind, app=None, **kwargs):
        app = app or SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}})
        win = (setup._WizardWindow(tmp_path / 'config.json', app) if kind == 'setup'
               else tutorial.TutorialWindow(app, **kwargs))
        made.append(win)
        win.show()
        for _ in range(3):
            qapp.processEvents()
        return win

    yield make
    for win in reversed(made):
        win.close()
        win.deleteLater()
    qapp.processEvents()


def test_titles_and_flow_labels_differ(windows):
    first, learn = windows('setup'), windows('tutorial')
    assert first.windowTitle() == first._title_lbl.text() == 'Set up Samsara'
    assert learn.windowTitle() == learn._title_lbl.text() == 'Learn Samsara'
    assert first._step_lbl.text().endswith('\nSetup')
    assert learn._step_lbl.text() == 'Step 1 of 7'
    assert learn._step_indicator.variant == 'C'
    assert first._step_indicator.variant == 'A'
    assert learn._step_indicator.steps[4] == 'Show'
    assert first._step_indicator.count() == 10


@pytest.mark.parametrize('display', ['full', 'compact'])
@pytest.mark.parametrize('position', ['top-right', 'bottom-left'])
def test_show_uses_saved_display_and_position_without_chooser(windows, display, position):
    config = {'ui': {'live_surface': {'enabled': display == 'full'}},
              'listening_indicator_enabled': display == 'compact'}
    config.update(placement_updates(config, position))
    before = deepcopy(config)
    win = windows('tutorial', SimpleNamespace(config=config))
    win._step = next(i for i, (key, _) in enumerate(win._steps) if key == 'display')
    win._show_step()
    page = win._current_page
    assert not page.findChildren(IndicatorPlacePicker)
    assert not page.findChildren(QRadioButton)
    assert not page.findChildren(QComboBox)
    desktop = win._display_demo.desktop
    assert desktop.display == display
    for beat in range(5):
        win._display_demo.set_frame(beat)
        assert desktop.indicator_rect() == desktop.target_rect(position, *desktop.indicator_size())
    assert config == before
    assert 'staged' in win._display_demo.captions[1].lower()
    assert 'hands-free' in win._display_demo.captions[2].lower()


def test_complete_action_hands_off_after_finish_and_home_still_opens_welcome(windows, monkeypatch):
    app = SimpleNamespace(config={})
    win = windows('setup', app)
    assert win._tutorial_btn.isHidden()
    win._config['microphone'] = 7
    win._step = len(setup._STEPS) - 1
    win._show_step()
    assert win._tutorial_btn.isVisible()
    assert win._next_btn.text() == 'Start Samsara'
    win._tutorial_btn.setFocus()
    QTest.keyClick(win._tutorial_btn, Qt.Key.Key_Return)
    assert win.result['first_run_complete']
    assert app._tutorial_start_from_setup is True
    # Same scheduling seam used by startup, without bootstrapping dictation.
    app.config = win.result
    real_window = tutorial.TutorialWindow
    opened = []
    def build(app, **kwargs):
        result = real_window(app, **kwargs)
        opened.append(result)
        return result
    monkeypatch.setattr(tutorial, 'TutorialWindow', build)
    try:
        tutorial.show_tutorial(app)
        assert opened[-1]._step == 1
        assert opened[-1]._steps[1][0] == 'dictation'
        assert opened[-1]._step_lbl.text().startswith('Step 2 of 7')
        assert app._tutorial_window is opened[-1]
        tutorial.show_tutorial(app)
        assert opened[-1]._step == 0
    finally:
        for item in opened:
            item.close()
            item.deleteLater()


def test_setup_width_cap_and_enter_default(windows, qapp):
    win = windows('setup')
    assert win._stack.width() <= 680
    assert win._step_indicator.width() <= 680
    assert abs(win._stack.geometry().center().x() - win.centralWidget().rect().center().x()) <= 1
    assert win._next_btn.isDefault()
    assert win.focusWidget() is win._next_btn
    QTest.keyClick(win._next_btn, Qt.Key.Key_Return)
    assert win._step == 1
    # Every subsequent page restores navigation focus; microphone keeps its gate.
    for step in range(2, len(setup._STEPS)):
        win._step = step
        win._show_step()
        qapp.processEvents()
        assert win.focusWidget() is win._next_btn


@pytest.mark.parametrize('display', ['full', 'compact'])
def test_demo_states_and_label_fit(windows, display):
    from samsara.live_surface.model import CaptureState
    from samsara.ui.listening_indicator import ListeningIndicator
    from samsara.ui.widgets.indicator_demo import compact_stand_in
    config = {'ui': {'listening_display': display, 'live_surface': {'enabled': True}}}
    win = windows('tutorial', SimpleNamespace(config=config))
    demo = win._display_demo
    demo.set_frame(0)
    assert demo.desktop.demo_view().capture is CaptureState.RECORDING
    demo.set_frame(1)
    assert demo.desktop.demo_view().text
    assert demo.desktop.state_words()[1] == 'staged'
    demo.set_frame(2)
    assert demo.desktop.demo_view().capture is CaptureState.HANDS_FREE_LISTENING
    if display == 'compact':
        proxy = compact_stand_in()
        proxy._font = lambda: ListeningIndicator._font(proxy)
        assert demo.desktop.indicator_size()[0] >= ListeningIndicator._pill_width(proxy, 'Hands-free', True)
