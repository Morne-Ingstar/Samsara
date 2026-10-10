"""The Ava destination must follow the explicit policy and consent record."""

from copy import deepcopy

import pytest

from samsara import ai_preferences as ai
from samsara.ui import ava_consent_qt, home_signals, settings_qt
from tests.test_settings_pages import _StubApp, _schema_defaults


def _config():
    cfg = _schema_defaults()
    cfg["ava"].update(provider_policy="local", consent={})
    cfg["ava_command_session"].update(enabled=True, model="llama3.2:3b")
    cfg["ava_mode_enabled"] = True
    cfg["ollama"].update(enabled=True, model="llama3.2:3b")
    cfg["cloud_llm"].update(enabled=False, provider="deepseek", api_key="synthetic")
    return cfg


@pytest.fixture
def window(qapp, monkeypatch):
    from samsara import autostart
    from samsara.ui.first_run_qt import ComponentsPage

    monkeypatch.setattr(autostart, "sync_run_entry", lambda *a, **k: None)
    monkeypatch.setattr(ComponentsPage, "refresh_async", lambda self: None)
    win = settings_qt._SettingsWindow(_StubApp(_config()))
    yield win
    win.deleteLater()


def test_cloud_choice_declined_restores_prior_route(window, monkeypatch):
    called = []
    monkeypatch.setattr(ava_consent_qt, "request_consent",
                        lambda *a, **k: called.append(k) or None)
    route = window._widgets["providerRoute"]
    route.setCurrentIndex(2)
    assert len(called) == 1 and called[0]["cloud_enabled"] is True
    assert route.currentData() == "local"
    assert not window._widgets["cloud_enabled"].isChecked()
    assert "consent was not accepted" in window._widgets["ava_route_status"].text()
    updates = window._collect_destination_updates({})
    assert updates["ava"]["provider_policy"] == "local"
    assert updates["cloud_llm"]["enabled"] is False
    assert (updates["ava"].get("consent") or {}).get("cloud_version") is None


def test_new_cloud_policy_cannot_be_built_without_consent():
    with pytest.raises(ValueError, match="cloud consent required"):
        ai.apply_ava_route_choice(_config(), policy="cloud")


def test_cloud_acceptance_stages_then_writes_policy_and_consent(window, monkeypatch):
    def accept(_parent, staged, *, cloud_enabled):
        assert cloud_enabled and window.app.config["ava"]["provider_policy"] == "local"
        return ava_consent_qt.accepted_consent(
            staged, cloud_enabled=True, accepted_at="2026-09-27T00:00:00+00:00")

    monkeypatch.setattr(ava_consent_qt, "request_consent", accept)
    window._widgets["providerRoute"].setCurrentIndex(2)
    assert window.app.config["ava"]["provider_policy"] == "local"
    assert window._widgets["cloud_enabled"].isChecked()
    for key in ("cloud_provider", "cloud_model", "cloud_api_key"):
        assert window._widgets[key].isEnabled()
    updates = window._collect_destination_updates({})
    assert updates["ava"]["provider_policy"] == "cloud"
    assert updates["ava"]["consent"]["cloud_version"] == 2
    assert updates["cloud_llm"]["enabled"] is True
    assert updates["ava_command_session"]["backend"] == "cloud"
    assert ai.runtime_state({**window.app.config, **updates}, "ava").allowed


def test_provider_change_decline_keeps_original_cloud_grant(window, monkeypatch):
    calls = {"count": 0}

    def consent(_parent, staged, *, cloud_enabled):
        calls["count"] += 1
        if calls["count"] == 2:
            return None
        return ava_consent_qt.accepted_consent(
            staged, cloud_enabled=cloud_enabled, accepted_at="2026-09-27T00:00:00+00:00")

    monkeypatch.setattr(ava_consent_qt, "request_consent", consent)
    window._widgets["providerRoute"].setCurrentIndex(2)
    provider = window._widgets["cloud_provider"]
    provider.setCurrentText("OpenAI")
    assert calls["count"] == 2
    assert provider.currentText() == "DeepSeek (default)"
    updates = window._collect_destination_updates({})
    assert updates["cloud_llm"]["provider"] == "deepseek"
    assert updates["ava"]["consent"]["provider"]["identity"] == "deepseek"


