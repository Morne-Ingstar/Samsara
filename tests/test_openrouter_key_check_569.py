"""569: free checks, synthetic credentials, isolated profile and fake transport."""
import json
import logging
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import requests

from samsara import ai_preferences, ava_readiness as ready, cloud_llm as cloud
from samsara import openrouter_catalog as catalog


def app_for():
    config = {"ava": {"provider_policy": "cloud"}, "ava_command_session": {"enabled": True},
              "cloud_llm": {"enabled": True, "provider": "openrouter", "api_key": "synthetic", "model": "example/text"}}
    config["ava"]["consent"] = ai_preferences.accept_consent(config, features=("ava",),
        provider=ai_preferences.provider_for(config, "cloud"), cloud=True, accepted_at="2026-10-13T00:00:00Z")
    return SimpleNamespace(config=config)


def response(status=200, data=None, retry=None):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps({"data": {} if data is None else data}).encode()
    r._content_consumed = True
    if retry is not None:
        r.headers["Retry-After"] = retry
    return r


def models(**extra):
    row = {"id": "example/text", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
           "supported_parameters": [], **extra}
    return catalog.Catalogue(catalog.parse_models({"data": [row]}))


@pytest.fixture(autouse=True)
def isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    monkeypatch.setattr(cloud.ai_transport.requests.Session, "request",
                        lambda *a, **k: pytest.fail("real network forbidden"))


@pytest.mark.parametrize("status,kind,state", [(401, "bad_key", ready.OFFLINE),
    (402, "out_of_credit", ready.NEEDS_ATTENTION), (403, "blocked", ready.NEEDS_ATTENTION),
    (404, "model_unavailable", ready.NEEDS_ATTENTION), (429, "rate_limited", ready.WAITING),
    (503, "provider_down", ready.OFFLINE)])
def test_failure_states(status, kind, state):
    result = cloud.check_openrouter(app_for(), http_get=lambda *a, **k: response(status, retry="999999"))
    assert result.failure.kind == kind
    snap = ready.ReadinessTracker(clock=lambda: 10).record_probe("openrouter", result.failure)
    assert snap.state == state
    if state == ready.WAITING:
        assert snap.retry_at == 3610


@pytest.mark.parametrize("remaining", [0, -1])
def test_exhausted_key_cap(remaining):
    result = cloud.check_openrouter(app_for(), http_get=lambda *a, **k: response(data={"limit": 5, "limit_remaining": remaining}))
    assert result.key_valid and result.failure.kind == "out_of_credit"


def test_metadata_and_no_private_response_in_logs(caplog):
    app = app_for()
    private = "private-" + chr(122) * 34
    app.config['cloud_llm']['api_key'] = private
    def get(url, **kw):
        assert url == "https://openrouter.ai/api/v1/key"
        assert kw == {"timeout": 3, "allow_redirects": False, "headers": {"Authorization": "Bearer " + private}}
        return response(data={"limit": 10, "limit_remaining": 4, "limit_reset": "monthly",
                              "is_free_tier": False, "label": private, "unknown": private})
    with caplog.at_level(logging.DEBUG):
        result = cloud.check_openrouter(app, http_get=get, catalogue=models())
    assert (result.key_valid, result.limit, result.limit_remaining, result.usage_reset, result.is_free_tier) == (True, 10, 4, "monthly", False)
    assert result.failure is None
    assert private not in repr(result) + result.status_text() + caplog.text
    assert "real turn" in result.status_text()


@pytest.mark.parametrize("change", ["consent", "route", "provider", "disabled"])
def test_gates_send_nothing(change):
    app = app_for()
    if change == 'consent': app.config['ava']['consent'] = {}
    if change == 'route': app.config['ava']['provider_policy'] = 'local'
    if change == 'provider': app.config['cloud_llm']['provider'] = 'openai'
    if change == 'disabled': app.config['cloud_llm']['enabled'] = False
    result = cloud.check_openrouter(app, http_get=lambda *a, **k: pytest.fail('request forbidden'))
    assert result.failure.kind == 'not_configured'


@pytest.mark.parametrize("extra,tools,blocked", [({}, False, False), ({}, True, True),
    ({"supported_parameters": ["tools"]}, True, False),
    ({"expiration_date": (date.today() - timedelta(days=1)).isoformat()}, False, True),
    ({"architecture": {"input_modalities": ["image"], "output_modalities": ["text"]}}, False, True),
    ({"architecture": {"input_modalities": ["text"], "output_modalities": ["audio"]}}, False, True),
    ({"architecture": None}, False, False),
    ({"architecture": {}}, False, False),
    ({"architecture": {"input_modalities": [], "output_modalities": ["text"]}}, False, False),
    ({"architecture": {"input_modalities": ["text"]}}, False, False)])
def test_selected_model(extra, tools, blocked):
    result = cloud.check_openrouter(app_for(), http_get=lambda *a, **k: response(), catalogue=models(**extra), needs_tools=tools)
    assert bool(result.failure) == blocked
    if blocked: assert result.failure.kind == 'model_unavailable'


