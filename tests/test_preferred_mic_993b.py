"""Preferred mic priority, idle return and display-only stars with fake audio."""
# 997b: HFP display labels include the phone call quality suffix.
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox

from samsara import microphone_recovery as recovery
from samsara.audio_engine import device_resolver
from samsara.ui.settings.general_qt import _microphone_updates, _populate_microphone_combo
from tests.test_mic_missing_at_start_993 import HEADSET, SAVED, absent, tray_menu
from tests.test_mic_index_drift_901 import MIC, Audio, application, boot_app, device


@pytest.mark.parametrize('present', [False, True])
def test_preference_star_and_active_check_are_independent(qapp, present):
    app, audio = absent()
    if present:
        audio.devices.append(device(HEADSET))
        app.available_mics.append({'id': 3, 'name': HEADSET})
    combo, menu = QComboBox(), tray_menu(app)
    try:
        _populate_microphone_combo(combo, app.available_mics, app.config)
        label = '★ ' + HEADSET + ' (low quality - phone call mode)' + ('' if present else ' (not connected)')
        starred = [a for a in menu.actions() if a.text().startswith('★')]
        assert len(starred) == 1 and starred[0].text() == label
        assert starred[0].isEnabled() == present
        assert [a.text() for a in menu.actions() if a.isChecked()] == [
            '★ ' + HEADSET + ' (low quality - phone call mode)' if present else 'System default']
        row = next(i for i in range(combo.count()) if combo.itemText(i) == label)
        assert bool(combo.model().flags(combo.model().index(row, 0)) & Qt.ItemFlag.ItemIsEnabled) == present
        assert combo.currentIndex() >= 0
        assert combo.currentText() == ('★ ' + HEADSET + ' (low quality - phone call mode)' if present else 'System default')
        assert combo.itemData(row) == (3 if present else SAVED['microphone'])
        assert _microphone_updates(combo) == (
            {'microphone': 3, 'microphone_name': HEADSET} if present else {})
    finally:
        combo.deleteLater()
        menu.deleteLater()


def test_staged_selection_moves_star_and_saves_raw_name(qapp):
    app, _audio = absent()
    combo = QComboBox()
    try:
        _populate_microphone_combo(combo, app.available_mics, app.config)
        combo.setCurrentIndex(1)
        assert combo.currentText() == '★ Desk microphone'
        assert [combo.itemText(i) for i in range(combo.count())] == [
            'System default', '★ Desk microphone', 'Built-in microphone']
        assert _microphone_updates(combo) == {'microphone': 1, 'microphone_name': 'Desk microphone'}
        combo.setCurrentIndex(0)
        assert _microphone_updates(combo) == {'microphone': None, 'microphone_name': 'System default'}
        assert not any('★' in combo.itemText(i) for i in range(combo.count()))
        assert app.config == SAVED  # Staged until Apply.
    finally:
        combo.deleteLater()


def test_reselect_active_default_removes_disconnected_star(qapp):
    app, _audio = absent()
    combo = QComboBox()
    try:
        _populate_microphone_combo(combo, app.available_mics, app.config)
        combo.activated.emit(0)
        assert combo.count() == len(app.available_mics) + 1
        assert _microphone_updates(combo) == {'microphone': None, 'microphone_name': 'System default'}
        _populate_microphone_combo(combo, app.available_mics + [{'id': 3, 'name': HEADSET}],
                                   _microphone_updates(combo))
        assert combo.currentText() == 'System default'
        assert not any('★' in combo.itemText(i) for i in range(combo.count()))
    finally:
        combo.deleteLater()


def test_tray_selection_reusing_disconnected_preferred_id_saves_new_preference(qapp):
    app, _audio = absent()
    app.refresh_audio_devices = Mock()
    menu = tray_menu(app)
    try:
        next(a for a in menu.actions() if a.text() == 'Desk microphone').trigger()
        assert app.config == {'microphone': 1, 'microphone_name': 'Desk microphone'}
        assert app.saved == [app.config]
        app.refresh_audio_devices.assert_called_once_with()
        app.switch_microphone_and_refresh.assert_not_called()
    finally:
        menu.deleteLater()


def test_explicit_default_cancels_automatic_return(qapp):
    app, audio = absent()
    app._ace_engine = None
    app.refresh_audio_devices = Mock()
    app.switch_microphone_and_refresh = lambda index: app.config.update(microphone=index)
    menu = tray_menu(app)
    try:
        menu.actions()[0].trigger()
        assert app.config == {'microphone': None, 'microphone_name': 'System default'}
        audio.devices.append(device(HEADSET))
        app.available_mics.append({'id': 3, 'name': HEADSET})
        assert device_resolver.select_input(audio, app.config) == (2, False)
        new_menu = tray_menu(app)
        try:
            assert not any('★' in a.text() for a in new_menu.actions())
            assert new_menu.actions()[0].isChecked()
        finally:
            new_menu.deleteLater()
    finally:
        menu.deleteLater()


@pytest.mark.parametrize('busy_mode', [None, 'recording', 'continuous_active', 'wake_word_active',
                                      'command_mode_active', 'ava_mode_active', 'ava_command_session_active'])
def test_hotplug_returns_to_preferred_only_at_idle(monkeypatch, busy_mode):
    now = [0.0]
    app, audio, changes = boot_app(monkeypatch, now, fallback=True)
    app._mic_boot_ready = app._mic_boot_scan_complete = True
    app._rescan_idle_since = -10.0
    endpoints = {('builtin', 'Built-in')}
    changes.endpoints = lambda: frozenset(endpoints)
    changes.prime()
    if busy_mode:
        setattr(app, busy_mode, True)
    try:
        initial = list(audio.opened)
        audio.pending = [device('Built-in'), device(MIC)]
        endpoints.add(('preferred', MIC))
        changes.notify()
        now[0] = 1.0
        changes.tick()
        if busy_mode:
            assert audio.opened == initial
            assert app._using_default_mic
            assert changes._input_pending
            setattr(app, busy_mode, False)
            now[0] = 2.0
            changes.tick()
        assert audio.opened[-1] == 1
        assert app._ace_engine.capture_name == MIC
        assert not app._using_default_mic
        assert app.config['microphone_name'] == MIC
        assert not changes._input_pending
    finally:
        app._ace_engine.stop()


def test_saved_preference_wins_at_startup_and_survives_absence():
    app = application({'microphone': 9, 'microphone_name': MIC})
    audio = Audio([device('Built-in'), device(MIC)], default=0)
    assert device_resolver.select_input(audio, app.config) == (1, False)
    audio.devices.pop()
    recovery.reconcile(app, audio)
    assert (app._capture_device, app._using_default_mic) == (0, True)
    assert app.config == {'microphone': 9, 'microphone_name': MIC}
    audio.devices.append(device(MIC))
    recovery.reconcile(app, audio)
    assert (app._capture_device, app._using_default_mic) == (1, False)
    assert app.config['microphone_name'] == MIC
