from ctypes import addressof, wintypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

from samsara import listening_display, paste_again
from samsara.live_surface.model import EventKind, NoticeKind, SurfaceEvent
from samsara.ui import paste_again_qt
from samsara.ui.button_voice_qt import visible_actions
from samsara.ui.listening_indicator import ListeningIndicator


@pytest.fixture(params=['full', 'compact'])
def display(request, qt_widget_scope):
    value = request.param
    app = SimpleNamespace(config={'ui': {'listening_display': value}},
                          _schedule_ui=lambda fn, *args: fn(*args))
    listening_display.build_display(app, ListeningIndicator)
    indicator = app.listening_indicator
    if value == 'full':
        owner = hit = app.live_surface.widget
    else:
        owner, hit = indicator, indicator._pill_window
    app.apply_listening_indicator_settings = lambda: (
        indicator.show() if listening_display.display_visible(app) else indicator.hide()
    )
    with qt_widget_scope(owner):
        owner.show()
        yield app, owner, hit


def _context_menu(hit, reason, pos=None):
    pos = hit.rect().center() if pos is None else pos
    event = QContextMenuEvent(reason, pos, hit.mapToGlobal(pos))
    QApplication.sendEvent(hit, event)
    return QApplication.activePopupWidget()


@pytest.mark.parametrize('reason', [QContextMenuEvent.Reason.Mouse, QContextMenuEvent.Reason.Keyboard])
def test_real_listening_display_opens_shared_menu(display, monkeypatch, reason):
    from samsara.ui import home_qt

    app, owner, hit = display
    switch = Mock(return_value=True)
    monkeypatch.setattr(home_qt, 'open_settings_tab', switch)
    before = listening_display.run_display(app)
    other = 'Compact' if before.full else 'Full'
    menu = _context_menu(hit, reason)
    assert menu is not None
    try:
        actions = [action for action in menu.actions() if not action.isSeparator()]
        assert [action.text() for action in actions] == [
            f'Switch to {other} in Settings (next launch)', 'Hide indicator', 'Reset position'
        ]
        actions[0].trigger()
        switch.assert_called_once_with(app, 'General')
        assert app.config['ui']['listening_display'] == before.value
        assert listening_display.run_display(app) is before
        assert hit.isVisible()
    finally:
        menu.close()


def test_mask_blocks_context_menu_outside_painted_display(display):
    _, _, hit = display
    assert not hit.mask().contains(QPoint(0, 0))
    assert _context_menu(hit, QContextMenuEvent.Reason.Mouse, QPoint(0, 0)) is None


def test_reset_dispatches_to_the_selected_display_placement(display, monkeypatch):
    from samsara.ui import listening_indicator

    app, _, hit = display
    reset = Mock()
    if listening_display.run_display(app).full:
        monkeypatch.setattr(app.live_surface, 'reset_placement', reset)
        args = ()
    else:
        monkeypatch.setattr(listening_indicator, 'reset_indicator_placement', reset)
        args = (app,)
    menu = _context_menu(hit, QContextMenuEvent.Reason.Mouse)
    assert menu is not None
    try:
        next(action for action in menu.actions() if action.text() == 'Reset position').trigger()
        reset.assert_called_once_with(*args)
    finally:
        menu.close()


def test_menu_hide_retires_retry_and_tray_restores_display(display, monkeypatch):
    app, owner, hit = display
    request = Mock()
    monkeypatch.setattr(paste_again, 'request', request)
    paste_again_qt.show_control(owner, app)
    control = owner._paste_again_control
    menu = _context_menu(hit, QContextMenuEvent.Reason.Mouse)
    assert menu is not None
    try:
        next(action for action in menu.actions() if action.text() == 'Hide indicator').trigger()
        control.sync()
        assert not listening_display.display_visible(app)
        assert not hit.isVisible()
        assert not control.isVisible()
        paste_again_qt.show_control(owner, app)
        assert not control.isVisible()
        request.assert_not_called()
        listening_display.toggle_display_visibility(app)
        assert hit.isVisible() and not control.isVisible()
    finally:
        menu.close()


def test_retry_is_an_inline_button_with_passive_windows_hit_path(display, monkeypatch):
    app, owner, hit = display
    request = Mock()
    monkeypatch.setattr(paste_again, 'request', request)
    paste_again_qt.show_control(owner, app)
    control = owner._paste_again_control
    assert not control.isWindow() and control.parentWidget() is hit
    assert control.button.window() is hit
    assert hit.mask().contains(control.geometry().topLeft())
    assert hit.mask().contains(control.geometry().bottomRight())
    assert hit.childAt(control.geometry().center()) is control.button
    assert isinstance(control.button, QPushButton)
    assert control.button.accessibleName() == 'Paste again'
    assert control.button.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert hit.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert hit.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    message = wintypes.MSG()
    message.message = 0x0021  # WM_MOUSEACTIVATE must deliver the click without activation.
    assert hit.nativeEvent(b'windows_generic_MSG', addressof(message)) == (True, 3)
    assert any(action.label == 'Paste again' for action in visible_actions(QApplication.instance()))
    request.assert_not_called()
    QTest.mouseClick(control.button, Qt.MouseButton.LeftButton)
    request.assert_called_once_with(app)
    paste_again_qt.show_control(owner, app, available=False)
    control.sync()
    assert not control.isVisible()
    request.assert_called_once_with(app)


def test_retry_show_and_click_keep_editor_focus(display, qt_widget_scope, monkeypatch):
    app, owner, _ = display
    with qt_widget_scope(QLineEdit()) as editor:
        editor.show()
        editor.activateWindow()
        editor.setFocus()
        QApplication.processEvents()
        assert QApplication.focusWidget() is editor
        request = Mock(side_effect=lambda _: QApplication.focusWidget() is editor)
        monkeypatch.setattr(paste_again, 'request', request)
        paste_again_qt.show_control(owner, app)
        QApplication.processEvents()
        assert QApplication.focusWidget() is editor
        request.assert_not_called()
        QTest.mouseClick(owner._paste_again_control.button, Qt.MouseButton.LeftButton)
        assert QApplication.focusWidget() is editor
        request.assert_called_once_with(app)


@pytest.mark.parametrize('display', ['compact'], indirect=True)
def test_retiring_the_compact_input_window_releases_its_retry(display, monkeypatch):
    from shiboken6 import isValid

    app, owner, _ = display
    paste_again_qt.show_control(owner, app)
    previous = owner._paste_again_control
    owner.destroy()
    assert not isValid(previous)


@pytest.mark.parametrize('display', ['full'], indirect=True)
def test_full_retry_keeps_confirmation_reachable_at_monitor_edge(display, monkeypatch):
    app, owner, hit = display
    controller = app.live_surface
    controller.model.consume(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.CONFIRMATION,
                                          message='Confirm the pending action?'))
    hit.refresh(animate=False)
    hit.move(hit.screen().availableGeometry().bottomRight() - hit.rect().bottomRight())
    assert hit._band.isVisible()
    request = Mock()
    monkeypatch.setattr(paste_again, 'request', request)
    paste_again_qt.show_control(owner, app)
    control = owner._paste_again_control
    assert hit.screen().availableGeometry().contains(hit.frameGeometry())
    assert hit.rect().contains(control.geometry())
    assert not hit._band.geometry().intersects(control.geometry())
    assert controller.model.view().notice.kind is NoticeKind.CONFIRMATION
    assert hit._clear.isVisible() and not hit._clear.geometry().intersects(control.geometry())
    request.assert_not_called()
