"""Queue 183: Ava consent is explicit, versioned, and cloud-aware."""

from types import MethodType, SimpleNamespace
import json

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QWidget

from samsara import config_schema
from samsara import ai_preferences
from samsara.ui import ava_consent_qt as consent


@pytest.fixture(autouse=True)
def _dispose_test_widgets(request):
    """Destroy this test's windows while their Python wrappers are still valid."""
    if "qapp" not in request.fixturenames:
        yield
        return
    qapp = request.getfixturevalue("qapp")
    before = set(qapp.topLevelWidgets())
    yield
    # Settings.close() only hides, and deleteLater() needs an explicit drain
    # when pytest runs without Qt's main event loop.
    from shiboken6 import isValid
    created = [w for w in qapp.topLevelWidgets() if w not in before]
    for widget in created:
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    assert all(not isValid(widget) for widget in created)


@pytest.mark.parametrize("read_only", [False, True])
def test_consent_copy_is_announced_before_buttons(qapp, read_only):
    dialog = consent.AvaConsentDialog(read_only=read_only)
    try:
        body = dialog.findChild(QLabel)
        buttons = dialog.findChildren(QPushButton)
        assert body.text() == consent.CONSENT_TEXT
        name = QAccessible.queryAccessibleInterface(body).text(QAccessible.Text.Name)
        assert "She can get things wrong" in name
        assert "What turning Ava on means" not in name
        assert dialog.accessibleDescription() == consent.CONSENT_TEXT
        assert body.focusPolicy() == Qt.FocusPolicy.StrongFocus
        assert body.nextInFocusChain() is buttons[0]
        if not read_only:
            assert buttons[0].nextInFocusChain() is buttons[1]

        dialog.show()
        qapp.processEvents()
        assert dialog.focusWidget() is body
    finally:
        dialog.close()
        dialog.deleteLater()


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
    app.config = {"ava": {"provider_policy": "off"}}
    window = _SettingsWindow(app)
    shown = []
    monkeypatch.setattr(
        consent, "request_consent", lambda *_args, **_kwargs: shown.append(True) or None)
    try:
        checkbox = window._widgets["ava_enabled"]
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


def test_ava_page_accept_and_apply_enables_once_and_survives_reload(
    qapp, monkeypatch, tmp_path
):
    """The Ava page alone activates AI; no Modes interaction is required."""
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp

    app = _StubApp()
    app.config = _configured_local_app()
    state_file = tmp_path / "config.json"
    state_file.write_text(json.dumps(app.config), encoding="utf-8")
    saves = []

    def save_config():
        saves.append(dict(app.config))
        state_file.write_text(json.dumps(app.config), encoding="utf-8")

    app.save_config = save_config
    monkeypatch.setattr(consent, "AvaConsentDialog", _AcceptedDialog)
    _AcceptedDialog.shown = 0
    window = _SettingsWindow(app)
    try:
        checkbox = window._widgets["ava_enabled"]
        checkbox.click()
        assert checkbox.isChecked() is True
        assert _AcceptedDialog.shown == 1
        assert len(saves) == 1
        assert ai_preferences.runtime_state(app.config, "ava").allowed
        window._apply_and_close()
        assert len(saves) == 2
        assert app.config["ava"]["provider_policy"] == "local"
        assert app.config["ava_command_session"]["enabled"] is True
        assert app.config["ava_mode_enabled"] is True
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

        window2 = _SettingsWindow(app2)
        try:
            checkbox2 = window2._widgets["ava_enabled"]
            assert checkbox2.isChecked() is True
            checkbox2.click()
            checkbox2.click()
        finally:
            window2.deleteLater()
        assert _AcceptedDialog.shown == 1
    finally:
        window.deleteLater()


def test_modes_has_no_ava_enable_switch_and_preserves_master_state(qapp):
    """Modes cannot overwrite or silently block the Ava-page master."""
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp

    app = _StubApp()
    app.config = _configured_local_app()
    window = _SettingsWindow(app)
    try:
        assert "ava_cmd_enabled" not in window._widgets
        assert "ava_mode_enabled" not in window._widgets
        app.config["ava_command_session"]["enabled"] = True
        modes_save = next(save for save in window._save_fns
                          if "_build_modes_tab" in save.__qualname__)
        updates = modes_save({})
        assert updates["ava_command_session"]["enabled"] is True
        assert "ava_mode_enabled" not in updates
    finally:
        window.deleteLater()


def test_fresh_config_master_on_local_is_ready_without_modes(qapp, monkeypatch):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp

    app = _StubApp()
    app.config = {}
    monkeypatch.setattr(consent, "AvaConsentDialog", _AcceptedDialog)
    _AcceptedDialog.shown = 0
    window = _SettingsWindow(app)
    try:
        window._widgets["ava_enabled"].click()
        window._apply_and_close()

        assert _AcceptedDialog.shown == 1
        assert app.config["ava"]["provider_policy"] == "local"
        assert app.config["ava_command_session"]["enabled"] is True
        assert app.config["ava_mode_enabled"] is True
        assert ai_preferences.runtime_state(app.config, "ava").allowed
    finally:
        window.deleteLater()


