"""Queue 175e: Ava warm-on-boot consent belongs in Ava / Cloud settings."""

from __future__ import annotations

from tests.test_settings import _StubApp


def _window_and_save(config, qapp):
    from samsara.ui.settings_qt import _SettingsWindow

    app = _StubApp()
    app.config = config
    window = _SettingsWindow(app)
    save = next(fn for fn in window._save_fns
                if "_build_ava_cloud_tab" in fn.__qualname__)
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
        assert checkbox.isChecked() is True
        assert "small request" in checkbox.text().lower()

        checkbox.setChecked(False)
        saved = save({})["ava"]
        assert saved == {"future_setting": "preserve me", "warm_on_boot": False}
    finally:
        window.deleteLater()

    reloaded = dict(original)
    reloaded["ava"] = saved
    _app, second_window, _save = _window_and_save(reloaded, qapp)
    try:
        assert second_window._widgets["ava_warm_on_boot"].isChecked() is False
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
