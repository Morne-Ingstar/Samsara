"""Windows delivery policy, real tray seams and the persisted General row."""
import ast
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject, QEvent
from PySide6.QtWidgets import QSystemTrayIcon

from samsara.config_schema import SETTINGS_SCHEMA
from samsara.notify_gate import CLASSES, LEVELS, notify, saved_level
from samsara.ui.tray_qt import SamsaraTrayQt


@pytest.mark.parametrize('preference', LEVELS)
@pytest.mark.parametrize('level', CLASSES)
def test_level_class_delivery_matrix(preference, level):
    deliver = Mock()
    expected = level == 'reminder' or preference == 'all' or (
        preference == 'important' and level == 'important')
    assert notify({'notifications_level': preference}, level, deliver) is expected
    assert deliver.call_count == int(expected)


@pytest.mark.parametrize('config', [{}, {'notifications_level': None},
                                    {'notifications_level': 'invalid'}])
def test_existing_users_and_invalid_values_default_to_important(config):
    assert saved_level(config) == SETTINGS_SCHEMA['notifications_level']['default'] == 'important'
    assert not notify(config, 'info', Mock())
    assert notify(config, 'important', Mock())


def test_unknown_class_is_rejected():
    with pytest.raises(ValueError):
        notify({}, 'typo', Mock())


@pytest.fixture
def tray(qapp):
    # Wire the production signal/slot without registering any shell icon,
    # starting timers, checking updates or bootstrapping DictationApp.
    tray = SamsaraTrayQt.__new__(SamsaraTrayQt)
    QObject.__init__(tray)
    tray._app = SimpleNamespace(config={})
    tray._tray = SimpleNamespace(showMessage=Mock())
    tray._last_notification_class = None
    tray._available_update = object()
    tray._notification_sig.connect(tray._deliver_notification)
    yield tray
    tray.deleteLater()


@pytest.mark.parametrize('preference', LEVELS)
@pytest.mark.parametrize('level', CLASSES)
def test_production_tray_delivery_respects_matrix(tray, preference, level):
    tray._app.config['notifications_level'] = preference
    tray._notify(level, 'Title', 'Body')
    expected = level == 'reminder' or preference == 'all' or (
        preference == 'important' and level == 'important')
    assert tray._tray.showMessage.call_count == int(expected)
    if expected:
        assert tray._last_notification_class == level
        body = tray._tray.showMessage.call_args.args[1]
        assert ('Click to change Windows notifications.' in body) is (level != 'reminder')
    else:
        assert tray._last_notification_class is None


def test_warning_and_info_entry_points_are_classified(tray):
    tray.notify_info('Tip', 'Optional')
    tray._tray.showMessage.assert_not_called()
    tray.notify_warning('No microphone', 'Cannot listen')
    assert tray._last_notification_class == 'important'
    assert tray._tray.showMessage.call_args.args[2] == QSystemTrayIcon.MessageIcon.Warning


def test_worker_notice_checks_preference_when_delivered(tray, qapp):
    worker = Thread(target=lambda: tray.notify_warning('Warning', 'Queued'))
    worker.start()
    worker.join()
    tray._app.config['notifications_level'] = 'off'
    qapp.processEvents()
    tray._tray.showMessage.assert_not_called()



def test_worker_notice_is_delivered_on_the_host_qobject_thread(tray, qapp):
    from PySide6.QtCore import QThread
    delivered_threads = []
    tray._tray.showMessage.side_effect = lambda *args: delivered_threads.append(
        QThread.currentThread())
    worker = Thread(target=lambda: tray.notify_warning('Warning', 'Queued'))
    worker.start()
    worker.join()
    tray._tray.showMessage.assert_not_called()
    qapp.processEvents()
    assert delivered_threads == [tray.thread()] == [qapp.thread()]


def test_suppressed_notice_does_not_replace_last_delivered_class(tray):
    tray._notify('reminder', 'Reminder', 'Due')
    tray.notify_info('Tip', 'Hidden')
    assert tray._last_notification_class == 'reminder'


@pytest.mark.parametrize('level', ['important', 'info'])
def test_click_opens_exact_general_row_even_with_an_update_available(tray, level):
    tools = Mock()
    tray._app._settings_qt = tools
    tray._last_notification_class = level
    tray._open_update_dialog = Mock()
    tray._on_message_clicked()
    tools._show_preferences.assert_called_once_with('General')
    tools._window.focus_setting.assert_called_once_with('notifications_level')
    tray._open_update_dialog.assert_not_called()


@pytest.mark.parametrize('level', [None, 'reminder'])
def test_reminder_or_absent_notification_click_does_not_open_settings(tray, level):
    tray._app._settings_qt = Mock()
    tray._last_notification_class = level
    tray._on_message_clicked()
    tray._app._settings_qt._show_preferences.assert_not_called()


