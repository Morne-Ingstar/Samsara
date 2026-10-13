"""Settings local consent, immediate persistence and useful route status."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QPushButton, QDialog

from samsara import ai_preferences as ai, ava_readiness
from samsara.ui import ava_consent_qt as consent, settings_qt
from tests.test_ava_route_master_343 import _config
from tests.test_settings_pages import _StubApp


@pytest.fixture
def make_window(qapp, monkeypatch):
    from samsara import autostart
    from samsara.ui.first_run_qt import ComponentsPage
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda self: None)
    monkeypatch.setattr(ava_readiness, 'readiness_for', lambda app: ava_readiness.Readiness(ava_readiness.UNKNOWN))
    windows = []
    def make(config=None):
        win = settings_qt._SettingsWindow(_StubApp(deepcopy(config or _config())))
        windows.append(win)
        return win
    yield make
    for win in windows:
        win.deleteLater()


def grant(config, route='local'):
    return consent.accepted_consent(config, cloud_enabled=route == 'cloud', accepted_at='2026-10-09T00:00:00Z')


def test_local_choice_decline_restores_without_writes(make_window, monkeypatch):
    cfg = _config()
    cfg['ava']['provider_policy'] = 'off'
    win = make_window(cfg)
    original = deepcopy(win.app.config)
    request = Mock(return_value=None)
    monkeypatch.setattr(consent, 'request_consent', request)
    win._widgets['providerRoute'].setCurrentIndex(1)
    request.assert_called_once()
    assert request.call_args.kwargs == {'cloud_enabled': False}
    assert win._widgets['providerRoute'].currentData() == 'off'
    assert win.app.config == original
    updates = win._collect_destination_updates({})
    assert updates['ava']['provider_policy'] == 'off'
    assert not updates['ava'].get('consent')


def test_local_choice_accept_persists_and_apply_rechecks_once(make_window, monkeypatch):
    cfg = _config()
    cfg['ava']['provider_policy'] = 'off'
    win = make_window(cfg)
    original = deepcopy(win.app.config)
    monkeypatch.setattr(consent, 'request_consent', lambda parent, staged, **kw: grant(staged))
    win._widgets['providerRoute'].setCurrentIndex(1)
    assert win.app.config != original
    record = win.app.config['ava']['consent']
    assert ai.consent_covers(record, features=('ava',), provider=ai.provider_for(win._ava_staged_config(), 'local'), cloud=False)
    recheck = Mock()
    monkeypatch.setattr(ava_readiness, 'recheck_after_settings', recheck)
    from samsara import listening_display
    from samsara.ui import qt_runtime
    monkeypatch.setattr(listening_display, 'run_display', lambda *a: None)
    monkeypatch.setattr(listening_display, 'request_restart_once', lambda *a: None)
    monkeypatch.setattr(qt_runtime, 'post', lambda *a: None)
    monkeypatch.setattr(win, '_replace_tts_runtime', lambda *a, **k: None)
    win.app.save_config = Mock()
    win._apply_and_close()
    assert win.app.config['ava']['consent'] == record
    assert ai.runtime_state(win.app.config, 'ava').allowed
    recheck.assert_called_once()
    win.app.save_config.assert_called_once()


def test_saved_pending_button_accepts_without_route_toggle(make_window, monkeypatch):
    win = make_window()
    original = deepcopy(win.app.config)
    row = win._rows_by_widget['ava_route_status']
    button = win._widgets['ava_consent_button']
    assert not row.isHidden()
    assert win._widgets['ava_route_status'].text() == 'Ava needs your OK to run on this PC.'
    assert button.text() == 'Give your OK'
    assert button.accessibleName() == 'Give your OK'
    assert row.heading.text() == 'Before Ava can answer'
    assert not row.isAncestorOf(row.detail) and row.text.layout().count() == 1
    assert row.isAncestorOf(button)
    monkeypatch.setattr(consent, 'request_consent', lambda parent, staged, **kw: grant(staged))
    button.click()
    assert win.app.config != original
    assert ai.runtime_state(win.app.config, 'ava').allowed
    updates = win._collect_destination_updates({})
    assert ai.runtime_state({**win.app.config, **updates}, 'ava').allowed
    assert row.isHidden()
    assert button.text() == 'Read this again'
    assert button.accessibleName() == 'Read this again'
    assert win._rows_by_widget['ava:Read this again'].isAncestorOf(button)


def test_saved_pending_button_decline_keeps_pending(make_window, monkeypatch):
    win = make_window()
    before = deepcopy(win.app.config)
    monkeypatch.setattr(consent, 'request_consent', lambda *a, **k: None)
    win._widgets['ava_consent_button'].click()
    assert win.app.config == before
    assert getattr(win, '_ava_consent_staged', None) is None
    assert not win._rows_by_widget['ava_route_status'].isHidden()


def test_legacy_cloud_grant_shows_local_pending_action(make_window, monkeypatch):
    cfg = _config()
    cfg['ava']['consent'] = grant(cfg, 'cloud')
    cfg['cloud_llm']['enabled'] = True
    win = make_window(cfg)
    assert win._widgets['ava_route_status'].text() == 'Ava needs your OK to run on this PC.'
    assert win._rows_by_widget['ava_route_status'].isAncestorOf(win._widgets['ava_consent_button'])
    monkeypatch.setattr(consent, 'request_consent', lambda parent, staged, **kw: grant(staged))
    win._widgets['ava_consent_button'].click()
    assert win._rows_by_widget['ava_route_status'].isHidden()
    assert not win._ava_route_confirmation_pending


def test_covered_button_is_read_only(make_window, monkeypatch):
    cfg = _config()
    cfg['ava']['consent'] = grant(cfg)
    win = make_window(cfg)
    assert win._widgets['ava_consent_button'].text() == 'Read this again'
    replay = Mock()
    monkeypatch.setattr(consent, 'show_consent_copy', replay)
    monkeypatch.setattr(consent, 'request_consent', lambda *a, **k: pytest.fail('covered destination'))
    win._widgets['ava_consent_button'].click()
    replay.assert_called_once_with(win)


@pytest.mark.parametrize('route', ['local', 'cloud'])
def test_grants_never_cross_routes(route):
    cfg = _config()
    record = grant(cfg, route)
    other = 'cloud' if route == 'local' else 'local'
    assert not ai.consent_covers(record, features=('ava',), provider=ai.provider_for(cfg, other), cloud=other == 'cloud')


def test_model_keeps_grant_address_requires_accept(make_window, monkeypatch):
    cfg = _config()
    cfg['ava']['consent'] = grant(cfg)
    win = make_window(cfg)
    win._widgets['ava_cmd_model'].setText('another-model')
    assert not consent.consent_required(win._ava_staged_config())
    win.app.config['ollama']['host'] = 'http://127.0.0.1:11435'
    win._refresh_ava_route_status()
    assert consent.consent_required(win._ava_staged_config())
    requested = Mock(side_effect=lambda parent, staged, **kw: grant(staged))
    monkeypatch.setattr(consent, 'request_consent', requested)
    win._widgets['ava_consent_button'].click()
    requested.assert_called_once()
    assert not consent.consent_required(win._ava_staged_config())


@pytest.mark.parametrize('route', ['off', 'local', 'cloud'])
def test_redundant_status_hidden_through_condition_refresh(make_window, route):
    cfg = _config()
    cfg['ava'].update(provider_policy=route, consent=grant(cfg, 'cloud' if route == 'cloud' else 'local'))
    cfg['cloud_llm']['enabled'] = route == 'cloud'
    win = make_window(cfg)
    win._refresh_conditions()
    assert win._rows_by_widget['ava_route_status'].isHidden()


@pytest.mark.parametrize('case', ['pending', 'needs-key', 'unreachable'])
def test_useful_status_visible(make_window, monkeypatch, case):
    cfg = _config()
    cfg['ava']['provider_policy'] = 'cloud'
    cfg['cloud_llm']['enabled'] = True
    if case != 'pending': cfg['ava']['consent'] = grant(cfg, 'cloud')
    if case == 'needs-key': cfg['cloud_llm']['api_key'] = ''
    win = make_window(cfg)
    if case == 'unreachable':
        monkeypatch.setattr(ava_readiness, 'readiness_for', lambda app: ava_readiness.Readiness(ava_readiness.OFFLINE, provider='deepseek', failure_kind=ava_readiness.UNREACHABLE))
    win._refresh_conditions()
    assert not win._rows_by_widget['ava_route_status'].isHidden()
    expected = {'pending': 'Ava needs your OK to run in the cloud.', 'needs-key': 'API key needed', 'unreachable': "can't reach DeepSeek"}[case]
    assert expected in win._widgets['ava_route_status'].text()
    assert win._widgets['ava_consent_button'].text() == ('Give your OK' if case == 'pending' else 'Read this again')
    assert win._rows_by_widget['ava_route_status'].text.layout().count() == 1


def test_pending_wins_over_cached_ready(make_window, monkeypatch):
    monkeypatch.setattr(ava_readiness, 'readiness_for', lambda app: ava_readiness.Readiness(ava_readiness.READY, provider='ollama'))
    win = make_window()
    assert win._widgets['ava_route_status'].text() == 'Ava needs your OK to run on this PC.'
    assert 'ready' not in win._widgets['ava_route_status'].text()


def test_pending_replay_opens_real_accepting_dialog(make_window, monkeypatch):
    win = make_window()
    def accept(dialog):
        assert {b.text() for b in dialog.findChildren(QPushButton)} == {'Not now', 'I understand, turn Ava on'}
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(consent.AvaConsentDialog, 'exec', accept)
    win._widgets['ava_consent_button'].click()
    assert not consent.consent_required(win._ava_staged_config())
