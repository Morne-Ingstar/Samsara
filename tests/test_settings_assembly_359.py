"""Assembly resolves grouped Settings help from schema keys."""

from samsara.config_schema import SETTINGS_SCHEMA
from samsara.ui.settings import (
    audio_page, ava_page, dictation_page, general_page, help_page, shortcuts_page,
)
from samsara.ui.settings_qt import _SettingsWindow
from tests.test_settings_pages import _StubApp, _schema_defaults


def test_page_row_specs_only_define_label_description_controls_and_keys():
    for module in (general_page, dictation_page, audio_page, ava_page, shortcuts_page, help_page):
        assert all(len(spec) == 4 for spec in module.BASE + module.ADVANCED), module.TITLE


def test_grouped_and_custom_rows_use_schema_help(qapp):
    window = _SettingsWindow(_StubApp(_schema_defaults()))
    try:
        route = window._rows_by_widget["providerRoute"]
        assert route.help_data == SETTINGS_SCHEMA["ava.provider_policy"]["help"]
        assert route.help_data["docs"].endswith("#ava.provider_policy")
        wake_profiles = window._rows_by_widget["wake_profiles"]
        assert wake_profiles.help_data == SETTINGS_SCHEMA["wake_profiles"]["help"]
        microphone = window._rows_by_widget["mic_combo"]
        assert microphone.help is None
        assert microphone.description in microphone.full_help
    finally:
        window.deleteLater()
