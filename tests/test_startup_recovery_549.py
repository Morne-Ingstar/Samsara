"""Absent picker choices and early second-start activation, without a live app."""
import ctypes
import inspect
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QComboBox

from samsara import boot, single_instance as instance
from samsara.ui.settings.general_qt import (
    _MIC_CHOICE_ROLE, _microphone_updates, _populate_microphone_combo,
)
from samsara.ui.settings.services import SettingsServices, apply_microphone_choice


@pytest.fixture
def combo(qapp):
    widget = QComboBox()
    yield widget
    widget.deleteLater()


@pytest.mark.parametrize('config', [
    {'microphone': 9, 'microphone_name': 'Saved headset'},
    {'microphone': None, 'microphone_name': 'Saved headset'},
])
def test_absent_choice_submits_neither_key_and_never_switches(combo, config):
    _populate_microphone_combo(combo, [], config)
    assert combo.currentIndex() == 1
    assert '(not connected)' in combo.currentText()
    assert _microphone_updates(combo) == {}
    app = SimpleNamespace(config=dict(config), switch_microphone=Mock())
    apply_microphone_choice(app, _microphone_updates(combo))
    assert app.config == config
    app.switch_microphone.assert_not_called()


def test_unnamed_id_matching_nothing_keeps_the_system_default_row(combo):
    # G2: "Microphone 999 (not connected)" tells a user nothing; HEAD's behaviour stays.
    _populate_microphone_combo(combo, [{'id': 9, 'name': 'Other'}], {'microphone': 999})
    assert combo.currentIndex() == 0
    assert combo.currentData() is None
    assert all('not connected' not in combo.itemText(i) for i in range(combo.count()))


def test_apply_microphone_choice_is_a_function_not_a_method_of_the_caller():
    # G1: _apply_and_close runs on whatever object it is handed.
    assert not hasattr(SettingsServices, '_apply_microphone_choice')


def test_name_wins_over_reused_id(combo):
    devices = [{'id': 9, 'name': 'Other'}, {'id': 24, 'name': 'Saved headset'}]
    _populate_microphone_combo(combo, devices,
                               {'microphone': 9, 'microphone_name': 'Saved headset'})
    assert _microphone_updates(combo) == {'microphone': 24, 'microphone_name': 'Saved headset'}


def test_absent_name_does_not_select_unrelated_device_at_old_id(combo):
    _populate_microphone_combo(combo, [{'id': 9, 'name': 'Other'}],
                               {'microphone': 9, 'microphone_name': 'Saved headset'})
    assert combo.currentText() == 'Saved headset (not connected)'
    assert _microphone_updates(combo) == {}


def test_legacy_id_only_config_matches_by_id(combo):
    _populate_microphone_combo(combo, [{'id': 9, 'name': 'Saved headset'}], {'microphone': 9})
    assert _microphone_updates(combo) == {'microphone': 9, 'microphone_name': 'Saved headset'}


@pytest.mark.parametrize('config', [{}, {'microphone': None},
                                 {'microphone': 9, 'microphone_name': 'System default'}])
def test_default_is_first_and_deliberate(combo, config):
    _populate_microphone_combo(combo, [{'id': 9, 'name': 'Other'}], config)
    assert combo.currentIndex() == 0
    assert _microphone_updates(combo) == {'microphone': None, 'microphone_name': 'System default'}


def test_refresh_preserves_missing_then_reselects_reconnected_name(combo):
    config = {'microphone': 9, 'microphone_name': 'Saved headset'}
    _populate_microphone_combo(combo, [], config)
    _populate_microphone_combo(combo, [], combo.currentData(_MIC_CHOICE_ROLE))
    assert combo.currentText() == 'Saved headset (not connected)'
    _populate_microphone_combo(combo, [{'id': 24, 'name': 'Saved headset'}],
                               combo.currentData(_MIC_CHOICE_ROLE))
    assert _microphone_updates(combo)['microphone'] == 24


def test_refresh_keeps_explicit_default_even_when_headset_returns(combo):
    _populate_microphone_combo(combo, [], {'microphone': 9, 'microphone_name': 'Saved headset'})
    combo.setCurrentIndex(0)
    _populate_microphone_combo(combo, [{'id': 24, 'name': 'Saved headset'}],
                               combo.currentData(_MIC_CHOICE_ROLE))
    assert _microphone_updates(combo) == {'microphone': None, 'microphone_name': 'System default'}


@pytest.mark.parametrize('old_id', [9, None])
def test_explicit_default_name_reaches_resolver_before_audio_switch(combo, old_id):
    app = SimpleNamespace(config={'microphone': old_id, 'microphone_name': 'Saved headset'},
                          _config_lock=threading.Lock())
    seen = []
    def capture(*args):
        seen.append((args, dict(app.config)))
    app.switch_microphone = Mock(side_effect=capture)
    app.refresh_audio_devices = Mock(side_effect=capture)
    _populate_microphone_combo(combo, [], app.config)
    combo.setCurrentIndex(0)
    updates = _microphone_updates(combo)
    apply_microphone_choice(app, updates)
    app.config.update(updates)  # The subsequent locked bulk save.
    assert seen[0][1]['microphone_name'] == 'System default'
    assert app.config == {'microphone': None, 'microphone_name': 'System default'}
    if old_id is None:
        app.switch_microphone.assert_not_called()
        app.refresh_audio_devices.assert_called_once()
    else:
        app.switch_microphone.assert_called_once_with(None)


