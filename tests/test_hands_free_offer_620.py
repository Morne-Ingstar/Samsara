"""Hands-free consent persists; new installs toggle, saved hold stays hold."""
from copy import deepcopy

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from samsara import config_defaults
from samsara.config_seed import build_default_config
from samsara.ui import first_run_pages_qt as pages
from tests.test_first_run_walkthrough_373 import make_window, finish, visit


def test_fresh_seed_uses_toggle_without_enabling_session(tmp_path):
    first = build_default_config(home=tmp_path, macro_defaults={}, notification_defaults={})
    second = build_default_config(home=tmp_path, macro_defaults={}, notification_defaults={})
    assert first['command_mode']['mode'] == 'toggle'
    assert first['command_mode']['enabled'] is False
    first['command_mode']['mode'] = 'hold'
    assert second['command_mode']['mode'] == 'toggle'
    assert config_defaults.SETTINGS_SCHEMA['command_mode.mode']['options'] == ['hold', 'toggle']


@pytest.mark.parametrize('enable', [True, False])
def test_offer_yes_no_survives_back_and_finish(make_window, enable):
    window = make_window()
    visit(window, 'Talking')
    offer = window._decisions.hands_free_offer
    assert offer.combo.currentData() is False
    assert window._config['command_mode']['mode'] == 'toggle'
    assert 'Tap Right Ctrl' in offer.description.text()
    assert 'again to stop' in offer.description.text()
    index = offer.combo.findData(enable)
    button = offer.segments.buttons[index]
    button.setFocus()
    QTest.keyClick(button, Qt.Key.Key_Space)
    window._go_next()
    window._go_back()
    assert offer.combo.currentData() is enable
    result = finish(window)
    assert result['command_mode']['enabled'] is enable
    assert result['command_mode']['mode'] == 'toggle'
    window._decisions.summary()
    assert f'Hands-free: {"On" if enable else "Off"}' in window._summary_labels[4].text()


@pytest.mark.parametrize('mode', ['hold', 'toggle'])
@pytest.mark.parametrize('enabled', [True, False])
def test_rerun_preserves_saved_session_and_other_settings(make_window, mode, enabled):
    saved = {'command_mode': {'enabled': enabled, 'mode': mode, 'button': 'mouse5',
                             'inactivity_timeout_s': 88, 'extra': 'retained'}}
    window = make_window(saved)
    visit(window, 'Talking')
    offer = window._decisions.hands_free_offer
    assert offer.combo.currentData() is enabled
    assert ('Hold' if mode == 'hold' else 'Tap') in offer.description.text()
    assert 'Mouse 5' in offer.description.text()
    before = deepcopy(window._config)
    result = finish(window)
    assert result['command_mode'] == before['command_mode']


def test_rebound_key_updates_offer_and_persists(make_window):
    window = make_window()
    visit(window, 'Talking')
    decisions = window._decisions
    decisions.hands_free._combo = 'f8'
    decisions.hands_free.bindingChanged.emit('f8')
    assert 'F8' in decisions.hands_free_offer.description.text()
    decisions.hands_free_offer.segments.buttons[0].click()
    result = finish(window)
    assert result['command_mode']['button'] == 'f8'
    assert result['command_mode']['enabled'] is True


def test_legacy_missing_mode_retains_hold_fallback(tmp_path):
    path = tmp_path / 'legacy.json'
    path.write_text('{"command_mode": {"enabled": true}}', encoding='utf-8')
    config, _ = pages.initial_config({}, path)
    assert config['command_mode']['mode'] == 'hold'
