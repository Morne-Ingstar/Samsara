"""Queue 183: Ava consent is explicit, versioned, and cloud-aware."""

from types import SimpleNamespace
import json

from PySide6.QtWidgets import QDialog, QPushButton

from samsara import config_schema
from samsara import ai_preferences
from samsara.ui import ava_consent_qt as consent


class _AcceptedDialog:
    shown = 0

    def __init__(self, *_args, **_kwargs):
        type(self).shown += 1

    def exec(self):
        return QDialog.DialogCode.Accepted


def test_first_enable_acknowledges_once_and_stores_version_timestamp(monkeypatch):
    monkeypatch.setattr(consent, "AvaConsentDialog", _AcceptedDialog)
    _AcceptedDialog.shown = 0
    config = {}

    record = consent.request_consent(None, config)

    assert _AcceptedDialog.shown == 1
    assert record["version"] == consent.CONSENT_VERSION
    assert record["accepted_at"]
    assert record["cloud_version"] is None
    config["ava"] = {"consent": record}
    assert consent.consent_required(config) is False
    assert consent.request_consent(None, config) == record
    assert _AcceptedDialog.shown == 1


def test_decline_produces_no_record_and_leaves_ava_off(qapp, monkeypatch):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp

    app = _StubApp()
    app.config = {"ava_mode_enabled": False}
    window = _SettingsWindow(app)
    shown = []
    monkeypatch.setattr(
        consent, "request_consent", lambda *_args, **_kwargs: shown.append(True) or None)
    try:
        checkbox = window._widgets["ava_mode_enabled"]
        checkbox.click()
        assert shown == [True]
        assert checkbox.isChecked() is False
    finally:
        window.deleteLater()


def _configured_local_app():
    return {
        "ava": {"provider_policy": "off"},
        "ava_command_session": {
            "enabled": False, "backend": "ollama", "model": "fixture-model",
            "key": "f9", "keep_warm": True,
        },
        "ava_edit": {"enabled": False},
        "command_packs": {"ai": False},
        "ollama": {"enabled": True, "host": "http://127.0.0.1:11434", "model": "fixture-model"},
        "cloud_llm": {"enabled": False},
    }


def test_settings_accept_enables_immediately_once_and_survives_close_reload(
    qapp, monkeypatch, tmp_path
):
    """ACCEPT must durably activate AI now; Settings close cannot discard it."""
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp

    app = _StubApp()
    app.config = _configured_local_app()
    state_file = tmp_path / "config.json"
    state_file.write_text(json.dumps(app.config), encoding="utf-8")
    saves = []

    def update_config(changes, save=True):
        app.config.update(changes)
        if save:
            saves.append(dict(changes))
            state_file.write_text(json.dumps(app.config), encoding="utf-8")

    app.update_config = update_config
    monkeypatch.setattr(consent, "AvaConsentDialog", _AcceptedDialog)
    _AcceptedDialog.shown = 0
    window = _SettingsWindow(app)
    try:
        checkbox = window._widgets["ava_cmd_enabled"]
        checkbox.click()
        assert checkbox.isChecked() is True
        assert _AcceptedDialog.shown == 1
        assert len(saves) == 1
        assert app.config["ava"]["provider_policy"] == "local"
        assert app.config["ava_command_session"]["enabled"] is True
        assert app.config["command_packs"]["ai"] is True
        assert ai_preferences.runtime_state(app.config, "ava").allowed
        assert json.loads(state_file.read_text(encoding="utf-8"))[
            "ava_command_session"]["enabled"] is True

        window.close()  # closeEvent only hides; persisted acceptance remains
        qapp.processEvents()
        reloaded = json.loads(state_file.read_text(encoding="utf-8"))
        assert reloaded["ava_command_session"]["enabled"] is True
        assert ai_preferences.runtime_state(reloaded, "ava").allowed

        # A new Settings instance must reuse the saved acceptance, not memory.
        app2 = _StubApp()
        app2.config = reloaded

        def update_reloaded_config(changes, save=True):
            app2.config.update(changes)
            if save:
                state_file.write_text(json.dumps(app2.config), encoding="utf-8")

        app2.update_config = update_reloaded_config
        window2 = _SettingsWindow(app2)
        try:
            checkbox2 = window2._widgets["ava_cmd_enabled"]
            assert checkbox2.isChecked() is True
            checkbox2.click()
            checkbox2.click()
        finally:
            window2.deleteLater()
        assert _AcceptedDialog.shown == 1
    finally:
        window.deleteLater()


def test_modes_save_does_not_restore_a_stale_disabled_session(qapp):
    """Untouched Settings state must not overwrite a newer config transaction."""
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp

    app = _StubApp()
    app.config = _configured_local_app()
    window = _SettingsWindow(app)
    try:
        assert window._widgets["ava_cmd_enabled"].isChecked() is False
        app.config["ava_command_session"]["enabled"] = True
        modes_save = next(save for save in window._save_fns
                          if "_build_modes_tab" in save.__qualname__)
        updates = modes_save({})
        assert updates["ava_command_session"]["enabled"] is True
    finally:
        window.deleteLater()


