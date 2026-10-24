"""Truthful Ava status and grouped Settings, without a live app or provider."""
import copy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from samsara import ai_preferences, ava_readiness, cloud_llm, listening_display
from samsara.runtime import thread_registry
from samsara.ui import home_qt, home_signals, qt_runtime
from samsara.ui.settings.services import SettingsServices


def config_for(route='cloud', provider='deepseek'):
    config = {'ava': {'provider_policy': route}, 'ava_command_session': {'enabled': True},
              'ollama': {'enabled': route == 'local', 'host': 'http://localhost:11434'},
              'cloud_llm': {'enabled': route == 'cloud', 'provider': provider,
                            'api_key': chr(120) * 8}, 'tts': {'enabled': False}}
    identity = ai_preferences.provider_for(config, route, resolved_addresses=('127.0.0.1', '::1'))
    config['ava']['consent'] = ai_preferences.accept_consent(
        config, features=('ava',), provider=identity, cloud=route == 'cloud', accepted_at='2026-10-09T00:00:00Z')
    return config


@pytest.fixture
def tracker(monkeypatch):
    tracker = ava_readiness.ReadinessTracker()
    monkeypatch.setattr(ava_readiness, 'tracker', tracker)
    monkeypatch.setattr(ava_readiness, '_monitor', None)
    return tracker


@pytest.mark.parametrize('route', ['local', 'cloud'])
@pytest.mark.parametrize('failure', [None, ava_readiness.BAD_KEY])
def test_apply_checks_without_a_turn(qapp, monkeypatch, tracker, route, failure):
    final = config_for(route)
    app = SimpleNamespace(config={'ava': {'provider_policy': 'off'}}, save_config=Mock(),
                          _config_lock=threading.Lock())
    harness = SimpleNamespace(app=app, _save_fns=[lambda _updates: copy.deepcopy(final)],
                              _replace_tts_runtime=Mock(), close=Mock())
    monkeypatch.setattr(listening_display, 'run_display', lambda _app: None)
    monkeypatch.setattr(listening_display, 'request_restart_once', lambda *_args: None)
    monkeypatch.setattr(qt_runtime, 'post', lambda _fn: None)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls, workers = [], []

    def probe(current):
        calls.append(ava_readiness.configured_provider(current))
        entered.set()
        assert release.wait(3)
        return calls[-1], failure

    def spawn(_name, fn, **_kwargs):
        def run():
            try:
                fn()
            finally:
                finished.set()
        worker = threading.Thread(target=run, daemon=True)
        workers.append(worker)
        worker.start()
        return worker

    monkeypatch.setattr(ava_readiness, 'probe_configured_provider', probe)
    monkeypatch.setattr(thread_registry, 'spawn', spawn)
    header = home_qt.AvaHeaderControl(app)
    try:
        SettingsServices._apply_and_close(harness)
        assert entered.wait(2)
        header.refresh()
        assert header.visible_text.startswith('Ava: checking')  # 824: state precedes the retained route.
        assert tracker.snapshot().state == ava_readiness.UNKNOWN
        done = threading.Event()
        tracker.add_listener(lambda _old, new: done.set() if new.state != ava_readiness.UNKNOWN else None)
        release.set()
        assert done.wait(2)
        header.refresh()
        assert header.visible_text.startswith('Ava: unavailable' if failure else 'Ava: ready')  # 824: state first.
        if failure:
            reason = ava_readiness.failure_short(failure)
            assert reason in header.toolTip() == header.accessibleDescription()
            assert reason in home_signals.problem_notice(app).headline
        assert calls == ['ollama' if route == 'local' else 'deepseek']
        assert app.config['ava']['consent'] == final['ava']['consent']
        app.save_config.assert_called_once()
    finally:
        release.set()
        if ava_readiness._monitor:
            ava_readiness._monitor.stop()
        for worker in workers:
            worker.join(2)
        header.deleteLater()
    assert finished.is_set()