def test_off_never_asks_local_requests_its_own_consent(window, monkeypatch):
    monkeypatch.setattr(ava_consent_qt, "request_consent",
                        lambda *a, **k: pytest.fail("Off must not ask"))
    route = window._widgets["providerRoute"]
    route.setCurrentIndex(0)
    assert not window._widgets["cloud_enabled"].isChecked()
    assert not window._widgets["cloud_provider"].isEnabled()
    off = window._collect_destination_updates({})
    assert off["ava"]["provider_policy"] == "off"
    assert off["cloud_llm"]["enabled"] is False
    calls = []
    monkeypatch.setattr(ava_consent_qt, "request_consent",
                        lambda parent, staged, **kw: calls.append(kw) or ava_consent_qt.accepted_consent(staged))
    route.setCurrentIndex(1)
    assert calls == [{"cloud_enabled": False}]
    local = window._collect_destination_updates({})
    assert local["ava"]["provider_policy"] == "local"
    assert local["cloud_llm"]["enabled"] is False
    assert ai.runtime_state({**window.app.config, **local}, "ava").allowed


def test_loaded_legacy_cloud_flag_cannot_claim_cloud(qapp, monkeypatch):
    from samsara import autostart
    from samsara.ui.first_run_qt import ComponentsPage

    monkeypatch.setattr(autostart, "sync_run_entry", lambda *a, **k: None)
    monkeypatch.setattr(ComponentsPage, "refresh_async", lambda self: None)
    cfg = _config()
    cfg["cloud_llm"]["enabled"] = True
    win = settings_qt._SettingsWindow(_StubApp(cfg))
    try:
        assert win._widgets["providerRoute"].currentData() == "local"
        assert not win._widgets["cloud_enabled"].isChecked()
        win._widgets["cloud_enabled"].setChecked(True)
        updates = win._collect_destination_updates({})
        assert updates["ava"]["provider_policy"] == "local"
        assert updates["cloud_llm"]["enabled"] is False
    finally:
        win.deleteLater()


def test_status_comes_from_effective_destination():
    local = _config()
    provider = ai.provider_for(local, "local", resolved_addresses=("127.0.0.1", "::1"))
    local["ava"]["consent"] = ai.accept_consent(
        local, provider=provider, accepted_at="2026-09-27T00:00:00+00:00")
    assert ai.ava_route_status(local) == "Ava: on this PC - llama3.2:3b"
    assert home_signals.ava_route_label(_StubApp(local)) == ai.ava_route_status(local)

    cloud = deepcopy(local)
    cloud["cloud_llm"]["enabled"] = True
    cloud["ava"]["provider_policy"] = "cloud"
    assert ai.ava_route_status(cloud) == "Ava: cloud chosen, consent pending"
    assert not ai.runtime_state(cloud, "ava").allowed
    consent = ai.accept_consent(
        cloud, provider=ai.provider_for(cloud, "cloud"), cloud=True,
        accepted_at="2026-09-27T00:00:00+00:00")
    cloud["ava"]["consent"] = consent
    assert ai.ava_route_status(cloud) == "Ava: cloud - DeepSeek"


def test_home_header_shows_selected_route(qapp):
    from samsara.ui.home_qt import AvaHeaderControl

    cfg = _config()
    provider = ai.provider_for(cfg, "local", resolved_addresses=("127.0.0.1", "::1"))
    cfg["ava"]["consent"] = ai.accept_consent(
        cfg, provider=provider, accepted_at="2026-09-27T00:00:00+00:00")
    control = AvaHeaderControl(_StubApp(cfg))
    try:
        control.refresh()
        assert "Ava: on this PC - llama3.2:3b" in control.visible_text
    finally:
        control.deleteLater()


def test_legacy_cloud_consent_shows_confirmation_without_switch(qapp, monkeypatch):
    from samsara import autostart
    from samsara.ui.first_run_qt import ComponentsPage

    monkeypatch.setattr(autostart, "sync_run_entry", lambda *a, **k: None)
    monkeypatch.setattr(ComponentsPage, "refresh_async", lambda self: None)
    cfg = _config()
    cfg["cloud_llm"]["enabled"] = True
    cfg["ava"]["consent"] = ai.accept_consent(
        cfg, provider=ai.provider_for(cfg, "cloud"), cloud=True,
        accepted_at="2026-09-27T00:00:00+00:00")
    win = settings_qt._SettingsWindow(_StubApp(cfg))
    try:
        assert ai.cloud_route_confirmation_needed(cfg)
        assert win._ava_route_confirmation_pending
        assert win._widgets["providerRoute"].currentData() == "local"
        # The selected local route needs its own consent before any switch.
        assert "Ava needs your OK to run on this PC." == win._widgets["ava_route_status"].text()
        assert win._rows_by_widget['ava_route_status'].isAncestorOf(
            win._widgets['ava_consent_button'])
        updates = win._collect_destination_updates({})
        assert updates["ava"]["provider_policy"] == "local"
        assert updates["cloud_llm"]["enabled"] is False
    finally:
        win.deleteLater()
