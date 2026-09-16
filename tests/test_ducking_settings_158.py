"""Queue 158: hands-free ducking is discoverable and saves safely."""

from __future__ import annotations

from pathlib import Path

import pytest


_KEYS = {
    'ducking.hands_free_enabled': True,
    'ducking.hands_free_level': 0.15,
    'ducking.hands_free_idle_level': 0.8,
}


def _advanced_save(config, qapp):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp

    app = _StubApp()
    app.config = config
    window = _SettingsWindow(app)
    save = next(
        fn for fn in window._save_fns
        if '_build_advanced_tab' in fn.__qualname__
    )
    return window, save


def test_schema_defaults_reach_advanced_controls_and_saved_ducking(qapp):
    from samsara.config_defaults import DEFAULTS
    from samsara.config_schema import SETTINGS_SCHEMA

    for key, default in _KEYS.items():
        entry = SETTINGS_SCHEMA[key]
        assert entry['type'] in {'bool', 'float'}
        assert entry['default'] == default == DEFAULTS[key]
        assert entry['tab'] == 'advanced'
    assert SETTINGS_SCHEMA['ducking.hands_free_level']['depends_on'] == (
        'ducking.hands_free_enabled'
    )
    assert SETTINGS_SCHEMA['ducking.hands_free_idle_level']['depends_on'] == (
        'ducking.hands_free_enabled'
    )

    window, save = _advanced_save({}, qapp)
    try:
        assert window._widgets['adv_hands_free_ducking_enabled'].isChecked() is True
        assert window._widgets['adv_hands_free_ducking_level'].value() == pytest.approx(0.15)
        assert window._widgets['adv_hands_free_idle_level'].value() == pytest.approx(0.8)

        window._widgets['adv_hands_free_ducking_enabled'].setChecked(False)
        window._widgets['adv_hands_free_ducking_level'].setValue(0.25)
        window._widgets['adv_hands_free_idle_level'].setValue(0.65)
        saved = save({})['ducking']

        assert saved['hands_free_enabled'] is False
        assert saved['hands_free_level'] == pytest.approx(0.25)
        assert saved['hands_free_idle_level'] == pytest.approx(0.65)
    finally:
        window.deleteLater()


def test_existing_hands_free_values_round_trip_without_dropping_siblings(qapp):
    original = {
        'ducking': {
            'enabled': True,
            'level': 0.4,
            'hands_free_enabled': False,
            'hands_free_level': 0.35,
            'hands_free_idle_level': 0.7,
            'future_ducking_key': 'keep me',
        }
    }
    window, save = _advanced_save(original, qapp)
    try:
        assert window._widgets['adv_hands_free_ducking_enabled'].isChecked() is False
        assert window._widgets['adv_hands_free_ducking_level'].value() == pytest.approx(0.35)
        assert window._widgets['adv_hands_free_idle_level'].value() == pytest.approx(0.7)

        saved = save({})['ducking']
        assert saved == original['ducking']
    finally:
        window.deleteLater()


def test_hands_free_consumer_reads_the_same_three_keys_without_importing_app():
    """Pin the live consumer contract without importing dictation.py.

    Importing the live app attaches a second log handler while Samsara runs;
    source inspection keeps this regression test hermetic while proving the
    Settings keys and consumer reads stay aligned.
    """
    source = (Path(__file__).parent.parent / 'dictation.py').read_text(
        encoding='utf-8'
    )
    assert "cfg.get('hands_free_enabled', True)" in source
    assert "cfg.get('hands_free_level', 0.15)" in source
    assert "cfg.get('hands_free_idle_level', 0.8)" in source