@pytest.mark.parametrize('change', ['provider', 'api_key', 'local_model'])
def test_changed_inputs_reset_and_wake_one_existing_monitor(monkeypatch, tracker, change):
    app = SimpleNamespace(config=config_for('local' if change == 'local_model' else 'cloud'))
    before = ava_readiness.settings_signature(app)
    if change == 'provider':
        app.config = config_for(provider='openai')
    elif change == 'api_key':
        app.config['cloud_llm']['api_key'] = chr(121) * 8
    else:
        app.config['ava_command_session']['model'] = 'fixture-model'
    tracker.record_probe(ava_readiness.configured_provider(app), None)
    monitor = Mock()
    monkeypatch.setattr(ava_readiness, '_monitor', monitor)
    spawn = Mock()
    monkeypatch.setattr(thread_registry, 'spawn', spawn)
    ava_readiness.recheck_after_settings(app, before)
    assert tracker.snapshot().state == ava_readiness.UNKNOWN
    monitor.wake.assert_called_once()
    spawn.assert_not_called()
    ava_readiness.recheck_after_settings(app, ava_readiness.settings_signature(app))
    monitor.wake.assert_called_once()


def test_old_probe_cannot_replace_changed_settings(tracker):
    def probe():
        tracker._set('openai', None, 'settings', state=ava_readiness.UNKNOWN, invalidate_warm=True)
        return 'deepseek', ava_readiness.BAD_KEY
    monitor = ava_readiness.ReadinessMonitor(tracker, probe)
    assert monitor.run_once() == tracker.snapshot()
    assert tracker.snapshot().state == ava_readiness.UNKNOWN
    assert tracker.snapshot().provider == 'openai'


def test_off_startup_keeps_a_monitor_without_provider_io(monkeypatch, tracker):
    from plugins.commands import ask_ollama
    app = SimpleNamespace(config={'ava': {'provider_policy': 'off'}})
    spawn = Mock()
    health = Mock()
    monkeypatch.setattr(ask_ollama.thread_registry, 'spawn', spawn)
    monkeypatch.setattr(ask_ollama, '_start_health_monitor', health)
    ask_ollama.start_services(app)
    assert ava_readiness._monitor is not None
    spawn.assert_called_once()
    health.assert_not_called()
    get = Mock(side_effect=AssertionError('off must not make a request'))
    assert ava_readiness.probe_configured_provider(app, http_get=get) == ('none', ava_readiness.NOT_CONFIGURED)
    get.assert_not_called()


@pytest.mark.parametrize('provider', list(cloud_llm.BUILTIN_PROVIDERS))
@pytest.mark.parametrize('status, reason', [(200, None), (401, 'The key was rejected.'),
    (403, 'The key was rejected.'), (402, 'The account is out of balance.'),
    (429, 'Rate limited. Try again in a minute.'), (500, 'The provider returned an error.')])
def test_authenticated_test_reads_status(monkeypatch, provider, status, reason):
    app = SimpleNamespace(config=config_for(provider=provider))
    get = Mock(return_value=SimpleNamespace(status_code=status, json=lambda: {'data': {}}, close=lambda: None))
    monkeypatch.setattr(cloud_llm.ai_transport, 'get_cloud', get)
    result = cloud_llm.check_available(app)
    if status == 429:
        # Stand-in copy: no Retry-After header means a short, unspecified wait.
        names = {"deepseek": "DeepSeek", "openai": "OpenAI", "anthropic": "Anthropic", "openrouter": "OpenRouter"}
        reason = f"{names[provider]} is rate limiting requests. Try again shortly."
    if provider == "openrouter" and status == 403:
        reason = "OpenRouter blocked the request because of provider policy or budget."
    if provider == "openrouter":
        if status == 200:
            assert result[0] and 'Key works.' in result[1] and 'real turn' in result[1]
        else:
            assert result == (False, ava_readiness.failure_sentence(
                cloud_llm.failure_from_response(provider, get.return_value).kind, provider))
    else:
        assert result == ((False, reason) if reason else (True, provider))
    endpoint = '/key' if provider == 'openrouter' else '/models'
    assert get.call_args.args[0] == cloud_llm.BUILTIN_PROVIDERS[provider]['base_url'] + endpoint
    headers = get.call_args.kwargs['headers']
    assert ('x-api-key' if provider == 'anthropic' else 'Authorization') in headers
    if provider == 'anthropic':
        assert headers['anthropic-version'] == '2023-06-01'


