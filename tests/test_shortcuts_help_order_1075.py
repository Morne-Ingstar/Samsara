"""1075: shortcut ownership, staged values and Help navigation."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from samsara.ui.settings.presentation import SettingRow
from samsara.ui.settings_qt import _SettingsWindow
from tests.test_settings_pages import _StubApp, _schema_defaults


@pytest.fixture
def window(qapp, monkeypatch, qt_widget_scope):
    monkeypatch.setattr('samsara.autostart.sync_run_entry', lambda *a, **kw: None)
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda *a, **kw: None)
    monkeypatch.setattr('samsara.ui.first_run_qt.ComponentsPage.refresh_async', lambda *a: None)
    monkeypatch.setattr('samsara.ava_readiness.recheck_after_settings', lambda *a: None)
    monkeypatch.setattr('samsara.ui.window_activation.surface_requested', lambda window: window.show())
    app = _StubApp(_schema_defaults())
    app._open_profiles_fixture = Mock()
    monkeypatch.setattr(_SettingsWindow, '_open_profile_manager',
                        lambda self: self.app._open_profiles_fixture())
    app.save_config = Mock()
    win = _SettingsWindow(app)
    monkeypatch.setattr(win, '_replace_tts_runtime', lambda *a: None)
    with qt_widget_scope(win):
        yield win


def _capture(button, *keys):
    button.click()
    assert button._capturing
    for key in keys:
        button.capture_physical(key, True)
    button.capture_physical(keys[-1], False)
    assert button.combo == '+'.join(keys)


def test_shortcuts_and_help_order(window):
    window.show_tab('Shortcuts')
    page = window._pages['Shortcuts']
    assert [row.title for row in page.base_rows] == [
        'Primary dictation', 'Command-only key', 'Session button', 'Ava held key',
        'Wake toggle', 'Cancel', 'Undo', 'Paste staged thought', 'Task held key',
        'Quick memo', 'Corrections', 'Keep this dictation',
    ]
    rows = page.advanced.rows
    assert [rows.itemAt(index).widget().title for index in range(rows.count())
            if isinstance(rows.itemAt(index).widget(), SettingRow)] == [
        'Continuous toggle', 'Continuous commit', 'Correction report shortcut',
        'Mouse suppression', "Scroll the app you're using, not the one under the mouse",
        'Complete alarm', 'Dismiss alarm',
    ]
    assert not page.advanced.toggle.isChecked()
    assert window._widgets['ava_mode_key_off_note'].parentWidget() is window._source_store

    window.show_tab('Help')
    assert [row.title for row in window._pages['Help'].base_rows] == [
        'Setup / practice', 'Support / problem', 'Documentation',
        'Help and community', 'Private backup / restore', 'Updates',
    ]
    guide = window._widgets['setupGuides'].menu()
    assert [a.text() for a in guide.actions() if not a.isSeparator()] == [
        'Interactive tutorial', 'Microphone setup', 'Ava setup',
        'Quick Reference', 'Offline explanations',
    ]
    backup = window._widgets['backupChoices'].menu()
    assert [a.text() for a in backup.actions()] == [
        'Export private configuration', 'Import private configuration',
        'Restore automatic backup', 'Manage profiles',
    ]
    backup.actions()[-1].trigger()
    window.app._open_profiles_fixture.assert_called_once()


def test_ava_summary_search_and_keyboard_navigation(window, qapp):
    window.show_tab('Ava')
    # 1296: keyboard activation starts capture on the Ava page.
    ava = window._rows_by_widget['ava_key_picker']
    assert ava.isAncestorOf(window._widgets['ava_mode_key_off_note'])
    assert not window._widgets['ava_mode_key_off_note'].isHidden()
    picker = window._widgets['ava_key_picker']
    assert picker.text() == 'Right Alt'
    window._search_edit.setText('Ava held key')
    window._apply_search_filter()
    shortcut = window._rows_by_widget['ava_mode_key']
    assert ava.page_name == 'Ava' and shortcut.page_name == 'Shortcuts'
    assert ava.title == shortcut.title == 'Ava held key'
    assert not ava.isHidden() and not shortcut.isHidden()
    assert not ava.config_keys
    assert shortcut.config_keys == ('ava_mode_key',)
    window.show_tab('Ava')
    window.activateWindow()
    picker.setFocus()
    qapp.processEvents()
    assert picker.hasFocus()
    QTest.keyClick(picker, Qt.Key.Key_Space)
    assert picker._capturing
    QTest.keyClick(picker, Qt.Key.Key_F9)
    assert picker.combo == 'f9'
    assert window._stack.currentWidget() is window._pages['Ava']


def test_ava_wake_and_alarms_apply_once_and_cancel_reverts(window):
    window.show_tab('Shortcuts')
    window._pages['Shortcuts'].advanced.reveal()
    before = deepcopy(window.app.config)
    _capture(window._widgets['ava_mode_key'], 'f9')
    _capture(window._widgets['wake_word_hotkey'], 'ctrl', 'alt', 'y')
    _capture(window._widgets['alarms_complete_key'], 'f10')
    _capture(window._widgets['alarms_dismiss_key'], 'f11')
    # 1296: the Ava picker can be built after editing Shortcuts.
    window.show_tab('Ava')
    assert window._widgets['ava_key_picker'].text() == 'F9'
    assert window.app.config == before
    window.app.save_config.assert_not_called()
    window._apply_button.click()
    window.app.save_config.assert_called_once()
    assert not window.isVisible()
    saved = {
        'ava_mode_key': 'f9', 'wake_word_hotkey': 'ctrl+alt+y',
        'alarms_complete_key': 'f10', 'alarms_dismiss_key': 'f11',
    }
    assert window.app.config['ava_mode_key'] == saved['ava_mode_key']
    assert window.app.config['wake_word_hotkey'] == saved['wake_word_hotkey']
    assert window.app.config['alarms']['complete_hotkey'] == saved['alarms_complete_key']
    assert window.app.config['alarms']['dismiss_hotkey'] == saved['alarms_dismiss_key']
    window.show_tab('Ava')
    picker = window._widgets['ava_key_picker']
    assert picker.text() == 'F9'
    window.show_tab('Shortcuts')
    assert {key: window._widgets[key].combo for key in saved} == saved
    window._pages['Shortcuts'].advanced.reveal()
    _capture(window._widgets['ava_mode_key'], 'f6')
    _capture(window._widgets['wake_word_hotkey'], 'ctrl', 'alt', 'u')
    _capture(window._widgets['alarms_complete_key'], 'f12')
    _capture(window._widgets['alarms_dismiss_key'], 'f4')
    assert picker.text() == 'F6'
    window._close_button.click()
    window.show_tab('Ava')
    assert window._widgets['ava_key_picker'].text() == 'F9'
    window.show_tab('Shortcuts')
    assert {key: window._widgets[key].combo for key in saved} == saved
    window.app.save_config.assert_called_once()