def test_cold_click_creates_and_reuses_main_settings_controller(tray, monkeypatch):
    from samsara.ui import settings_qt
    settings = Mock()
    factory = Mock(return_value=settings)
    monkeypatch.setattr(settings_qt, 'SettingsQt', factory)
    tray._last_notification_class = 'important'
    tray._on_message_clicked()
    tray._on_message_clicked()
    factory.assert_called_once_with(tray._app)
    assert tray._app._settings_qt is settings
    assert settings._window.focus_setting.call_count == 2


@pytest.mark.parametrize('preference', LEVELS)
def test_actual_reminder_delivery_is_preserved_with_every_preference(tray, tmp_path,
                                                                    monkeypatch, preference):
    from datetime import datetime
    from samsara import notifications
    tray._app.config['notifications_level'] = preference
    manager = tray._app.notification_manager = notifications.NotificationManager(tmp_path)
    reminder = {'id': 'one', 'name': 'Reminder', 'message': 'Time for a break',
                'schedule': {'type': 'once'}, 'enabled': True}
    manager.reminders = [reminder]
    toast = Mock()
    def shown(title, message, on_shown):
        on_shown()
        return True
    toast.show.side_effect = shown
    monkeypatch.setattr(notifications, 'get_toast', lambda: toast)
    manager._fire_reminder(reminder, datetime(2026, 11, 2, 12))
    assert toast.show.call_count == 1
    assert reminder['enabled'] is False  # Existing actual-delivery acknowledgment.
    assert 'last_fired' in reminder


def test_no_windows_showmessage_calls_bypass_the_gate():
    root = Path(__file__).resolve().parents[1]
    calls = []
    paths = [root / 'dictation.py']
    for folder in ('samsara', 'plugins'):
        paths.extend((root / folder).rglob('*.py'))
    for path in paths:
        source = path.read_text(encoding='utf-8-sig')
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'showMessage'):
                continue
            relative = path.relative_to(root).as_posix()
            # QStatusBar.showMessage is an in-window status, not a balloon.
            if (relative == 'samsara/failure_reasons.py'
                    and isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Attribute)
                    and node.func.value.func.attr == 'statusBar'):
                continue
            calls.append(relative)
    # There is exactly one native dispatch, inside the policy callback.
    assert calls == ['samsara/notify_gate.py']
    source = (root / 'samsara/notify_gate.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    owner = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == '_deliver_notification')
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == 'notify' for node in ast.walk(owner))


def test_smart_corrections_is_info_not_a_reminder(tray, monkeypatch):
    from samsara import smart_corrections
    monkeypatch.setattr(smart_corrections, '_fallback_notice_shown', False)
    tray._app.tray_icon = tray
    tray._app.config['notifications_level'] = 'all'
    smart_corrections._notify_fallback_issue(tray._app, 'skip')
    assert tray._last_notification_class == 'info'


@pytest.fixture
def window(qapp, monkeypatch, request):
    from samsara.ui import settings_qt
    from tests.test_ava_page_guide_669 import _App
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda *a, **kw: None)
    monkeypatch.setattr('samsara.cuda_detect.request_cuda_status', lambda cb: None)
    monkeypatch.setattr(settings_qt.window_activation, 'confirm_foreground', lambda *a: None)
    monkeypatch.setattr('samsara.autostart.sync_run_entry', lambda *a, **kw: None)
    app = _App()
    if hasattr(request.node, 'callspec') and 'saved' in request.node.callspec.params:
        app.config['notifications_level'] = request.node.callspec.params['saved']
    win = settings_qt._SettingsWindow(app)
    yield win
    win.hide()
    win.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize('saved', LEVELS)
def test_general_row_reads_writes_and_is_sized_to_content(window, qapp, saved):
    window.app.config['notifications_level'] = saved
    window._controls_at_open['notifications_level'] = saved
    window.show_tab('General')
    qapp.processEvents()
    combo = window._widgets['notifications_level']
    assert combo.currentData() == saved
    assert [combo.itemText(i) for i in range(combo.count())] == ['Important only', 'All', 'Off']
    assert combo.accessibleName() == 'Windows notifications'
    assert combo.width() < 300
    row = window._rows_by_widget['notifications_level']
    assert row.page_name == 'General' and row.disclosure is None
    for choice in LEVELS:
        combo.setCurrentIndex(combo.findData(choice))
        updates = {}
        for save in window._source_saves['general']:
            updates.update(save(updates))
        assert updates['notifications_level'] == choice
        window.app.update_config(updates)
        assert saved_level(window.app.config) == choice


def test_missing_setting_shows_important_only_and_focus_navigation(window, qapp):
    window.show_tab('General')
    assert window._widgets['notifications_level'].currentData() == 'important'
    window._search_edit.setText('theme')
    window._apply_search_filter()
    window.focus_setting('notifications_level')
    qapp.processEvents()
    assert window._search_edit.text() == ''
    assert window._widgets['notifications_level'].isVisible()
