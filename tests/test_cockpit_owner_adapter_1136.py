"""Samsara approval flow against a local protocol fake; no Cockpit install needed.

The real Cockpit transaction/credential tests belong to its separate candidate.
This fake supplies responses and records requests; it does not qualify that engine.
"""
from copy import deepcopy
import hashlib
import json
import secrets
import time
from types import SimpleNamespace

import pytest
from samsara import cockpit_memos as cm, execution_policy as policy
from samsara.ava_structured_request import StructuredRequest
from samsara.cockpit_client import CockpitClient, CockpitError, BASE_URL
from samsara.cockpit_owner_adapter import NativeOwnerAdapter
from tests.test_cockpit_memos_975c import app, policy_window, sources, FIXTURE


def protocol_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class FakeCockpit:
    """Only the v1 responses used below; deliberately no server imports or I/O."""

    def __init__(self):
        self.state = dict(rev=0, tasks=[dict(id=r['id'], t=r['title'], s='')
            for r in FIXTURE['matching_context'] if r.get('collection') != 'events'],
            sched=[dict(id='e2', t='Repair visit', date='2026-11-03', time='14:00',
                        timezone=FIXTURE['timezone'])], notes={})
        self.state['tasks'][-1]['due'] = '2026-11-02'
        self.refresh_hash()
        self.calls, self.previews, self.receipts = [], {}, {}
        self.failure = dict(apply=False, recover=False)
        self.active = self.authorization = None

    def refresh_hash(self):
        self.state['_stateHash'] = protocol_hash({k: v for k, v in self.state.items() if k != '_stateHash'})

    def initialize_native_owner(self, path):
        # Synthetic temporary credential only; rotation tests the client's reread.
        path.parent.mkdir(exist_ok=True)
        self.key = secrets.token_urlsafe(32)
        path.write_text(self.key, encoding='ascii')

    def context(self):
        records = []
        for collection, prefix, kind in [('tasks', 'task', 'task'), ('sched', 'sched', 'event')]:
            for row in self.state[collection]:
                records.append(dict(id=prefix + ':' + row['id'], source_id=row['id'], kind=kind,
                    title=row['t'], writable=True, status='open', blocked_by=[],
                    due_date=row.get('due'), **row.get('planning', {}),
                    **{k: row[k] for k in ('date', 'time', 'timezone') if k in row}))
        return dict(schema_version='cockpit.context.v1', records=records,
            captured_at=FIXTURE['now'], local_date='2026-11-02', timezone=FIXTURE['timezone'],
            available=True, snapshot_consistent=True, coverage=dict(state_complete=True),
            sources=dict(state=dict(revision=self.state['rev'], hash=self.state['_stateHash'])))

    def preview(self, body):
        draft = deepcopy(body['extraction'])
        choices = body.get('target_choices', {})
        selected = body.get('selected_keys', [i['item_key'] for i in draft['items']])
        rows = []
        for item in draft['items']:
            key = item['item_key']
            if key in choices:
                item['target_id'] = choices[key]
            resolved = not (item.get('match') or {}).get('needs_resolution') or key in choices
            target = next((r for r in self.context()['records'] if r['id'] == item['target_id']), {})
            included = key in selected and resolved
            rows.append(dict(item_key=key, action=item['action'], target_id=item['target_id'],
                included=included, disposition='included' if included else 'excluded',
                diff=dict(title=item['fields'].get('title') or target.get('title'), fields=[])))
        manifest = [{k: m.get(k) for k in ('memo_id', 'text_sha256', 'captured_at', 'capture_timezone',
                     'source_kind', 'partial', 'upstream_id')} for m in body['sources']]
        proposal = dict(schema_version='cockpit.changes.v1', kind='apply',
            request_id=body['request_id'], base_revision=body['base_revision'], base_hash=body['base_hash'],
            source_hash=protocol_hash(manifest), extraction_hash=protocol_hash(body['extraction']),
            items=rows, unresolved=draft['unresolved'], no_change=draft['no_change'])
        return self.save(proposal, draft)

    def save(self, proposal, draft):
        proposal.update(proposal_id='preview_' + str(len(self.previews)), expires_at=time.time() + 300,
            counts=dict(selected=sum(r['included'] for r in proposal['items']),
                        excluded=sum(not r['included'] for r in proposal['items']),
                        unresolved=len(proposal['unresolved']), no_change=len(proposal['no_change'])))
        proposal['proposal_hash'] = protocol_hash(proposal)
        self.previews[proposal['proposal_id']] = (deepcopy(proposal), draft)
        self.active, self.authorization = proposal['proposal_id'], None
        return proposal

    def check_binding(self, body):
        proposal, draft = self.previews[self.active]
        assert {k: body[k] for k in ('proposal_id', 'proposal_hash', 'request_id')} == {
            k: proposal[k] for k in ('proposal_id', 'proposal_hash', 'request_id')}
        return proposal, draft

    def apply(self, body):
        proposal, draft = self.check_binding(body)
        assert self.authorization is not None and body['authorization'] == self.authorization
        assert body['request_id'] not in self.receipts
        self.authorization = None
        if proposal['kind'] == 'undo':
            # The undo scenario changes one task, preserving another task's edit.
            original = self.receipts[proposal['undo_of']]
            for item in original['draft']['items']:
                collection = 'tasks' if item['action'] == 'update_task' else 'sched'
                ident = item['target_id'].split(':')[1]
                row = next(r for r in self.state[collection] if r['id'] == ident)
                before = next(r for r in original['before'][collection] if r['id'] == ident)
                row.clear()
                row.update(deepcopy(before))
        else:
            before = deepcopy(self.state)
            for row, item in zip(proposal['items'], draft['items']):
                if not row['included']:
                    continue
                fields, action = item['fields'], item['action']
                if action == 'append_note':
                    self.state['notes'][fields['date']] = fields['text']
                    continue
                collection = 'tasks' if action.endswith('task') else 'sched'
                if action.startswith('create_'):
                    target = dict(id=proposal['proposal_id'] + item['item_key'])
                    self.state[collection].append(target)
                else:
                    target = next(r for r in self.state[collection] if r['id'] == item['target_id'].split(':')[1])
                for field, value in fields.items():
                    if field in ('priority', 'start_after', 'notes_append'):
                        target.setdefault('planning', {})[field] = value
                    else:
                        assert field in ('title', 'due_date', 'status', 'date', 'time', 'timezone')
                        target[dict(title='t', due_date='due', status='s').get(field, field)] = value
        receipt = dict(status='applied', receipt_id=body['request_id'], request_id=body['request_id'],
                       counts=proposal['counts'], source_hash=proposal['source_hash'])
        self.receipts[body['request_id']] = dict(receipt=receipt, draft=draft,
                                               before=before if proposal['kind'] == 'apply' else {})
        self.state['rev'] += 1
        self.refresh_hash()
        if self.failure['apply']:
            raise CockpitError('Synthetic lost response')
        return receipt

    def undo_preview(self, receipt_id, body):
        original = self.receipts[receipt_id]
        rows = [dict(item_key=item['item_key'], action='undo', target_id=item['target_id'],
                     included=True, disposition='included', diff={}) for item in original['draft']['items']]
        return self.save(dict(schema_version='cockpit.changes.v1', kind='undo', undo_of=receipt_id,
            request_id=body['request_id'], base_revision=self.state['rev'], base_hash=self.state['_stateHash'],
            source_hash=original['receipt']['source_hash'], items=rows, unresolved=[], no_change=[]), {})

    def request(self, method, url, **kwargs):
        assert url.startswith(BASE_URL) and kwargs['allow_redirects'] is False
        endpoint = url[len(BASE_URL):]
        self.calls.append(endpoint)
        body, headers = kwargs.get('json'), kwargs.get('headers', {})
        native = endpoint in ('/api/changes/confirm', '/api/changes/cancel')
        assert method == ('GET' if endpoint.startswith('/api/context') or
                          endpoint.rsplit('/', 1)[-1] in self.receipts else 'POST')
        if native:
            # Literal header spelling keeps this check independent of the adapter.
            assert headers == {'X-Cockpit-Native-Owner': self.key}
        else:
            assert headers == {}, 'Credential leaked outside native confirm/cancel'
        if endpoint == '/api/context?purpose=memo':
            value = self.context()
        elif endpoint == '/api/changes/preview':
            value = self.preview(body)
        elif endpoint == '/api/changes/confirm':
            self.check_binding(body)
            self.authorization = 'synthetic-authorization'
            value = dict(authorization=self.authorization)
        elif endpoint == '/api/changes/cancel':
            assert body == {}
            self.active = self.authorization = None
            value = dict(status='cancelled')
        elif endpoint == '/api/changes/apply':
            value = self.apply(body)
        elif endpoint.endswith('/undo-preview'):
            value = self.undo_preview(endpoint.split('/')[-2], body)
        else:
            assert endpoint.startswith('/api/changes/')
            if self.failure['recover']:
                raise CockpitError('Synthetic lost receipt')
            value = dict(receipt=self.receipts[endpoint.split('/')[-1]]['receipt'])
        return SimpleNamespace(status_code=200, content=json.dumps(value).encode(), close=lambda: None)