def test_failed_switch_restores_previous_name():
    app = SimpleNamespace(config={'microphone': 9, 'microphone_name': 'Saved headset'},
                          _config_lock=threading.Lock(),
                          switch_microphone=Mock(side_effect=RuntimeError('failed')))
    with pytest.raises(RuntimeError):
        apply_microphone_choice(app, {'microphone': None, 'microphone_name': 'System default'})
    assert app.config['microphone_name'] == 'Saved headset'


class FakeAPI:
    def __init__(self, *, hwnd=42, reply=1, delivered=1):
        self.hwnd, self.reply, self.delivered = hwnd, reply, delivered
        self.calls = []

    def RegisterWindowMessageW(self, name):
        return 0xC123

    def FindWindowW(self, cls, title):
        self.calls.append(('find', title))
        return self.hwnd

    def GetWindowThreadProcessId(self, hwnd, pid):
        pid._obj.value = 1234
        return 22

    def AllowSetForegroundWindow(self, pid):
        self.calls.append(('grant', pid))
        return 1

    def SendMessageTimeoutW(self, hwnd, msg, wp, lp, flags, timeout, reply):
        self.calls.append(('send', hwnd, flags, timeout))
        reply._obj.value = self.reply
        return self.delivered


def test_sender_grants_specific_pid_before_signal_and_requires_ack():
    api = FakeAPI()
    assert instance.request_existing_activation('test-profile', api=api)
    assert api.calls[0] == ('find', 'test-profile.ActivationReceiver')
    assert api.calls[1] == ('grant', 1234)
    assert api.calls[2][0] == 'send'
    assert 0 < api.calls[2][3] <= 2000
    assert api.calls[2][2] == 0x23


@pytest.mark.parametrize('hwnd,reply,delivered', [(0, 0, 0), (42, 0, 1), (42, 1, 0)])
def test_unavailable_or_unresponsive_instance_has_one_two_second_budget(hwnd, reply, delivered):
    now = [0.0]
    api = FakeAPI(hwnd=hwnd, reply=reply, delivered=delivered)
    def sleep(duration):
        now[0] += duration
    assert not instance.request_existing_activation('test-profile', api=api,
        clock=lambda: now[0], sleep=sleep)
    assert now[0] == pytest.approx(2.0)


def test_sender_waits_for_receiver_while_first_process_starts():
    now = [0.0]
    api = FakeAPI(hwnd=0)
    def sleep(duration):
        now[0] += duration
        api.hwnd = 42
    assert instance.request_existing_activation('test-profile', api=api,
        clock=lambda: now[0], sleep=sleep)
    assert now[0] < 2.0


@pytest.mark.parametrize('outcome', [False, OSError('unavailable')])
def test_failed_request_shows_notice(monkeypatch, outcome):
    request = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
    notice = Mock()
    monkeypatch.setattr(instance, 'request_existing_activation', request)
    monkeypatch.setattr(instance, '_show_activation_failure', notice)
    assert not instance.bring_existing_forward('test-profile')
    notice.assert_called_once()


def test_successful_request_does_not_show_failure(monkeypatch):
    monkeypatch.setattr(instance, 'request_existing_activation', lambda _: True)
    notice = Mock()
    monkeypatch.setattr(instance, '_show_activation_failure', notice)
    assert instance.bring_existing_forward('test-profile')
    notice.assert_not_called()


def window(kind, visible=True):
    return type(kind, (), {'isVisible': lambda self: visible})()


@pytest.mark.parametrize('kind', ['_WizardWindow', 'TutorialWindow', '_SettingsWindow', '_MainWindow'])
def test_each_existing_task_window_is_selected(kind):
    target = window(kind)
    assert instance._choose_activation_window([window('_MainWindow', False), target]) is target


def test_active_modal_and_last_visible_task_have_priority():
    home, settings, tutorial, modal = [window(k) for k in
        ('_MainWindow', '_SettingsWindow', 'TutorialWindow', 'QMessageBox')]
    windows = [home, settings, tutorial]
    assert instance._choose_activation_window(windows, modal=modal, active=home) is modal
    assert instance._choose_activation_window(windows, active=settings, last=tutorial) is settings
    assert instance._choose_activation_window(windows, last=settings) is settings
    assert instance._choose_activation_window(windows) is tutorial


def test_hidden_completed_setup_is_not_reopened():
    home = window('_MainWindow', False)
    assert instance._choose_activation_window([window('_WizardWindow', False), home]) is home