def test_custom_and_absent_cache_are_allowed():
    result = cloud.check_openrouter(app_for(), http_get=lambda *a, **k: response(), catalogue=catalog.Catalogue())
    assert result.failure is None and 'not checked' in result.model_note


def test_unknown_model_id_needs_attention():
    app = app_for()
    app.config['cloud_llm']['model'] = 'unknown'
    result = cloud.check_openrouter(app, http_get=lambda *a, **k: response(), catalogue=models())
    assert result.failure.kind == 'model_unavailable' and 'unknown' in result.model_note


def test_cache_preserves_modalities_without_fetch(tmp_path):
    path = tmp_path / 'openrouter_catalog.json'
    catalog._write_cache(path, models())
    result = cloud.check_openrouter(app_for(), http_get=lambda *a, **k: response())
    assert result.failure is None and 'supports' in result.model_note


@pytest.mark.parametrize('model_id', ['example/text', ''])
def test_pre_569_cache_without_architecture_is_allowed(tmp_path, model_id):
    # This is the JSON shape written by the 568 catalogue before modalities existed.
    app = app_for()
    app.config['cloud_llm']['model'] = model_id
    (tmp_path / 'openrouter_catalog.json').write_text(json.dumps({'data': [
        {'id': 'example/text', 'name': 'Text', 'pricing': {'prompt': '0', 'completion': '0'},
         'context_length': 4096, 'expiration_date': None, 'supported_parameters': []},
        {'id': 'openrouter/auto', 'name': 'Auto', 'pricing': {}, 'supported_parameters': []}],
        'fetched_at': 100, 'attempted_at': 100, 'reason': ''}), encoding='utf-8')
    result = cloud.check_openrouter(app, http_get=lambda *a, **k: response())
    assert result.failure is None and 'not checked' in result.model_note
    assert ready.ReadinessTracker().record_probe('openrouter', result.failure).state == ready.CONFIGURED


def test_live_row_without_architecture_is_allowed():
    rows = catalog.parse_models({'data': [{'id': 'example/text'}]})
    assert rows[0]['architecture'] == {}
    result = cloud.check_openrouter(app_for(), http_get=lambda *a, **k: response(),
                                    catalogue=catalog.Catalogue(rows))
    assert result.failure is None and 'not checked' in result.model_note


@pytest.mark.parametrize('kind', ['unreachable', 'timeout', 'provider_down'])
def test_transient_probe_recovers_at_offline_cadence(monkeypatch, kind):
    app, calls, clock = app_for(), [], [100.0]
    monkeypatch.setattr(ready.time, 'monotonic', lambda: clock[0])
    def check(*a, **k):
        calls.append(clock[0])
        return cloud.OpenRouterCheck(cloud.make_failure(kind, 'openrouter')) if len(calls) == 1 else cloud.OpenRouterCheck(key_valid=True)
    monkeypatch.setattr(cloud, 'check_openrouter', check)
    assert ready.probe_configured_provider(app)[1].kind == kind
    clock[0] += ready.OFFLINE_POLL_S - .01
    assert ready.probe_configured_provider(app)[1].kind == kind
    assert len(calls) == 1
    clock[0] += .01
    assert ready.probe_configured_provider(app)[1] is None
    clock[0] += 100
    assert ready.probe_configured_provider(app)[1] is None
    assert len(calls) == 2


def test_transient_probe_honours_bounded_retry_after(monkeypatch):
    app, calls, clock = app_for(), [], [100.0]
    monkeypatch.setattr(ready.time, 'monotonic', lambda: clock[0])
    def check(*a, **k):
        calls.append(1)
        return cloud.OpenRouterCheck(cloud.make_failure('provider_down', 'openrouter', retry_after=1e308))
    monkeypatch.setattr(cloud, 'check_openrouter', check)
    ready.probe_configured_provider(app)
    clock[0] += 3599
    ready.probe_configured_provider(app)
    assert len(calls) == 1
    clock[0] += 1
    ready.probe_configured_provider(app)
    assert len(calls) == 2


@pytest.mark.parametrize('kind', ['bad_key', 'out_of_credit', 'rate_limited', 'model_unavailable'])
def test_nontransient_probe_waits_for_explicit_recheck(monkeypatch, kind):
    app, calls, clock = app_for(), [], [100.0]
    monkeypatch.setattr(ready.time, 'monotonic', lambda: clock[0])
    def check(*a, **k):
        calls.append(1)
        return cloud.OpenRouterCheck(cloud.make_failure(kind, 'openrouter'))
    monkeypatch.setattr(cloud, 'check_openrouter', check)
    ready.probe_configured_provider(app)
    clock[0] += 3600
    ready.probe_configured_provider(app)
    assert len(calls) == 1
    ready.request_recheck()
    ready.probe_configured_provider(app)
    assert len(calls) == 2