def test_declining_cloud_consent_leaves_the_cloud_toggle_off(qapp, monkeypatch):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp

    app = _StubApp()
    app.config = {"cloud_llm": {"enabled": False}}
    window = _SettingsWindow(app)
    monkeypatch.setattr(consent, "request_consent", lambda *_args, **_kwargs: None)
    try:
        cloud = window._widgets["cloud_enabled"]
        warm = window._widgets["ava_warm_on_boot"]
        # 252's fail-closed policy disables warm-up until Ava is enabled.
        assert warm.isChecked() is False
        cloud.click()
        assert cloud.isChecked() is False
        assert warm.isChecked() is False
    finally:
        window.deleteLater()


def test_version_bump_requires_a_fresh_acknowledgement(monkeypatch):
    config = {"ava": {"consent": {
        "version": consent.CONSENT_VERSION,
        "accepted_at": "2026-09-17T00:00:00+00:00",
        "cloud_version": None,
    }}}

    monkeypatch.setattr(consent, "CONSENT_VERSION", consent.CONSENT_VERSION + 1)

    assert consent.consent_required(config) is True


def test_first_cloud_configuration_reprompts_once(monkeypatch):
    monkeypatch.setattr(consent, "AvaConsentDialog", _AcceptedDialog)
    _AcceptedDialog.shown = 0
    config = {"ava": {"consent": consent.accepted_consent(
        {}, accepted_at="2026-09-17T00:00:00+00:00"
    )}}

    assert consent.consent_required(config, cloud_enabled=True) is True
    record = consent.request_consent(None, config, cloud_enabled=True)
    config["ava"]["consent"] = record

    assert _AcceptedDialog.shown == 1
    assert record["cloud_version"] == consent.CONSENT_VERSION
    assert consent.consent_required(config, cloud_enabled=True) is False


def test_local_ava_backend_does_not_inherit_the_separate_cloud_toggle():
    config = {
        "ava_command_session": {"backend": "ollama"},
        "cloud_llm": {"enabled": True, "provider": "deepseek"},
    }
    assert consent.selected_ava_policy(config) == "local"


def test_dialog_has_accessible_actions_and_safe_initial_focus(qapp):
    dialog = consent.AvaConsentDialog()
    buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}

    assert set(buttons) == {"Not now", "I understand, turn Ava on"}
    assert buttons["Not now"].accessibleName() == "Not now"
    assert buttons["I understand, turn Ava on"].accessibleName() == "I understand, turn Ava on"
    dialog.show()
    qapp.processEvents()
    assert dialog.focusWidget() is buttons["Not now"]
    assert dialog.isModal() is True
    dialog.close()


def test_guide_decline_does_not_enable_ai_pack(monkeypatch):
    from samsara.ui.ava_guide_qt import _WizardWindow

    updates = []
    app = SimpleNamespace(
        config={"command_packs": {"ai": False}},
        update_config=lambda update, save: updates.append((update, save)),
    )
    button = SimpleNamespace(setText=lambda _text: None, setEnabled=lambda _enabled: None)
    guide = SimpleNamespace(_app=app, _enable_pack_btn=button)
    monkeypatch.setattr(consent, "request_consent", lambda *_args, **_kwargs: None)

    _WizardWindow._enable_ai_pack(guide)

    assert updates == []


def test_guide_accept_persists_and_activates_the_selected_local_route(
    monkeypatch, tmp_path
):
    from samsara.ui.ava_guide_qt import _WizardWindow

    app = SimpleNamespace(config=_configured_local_app())
    state_file = tmp_path / "config.json"
    saves = []

    def update_config(changes, save=True):
        app.config.update(changes)
        if save:
            saves.append(dict(changes))
            state_file.write_text(json.dumps(app.config), encoding="utf-8")

    app.update_config = update_config
    rebuilt = []
    app.command_executor = SimpleNamespace(
        rebuild_matcher=lambda: rebuilt.append(True)
    )
    monkeypatch.setattr(consent, "AvaConsentDialog", _AcceptedDialog)
    _AcceptedDialog.shown = 0

    class Button:
        text = ""
        enabled = True

        def setText(self, value):
            self.text = value

        def setEnabled(self, value):
            self.enabled = value

    button = Button()
    guide = SimpleNamespace(_app=app, _enable_pack_btn=button)
    _WizardWindow._enable_ai_pack(guide)

    assert _AcceptedDialog.shown == 1
    assert len(saves) == 1
    assert app.config["ava"]["provider_policy"] == "local"
    assert app.config["ava_command_session"]["enabled"] is True
    assert app.config["command_packs"]["ai"] is True
    assert ai_preferences.runtime_state(app.config, "ava").allowed
    assert json.loads(state_file.read_text(encoding="utf-8"))[
        "ava_command_session"]["enabled"] is True
    assert rebuilt == [True]
    assert "restart" not in button.text.lower()


def test_schema_registers_the_nested_consent_record():
    assert config_schema.SETTINGS_SCHEMA["ava.consent"]["default"] == {
        "version": 0, "accepted_at": None, "cloud_version": None,
    }