@pytest.fixture
def native(app, policy_window, tmp_path):
    service = FakeCockpit()
    path = tmp_path / 'private' / 'native-owner.key'
    service.initialize_native_owner(path)
    adapter = NativeOwnerAdapter(key_path=path, request=service.request)
    client = CockpitClient(owner_adapter=adapter, request=service.request)
    def make(ids, draft=None):
        data = sources(ids)
        if draft is None:
            case = next(c for c in FIXTURE['extraction_cases'] if c['memo_ids'] == ids)
            draft = case['expected']
        extractor = StructuredRequest(app, transport=lambda *args: json.dumps(draft))
        runner = cm.ProposalRunner(app, client, extractor, lambda: deepcopy(data))
        runner.start(cm.select_memos(data, count=len(ids)))
        app._cockpit_memo_runner = runner
        runner.test_data = data
        return runner
    yield SimpleNamespace(app=app, service=service, state=service.state, client=client, adapter=adapter,
                          path=path, calls=service.calls, make=make, failure=service.failure)
    path.unlink(missing_ok=True)


def review(runner):
    for page in range((len(runner.proposal['items']) + 4) // 5): runner.read_page(page, 5)
    return runner.stage_approval(listen=False)


def approve(runner):
    review(runner)
    assert policy.answer_pending(runner.app, 'yes') == 'approved'
    return (runner.last_result or {}).get('receipt') or runner.last_result


def test_drop_one_of_three_rebinds_without_confirm_or_apply(native):
    runner = native.make(['m1', 'm2', 'm3'])
    keys = [r['item_key'] for r in runner.proposal['items'] if r['included']][:3]
    runner.repreview(selected_keys=keys)
    old = review(runner)
    runner.repreview(selected_keys=keys[1:])
    assert old.approved is False and not old.approve(app=runner.app)
    assert '/api/changes/confirm' not in native.calls and '/api/changes/apply' not in native.calls
    assert native.state['rev'] == 0
    assert approve(runner)['counts']['selected'] == 2
    assert native.calls.count('/api/changes/apply') == 1


def test_choice_keeps_item_order_and_requires_new_review(native):
    runner = native.make(['m1', 'm2', 'm3'])
    item = runner.draft['items'][0]
    item.update(action='update_task', target_id='task:p2', fields={'notes_append': 'Waiting for the form.'},
        match=dict(method='semantic', candidates=[dict(id='task:p2', reason='possible'), dict(id='task:p3', reason='possible')], needs_resolution=True))
    runner.repreview()
    order = [r['item_key'] for r in runner.proposal['items']]
    old = review(runner)
    runner.repreview(target_choices={item['item_key']: 'task:p3'})
    assert [r['item_key'] for r in runner.proposal['items']] == order
    assert old.approved is False
    with pytest.raises(cm.MemoDisposition, match='Read all'): runner.stage_approval(listen=False)
    assert 'Renew archive pass' in runner.read_page(0, 5)
    assert approve(runner)['status'] == 'applied'


def test_m4_is_unresolved_then_explicit_new_preview_with_evidence(native):
    runner = native.make(['m4'])
    assert runner.proposal['items'] == []
    assert 'clarify cockpit item' in runner.read_page()
    old_request = runner.request_id
    fresh = runner.clarify(runner.draft['unresolved'][0]['key'], 'task:p2', {'notes_append': 'Waiting for the form.'})
    assert fresh.request_id != old_request
    assert fresh.draft['items'][0]['evidence'][:-1] == runner.draft['unresolved'][0]['evidence']
    assert native.state['rev'] == 0
    assert len(fresh.draft['items']) == 1 and fresh.proposal['unresolved'] == []
    assert approve(fresh)['status'] == 'applied'
    assert next(r for r in native.state['tasks'] if r['id'] == 'p2')['planning']['notes_append'] == 'Waiting for the form.'


def test_mixed_partial_apply_retains_unsafe_and_uses_new_request(native):
    cases = [c['expected'] for c in FIXTURE['extraction_cases'] if c['memo_ids'] in (['m5'], ['m6'])]
    draft = dict(schema_version=cm.EXTRACTION_VERSION,
                 **{k: sum((deepcopy(c[k]) for c in cases), []) for k in ('items', 'unresolved', 'no_change')})
    runner = native.make(['m5', 'm6'], draft)
    first = approve(runner)
    assert first['status'] == 'applied' and first['counts']['unresolved'] == 1
    with pytest.raises(cm.MemoDisposition, match='was applied'): runner.repreview()
    old_op = runner.operation
    fresh = runner.clarify(runner.draft['unresolved'][0]['key'], 'sched:e2', {'date': '2026-11-04'})
    assert fresh.request_id != first['receipt_id'] and len(fresh.draft['items']) == 1
    assert any(r['reason'] == 'unsafe_instruction' for r in fresh.proposal['no_change'])
    with pytest.raises(CockpitError): native.client.confirm(fresh._binding, old_op)
    assert approve(fresh)['status'] == 'applied'
    assert native.state['rev'] == 2 and native.state['sched'][0]['date'] == '2026-11-04'


@pytest.mark.parametrize('change', ['cancel', 'expiry', 'generation', 'off', 'provider', 'source'])
def test_revoked_approval_never_reaches_native_confirm(native, change):
    runner = native.make(['m1', 'm2', 'm3'])
    op = review(runner)
    if change == 'cancel': runner.cancel()
    elif change == 'expiry': op.deadline = 0
    elif change == 'generation': runner.app._ava_cmd_generation += 1
    elif change == 'off': runner.app.config['ava']['provider_policy'] = 'off'
    elif change == 'provider': runner.app.config['ollama']['model'] = 'changed'
    else: runner.test_data[0]['text'] += ' changed'
    assert policy.answer_pending(runner.app, 'yes') != 'approved'
    assert '/api/changes/confirm' not in native.calls and native.state['rev'] == 0


def test_model_forgery_direct_adapter_and_reuse_are_rejected(native):
    runner = native.make(['m1', 'm2', 'm3'])
    op = review(runner)
    assert policy.answer_pending(runner.app, 'yes', source='model') == 'refused:model'
    fake = policy.PendingOperation(op.invocation, 'forged', app=runner.app)
    fake.approved = True
    fake._event.set()
    for forged in ({'approved': True}, fake):
        with pytest.raises(CockpitError): native.client.confirm(runner._binding, forged)
    with pytest.raises(CockpitError): native.adapter.confirm(runner._binding, op)
    assert '/api/changes/confirm' not in native.calls
    assert policy.answer_pending(runner.app, 'yes') == 'approved'
    with pytest.raises(CockpitError): native.client.confirm(runner._binding, op)
    assert native.calls.count('/api/changes/confirm') == 1


def test_fresh_key_on_confirm_and_cancel_and_absent_outcome(native):
    runner = native.make(['m1', 'm2', 'm3'])
    native.service.initialize_native_owner(native.path)
    assert approve(runner)['status'] == 'applied'
    native.service.initialize_native_owner(native.path)
    native.client.cancel()
    assert native.calls[-1] == '/api/changes/cancel'
    native.path.unlink()
    assert not native.client.can_confirm
    with pytest.raises(CockpitError, match='not available yet'): native.client.confirm({}, None)


def test_lost_response_recovers_and_unknown_blocks_clarification(native):
    runner = native.make(['m1', 'm2', 'm3'])
    native.failure.update(apply=True, recover=True)
    assert approve(runner)['status'] == 'unknown'
    assert runner.application_uncertain
    with pytest.raises(cm.MemoDisposition, match='Recover'): runner.repreview()
    with pytest.raises(cm.MemoDisposition, match='Recover'): runner.start(runner.selection)
    with pytest.raises(cm.MemoDisposition, match='Recover'): runner.clarify('any', 'task:p2', {'priority': 'high'})
    native.failure['recover'] = False
    assert runner.recover(runner.request_id)['receipt']['status'] == 'applied'
    assert not runner.application_uncertain and native.calls.count('/api/changes/apply') == 1


@pytest.mark.parametrize('action,value,field', [('move', 'tomorrow', 'start_after'), ('deadline', 'tomorrow', 'due_date'),
                                              ('complete', None, 'status'), ('priority', 'high', 'priority')])
def test_followup_applies_once_after_approval(native, action, value, field):
    from plugins.commands import cockpit
    native.app._cockpit_read_client = native.client
    cockpit.followup(native.app, action, 'task:p6', value)
    runner = native.app._cockpit_memo_runner
    assert field in runner.draft['items'][0]['fields'] and native.state['rev'] == 0
    assert approve(runner)['status'] == 'applied'
    assert policy.answer_pending(native.app, 'yes') != 'approved'
    assert native.state['rev'] == 1 and native.calls.count('/api/changes/apply') == 1


def test_undo_after_unrelated_edit_preserves_that_edit(native):
    runner = cm.FollowupRunner(native.app, native.client)
    runner.start_fields('task:p6', {'priority': 'high'})
    receipt = approve(runner)
    native.state['tasks'][0]['t'] = 'Unrelated owner edit'
    native.state['rev'] += 1
    native.service.refresh_hash()
    inverse = cm.FollowupRunner(native.app, native.client)
    inverse.start_undo(receipt['receipt_id'])
    assert approve(inverse)['status'] == 'applied'
    assert native.state['tasks'][0]['t'] == 'Unrelated owner edit'
    assert 'priority' not in native.state['tasks'][-1].get('planning', {})


def test_plugin_installs_adapter_and_model_cannot_clarify(app, policy_window, monkeypatch, tmp_path):
    from plugins.commands import cockpit
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert isinstance(cockpit.briefing_client(app)._owner, NativeOwnerAdapter)
    assert isinstance(cockpit.runner_for(app, tmp_path).client._owner, NativeOwnerAdapter)
    inv = policy.Invocation('clarify cockpit item', {'remainder': 'one as complete task:p2'}, policy.Route.MODEL, 0)
    assert isinstance(policy.authorize(inv, app=app), policy.Denied)


def test_clarification_command_starts_separate_preview(native, monkeypatch):
    from plugins.commands import cockpit
    runner = native.make(['m4'])
    spoken = []
    monkeypatch.setattr(cockpit, '_briefing_work', lambda app, work: spoken.append(work()) or True)
    assert cockpit.clarify_item(native.app, 'one as note Renew workshop pass: Waiting for the form.')
    fresh = native.app._cockpit_memo_runner
    assert fresh is not runner and fresh.request_id != runner.request_id
    assert spoken[0].startswith('New clarification preview ')
    assert native.state['rev'] == 0