@pytest.mark.parametrize('error, reason', [(requests.exceptions.Timeout, 'Did not answer in time.'),
    (requests.exceptions.ConnectionError, 'Could not be reached.'),
    (ValueError, 'The provider returned an error.')])
def test_test_failure_never_echoes_raw_error(monkeypatch, error, reason):
    marker = chr(122) * 12
    monkeypatch.setattr(cloud_llm.ai_transport, 'get_cloud', Mock(side_effect=error(marker)))
    assert cloud_llm.check_available(SimpleNamespace(config=config_for())) == (False, reason)


@pytest.mark.parametrize('status, text', [(200, 'Connected to DeepSeek.'), (401, 'The key was rejected.')])
def test_button_tests_unsaved_field(qapp, monkeypatch, status, text):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp
    app = _StubApp(config_for())
    window = _SettingsWindow(app)
    entered = chr(119) * 8
    window._widgets['cloud_api_key'].setText(entered)
    monkeypatch.setattr(thread_registry, 'spawn', lambda _name, fn, **_kw: fn())
    def get(_url, **kwargs):
        assert kwargs['headers']['Authorization'] == 'Bearer ' + entered
        return SimpleNamespace(status_code=status)
    monkeypatch.setattr(cloud_llm.ai_transport, 'get_cloud', get)
    try:
        window._run_test_connection()
        assert window._widgets['cloud_test_status'].text() == text
        assert app.config['cloud_llm']['api_key'] != entered
    finally:
        window.deleteLater()


@pytest.mark.parametrize('route', ['off', 'local', 'cloud'])
def test_ava_rows_grouping_and_search(qapp, route):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp
    from samsara.ui.settings.presentation import SettingRow
    config = config_for('local' if route == 'off' else route)
    if route == 'off':
        config['ava']['provider_policy'] = 'off'
        config['ava_command_session']['enabled'] = False
    window = _SettingsWindow(_StubApp(config))
    window._stack.setCurrentWidget(window._pages['Ava'])
    window.show()
    qapp.processEvents()
    try:
        page = window._pages['Ava']
        rows = [page.base.itemAt(i).widget() for i in range(page.base.count())]
        rows = [row for row in rows if isinstance(row, SettingRow)]
        assert [r.widget_keys[0] for r in rows[:4]] == ['providerRoute', 'cloud_provider', 'ava_cmd_model', 'ava_mode_key']
        assert window._rows_by_widget['cloud_provider'].isVisible() == (route == 'cloud')
        assert window._rows_by_widget['ava_cmd_model'].isVisible() == (route == 'local')
        route_control = window._widgets['providerRoute']
        assert [route_control.itemText(i) for i in range(3)] == ['Off', 'On this PC', 'In the cloud']
        key = window._rows_by_widget['ava_mode_key']
        assert key.page_name == 'Ava'
        assert key.isAncestorOf(window._widgets['ava_mode_key_off_note'])
        assert page.smart_corrections.isAncestorOf(window._widgets['sc_status_label'])
        for name, row in window._rows_by_widget.items():
            if name.startswith('sc_'):
                assert page.smart_corrections.isAncestorOf(row)
        assert window._widgets['sc_status_label'].text().startswith('Smart Corrections:')
        assert 'Cloud AI enabled' not in window._widgets['sc_status_label'].text()
        window._stack.setCurrentWidget(window._pages['Shortcuts'])
        window._search_edit.setText('Ava held key')
        window._apply_search_filter()
        assert window._stack.currentWidget() is page
        assert key.isVisible()
    finally:
        window.hide()
        window.deleteLater()