def test_lazy_home_is_requested_without_importing_app(monkeypatch):
    from PySide6.QtWidgets import QApplication
    import __main__
    app = SimpleNamespace(show_main_window=Mock())
    monkeypatch.setattr(__main__, 'app', app, raising=False)
    monkeypatch.setattr(QApplication, 'topLevelWidgets', lambda: [])
    monkeypatch.setattr(QApplication, 'activeModalWidget', lambda: None)
    monkeypatch.setattr(QApplication, 'activeWindow', lambda: None)
    assert not instance._activate_existing_window()
    app.show_main_window.assert_called_once()


def test_listener_is_posted_to_existing_runtime(monkeypatch):
    from samsara.ui import qt_runtime
    queued, installed = [], []
    monkeypatch.setattr(instance.sys, 'platform', 'win32')
    monkeypatch.setattr(instance, 'mutex_name_for_profile', lambda: 'test-profile')
    monkeypatch.setattr(qt_runtime, 'post', queued.append)
    monkeypatch.setattr(instance, '_create_activation_receiver', installed.append)
    instance.start_activation_listener()
    assert installed == []
    queued[0]()
    assert installed == ['test-profile']
    source = inspect.getsource(boot.create_splash)
    assert source.index('SplashScreenQt()') < source.index('start_activation_listener()')


def test_receiver_dispatches_native_message_before_any_app_exists(qapp, monkeypatch):
    from ctypes import wintypes
    monkeypatch.setattr(instance, '_activation_receiver', None)
    monkeypatch.setattr(instance, '_activation_user32', lambda: FakeAPI())
    activate = Mock(return_value=True)
    monkeypatch.setattr(instance, '_activate_existing_window', activate)
    instance._create_activation_receiver('isolated-test-profile')
    receiver = instance._activation_receiver
    try:
        msg = wintypes.MSG()
        msg.message = 0xC123
        assert receiver.nativeEvent(b'windows_generic_MSG', ctypes.addressof(msg)) == (True, 1)
        activate.assert_called_once_with(None)
        assert not receiver.isVisible()
    finally:
        qapp.removeEventFilter(receiver)
        receiver.deleteLater()


def test_receiver_only_acknowledges_verified_foreground(qapp, monkeypatch):
    from PySide6.QtWidgets import QApplication
    target = SimpleNamespace(isMinimized=lambda: True, showNormal=Mock(), show=Mock(),
        raise_=Mock(), activateWindow=Mock(), winId=lambda: 42)
    monkeypatch.setattr(instance, '_choose_activation_window', lambda *a, **k: target)
    api = SimpleNamespace(SetForegroundWindow=Mock(), GetForegroundWindow=lambda: 7)
    monkeypatch.setattr(instance, '_activation_user32', lambda: api)
    assert not instance._activate_existing_window()
    target.showNormal.assert_called_once()
    api.GetForegroundWindow = lambda: 42
    assert instance._activate_existing_window()


def test_ui_copy_proof(qapp, combo, tmp_path):
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
    from samsara.ui import theme
    panel = QWidget()
    layout = QVBoxLayout(panel)
    layout.addWidget(QLabel('Microphone'))
    _populate_microphone_combo(combo, [], {'microphone': 9, 'microphone_name': 'Saved headset'})
    layout.addWidget(combo)
    panel.setStyleSheet(f'background:{theme.BG0}; color:{theme.TEXT_PRIMARY}; font-size:16px;')
    panel.resize(580, 120)
    panel.show()
    dialog = instance._activation_failure_dialog()
    dialog.show()
    qapp.processEvents()
    assert panel.grab().save(str(tmp_path / 'missing-microphone.png'))
    assert dialog.grab().save(str(tmp_path / 'activation-message.png'))
    dialog.close()
    panel.hide()
    combo.setParent(None)
    panel.deleteLater()
    dialog.deleteLater()


# Exercise the real General save closure and Settings Apply, with 543's inert
# fixture (no engines, registry changes, live profile or application bootstrap).
from tests.test_settings_revisit_543 import window as settings_window


def test_unrelated_apply_preserves_missing_microphone(settings_window, qapp, monkeypatch):
    from samsara.ui import window_activation
    monkeypatch.setattr(window_activation, 'surface_requested', lambda w, **k: w.show())
    win = settings_window
    win.hide()
    win.app.config.update(microphone=9, microphone_name='Saved headset')
    win.app.available_mics = []
    win.app.switch_microphone = Mock()
    win.show_tab('Audio')
    qapp.processEvents()
    assert win._widgets['mic_combo'].currentText() == 'Saved headset (not connected)'
    win._widgets['auto_capitalize'].setChecked(False)
    win._apply_and_close()
    assert win.app.config['microphone'] == 9
    assert win.app.config['microphone_name'] == 'Saved headset'
    assert win.app.config['auto_capitalize'] is False
    win.app.switch_microphone.assert_not_called()
    win.app.save_config.assert_called()


def test_refused_second_start_message_takes_the_theme(qapp):
    # G3: it was a plain grey Windows box.
    from samsara.ui import theme
    dialog = instance._activation_failure_dialog()
    assert '#5cc4d4' in dialog.styleSheet() and 'QRadioButton' in dialog.styleSheet()
    dialog.deleteLater()
