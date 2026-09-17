"""Queue 183: Ava consent is explicit, versioned, and cloud-aware."""

from types import SimpleNamespace

from PySide6.QtWidgets import QDialog, QPushButton

from samsara import config_schema
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


def test_settings_accept_stages_consent_with_the_enable_change(qapp, monkeypatch):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp

    app = _StubApp()
    app.config = {"ava_mode_enabled": False}
    window = _SettingsWindow(app)
    record = {"version": consent.CONSENT_VERSION, "accepted_at": "now", "cloud_version": None}
    monkeypatch.setattr(consent, "request_consent", lambda *_args, **_kwargs: record)
    try:
        checkbox = window._widgets["ava_mode_enabled"]
        checkbox.click()
        modes_save = next(save for save in window._save_fns
                          if "_build_modes_tab" in save.__qualname__)
        assert checkbox.isChecked() is True
        assert modes_save({})["ava"]["consent"] == record
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
        assert warm.isChecked() is True
        cloud.click()
        assert cloud.isChecked() is False
        assert warm.isChecked() is True
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


def test_schema_registers_the_nested_consent_record():
    assert config_schema.SETTINGS_SCHEMA["ava.consent"]["default"] == {
        "version": 0, "accepted_at": None, "cloud_version": None,
    }
