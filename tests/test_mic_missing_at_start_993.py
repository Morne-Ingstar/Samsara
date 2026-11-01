"""993: active default selection and the remembered reconnect preference agree."""
from types import SimpleNamespace
# 997b: HFP display labels include the phone call quality suffix.
from unittest.mock import Mock
import sys

import pytest
from PySide6.QtWidgets import QComboBox, QPushButton

from samsara import microphone_recovery as recovery
from samsara.audio_engine import device_resolver
from samsara.audio_engine.engine import AudioCaptureEngine
from samsara.ui.home_signals import microphone_name
from samsara.ui.settings.general_qt import (
    _microphone_updates, _populate_microphone_combo,
)
from samsara.ui.tray_microphones import build_microphone_menu
from tests.test_mic_index_drift_901 import Audio, application, device

HEADSET = 'Headset (Bluetooth HF Audio)'
SAVED = {'microphone': 1, 'microphone_name': HEADSET}


def absent():
    # The saved ID is reused by a different mic; the Windows default is NOT first.
    audio = Audio([device('Output', channels=0), device('Desk microphone'),
                   device('Built-in microphone')], default=2)
    app = application(SAVED)
    app.available_mics = [{'id': i, 'name': d['name']}
                          for i, d in enumerate(audio.devices) if d['max_input_channels']]
    app._mic_refresh_blocked = lambda: False
    app.get_available_microphones = lambda: list(app.available_mics)
    app._reconcile_microphone_selection = lambda: recovery.reconcile(app, audio)
    app.switch_microphone_and_refresh = Mock()
    app._ace_engine = SimpleNamespace(device_lost=False, capture_name='Built-in microphone')
    app._no_microphone = False
    return app, audio


def tray_menu(app):
    def add(menu, label, fn, *, checked, **kwargs):
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(checked)
        action.triggered.connect(fn)
    return build_microphone_menu(app, add)


def assert_default_surfaces(app):
    combo = QComboBox()
    menu = tray_menu(app)
    try:
        _populate_microphone_combo(combo, app.available_mics, app.config)
        assert combo.currentIndex() == 0
        assert combo.currentText() == 'System default'
        assert combo.currentData() is None
        assert microphone_name(app) == 'System default'
        assert recovery.selection_name(app) == 'System default'
        assert menu.title() == '[MIC]  System default'
        assert [a.text() for a in menu.actions() if a.isChecked()] == ['System default']
        assert all(combo.itemText(i) for i in range(combo.count()))
        assert all(a.text() for a in menu.actions())
    finally:
        combo.deleteLater()
        menu.deleteLater()


def test_absent_startup_opens_windows_default_and_all_surfaces_agree(qapp, monkeypatch):
    app, audio = absent()
    assert device_resolver.select_input(audio, app.config) == (2, True)
    recovery.reconcile(app, audio)
    ring = SimpleNamespace(write=lambda *a, **k: None, bump_device_epoch=lambda: 1)
    monkeypatch.setitem(sys.modules, 'sounddevice', audio)
    capture = AudioCaptureEngine(ring, config=app.config)
    try:
        capture.start()
        assert audio.opened == [2]
        assert capture.running and not capture.device_lost
        assert_default_surfaces(app)
        assert app.config == SAVED and app.saved == []
    finally:
        capture.stop()


@pytest.mark.parametrize('config', [{}, {'microphone': None},
                                   {'microphone': None, 'microphone_name': ''},
                                   {'microphone': None, 'microphone_name': 'System default'}])
def test_never_chosen_and_explicit_default_are_nonempty_everywhere(qapp, config):
    app, audio = absent()
    app.config = dict(config)
    assert device_resolver.select_input(audio, app.config) == (2, False)
    recovery.reconcile(app, audio)
    assert_default_surfaces(app)
    assert app.config == config


def test_fallback_follows_windows_default_without_saving_over_headset(qapp):
    app, audio = absent()
    for default in (2, 1, 2):
        audio.default.device = (default, -1)
        recovery.reconcile(app, audio)
        assert (app._capture_device, app._using_default_mic) == (default, True)
        assert_default_surfaces(app)
    assert app.config == SAVED and app.saved == []


def test_reconnect_preserves_master_automatic_return_then_disconnect_falls_back(qapp):
    app, audio = absent()
    recovery.reconcile(app, audio)
    assert_default_surfaces(app)
    audio.devices.append(device(HEADSET))
    app.available_mics.append({'id': 3, 'name': HEADSET})
    recovery.reconcile(app, audio)
    app._ace_engine.capture_name = HEADSET
    assert device_resolver.select_input(audio, app.config) == (3, False)
    assert app.config == {'microphone': 3, 'microphone_name': HEADSET}
    assert app.saved == [app.config]
    combo, menu = QComboBox(), tray_menu(app)
    try:
        _populate_microphone_combo(combo, app.available_mics, app.config)
        # 993b: preferred entries carry a display-only star; Home keeps the raw name.
        assert microphone_name(app) == HEADSET  # 997b: quality text is display-only.
        assert combo.currentText() == '★ ' + HEADSET + ' (low quality - phone call mode)'
        assert [a.text() for a in menu.actions() if a.isChecked()] == ['★ ' + HEADSET + ' (low quality - phone call mode)']
    finally:
        combo.deleteLater()
        menu.deleteLater()
    audio.devices.pop()
    app.available_mics.pop()
    recovery.reconcile(app, audio)
    app._ace_engine.capture_name = 'Built-in microphone'
    assert_default_surfaces(app)
    assert app.config['microphone_name'] == HEADSET
    assert len(app.saved) == 1