def test_fresh_config_master_on_cloud_is_ready_without_modes(qapp, monkeypatch):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp

    app = _StubApp()
    app.config = {}
    monkeypatch.setattr(consent, "AvaConsentDialog", _AcceptedDialog)
    _AcceptedDialog.shown = 0
    window = _SettingsWindow(app)
    try:
        window._widgets["cloud_api_key"].setText("fixture-only")
        window._widgets["cloud_enabled"].click()
        window._widgets["ava_enabled"].click()
        window._apply_and_close()

        assert _AcceptedDialog.shown == 1
        assert app.config["ava"]["provider_policy"] == "cloud"
        assert app.config["ava_command_session"]["enabled"] is True
        assert app.config["cloud_llm"]["enabled"] is True
        assert ai_preferences.runtime_state(app.config, "ava").allowed
    finally:
        window.deleteLater()


def test_master_off_turns_every_ava_route_off(qapp):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp

    app = _StubApp()
    app.config = _configured_local_app()
    provider = ai_preferences.provider_for(app.config, "local")
    app.config["ava"]["consent"] = ai_preferences.accept_consent(
        app.config, features=("ava",), provider=provider, accepted_at="fixture"
    )
    app.config = ai_preferences.apply_accepted_ava(
        app.config, policy="local", consent=app.config["ava"]["consent"]
    )
    app.config["ava_edit"]["enabled"] = True
    window = _SettingsWindow(app)
    try:
        master = window._widgets["ava_enabled"]
        assert master.isChecked()
        master.click()
        window._apply_and_close()

        assert app.config["ava"]["provider_policy"] == "off"
        assert app.config["ava_command_session"]["enabled"] is False
        assert app.config["ava_edit"]["enabled"] is False
        assert app.config["ava_mode_enabled"] is False
        assert app.config["command_packs"]["ai"] is False
        assert app.config["ollama"]["enabled"] is False
        assert app.config["cloud_llm"]["enabled"] is False
        assert not ai_preferences.runtime_state(app.config, "ava").allowed
        assert not ai_preferences.runtime_state(app.config, "editing").allowed
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
    assert dialog.focusWidget() is dialog.findChild(QLabel)
    assert dialog.isModal() is True
    dialog.close()


def _pending_local_guide(app, owner):
    from samsara.ui.ava_guide_qt import _WizardWindow

    # The route is selected but cannot run until consent is accepted.
    app.config["ava"]["provider_policy"] = "local"
    app.config["ava_command_session"]["enabled"] = True
    model = app.config["ollama"]["model"]
    guide = SimpleNamespace(
        _app=app, _selected_model_name=model, _ollama_running=True,
        _installed_models=[model], _done_title=QLabel(owner), _done_summary=QLabel(owner),
        _skip_btn=QPushButton(owner), _next_btn=QPushButton(owner),
    )
    guide._build_done_summary = MethodType(_WizardWindow._build_done_summary, guide)
    guide._build_done_summary()
    assert guide._consent_only_missing
    assert guide._next_btn.text() == "Enable Ava"
    assert not ai_preferences.runtime_state(app.config, "ava").allowed
    return guide


@pytest.fixture
def pending_local_guide(qapp):
    owner = QWidget()
    guides = []

    def build(app):
        guide = _pending_local_guide(app, owner)
        guides.append(guide)
        return guide

    yield build
    # Break the bound-method/self cycle before destroying the owned C++ widgets.
    for guide in guides:
        del guide._build_done_summary
    owner.close()
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    from shiboken6 import isValid
    assert not isValid(owner)
    assert all(not isValid(guide._next_btn) for guide in guides)


def test_guide_decline_does_not_enable_ai_pack(qapp, monkeypatch, pending_local_guide):
    from samsara.ui.ava_guide_qt import _WizardWindow

    updates = []
    app = SimpleNamespace(
        config=_configured_local_app(),
        update_config=lambda update, save: updates.append((update, save)),
    )
    guide = pending_local_guide(app)
    monkeypatch.setattr(consent, "request_consent", lambda *_args, **_kwargs: None)

    _WizardWindow._enable_ai_pack(guide)

    assert updates == []
    assert not ai_preferences.runtime_state(app.config, "ava").allowed
    assert app.config["command_packs"]["ai"] is False
    assert guide._next_btn.text() == "Enable Ava"


def test_guide_accept_persists_and_activates_the_selected_local_route(
    qapp, monkeypatch, tmp_path, pending_local_guide
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

    guide = pending_local_guide(app)
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
    assert guide._next_btn.text() == "Finish"
    assert guide._done_title.text() == "Ava is ready."


def test_schema_registers_the_nested_consent_record():
    assert config_schema.SETTINGS_SCHEMA["ava.consent"]["default"] == {
        "version": 0, "accepted_at": None, "cloud_version": None,
    }
