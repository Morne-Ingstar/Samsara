"""Queue 175e: Ava warm-on-boot consent belongs in Ava / Cloud settings."""

from __future__ import annotations

from copy import deepcopy

from samsara import ai_preferences
from tests.test_settings import _StubApp


def _window_and_save(config, qapp):
    from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES

    app = _StubApp()
    config = deepcopy(config)
    route = "cloud" if config.get("cloud_llm", {}).get("enabled") else "local"
    config.setdefault("ollama", {}).setdefault("host", "http://127.0.0.1:11434")
    provider = ai_preferences.provider_for(config, route)
    config.setdefault("ava", {}).update(
        provider_policy=route,
        consent=ai_preferences.accept_consent(config, features=("ava",), provider=provider,
                                              cloud=route == "cloud", accepted_at="2026-10-17T00:00:00Z"))
    config.setdefault("ava_command_session", {})["enabled"] = True
    app.config = config
    window = _SettingsWindow(app)
    window._stack.setCurrentIndex(_TAB_NAMES.index("Ava"))
    save = window._source_saves["ava"][0]
    return app, window, save


def test_warm_on_boot_checkbox_round_trips_through_ava_settings(qapp):
    original = {
        "ollama": {"enabled": True},
        "cloud_llm": {"enabled": False, "api_key": ""},
        "ava": {"future_setting": "preserve me"},
    }
    _app, window, save = _window_and_save(original, qapp)
    try:
        checkbox = window._widgets["ava_warm_on_boot"]
        assert checkbox.isChecked() is False
        assert "small request" in window._rows_by_widget["ava_warm_on_boot"].description.lower()

        checkbox.setChecked(True)
        saved = save({})["ava"]
        assert saved == {"future_setting": "preserve me", "provider_policy": "local",
                         "consent": _app.config["ava"]["consent"], "warm_on_boot": True,
                         "conversation_log_enabled": False}
    finally:
        window.deleteLater()

    reloaded = dict(original)
    reloaded["ava"] = saved
    _app, second_window, _save = _window_and_save(reloaded, qapp)
    try:
        assert second_window._widgets["ava_warm_on_boot"].isChecked() is True
    finally:
        second_window.deleteLater()


def test_warm_on_boot_checkbox_defaults_off_for_configured_cloud(qapp):
    config = {
        "ollama": {"enabled": True},
        "cloud_llm": {"enabled": True, "api_key": "test-key"},
    }
    _app, window, _save = _window_and_save(config, qapp)
    try:
        assert window._widgets["ava_warm_on_boot"].isChecked() is False
    finally:
        window.deleteLater()
