"""Safety boundaries with fake providers and the synthetic 975b service."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from samsara import ai_preferences as ai, execution_policy as policy
from samsara import cockpit_memos as cm
from samsara.ava_structured_request import StructuredRequest, ExtractionRefused, strict_json
from samsara.cockpit_client import CockpitClient, CockpitError, BASE_URL
from tests.test_cockpit_memos_975c import app, backend, runner, policy_window, sources, context, FIXTURE


def test_local_never_cloud_fallback(app, monkeypatch):
    from samsara import ai_transport
    sent = []
    def unavailable(*args, **kwargs):
        sent.append(args[0])
        raise OSError('synthetic unavailable provider')
    monkeypatch.setattr(ai_transport, 'post_local', unavailable)
    with pytest.raises(ExtractionRefused, match='no fallback'):
        StructuredRequest(app).extract(sources(['m1']), context(), 0)
    assert sent == ['http://127.0.0.1:11434/api/chat']


def test_off_has_zero_transport(app):
    app.config['ava']['provider_policy'] = 'off'
    sent = []
    with pytest.raises(ExtractionRefused, match='off'):
        StructuredRequest(app, transport=lambda *a: sent.append(True)).extract(sources(['m1']), context(), 0)
    assert sent == []


def test_provider_scope_rechecked(app):
    sent = []
    def changed(*args):
        sent.append(True)
        app.config['ava']['consent'] = {}
        return json.dumps(FIXTURE['extraction_cases'][0]['expected'])
    with pytest.raises(ExtractionRefused, match='consent'):
        StructuredRequest(app, transport=changed).extract(sources(['m1']), context(), 0)
    assert sent == [True]
    with pytest.raises(ExtractionRefused, match='consent'):
        StructuredRequest(app, transport=lambda *a: pytest.fail('unconsented send')).extract(sources(['m1']), context(), 0)


def test_stateless_payload_has_no_memory_or_paths(app):
    captured = []
    data = sources(['m1'])
    data[0]['source_locator'] = 'SYNTHETIC_LOCAL_PATH'
    app.config['memory'] = {'synthetic': 'UNRELATED_SYNTHETIC_CONTEXT'}
    def reply(_config, _identity, _endpoint, system, user):
        captured.append((system, user))
        return json.dumps(FIXTURE['extraction_cases'][0]['expected'])
    StructuredRequest(app, transport=reply).extract(data, context(), 0)
    payload = json.loads(captured[0][1])
    assert set(payload) == {'schema', 'memos', 'records', 'local_date', 'timezone'}
    assert set(payload['memos'][0]) == {'memo_id', 'text', 'captured_at', 'capture_timezone', 'partial'}
    text = ''.join(captured[0])
    assert 'SYNTHETIC_LOCAL_PATH' not in text and 'UNRELATED_SYNTHETIC_CONTEXT' not in text
    assert 'tools' not in payload and 'messages' not in payload


def test_late_reply_after_stop_is_discarded(app):
    def late(*args):
        policy.stop_all(app, 'synthetic stop', chip=False)
        return json.dumps(FIXTURE['extraction_cases'][0]['expected'])
    with pytest.raises(ExtractionRefused, match='Late extraction'):
        StructuredRequest(app, transport=late).extract(sources(['m1']), context(), 0)


def test_confirmation_cannot_be_model_fabricated(runner):
    runner.start(cm.select_memos(runner.test_data, count=3))
    runner.read_page(0, 5)
    op = runner.stage_approval()
    assert policy.answer_pending(runner.app, 'yes', source='model') == 'refused:model'
    assert op.approved is None and 'confirm' not in runner.test_calls
    assert not op.approve(source='model', app=runner.app)
    assert policy.answer_pending(runner.app, 'yes') == 'approved'
    assert runner.last_result['status'] == 'applied'


def test_one_pending_question(runner):
    runner.start(cm.select_memos(runner.test_data, count=3))
    runner.read_page(0, 5)
    first = runner.stage_approval()
    old_question = runner.app._ava_question
    runner.repreview(selected_keys=['i1'])
    assert first.approved is False
    runner.read_page(0, 5)
    second = runner.stage_approval()
    assert policy.pending_operation() is second
    assert runner.app._ava_question is not old_question
    assert runner.app._ava_question.operation is second
    assert second.approved is None


@pytest.mark.parametrize('reason,content', [('length', '{"schema_version":'), ('length', '{"items":[]}'), ('tool_calls', '{}')])
def test_large_output_truncation_no_proposal(app, monkeypatch, reason, content):
    from samsara import ai_transport
    # Use a selected consented cloud route with synthetic credentials only.
    app.config['ava']['provider_policy'] = 'cloud'
    app.config['cloud_llm'] = dict(provider='deepseek', enabled=True, api_key='synthetic', model='synthetic-model')
    provider = ai.provider_for(app.config, 'cloud')
    app.config['ava']['consent'] = ai.accept_consent(app.config, provider=provider, cloud=True,
                                                   accepted_at='2026-11-01T00:00:00Z')
    app.config['cockpit_memos']['qualification'].update(provider='deepseek', origin=provider.origin)
    response = SimpleNamespace(raise_for_status=lambda: None, close=lambda: None,
        json=lambda: dict(choices=[dict(finish_reason=reason, message=dict(content=content))]))
    monkeypatch.setattr(ai_transport, 'post_cloud', lambda *a, **k: response)
    with pytest.raises(ExtractionRefused, match='truncated'):
        StructuredRequest(app).extract(sources(['m1']), context(), 0)


@pytest.mark.parametrize('text', ['{"items":[],"items":[]}', '{"x":NaN}', '```json\n{}\n```'])
def test_json_does_not_repair_untrusted_output(text):
    with pytest.raises(ExtractionRefused):
        strict_json(text)


def test_unqualified_feature_defaults_off_before_transport(app):
    app.config.pop('cockpit_memos')
    with pytest.raises(ExtractionRefused, match='not been enabled'):
        StructuredRequest(app, transport=lambda *a: pytest.fail('unqualified request')).extract(sources(['m1']), context(), 0)


def test_remote_local_destination_refused(app):
    app.config['ollama']['host'] = 'http://192.0.2.1:11434'
    with pytest.raises(ExtractionRefused):
        StructuredRequest(app, transport=lambda *a: pytest.fail('non-loopback')).extract(sources(['m1']), context(), 0)


def test_ai_off_cancels_pending_authority(runner):
    runner.start(cm.select_memos(runner.test_data, count=3))
    runner.read_page(0, 5)
    runner.stage_approval()
    runner.app.config['ava']['provider_policy'] = 'off'
    assert policy.answer_pending(runner.app, 'yes').startswith('refused:')
    assert 'confirm' not in runner.test_calls and 'apply' not in runner.test_calls


def test_source_and_proposal_arguments_are_bound(runner):
    runner.start(cm.select_memos(runner.test_data, count=3))
    runner.read_page(0, 5)
    runner.stage_approval()
    runner._binding['request_id'] = 'different'
    assert policy.answer_pending(runner.app, 'yes') == 'refused:arguments changed'
    assert 'apply' not in runner.test_calls


def test_lost_apply_response_recovers_once(runner):
    original = runner.client.apply
    def lost(*args):
        original(*args)
        raise CockpitError('synthetic lost response')
    runner.client.apply = lost
    runner.start(cm.select_memos(runner.test_data, count=3))
    runner.read_page(0, 5)
    runner.stage_approval()
    policy.answer_pending(runner.app, 'yes')
    assert runner.test_calls.count('apply') == runner.test_calls.count('recover') == 1
    assert runner.test_state['rev'] == 1
    assert runner.last_result['receipt']['status'] == 'applied'


def test_unknown_application_blocks_reextraction_until_receipt(runner):
    runner.start(cm.select_memos(runner.test_data, count=3))
    runner.read_page(0, 5)
    original_apply, original_recover = runner.client.apply, runner.client.recover
    def lost(*args):
        original_apply(*args)
        raise CockpitError('synthetic lost response')
    runner.client.apply = lost
    runner.client.recover = lambda *a: (_ for _ in ()).throw(CockpitError('synthetic unavailable receipt'))
    runner.stage_approval()
    policy.answer_pending(runner.app, 'yes')
    assert runner.application_uncertain
    with pytest.raises(cm.MemoDisposition, match='Recover'):
        runner.start(cm.select_memos(runner.test_data, count=3))
    runner.client.recover = original_recover
    assert runner.recover(runner.request_id)['receipt']['status'] == 'applied'
    assert not runner.application_uncertain


def test_registered_voice_review_uses_current_policy_window(runner):
    from plugins.commands.cockpit import review_batch
    runner.app._cockpit_memo_runner = runner
    runner.start(cm.select_memos(runner.test_data, count=3))
    runner.read_page(0, 5)
    assert review_batch(runner.app)
    assert runner.app._ava_question.operation is runner.operation
    assert runner.app._ava_question.opened
    assert policy.answer_pending(runner.app, 'yes') == 'approved'
    assert runner.last_result['status'] == 'applied'


def test_model_cannot_resolve_a_semantic_target_or_replace_review(app, policy_window):
    for command, remainder in [('choose cockpit target', 'item two candidate one'),
                                ('skip cockpit item', 'two'), ('review cockpit batch', '')]:
        inv = policy.Invocation(command, {'remainder': remainder}, policy.Route.MODEL, 0)
        decision = policy.authorize(inv, app=app)
        assert isinstance(decision, policy.Denied)
        assert decision.reason == 'not_allowed_for_model'


def test_client_uses_fixed_loopback_and_refuses_redirects():
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return SimpleNamespace(status_code=302, content=b'{}', close=lambda: None)
    with pytest.raises(CockpitError, match='302'):
        CockpitClient(request=request).context()
    assert calls[0][1] == BASE_URL + '/api/context?purpose=memo'
    assert calls[0][2]['allow_redirects'] is False
    assert len(calls) == 1


def test_browser_capability_not_reused_for_voice():
    with pytest.raises(CockpitError, match='not available yet'):
        CockpitClient().confirm({}, {'confirmed': True})


def test_model_authorization_key_invalidates_entire_draft(runner):
    malicious = deepcopy(FIXTURE['extraction_cases'][0]['expected'])
    malicious['confirmed'] = True
    runner.extractor = StructuredRequest(runner.app, transport=lambda *a: json.dumps(malicious))
    with pytest.raises(ValueError, match='keys'):
        runner.start(cm.select_memos(runner.test_data, count=3))
    assert runner.test_calls == []


def test_changed_cockpit_state_and_expiry_refuse_before_confirm(runner):
    runner.start(cm.select_memos(runner.test_data, count=3))
    runner.read_page(0, 5)
    runner.stage_approval()
    runner.test_state['rev'] += 1
    assert policy.answer_pending(runner.app, 'yes').startswith('refused:target changed')
    assert 'confirm' not in runner.test_calls


def test_expired_preview_has_no_authority(runner):
    runner.start(cm.select_memos(runner.test_data, count=3))
    runner.read_page(0, 5)
    operation = runner.stage_approval()
    operation.deadline = 0
    assert policy.answer_pending(runner.app, 'yes') == 'refused:expired'
    assert 'confirm' not in runner.test_calls and 'apply' not in runner.test_calls


def test_network_speech_consent_is_independent(app, monkeypatch):
    from plugins.commands.cockpit import announce
    from plugins.commands import ask_ollama
    app.config['tts'] = dict(engine='edge', network_voice_accepted=False)
    shown = []
    app._show_outcome_chip = lambda message, kind: shown.append(message)
    monkeypatch.setattr(ask_ollama, 'speak', lambda *a, **k: pytest.fail('unconsented network speech'))
    announce(app, 'Synthetic preview')
    assert shown == ['Synthetic preview']


def test_synthetic_http_client_and_real_service_contract(backend, monkeypatch):
    """Exercise actual requests/session against an ephemeral invented server."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import samsara.cockpit_client as client_module
    service, state, cc = backend
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            value = context()
            body = json.dumps(value).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            value = service.preview(body)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(client_module, 'BASE_URL', f'http://127.0.0.1:{server.server_port}')
        monkeypatch.setenv('HTTP_PROXY', 'http://192.0.2.1:1')
        client = CockpitClient()
        assert client.context()['records'] == context()['records']
        memos = sources(['m1', 'm2', 'm3'])
        draft = cm.normalize_extraction_v1(FIXTURE['extraction_cases'][0]['expected'], context(), memos)
        proposal = client.preview(dict(schema_version='cockpit.changes.v1', request_id='synthetic_http',
            base_revision=state['rev'], base_hash=state['_stateHash'], sources=memos, extraction=draft))
        assert proposal['counts']['selected'] == 4
        assert state['rev'] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
