"""997b: canonical mic picker, preference state and deliberate-pick warnings."""
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from samsara.audio_devices import handsfree_warning, is_handsfree_profile
from samsara.ui.settings import general_qt
from samsara.ui.settings.general_qt import _microphone_updates, _populate_microphone_combo
from samsara.ui.settings_qt import _SettingsWindow
from samsara.ui import tray_microphones
from tests.test_settings import _StubApp
from tests.test_mic_missing_at_start_993 import absent, tray_menu

HFP = 'Headset (Example Hands-Free AG Audio)'
GOOD = 'Microphone (Example Stereo)'
MICS = [{'id': 1, 'name': HFP}, {'id': 2, 'name': GOOD}]


@pytest.mark.parametrize('name, expected', [
    (HFP, True), ('Example HF Audio', True), ('Example AG Audio', True),
    ('example hands-free', True), ('Desk microphone', False), ('Handsome Audio', False),
])
def test_profile_detection(name, expected):
    assert is_handsfree_profile(name) == expected


def test_suggestion_uses_same_headset_input_only():
    assert GOOD in handsfree_warning(HFP, MICS)
    assert 'Other' not in handsfree_warning(HFP, [{'name': 'Other Stereo'}])
    assert handsfree_warning(GOOD, MICS) is None


@pytest.fixture
def settings(qapp, qt_widget_scope):
    app = _StubApp()
    app.available_mics = list(MICS)
    app.config.update(microphone=2, microphone_name=GOOD)
    window = _SettingsWindow(app)
    with qt_widget_scope(window):
        # 1073 / T71 b5: General is the single microphone selection page.
        page = window._pages['General']
        window._stack.setCurrentWidget(page)
        window.resize(1180, 850)
        window.apply_theme()
        window.show()
        qapp.processEvents()
        yield window


def test_audio_picker_saves_raw_preference_and_clears_default(settings, monkeypatch):
    warn = Mock()
    monkeypatch.setattr(general_qt.QMessageBox, 'information', warn)
    # 1073: keep 997b's behavior contract on the surviving General picker.
    picker = settings._widgets['mic_combo']
    source = settings._widgets['mic_combo']
    assert picker.isVisible()
    assert picker.itemText(0) == 'System default'
    assert picker.itemText(2) == '★ ' + GOOD
    assert 'low quality - phone call mode' in picker.itemText(1)
    assert picker in settings._rows_by_widget['mic_combo'].findChildren(type(picker))
    assert 'audio_mic_combo' not in settings._widgets
    picker.setCurrentIndex(1)
    warn.assert_not_called()  # Programmatic changes never warn.
    picker.activated.emit(1)
    warn.assert_called_once()
    assert GOOD in warn.call_args.args[2]
    assert _microphone_updates(source) == {'microphone': 1, 'microphone_name': HFP}
    updates = {}
    for save in settings._source_saves['general']:
        updates.update(save(updates))
    assert updates['microphone_name'] == HFP and updates['microphone'] == 1
    picker.activated.emit(1)
    assert warn.call_count == 2  # Exactly one warning per pick, including reselect.
    picker.setCurrentIndex(0)
    picker.activated.emit(0)
    assert warn.call_count == 2
    assert _microphone_updates(source) == {'microphone': None, 'microphone_name': 'System default'}
    assert not any('★' in picker.itemText(i) for i in range(picker.count()))
    picker.setCurrentIndex(2)
    assert _microphone_updates(source) == {'microphone': 2, 'microphone_name': GOOD}


def test_shared_picker_refresh_and_disconnected_preference(settings):
    source = settings._widgets['mic_combo']
    # 1073: exercise the real Refresh action on the one remaining picker.
    picker = source
    settings.app._mic_refresh_blocked = Mock(return_value=False)
    settings.app.refresh_audio_devices = Mock(return_value=MICS[:1])
    refresh = settings.findChild(QPushButton, 'microphoneRefreshButton')
    refresh.click()
    settings.app.refresh_audio_devices.assert_called_once_with()
    assert picker.currentText() == 'System default'
    missing = picker.count() - 1
    assert picker.itemText(missing) == '★ ' + GOOD + ' (not connected)'
    assert not picker.model().flags(picker.model().index(missing, 0)) & Qt.ItemFlag.ItemIsEnabled
    assert _microphone_updates(source) == {}
    picker.activated.emit(0)
    assert _microphone_updates(source) == {'microphone': None, 'microphone_name': 'System default'}
    assert picker.count() == 2
    settings.app.refresh_audio_devices.return_value = MICS
    refresh.click()
    # A deliberately cleared preference survives refresh, despite unsaved config.
    assert picker.currentText() == 'System default'
    assert _microphone_updates(source) == {'microphone': None, 'microphone_name': 'System default'}
    _populate_microphone_combo(source, MICS, {'microphone': 2, 'microphone_name': GOOD})
    assert picker.currentText() == '★ ' + GOOD


def test_tray_warns_once_then_keeps_choice_available(qapp, monkeypatch):
    app, _audio = absent()
    app.available_mics = list(MICS)
    app.config = {'microphone': None, 'microphone_name': 'System default'}
    warn = Mock()
    monkeypatch.setattr(tray_microphones.QMessageBox, 'information', warn)
    menu = tray_menu(app)
    try:
        warn.assert_not_called()
        action = next(a for a in menu.actions() if HFP in a.text())
        assert action.isEnabled() and 'low quality - phone call mode' in action.text()
        action.trigger()
        warn.assert_called_once()
        app.switch_microphone_and_refresh.assert_called_once_with(1)
        action.trigger()
        assert warn.call_count == 2
    finally:
        menu.deleteLater()