def test_settings_fallback_apply_does_not_erase_saved_choice(qapp):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp
    owner, audio = absent()
    app = _StubApp()
    app.config.update(SAVED)
    app.available_mics = owner.available_mics
    app.switch_microphone = Mock()
    win = _SettingsWindow(app)
    try:
        combo = win._widgets['mic_combo']
        assert combo.currentText() == 'System default'
        assert 'chosen microphone is not connected' in combo.toolTip()
        assert _microphone_updates(combo) == {}
        win._apply_and_close()
        assert app.config['microphone'] == SAVED['microphone']
        assert app.config['microphone_name'] == HEADSET
        app.switch_microphone.assert_not_called()
    finally:
        win.close()
        win.deleteLater()


def test_settings_refresh_retains_preference_and_returns_on_reconnect(qapp):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp
    owner, audio = absent()
    app = _StubApp()
    app.config.update(SAVED)
    app.available_mics = owner.available_mics
    app._mic_refresh_blocked = lambda: False
    app.refresh_audio_devices = lambda: app.available_mics
    win = _SettingsWindow(app)
    try:
        combo = win._widgets['mic_combo']
        refresh = win.findChild(QPushButton, 'microphoneRefreshButton')
        refresh.click()
        assert combo.currentText() == 'System default'
        assert _microphone_updates(combo) == {}
        app.available_mics.append({'id': 3, 'name': HEADSET})
        refresh.click()
        assert combo.currentText() == '★ ' + HEADSET + ' (low quality - phone call mode)'  # 993b: preferred label.
        assert _microphone_updates(combo) == {'microphone': 3, 'microphone_name': HEADSET}
        app.available_mics.pop()
        refresh.click()
        assert combo.currentText() == 'System default'
        assert _microphone_updates(combo) == {}
        assert app.config == SAVED
    finally:
        win.close()
        win.deleteLater()


def test_user_reselecting_default_removes_reconnect_preference_from_settings_update(qapp):
    app, audio = absent()
    combo = QComboBox()
    try:
        _populate_microphone_combo(combo, app.available_mics, app.config)
        assert _microphone_updates(combo) == {}
        combo.activated.emit(0)  # Even though fallback is already the visible row.
        chosen = _microphone_updates(combo)
        assert chosen == {'microphone': None, 'microphone_name': 'System default'}
        _populate_microphone_combo(combo, app.available_mics + [{'id': 3, 'name': HEADSET}], chosen)
        assert combo.currentText() == 'System default'
        assert app.config == SAVED  # Staged until Apply.
    finally:
        combo.deleteLater()


def test_user_selecting_concrete_input_wins_over_fallback(qapp):
    app, audio = absent()
    combo = QComboBox()
    try:
        _populate_microphone_combo(combo, app.available_mics, app.config)
        combo.setCurrentText('Desk microphone')
        assert _microphone_updates(combo) == {'microphone': 1, 'microphone_name': 'Desk microphone'}
        combo.setCurrentIndex(0)
        assert _microphone_updates(combo) == {'microphone': None, 'microphone_name': 'System default'}
    finally:
        combo.deleteLater()


def test_healthy_named_capture_label_survives_stale_enumeration():
    app, audio = absent()
    app.config = {'microphone': 1, 'microphone_name': 'Desk microphone'}
    app._ace_engine.capture_name = 'Desk microphone'
    app._using_default_mic = False
    app.available_mics.clear()
    assert microphone_name(app) == 'Desk microphone'


@pytest.mark.parametrize('present', [False, True])
def test_mic_setup_uses_name_not_reused_saved_index(qapp, present):
    from samsara.ui.mic_setup_wizard_qt import _WizardWindow
    app, audio = absent()
    if present:
        app.available_mics.append({'id': 3, 'name': HEADSET})
    combo = QComboBox()
    wizard = SimpleNamespace(_app=app, _device_combo=combo)
    try:
        _WizardWindow._populate_devices(wizard)
        assert combo.currentText() == (HEADSET if present else 'System default')
        assert wizard._selected_device == (3 if present else None)
        assert app.config == SAVED
    finally:
        combo.deleteLater()


def test_no_hardware_still_shows_default_choice_and_truthful_home_unavailable(qapp):
    app, audio = absent()
    audio.devices.clear()
    audio.default.device = (-1, -1)
    app.available_mics.clear()
    recovery.reconcile(app, audio)
    app._ace_engine.device_lost = True
    app._no_microphone = True
    combo, menu = QComboBox(), tray_menu(app)
    try:
        _populate_microphone_combo(combo, [], app.config)
        assert combo.currentText() == 'System default'
        assert menu.actions()[0].isChecked()
        assert microphone_name(app) == 'No microphone'
        assert app._capture_device is None
        assert app.config == SAVED
    finally:
        combo.deleteLater()
        menu.deleteLater()
