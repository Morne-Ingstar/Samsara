"""Queue 244: Ava / Cloud explains web-search limits and exposes Ava Edit."""

from copy import deepcopy
from types import SimpleNamespace

from samsara import ai_preferences

from tests.test_settings import _StubApp


def _window(config, qapp):
    from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES

    app = _StubApp()
    config = deepcopy(config)
    if config.get("cloud_llm", {}).get("enabled"):
        provider = ai_preferences.provider_for(config, "cloud")
        config.setdefault("ava", {}).update(
            provider_policy="cloud",
            consent=ai_preferences.accept_consent(
                config, features=("ava",), provider=provider, cloud=True,
                accepted_at="2026-10-17T00:00:00Z"))
    app.config = config
    window = _SettingsWindow(app)
    window._stack.setCurrentIndex(_TAB_NAMES.index("Ava"))
    qapp.processEvents()
    save = window._source_saves["ava"][0]
    return app, window, save


def test_web_search_reason_names_provider_and_deepseek_enables(qapp):
    _app, window, _save = _window({
        "cloud_llm": {"enabled": True, "provider": "openai"},
    }, qapp)
    try:
        checkbox = window._widgets["cloud_web_search"]
        reason = window._widgets["cloud_web_search_unavailable"]
        assert not checkbox.isEnabled()
        assert not reason.isHidden()
        assert "Web search is DeepSeek-only" in reason.text()
        assert "OpenAI" in reason.text()
    finally:
        window.deleteLater()

    _app, window, _save = _window({
        "cloud_llm": {"enabled": True, "provider": "deepseek"},
    }, qapp)
    try:
        assert window._widgets["cloud_web_search"].isEnabled()
        assert window._widgets["cloud_web_search_unavailable"].isHidden()
    finally:
        window.deleteLater()


def test_supporter_widgets_are_gone_and_saved_key_survives(qapp):
    key = "SAMSARA-AAAA-BBBB-CCCC"
    app, window, save = _window({
        "premium_license": key,
        "cloud_llm": {"enabled": False, "provider": "deepseek"},
    }, qapp)
    try:
        assert not any(name.startswith("cloud_license") for name in window._widgets)
        updates = save({})
        assert "premium_license" not in updates
        app.config.update(updates)
        assert app.config["premium_license"] == key
    finally:
        window.deleteLater()


def test_ava_master_is_the_first_control_on_the_page(qapp):
    _app, window, _save = _window({}, qapp)
    try:
        # The single route selector replaced the separate master/cloud switches.
        from samsara.ui.settings.presentation import SettingRow
        master = window._widgets["providerRoute"]
        assert master.accessibleName() == "Ava runs"
        base = window._pages["Ava"].base
        rows = [base.itemAt(i).widget() for i in range(base.count())
                if isinstance(base.itemAt(i).widget(), SettingRow)]
        assert rows[0] is window._rows_by_widget["providerRoute"]
        assert rows[1] is window._rows_by_widget["cloud_provider"]
    finally:
        window.deleteLater()


def test_ava_edit_controls_round_trip_and_depend_on_checkbox(qapp):
    app, window, save = _window({
        "ava_edit": {"enabled": False},
    }, qapp)
    try:
        enabled = window._widgets["ava_edit_enabled"]
        keys = ("ava_edit_timeout", "ava_edit_model", "ava_edit_demo_pacing")
        options = [window._rows_by_widget[key] for key in keys]
        assert not enabled.isChecked()
        assert all(row.isHidden() for row in options)

        enabled.setChecked(True)
        timeout = window._widgets["ava_edit_timeout"]
        model = window._widgets["ava_edit_model"]
        pacing = window._widgets["ava_edit_demo_pacing"]
        assert all(not row.isHidden() for row in options)
        assert not timeout.isHidden()
        assert not model.isHidden()
        assert not pacing.isHidden()

        timeout.setValue(24.5)
        model.setCurrentIndex(1)
        pacing.setChecked(True)
        updates = save({})
        assert updates["ava_edit"] == {
            "enabled": True,
            "timeout_seconds": 24.5,
            "model": model.currentData(),
            "demo_pacing": "cinematic",
        }
        assert app.config["ava_edit"] == {"enabled": False}
    finally:
        window.deleteLater()


def test_ava_edit_save_does_not_create_untouched_keys(qapp):
    _app, window, save = _window({
        "ava_edit": {
            "enabled": False,
            "timeout_seconds": 19.0,
            "model": "existing-model",
            "demo_pacing": "cinematic",
        },
    }, qapp)
    try:
        assert "ava_edit" not in save({})
    finally:
        window.deleteLater()


def test_ava_edit_missing_section_uses_disabled_default():
    from samsara.ava_edit import session

    assert session.enabled(SimpleNamespace(config={})) is False
    assert session.enabled(SimpleNamespace(config={"ava_edit": {}})) is False
