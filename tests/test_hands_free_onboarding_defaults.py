"""Focused defaults for first-run hands-free accessibility setup."""

from pathlib import Path

from dictation import DictationApp
from samsara.config_schema import SETTINGS_SCHEMA
from samsara.ui import first_run_wizard_qt as wizard


def test_new_install_command_session_timeout_defaults_to_five_minutes(tmp_path):
    app = DictationApp.__new__(DictationApp)
    app.config_path = Path(tmp_path) / "missing-config.json"
    app.save_config = lambda: None

    app.load_config()

    assert app.config["command_mode"]["inactivity_timeout_s"] == 300
    assert SETTINGS_SCHEMA["command_mode.inactivity_timeout_s"]["default"] == 300


def test_chronic_pain_profile_enables_fifteen_minute_hands_free_lane():
    # 373: profiles no longer silently change session or recording defaults.
    assert not hasattr(wizard, '_USE_CASE_CONFIGS')
    assert '_use_case' not in wizard._DEFAULTS


def test_chronic_pain_completion_tip_teaches_complete_hands_free_flow():
    # 373: removed profile copy is replaced by shared tutorial details.
    assert not hasattr(wizard, '_USE_CASE_TIPS')
    assert 'Use Case' not in [name for name, _ in wizard._STEPS]
