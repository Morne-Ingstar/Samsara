"""Scaled laptop layout and the tutorial's asynchronous guide handoff."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton

from samsara.ui import qt_runtime, theme, tutorial_qt
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
    # 936: optional lessons retain the same frame/footer safety coverage.
    entries = list(range(len(tutorial._steps))) + list(tutorial._optional_pages)
    for entry in entries:
        if isinstance(entry, str):
            tutorial._open_optional(entry)
        else:
            tutorial._optional_key = None
            tutorial._step = entry
            tutorial._show_step()
        tutorial._fit_available_screen()
        settle(qapp)
        assert area.contains(tutorial.frameGeometry()), (entry, tutorial.frameGeometry())
        for button in (tutorial._back_btn, tutorial._next_btn):
            rect = QRect(button.mapTo(tutorial, QPoint()), button.size())
            assert tutorial.rect().contains(rect)
            assert button.height() >= 44


@pytest.mark.parametrize('scale', [1, 1.25, 1.5])
def test_example_keeps_its_natural_width_at_smallest_window(tutorial, qapp, scale, monkeypatch):
    area = QRect(0, 0, round(1366 / scale), round(728 / scale))
    monkeypatch.setattr(tutorial, 'screen', lambda: SimpleNamespace(availableGeometry=lambda: area))
    # 936: preferences are an optional entry, with unchanged width assertions.
    tutorial._open_optional('try')
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
    # 934: Done keeps the incoming width; smaller viewports may scroll.
    assert page.horizontalScrollBar().maximum() == 0
    buttons = [b for b in page.findChildren(QPushButton) if b.text() == 'Open →']
    assert len(buttons) == 2
    for button in buttons:
        page.ensureWidgetVisible(button)
        settle(qapp)
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


def test_936_core_route_and_truthful_skip_then_completion(tutorial, qapp):
    assert [key for key, _ in tutorial._steps] == ['welcome', 'dictation', 'command', 'done']
    tutorial._go_next()
    assert tutorial._active_key() == 'dictation'
    assert not tutorial._next_btn.isEnabled()
    tutorial._skip_step()
    assert tutorial._active_key() == 'command'
    assert not tutorial._next_btn.isEnabled()
    tutorial._skip_step()
    assert tutorial._active_key() == 'done'
    assert tutorial._completed == set()
    assert tutorial._skipped_steps == {1, 2}
    assert all(icon.accessibleName() == 'Skipped' for icon, _ in tutorial._done_checks.values())
    tutorial._go_back()
    assert tutorial._active_key() == 'command'
    tutorial._app._tutorial_hooks['command']('synthetic command success')
    settle(qapp)
    tutorial._go_back()
    assert tutorial._active_key() == 'dictation'
    tutorial._app._tutorial_hooks['dictation']('Synthetic practice sentence.')
    settle(qapp)
    assert tutorial._dictation_box.toPlainText() == 'Synthetic practice sentence.'
    tutorial._go_next()
    tutorial._go_next()
    assert tutorial._active_key() == 'done'
    assert tutorial._completed == {1, 2} and not tutorial._skipped_steps
    assert all(icon.accessibleName() == 'Completed' for icon, _ in tutorial._done_checks.values())
    tutorial._go_back()
    assert tutorial._skip_all_btn.isVisible()


@pytest.mark.parametrize('key', ['ways', 'display', 'try'])
@pytest.mark.parametrize('return_action', ['_go_next', '_go_back'])
def test_936_optional_buttons_return_to_done_without_saving(tutorial, qapp, monkeypatch,
                                                           key, return_action):
    from copy import deepcopy
    from unittest.mock import Mock
    before = deepcopy(tutorial._app.config)
    tutorial._app.persist_config = Mock()
    monkeypatch.setattr(tutorial._try_page, 'sample', Mock())
    # A prior real exercise registration must be released before optional entry.
    tutorial._step = 1
    tutorial._show_step()
    assert tutorial._app._tutorial_hooks
    tutorial._step = 3
    tutorial._show_step()
    tutorial._optional_buttons[key].click()
    settle(qapp)
    assert tutorial._active_key() == key
    assert tutorial._current_page is tutorial._optional_pages[key]
    assert tutorial._step_lbl.text() == 'Optional lesson'
    assert not tutorial._app._tutorial_hooks
    assert tutorial._hint_timer is None
    if key == 'display':
        assert tutorial._display_demo.timer.isActive()
    if key == 'try':
        assert tutorial._try_page.timer.isActive()
        # Forward still visits the three existing preference exercises.
        if return_action == '_go_next':
            tutorial._go_next()
            assert tutorial._try_page.stage == 1
            tutorial._go_next()
            assert tutorial._try_page.stage == 2
            assert tutorial._next_btn.text() == 'Back to Done'
    getattr(tutorial, return_action)()
    settle(qapp)
    assert tutorial._active_key() == 'done'
    assert tutorial._step == 3
    assert not tutorial._display_demo.timer.isActive()
    assert not tutorial._try_page.timer.isActive()
    assert tutorial._app.config == before
    tutorial._app.persist_config.assert_not_called()
    assert not tutorial._completed and not tutorial._skipped_steps


@pytest.mark.parametrize('action', ['close', '_skip_tutorial', '_finish', '_go_back'])
def test_936_leaving_preferences_releases_owned_preview_and_reader(tutorial, qapp, monkeypatch, action):
    from unittest.mock import Mock
    tutorial._step = 1
    tutorial._show_step()
    tutorial._open_optional('try')
    page = tutorial._try_page
    # Existing passive reader and private preview teardown, with no real audio.
    capture = Mock()
    page.capture, page.reader = capture, Mock()
    preview = Mock()
    page.sample_engine = preview
    getattr(tutorial, action)()
    settle(qapp)
    assert page.reader is None and page.capture is None
    assert page.sample_engine is None
    capture.unregister_consumer.assert_called_once()
    preview.close.assert_called_once()
    assert not page.timer.isActive()
    assert not tutorial._app._tutorial_hooks
    assert tutorial._hint_timer is None


def test_936_setup_entry_still_starts_at_dictation(qapp):
    window = TutorialWindow(StubApp(), from_setup=True)
    try:
        assert window._active_key() == 'dictation'
        assert len(window._steps) == 4
        assert not window._completed
    finally:
        window.close()
        window.deleteLater()


def test_936_one_safe_enabled_catalog_example(tutorial):
    from samsara import command_catalog
    safe = command_catalog.pick_examples(tutorial._catalog, 1,
                                         enabled_packs=tutorial_qt._enabled_packs(tutorial._app.config))
    assert len(tutorial._examples) == 1
    assert tutorial._examples == [command_catalog.canonical_phrase(safe[0])]
    assert safe[0]['risk'] in ('read', 'ui')
    assert not safe[0].get('whole_utterance')


def test_936_try_failure_disables_only_its_optional_entry(qapp, monkeypatch):
    def unavailable(window):
        raise RuntimeError('synthetic optional page failure')
    monkeypatch.setattr(tutorial_qt, '_TryPage', unavailable)
    window = TutorialWindow(StubApp())
    try:
        assert len(window._steps) == 4
        assert set(window._optional_pages) == {'ways', 'display'}
        assert not window._optional_buttons['try'].isEnabled()
        window._step = 3
        window._show_step()
        window._open_optional('ways')
        window._go_back()
        assert window._active_key() == 'done'
        assert not window._completed
    finally:
        window.close()
        window.deleteLater()


def test_936_recovery_help_keeps_enter_and_keyboard_focus(qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from unittest.mock import Mock
    monkeypatch.setattr(tutorial_qt, '_tutorial_catalog', lambda app: None)
    support = Mock()
    monkeypatch.setattr(tutorial_qt, 'open_support_tab', support)
    window = TutorialWindow(StubApp())
    try:
        window._step = 2
        window._show_step()
        window.show()
        window._show_hint('command')
        settle(qapp)
        links = [label for label in window._current_page.findChildren(QLabel)
                 if tutorial_qt.HELP_LINK in label.text()]
        assert len(links) == 2
        for label in links:
            assert label.focusPolicy() == Qt.FocusPolicy.StrongFocus
            assert ':focus' in label.styleSheet() and theme.ACCENT in label.styleSheet()
            label.setFocus(Qt.FocusReason.TabFocusReason)
            QTest.keyClick(label, Qt.Key.Key_Return)
        assert support.call_count == 2
        assert not window._completed
    finally:
        window.close()
        window.deleteLater()