def test_redirect_refused_and_not_followed():
    calls = []
    def get(url, **kw):
        calls.append(url)
        assert kw['allow_redirects'] is False
        r = response(302)
        r.headers['Location'] = 'https://example.invalid'
        return r
    result = cloud.check_openrouter(app_for(), http_get=get)
    assert result.failure.kind == 'unreachable' and len(calls) == 1


@pytest.mark.parametrize('exc,kind', [(requests.Timeout, 'timeout'), (requests.ConnectionError, 'unreachable'), (ValueError, 'error')])
def test_exception_drops_private_message(exc, kind):
    def get(*a, **k): raise exc('synthetic-private')
    result = cloud.check_openrouter(app_for(), http_get=get)
    assert result.failure.kind == kind and 'synthetic-private' not in repr(result)


def test_probe_once_until_recheck_and_only_turn_proves_ready():
    app, calls = app_for(), []
    def get(*a, **k):
        calls.append(1)
        return response()
    tracker = ready.ReadinessTracker()
    for _ in range(3):
        provider, failure = ready.probe_configured_provider(app, http_get=get)
        assert tracker.record_probe(provider, failure).state == ready.CONFIGURED
    assert len(calls) == 1
    ready.request_recheck()
    ready.probe_configured_provider(app, http_get=get)
    assert len(calls) == 2
    assert tracker.record_turn('openrouter', None).ready
    assert tracker.record_probe('openrouter', None).ready
    assert ready.ReadinessTracker().record_warm_result('openrouter', None, 0).state == ready.CONFIGURED


def test_warmup_never_buys_a_completion(monkeypatch):
    monkeypatch.setattr(ready, 'tracker', ready.ReadinessTracker())
    monkeypatch.setattr(ready, 'probe_configured_provider', lambda app: ('openrouter', None))
    monkeypatch.setattr(cloud, 'send', lambda *a, **k: pytest.fail('paid probe forbidden'))
    assert ready.warm_configured_provider(app_for()).state == ready.CONFIGURED


def test_settings_checks_model_and_consent_once_per_open(monkeypatch):
    from unittest.mock import Mock
    from samsara.ui.settings.ava_cloud_qt import AvaCloudPage
    from samsara.runtime import thread_registry
    app = app_for()
    window = SimpleNamespace(_widgets={'cloud_provider': Mock(currentText=lambda: 'OpenRouter'),
        'cloud_api_key': Mock(text=lambda: 'synthetic'), 'cloud_model': Mock(text=lambda: 'custom/model')},
        _ava_staged_config=lambda: app.config, _test_result=Mock(), _visit_test_result=Mock(),
        _visit_callback=lambda fn: fn, _on_test_result=Mock())
    calls = []
    def check(fake):
        assert fake.config['ava']['consent'] == app.config['ava']['consent']
        calls.append(True)
        return True, 'Key works. Ava is proven ready only after a real turn.'
    monkeypatch.setattr(cloud, 'check_available', check)
    monkeypatch.setattr(thread_registry, 'spawn', lambda name, fn, **kw: fn())
    AvaCloudPage._run_test_connection(window)
    AvaCloudPage._run_test_connection(window)
    assert calls == [True]
    assert 'Already checked' in window._test_result.emit.call_args.args[0]
    assert 'real turn' in window._visit_test_result.emit.call_args.args[1]
    window._visit_generation = 1
    AvaCloudPage._run_test_connection(window)
    assert calls == [True, True]
    window._visit_generation = 2
    app.config['ava']['consent'] = {}
    AvaCloudPage._run_test_connection(window)
    assert calls == [True, True]
    assert 'accept consent' in window._test_result.emit.call_args.args[0]


def test_settings_changed_key_or_model_can_be_checked(monkeypatch):
    from unittest.mock import Mock
    from samsara.ui.settings.ava_cloud_qt import AvaCloudPage
    from samsara.runtime import thread_registry
    app, values, calls = app_for(), ['synthetic', 'custom/model'], []
    window = SimpleNamespace(_widgets={'cloud_provider': Mock(currentText=lambda: 'OpenRouter'),
        'cloud_api_key': Mock(text=lambda: values[0]), 'cloud_model': Mock(text=lambda: values[1])},
        _ava_staged_config=lambda: app.config, _test_result=Mock(), _visit_test_result=Mock(),
        _visit_callback=lambda fn: fn, _on_test_result=Mock())
    monkeypatch.setattr(cloud, 'check_available', lambda fake: (calls.append(1) or True, 'Key works.'))
    monkeypatch.setattr(thread_registry, 'spawn', lambda name, fn, **kw: fn())
    AvaCloudPage._run_test_connection(window)
    values[0] = 'corrected-synthetic'
    AvaCloudPage._run_test_connection(window)
    values[1] = 'custom/other'
    AvaCloudPage._run_test_connection(window)
    values[:] = ['synthetic', 'custom/model']
    AvaCloudPage._run_test_connection(window)
    assert len(calls) == 3
    assert 'Already checked' in window._test_result.emit.call_args.args[0]
    assert all(isinstance(signature[0], bytes) and len(signature[0]) == 32
               for signature in window._openrouter_key_checked)
