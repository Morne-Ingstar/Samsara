"""Focused defaults for first-run hands-free accessibility setup."""

from pathlib import Path

from dictation import DictationApp
from samsara.config_schema import SETTINGS_SCHEMA
from samsara.ui.first_run_wizard_qt import _USE_CASE_CONFIGS, _USE_CASE_TIPS


def test_new_install_command_session_timeout_defaults_to_five_minutes(tmp_path):
    app = DictationApp.__new__(DictationApp)
    app.config_path = Path(tmp_path) / "missing-config.json"
    app.save_config = lambda: None

    app.load_config()

    assert app.config["command_mode"]["inactivity_timeout_s"] == 300
    assert SETTINGS_SCHEMA["command_mode.inactivity_timeout_s"]["default"] == 300


def test_chronic_pain_profile_enables_fifteen_minute_hands_free_lane():
    command_mode = _USE_CASE_CONFIGS["chronic_pain"]["command_mode"]

    assert command_mode == {
        "enabled": True,
        "mode": "toggle",
        "command_matching_enabled": True,
        "inactivity_timeout_s": 900,
    }


def test_chronic_pain_completion_tip_teaches_complete_hands_free_flow():
    tip = _USE_CASE_TIPS["chronic_pain"].lower()

    for instruction in (
        "tap right ctrl once",
        "15-minute hands-free session",
        "'command', 'dictate', or 'ava'",
        "'end' by itself",
        "keep dictating",
        "'stop listening'",
    ):
        assert instruction in tip
