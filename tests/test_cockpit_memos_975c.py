"""Invented memo fixtures. Integration uses the reviewed 975a/b package only.

Set PYTHONPATH to a synthetic copy of the 975b backend to run service integration
nodes. No live server, configuration, memo, profile or provider is accessed.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from samsara import ai_preferences as ai, ava_readiness, execution_policy as policy
from samsara import cockpit_memos as cm
from samsara.ava_structured_request import StructuredRequest, PROMPT_SHA256
from samsara.cockpit_client import CockpitClient

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/975/synthetic_eval.json').read_text(encoding='utf-8'))


def sources(ids=None):
    rows = []
    for m in FIXTURE['memos']:
        row = cm.normalize_memo_v1(dict(m, memo_id=m['id'], source_kind='quick', source_locator='synthetic',
                                       partial=False, upstream_id=None))
        row.pop('id', None)
        row.pop('timezone', None)
        row.pop('media_only', None)
        if ids is None or row['memo_id'] in ids:
            rows.append(row)
    return rows


def context():
    records = []
    for r in FIXTURE['matching_context']:
        kind = 'event' if r.get('collection') == 'events' else 'task'
        records.append(dict(id=kind + ':' + r['id'], source_id=r['id'], kind=kind, title=r['title'],
                            writable=True, status='open', blocked_by=[]))
    return dict(schema_version='cockpit.context.v1', records=records, local_date='2026-11-02',
                timezone=FIXTURE['timezone'], available=True, snapshot_consistent=True,
                coverage=dict(state_complete=True), sources=dict(state=dict(revision=0, hash='synthetic')))


@pytest.fixture
def app(monkeypatch):
    config = dict(ava=dict(provider_policy='local'), ava_command_session=dict(enabled=True),
                  ollama=dict(enabled=True, host='http://localhost:11434', model='synthetic-model'),
                  cloud_llm=dict(enabled=False, provider='deepseek'), command_packs=dict(utilities=True))
    provider = ai.provider_for(config, 'local', resolved_addresses=('127.0.0.1',))
    config['ava']['consent'] = ai.accept_consent(config, provider=provider, accepted_at='2026-11-01T00:00:00Z')
    config['cockpit_memos'] = dict(enabled=True, qualification=dict(provider='ollama', origin=provider.origin,
        model='synthetic-model', schema_sha256=cm.SCHEMA_SHA256, prompt_sha256=PROMPT_SHA256,
        repetitions=3, passed=True))
    result = SimpleNamespace(config=config, _ava_cmd_generation=0)
    monkeypatch.setattr(ava_readiness, 'readiness_for', lambda app: ava_readiness.Readiness(
        ava_readiness.READY, provider='ollama' if app.config['ava']['provider_policy'] == 'local'
        else app.config['cloud_llm']['provider']))
    from samsara import ai_transport
    monkeypatch.setattr(ai_transport, 'post_local', lambda *a, **k: pytest.fail('unexpected real local transport'))
    monkeypatch.setattr(ai_transport, 'post_cloud', lambda *a, **k: pytest.fail('unexpected real cloud transport'))
    return result


@pytest.fixture
def policy_window(app, monkeypatch):
    from plugins.commands import ask_ollama, cockpit  # registers bound identity
    import importlib
    importlib.reload(cockpit)  # shared fixture resets plugin registry per test
    from samsara.runtime import thread_registry
    monkeypatch.setattr(ask_ollama, '_pending_action', None)
    speech = []
    def speak(_app, text, **kwargs):
        speech.append(text)
        done = kwargs.get('on_done')
        if done:
            done()
        return True
    monkeypatch.setattr(ask_ollama, 'speak', speak)
    monkeypatch.setattr(thread_registry, 'timer', lambda *a, **k: SimpleNamespace(cancel=lambda: None))
    app.app_state, app.wake_word_triggered = 'asleep', False
    app._ensure_wake_consumer = lambda owner: None
    app._release_wake_consumer = lambda owner: None
    yield speech
    op = policy.pending_operation()
    if op:
        policy._release_slot(op)
        op.cancel(app, chip=False)
    from samsara import ava_pending
    if getattr(app, '_ava_question', None):
        ava_pending._clear(app, app._ava_question)


@pytest.fixture
def backend(tmp_path):
    cc = pytest.importorskip('cockpit_context', reason='975a/b integration package required on PYTHONPATH')
    changes = pytest.importorskip('cockpit_changes')
    state = dict(rev=0, tasks=[dict(id=r['id'], t=r['title'], s='') for r in FIXTURE['matching_context']
                               if r.get('collection') != 'events'], sched=[], daily=[], weekly=[], goals=[], notes={})
    state['tasks'][-1]['due'] = '2026-11-02'
    state['_stateHash'] = cc._hash(state)
    def persist(saved):
        saved = deepcopy(saved)
        saved['rev'] = state['rev'] + 1
        saved.pop('_stateHash', None)
        saved['_stateHash'] = cc._hash(saved)
        state.clear()
        state.update(saved)
    service = changes.ChangeService(lambda: deepcopy(state), persist, threading.RLock(), tmp_path / 'proposals',
        lambda path, value: (path.parent.mkdir(exist_ok=True), path.write_text(json.dumps(value), encoding='utf-8')))
    return service, state, cc


@pytest.fixture
def runner(app, policy_window, backend):
    service, state, cc = backend
    calls = []
    class Owner:
        def confirm(self, binding, operation):
            calls.append('confirm')
            return service.confirm({k: binding[k] for k in ('proposal_id', 'proposal_hash', 'request_id')}, owner=True)
        def cancel(self):
            service.cancel({}, owner=True)
    class Client(CockpitClient):
        def context(self):
            value = cc.normalize_context_v1(state, now=datetime.fromisoformat(FIXTURE['now']))
            value['records'] = context()['records']
            value['sources'] = dict(state=dict(revision=state['rev'], hash=state['_stateHash']))
            return value
        def preview(self, body):
            calls.append('preview')
            return service.preview(body)
        def apply(self, binding, authorization):
            calls.append('apply')
            return service.apply(dict(proposal_id=binding['proposal_id'], proposal_hash=binding['proposal_hash'],
                                      request_id=binding['request_id'], authorization=authorization))
        def recover(self, request_id):
            calls.append('recover')
            return service.get(request_id)
    data = sources(['m1', 'm2', 'm3'])
    case = FIXTURE['extraction_cases'][0]
    extractor = StructuredRequest(app, transport=lambda *args: json.dumps(case['expected']))
    result = cm.ProposalRunner(app, Client(owner_adapter=Owner()), extractor, lambda: deepcopy(data))
    result.test_data, result.test_calls, result.test_state = data, calls, state
    return result


@pytest.mark.parametrize('case', FIXTURE['extraction_cases'], ids=lambda c: c['id'])
def test_seven_exact_extraction_cases(app, case):
    memos, ctx = sources(case['memo_ids']), context()
    sent = []
    def transport(*args):
        sent.append(args)
        return json.dumps(case['expected'])
    if any(m['has_transcript'] for m in memos):
        raw = StructuredRequest(app, transport=transport).extract(memos, ctx, 0)
        actual = cm.normalize_extraction_v1(raw, ctx, memos)
        assert actual == cm.normalize_extraction_v1(case['expected'], ctx, memos)
        assert len(sent) == 1
    else:
        raw = cm._missing_transcripts(memos)
        expected = deepcopy(case['expected']['unresolved'])
        # Batch-local keys are opaque; all semantic content/spans are exact.
        expected[0]['key'] = raw[0]['key']
        assert raw == expected
        assert sent == []


def test_schema_version_hash_parity():
    path = Path(__file__).parent / 'fixtures/975/memo-extraction-v1.schema.json'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == cm.SCHEMA_SHA256
    assert json.loads(path.read_bytes()) == cm.EXTRACTION_SCHEMA


def test_selection_is_immutable():
    data = sources(['m1', 'm2'])
    selection = cm.select_memos(data, count=1)
    before = selection.memos
    data[1]['text'] = 'Changed synthetic text'
    escaped = selection.memos
    escaped[0]['text'] = 'Also changed'
    data.append(sources(['m3'])[0])
    assert selection.memos == before
    assert not selection.unchanged(data)


def test_capture_timezone_controls_relative_date(app):
    case = FIXTURE['extraction_cases'][4]
    result = StructuredRequest(app, transport=lambda *args: json.dumps(case['expected'])).extract(sources(['m7']), context(), 0)
    result = cm.normalize_extraction_v1(result, context(), sources(['m7']))
    assert result['items'][0]['fields']['due_date'] == '2026-11-02'
    bad = deepcopy(result)
    bad['items'][0]['fields']['due_date'] = '2026-11-03'
    with pytest.raises(ValueError, match='relative date'):
        cm.validate_extraction_v1(bad, context(), sources(['m7']))


@pytest.mark.parametrize('day,clock', [('2026-03-08', '02:30'), ('2026-11-01', '01:30')])
def test_spring_gap_and_autumn_fold_unresolved(day, clock):
    draft = deepcopy(FIXTURE['extraction_cases'][0]['expected'])
    draft['items'] = [draft['items'][-1]]
    draft['no_change'] = []
    item = draft['items'][0]
    item['fields'].update(date=day, time=clock)
    item['time_resolution'] = None
    safe = cm._dst_unresolved(draft, context())
    assert safe['items'] == []
    assert safe['unresolved'][0]['reason'] == 'ambiguous_time'
    cm.validate_extraction_v1(safe, context(), sources(['m2']))


def test_legacy_adapter_does_not_scan(tmp_path, monkeypatch):
    note = tmp_path / 'Voice Memos.md'
    note.write_text('## 2026-11-01 12:00\n![[Attachments/memo_2026-11-01_120000.wav]]\n\nSynthetic thought\n', encoding='utf-8')
    monkeypatch.setattr(Path, 'glob', lambda *a: pytest.fail('vault scan'))
    monkeypatch.setattr(Path, 'rglob', lambda *a: pytest.fail('vault scan'))
    monkeypatch.setattr(Path, 'iterdir', lambda *a: pytest.fail('vault scan'))
    first = cm.legacy_sources(note)
    assert first[0]['text'] == 'Synthetic thought'
    assert first[0]['capture_timezone'] is None
    note.write_text(note.read_text().replace('thought', 'revision'), encoding='utf-8')
    second = cm.legacy_sources(note)
    assert first[0]['source_locator'] == second[0]['source_locator']
    assert first[0]['memo_id'] != second[0]['memo_id']


def test_source_edit_before_confirmation_invalidates(runner):
    runner.start(cm.select_memos(runner.test_data, count=3))
    runner.read_page(0, 5)
    op = runner.stage_approval()
    runner.test_data[0]['text'] = 'Edited synthetic source'
    assert policy.answer_pending(runner.app, 'yes').startswith('refused:')
    assert op.approved is False
    assert 'confirm' not in runner.test_calls and 'apply' not in runner.test_calls


def test_no_transcript_no_inference(app):
    data = sources(['m8'])
    assert not data[0]['has_transcript']
    with pytest.raises(ValueError, match='transcript'):
        StructuredRequest(app, transport=lambda *a: pytest.fail('media label sent')).extract(data, context(), 0)


def test_repeated_memo_receipt_not_duplicate(runner):
    runner.start(cm.select_memos(runner.test_data, count=3))
    runner.read_page(0, 5)
    runner.stage_approval()
    assert policy.answer_pending(runner.app, 'yes') == 'approved'
    assert runner.last_result['status'] == 'applied'
    assert any(t['t'] == 'Today Board' for t in runner.test_state['tasks'])
    count, rev = len(runner.test_state['tasks']), runner.test_state['rev']
    recovered = runner.recover(runner.request_id)
    assert recovered['receipt']['status'] == 'applied'
    proposal = runner.start(cm.select_memos(runner.test_data, count=3))
    assert proposal['counts']['selected'] == 0
    assert all(row['disposition'] == 'already_recorded' for row in proposal['items'])
    assert len(runner.test_state['tasks']) == count and runner.test_state['rev'] == rev


def test_semantic_target_requires_resolution(runner):
    case = deepcopy(FIXTURE['extraction_cases'][0]['expected'])
    case['items'] = [case['items'][2]]
    case['no_change'] = []
    case['items'][0]['match'].update(method='semantic', needs_resolution=True)
    runner.extractor = StructuredRequest(runner.app, transport=lambda *a: json.dumps(case))
    result = runner.start(cm.select_memos(runner.test_data, count=3))
    assert result['counts']['selected'] == 0
    assert result['items'][0]['disposition'] == 'needs_target_choice'
    result = runner.repreview(target_choices={'i3': 'task:p1'})
    assert result['counts']['selected'] == 1
    with pytest.raises(Exception, match='advertised'):
        runner.repreview(target_choices={'i3': 'task:p2'})


@pytest.mark.parametrize('mode,kwargs', [('last', dict(count=21)), ('date', dict(on_date='2026-11-09'))])
def test_empty_and_oversized_selection_visible(mode, kwargs):
    with pytest.raises(cm.MemoDisposition):
        cm.select_memos(sources(), mode=mode, **kwargs)


def test_read_only_quick_adapter_does_not_rebuild(tmp_path):
    from samsara import quick_memo
    path = quick_memo.memo_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('## 2026-11-01 12:00\nSynthetic fallback\n', encoding='utf-8')
    first, _warnings = cm.load_sources({}, tmp_path)
    second, _warnings = cm.load_sources({}, tmp_path)
    assert first == second and len(first) == 1
    assert not quick_memo.index_file(tmp_path).exists()


def test_unknown_capture_timezone_preserves_resolved_remainder():
    draft = deepcopy(FIXTURE['extraction_cases'][0]['expected'])
    memos = sources(['m1', 'm2', 'm3'])
    for memo in memos:
        memo['capture_timezone'] = None
        memo['captured_at'] = memo['captured_at'][:19]
    canonical = cm.normalize_extraction_v1(draft, context(), memos, validate=False)
    result = cm._dst_unresolved(canonical, context(), memos)
    result = cm.validate_extraction_v1(result, context(), memos)
    assert [i['item_key'] for i in result['items']] == ['i1', 'i3']
    assert len(result['unresolved']) == 2
    assert result['no_change'][0]['related_item_key'] is None


def test_last_and_today_are_distinct_and_timezone_fixed():
    data = sources(['m1', 'm7'])
    today = cm.select_memos(data, mode='today', now=datetime.fromisoformat('2026-11-02T00:10:00-08:00'),
                            display_timezone='America/Los_Angeles')
    last = cm.select_memos(data, count=2)
    assert [m['memo_id'] for m in today.memos] == ['m1']
    assert len(last.memos) == 2
    assert today.timezone == 'America/Los_Angeles'


def test_preview_readback_contains_every_changed_date_and_target(runner):
    runner.start(cm.select_memos(runner.test_data, count=3))
    text = runner.read_page(0, 5)
    assert 'Today Board' in text and 'Collect parcel' in text
    assert '2026-11-03' in text and '14:00' in text and 'America/Los_Angeles' in text
    assert 'provenance' not in text and 'proposal_id' not in text


def test_unread_large_batch_cannot_be_approved(runner):
    runner.start(cm.select_memos(runner.test_data, count=3))
    runner.read_page(0, 1)
    with pytest.raises(cm.MemoDisposition, match='Read all'):
        runner.stage_approval()
    assert policy.pending_operation() is None
