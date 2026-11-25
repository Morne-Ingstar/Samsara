"""Apply and Close through the assembled Settings window."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox, QPushButton

from tests.test_friendlier_settings_595 import window


@pytest.fixture
def saves(window, monkeypatch):
    save = Mock()
    monkeypatch.setattr(window.app, 'save_config', save)
    for target in (
        'samsara.listening_display.run_display',
        'samsara.listening_display.request_restart_once',
        'samsara.ava_readiness.recheck_after_settings',
        'samsara.ui.speech_hardware_qt.request_settings_restart',
        'samsara.ui.settings.services.apply_microphone_choice',
        'samsara.ui.theme.set_theme',
    ):
        monkeypatch.setattr(target, Mock())
    monkeypatch.setattr(window, '_replace_tts_runtime', Mock())
    return save


def _visible_message(window):
    messages = [box for box in window.findChildren(QMessageBox) if box.isVisible()]
    assert len(messages) == 1
    return messages[0]


def _choose(window, text):
    message = _visible_message(window)
    next(button for button in message.buttons() if button.text() == text).click()


def test_general_apply_keeps_page_and_clean_close_does_no_work(window, saves, monkeypatch):
    page = window._stack.currentWidget()
    hint = window._widgets['hints_enabled']
    hint.toggle()
    window._apply_button.click()

    assert window.isVisible() and window._stack.currentWidget() is page
    assert window.app.config['hints_enabled'] == hint.isChecked()
    assert window._config_at_open['hints_enabled'] == hint.isChecked()
    saves.assert_called_once_with()

    callbacks = Mock(side_effect=AssertionError('Close must not call a save callback'))
    monkeypatch.setattr(window, '_save_fns', [callbacks])
    window._close_button.click()
    assert not window.isVisible()
    saves.assert_called_once_with()
    callbacks.assert_not_called()


@pytest.mark.parametrize('through_title_bar', [False, True], ids=['footer', 'title-bar'])
def test_dirty_close_keeps_editing_or_discards(window, saves, qapp, through_title_bar):
    switch = window._widgets['home_recent_words']
    original = switch.isChecked()
    switch.toggle()

    def close():
        if through_title_bar:
            qapp.sendEvent(window, QCloseEvent())
        else:
            window._close_button.click()

    close()
    dialog = _visible_message(window)
    assert {button.text() for button in dialog.buttons()} == {'Discard changes', 'Keep editing'}
    assert dialog.defaultButton().text() == dialog.escapeButton().text() == 'Keep editing'
    _choose(window, 'Keep editing')
    assert window.isVisible() and switch.isChecked() is not original
    saves.assert_not_called()

    close()
    _choose(window, 'Discard changes')
    assert not window.isVisible()
    window.show_tab('General')
    assert window._widgets['home_recent_words'].isChecked() is original
    saves.assert_not_called()


def test_close_checks_visited_pages_and_allows_an_edit_to_be_reverted(window, saves):
    switch = window._widgets['home_recent_words']
    switch.toggle()
    window.open_tool('Health')
    assert window._apply_button.isHidden()
    window._close_button.click()
    _choose(window, 'Keep editing')
    assert window.isVisible()

    window.show_tab('General')
    switch.toggle()
    window.open_tool('Health')
    window._close_button.click()
    assert not window.isVisible()
    saves.assert_not_called()


def test_close_defers_ava_preview_rollback_until_discard(window, saves, monkeypatch):
    window.show_tab('Ava')
    rollback = Mock()
    monkeypatch.setattr(window, '_cancel_ava_route_preview', rollback)
    switch = window._widgets['ava_interpret_misses']
    switch.toggle()
    window.close()
    _choose(window, 'Keep editing')
    rollback.assert_not_called()

    switch.toggle()
    window.close()
    assert not window.isVisible()
    rollback.assert_not_called()

    window.show_tab('Ava')
    window._widgets['ava_interpret_misses'].toggle()
    window.close()
    _choose(window, 'Discard changes')
    assert not window.isVisible()
    rollback.assert_called_once_with()
    saves.assert_not_called()


def test_apply_preserves_outside_changes_across_repeated_applies(window, saves):
    original = window._widgets['home_recent_words'].isChecked()
    window.app.config['home']['show_recent_words'] = not original
    window._widgets['hints_enabled'].toggle()
    window._apply_button.click()
    assert window.app.config['home']['show_recent_words'] is not original

    window._widgets['hints_enabled'].toggle()
    window._apply_button.click()
    assert window.app.config['home']['show_recent_words'] is not original
    window._close_button.click()
    assert not window.isVisible()
    assert saves.call_count == 2


def test_tool_apply_does_not_rebase_other_destinations_on_outside_values(window, saves):
    window.close()
    window.app.config['alarms']['dismiss_hotkey'] = 'f8'
    window.show_tab('Shortcuts')
    shortcut = window._widgets['alarms_dismiss_key']
    outside = 'ctrl+f11'
    assert shortcut.combo != outside
    window.app.config['alarms']['dismiss_hotkey'] = outside

    window.open_tool('Alarms')
    window._apply_button.click()
    window.show_tab('General')
    window._widgets['hints_enabled'].toggle()
    window._apply_button.click()
    assert window.app.config['alarms']['dismiss_hotkey'] == outside
    window._close_button.click()
    assert not window.isVisible()
    assert saves.call_count == 2


@pytest.mark.parametrize('failure', ['raises', 'returns-false', 'device-write-raises'])
def test_failed_apply_preserves_edit_and_baseline_for_retry(window, saves, monkeypatch, failure):
    from samsara import model_guard

    page = window._stack.currentWidget()
    before = deepcopy(window._config_at_open)
    switch = window._widgets['home_recent_words']
    edited = not switch.isChecked()
    switch.setChecked(edited)
    guarded_save = model_guard.save_settings
    if failure == 'device-write-raises':
        from samsara.ui.settings import services

        services.apply_microphone_choice.side_effect = OSError('test device write failure')
    elif failure == 'raises':
        saves.side_effect = OSError('test write failure')
    else:
        monkeypatch.setattr(model_guard, 'save_settings', Mock(return_value=False))
    window._apply_button.click()

    assert window._stack.currentWidget() is page and window.isVisible()
    assert switch.isChecked() is edited
    assert window._config_at_open == before
    dialog = _visible_message(window)
    assert dialog.text() == 'Could not save your changes. Try Apply again.'
    dialog.button(QMessageBox.StandardButton.Ok).click()
    window._close_button.click()
    _choose(window, 'Keep editing')
    assert switch.isChecked() is edited

    monkeypatch.setattr(model_guard, 'save_settings', guarded_save)
    if failure == 'device-write-raises':
        services.apply_microphone_choice.side_effect = None
    saves.side_effect = None
    saves.reset_mock()
    window._apply_button.click()
    assert window.isVisible() and window._stack.currentWidget() is page
    assert window._config_at_open['home']['show_recent_words'] is edited
    saves.assert_called_once_with()
    window._close_button.click()
    assert not window.isVisible()


def test_apply_updates_sound_once_and_close_does_not_repeat_it(window, saves, monkeypatch):
    refresh = Mock()
    monkeypatch.setattr(window, '_apply_sound_theme', refresh)
    window.show_tab('Audio')
    sound = window._widgets['sound_theme_combo']
    sound.setCurrentIndex(next(i for i in range(sound.count()) if i != sound.currentIndex()))
    window._apply_button.click()
    assert window.app.config['sound_theme'] == sound.currentText()
    refresh.assert_called_once()
    window._close_button.click()
    assert not window.isVisible()
    saves.assert_called_once_with()
    refresh.assert_called_once()


@pytest.mark.parametrize('scale', [1.0, 1.30], ids=['100%', '130%'])
def test_footer_geometry_focus_and_tab_order(window, saves, qapp, scale):
    window.app.config['ui_scale'] = scale
    window.close()
    window.show_tab('General')
    window.resize(1160, 820)
    window.apply_theme()
    qapp.processEvents()
    assert window._widgets['ui_scale_combo'].currentText().endswith(f'({scale:.0%})')
    apply, close = window._apply_button, window._close_button
    assert [button.text() for button in (apply, close)] == ['Apply', 'Close']
    assert apply.height() == close.height()
    assert 0 < apply.x() < apply.geometry().right() < close.x()
    assert close.geometry().right() < close.parentWidget().width() - 1
    assert all(button.width() >= button.sizeHint().width() for button in (apply, close))
    apply.setFocus()
    QTest.keyClick(apply, Qt.Key.Key_Tab)
    assert close.hasFocus()
    QTest.keyClick(close, Qt.Key.Key_Backtab)
    assert apply.hasFocus()

    window.open_tool('Health')
    assert apply.isHidden() and close.isVisible()
    close.setFocus()
    assert close.hasFocus()
    QTest.keyClick(close, Qt.Key.Key_Space)
    assert not window.isVisible()
    saves.assert_not_called()


@pytest.mark.parametrize('name', ['Music', 'Components'])
def test_tool_discard_apply_and_reopen(window, saves, name):
    if name == 'Components':
        # Turning off an enabled gesture lane avoids the installation offer.
        window.app.config.setdefault('gesture', {})['enabled'] = True
        window.close()

    def read():
        if name == 'Music':
            return dict(window._music_library)
        return window._widgets['adv_gesture_enabled'].isChecked()

    def edit():
        if name == 'Music':
            window._widgets['music_library_name'].setText('Example track')
            window._widgets['music_library_link'].setText('spotify:track:0VjIjW4GlUZAMYd2vXMi3b')
            window._stack.currentWidget().findChild(QPushButton, 'musicLibraryAdd').click()
        else:
            window._widgets['adv_gesture_enabled'].toggle()

    window.open_tool(name)
    original = read()
    edit()
    edited = read()
    assert edited != original
    window._close_button.click()
    _choose(window, 'Discard changes')
    window.open_tool(name)
    assert read() == original
    saves.assert_not_called()

    edit()
    page = window._stack.currentWidget()
    window._apply_button.click()
    assert window.isVisible() and window._stack.currentWidget() is page
    window._close_button.click()
    assert not window.isVisible()
    window.open_tool(name)
    assert read() == edited
    saves.assert_called_once_with()


def test_apply_tool_does_not_clear_pending_preferences(window, saves):
    switch = window._widgets['home_recent_words']
    original = switch.isChecked()
    switch.toggle()
    window.open_tool('Music')
    window._apply_button.click()
    window._close_button.click()
    _choose(window, 'Discard changes')
    window.show_tab('General')
    assert window._widgets['home_recent_words'].isChecked() is original
    saves.assert_called_once_with()
