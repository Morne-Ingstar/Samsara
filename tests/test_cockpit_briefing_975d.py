"""Synthetic readback and confirmed follow-ups; no live API or model."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara import cockpit_memos as cm, execution_policy as policy
from samsara.cockpit_client import CockpitClient, CockpitError
from tests.test_cockpit_memos_975c import app, backend, policy_window, runner, sources, context, FIXTURE


def briefing():
    ct=pytest.importorskip('cockpit_today')
    rows=[dict(id='sched:e',kind='event',title='Synthetic check-in',date='2026-11-02',time='10:00',timezone=FIXTURE['timezone'])]
    rows += [dict(id='task:'+str(i),kind='task',title='Synthetic work '+str(i),due_date='2026-11-02',created_at='2026-11-01') for i in range(5)]
    c=context();c.update(captured_at=FIXTURE['now'],records=rows, warnings=[],coverage=dict(state_complete=True,tickets_complete=True))
    return ct.rank_context_v1(c)


def test_spoken_top_three_with_commitment():
    b=briefing()
    assert [x['id'] for x in b['speech_items']]==['sched:e','task:0','task:1','task:2']
    assert b['recommendations']==['task:0','task:1','task:2']
    assert '10:00' in b['speech_text'] and 'Due today' in b['speech_text']
    assert len(b['actionable'])==6


def test_why_matches_rank_evidence(monkeypatch):
    from plugins.commands import cockpit
    b=briefing();a=SimpleNamespace(_cockpit_last_briefing=b)
    text=cockpit.why_briefing(a,'task:0')
    row=next(r for r in b['actionable'] if r['id']=='task:0')
    assert all(r['text'] in text for r in row['reasons'])
    assert b['captured_at'] in text
    with pytest.raises(cm.MemoDisposition):
        cockpit.find_briefing_item(b,'Synthetic')


def test_move_tomorrow_preserves_deadline_until_confirmed(app, runner, monkeypatch):
    from plugins.commands import cockpit
    app._cockpit_read_client=runner.client
    original_context=runner.client.context
    def fresh_context():
        value=original_context()
        next(r for r in value['records'] if r['id']=='task:p6')['due_date']=runner.test_state['tasks'][-1]['due']
        return value
    monkeypatch.setattr(runner.client,'context',fresh_context)
    old_due=runner.test_state['tasks'][-1]['due']
    before=deepcopy(runner.test_state)
    text=cockpit.followup(app,'move','task:p6')
    follow=app._cockpit_memo_runner
    tomorrow=(datetime.fromisoformat(FIXTURE['now']).date()+timedelta(days=1)).isoformat()
    assert follow.draft['items'][0]['fields']=={'start_after':tomorrow}
    assert before==runner.test_state and 'preserved deadline' in text
    monkeypatch.setattr(policy,'listen_for_confirmation',lambda *args:None)
    op=follow.stage_approval(listen=False)
    assert runner.test_state==before
    assert policy.answer_pending(app,'yes') == 'approved'
    assert runner.test_state['tasks'][-1]['due']==old_due
    assert runner.test_state['tasks'][-1]['planning']['start_after']==tomorrow
    assert runner.test_calls.count('apply')==1


def test_no_provider_needed_for_templates(monkeypatch):
    from plugins.commands import cockpit
    from samsara import ava_structured_request
    monkeypatch.setattr(ava_structured_request,'_state',lambda *a:pytest.fail('Provider inspected'))
    b=briefing();a=SimpleNamespace(_cockpit_read_client=SimpleNamespace(briefing=lambda:deepcopy(b)))
    assert cockpit.read_briefing(a)==b['speech_text']


def test_optional_explainer_cannot_reorder():
    # V1 deliberately offers templates only: model-authored source fields have
    # no authority over the computed order, IDs, reasons or spoken template.
    ct=pytest.importorskip('cockpit_today')
    c=context();c.update(captured_at=FIXTURE['now'],warnings=[],records=[dict(id='task:a',kind='task',title='Synthetic a'),dict(id='task:b',kind='task',title='Synthetic b')])
    expected=ct.rank_context_v1(c)
    c.update(model_order=['task:b','task:a'], explanation={'id':'task:b','reason_codes':['deadline']})
    assert ct.rank_context_v1(c)==expected


def test_source_failure_is_not_nothing_due(monkeypatch):
    ct=pytest.importorskip('cockpit_today')
    c=context();c.update(captured_at=FIXTURE['now'],records=[],warnings=['state_unavailable'],available=False)
    b=ct.rank_context_v1(c)
    assert b['coverage']['partial'] and 'Coverage is incomplete' in b['speech_text']
    assert 'nothing due' not in b['speech_text'].lower()
    from plugins.commands import cockpit
    def fail():raise CockpitError('Cockpit unavailable')
    a=SimpleNamespace(_cockpit_read_client=SimpleNamespace(briefing=fail))
    with pytest.raises(CockpitError):cockpit.read_briefing(a)


def test_existing_todoist_command_unchanged():
    from plugins.commands import cockpit, tasks
    importlib.reload(tasks);importlib.reload(cockpit)
    from samsara.plugin_commands import find_command
    assert find_command("what's on today")[0]['func'] is tasks.handle_today_tasks
    assert find_command('cockpit today')[0]['func'] is cockpit.today_briefing
    assert find_command('read today')[0]['func'] is tasks.handle_today_tasks


def test_explicit_deadline_and_completion_are_only_previews(app, runner):
    from plugins.commands import cockpit
    app._cockpit_read_client=runner.client
    before=deepcopy(runner.test_state)
    cockpit.followup(app,'deadline','task:p6','2026-11-05')
    assert app._cockpit_memo_runner.draft['items'][0]['fields']=={'due_date':'2026-11-05'}
    cockpit.followup(app,'complete','task:p6')
    assert app._cockpit_memo_runner.draft['items'][0]['fields']=={'status':'done'}
    assert runner.test_state==before


def test_followup_without_owner_adapter_cannot_apply(app, runner):
    follow=cm.FollowupRunner(app,runner.client)
    follow.start_fields('task:p6',{'status':'done'});follow.read_page()
    runner.client._owner=None
    with pytest.raises(cm.MemoDisposition,match='not available'):
        follow.stage_approval(listen=False)
    assert 'apply' not in runner.test_calls


def test_followup_stale_state_and_generation_refuse(app, runner):
    follow=cm.FollowupRunner(app,runner.client)
    follow.start_fields('task:p6',{'status':'done'});follow.read_page()
    app._ava_cmd_generation+=1
    with pytest.raises(cm.MemoDisposition,match='stale'):
        follow.stage_approval(listen=False)
    assert 'apply' not in runner.test_calls


def test_bounded_memo_signals_require_explicit_target_and_origin():
    records=[dict(id='task:x',title='Synthetic x')]
    text='Urgent task:x.'
    source=cm.normalize_memo_v1(dict(memo_id='m',text=text,captured_at=FIXTURE['now'],has_transcript=True,partial=False))
    now=datetime.fromisoformat(FIXTURE['now'])
    signal=cm.bounded_memo_signals([source],records,now=now)
    assert signal[0]['target_id']=='task:x' and signal[0]['owner_urgent'] is True
    for text in ('Not urgent task:x.', '"Urgent task:x."', 'Synthetic x is urgent'):
        source=cm.normalize_memo_v1(dict(source,text=text))
        assert not any(r['owner_urgent'] for r in cm.bounded_memo_signals([source],records,now=now))
    with pytest.raises(cm.MemoDisposition):cm.bounded_memo_signals([source]*21,records,now=now)


def test_briefing_client_fixed_read_route_and_validation():
    calls=[]
    b=briefing()
    def request(method,url,**kwargs):
        calls.append((method,url,kwargs))
        return SimpleNamespace(status_code=200,content=json.dumps(b).encode(),close=lambda:None)
    client=CockpitClient(request=request)
    assert client.briefing()==b
    assert calls[0][0:2]==('GET','http://127.0.0.1:8790/api/briefing')
    assert calls[0][2]['allow_redirects'] is False
    b['full_order'].reverse()
    with pytest.raises(CockpitError,match='invalid'):client.briefing()



def test_confirmed_completion_and_undo_share_service(app,runner,backend,monkeypatch):
    service,state,cc=backend
    monkeypatch.setattr(runner.client,'undo_preview',lambda receipt,request:service.undo_preview(receipt,{'request_id':request}))
    first=cm.FollowupRunner(app,runner.client)
    before=deepcopy(state['tasks'][-1])
    first.start_fields('task:p6',{'status':'done'});first.read_page();first.stage_approval(listen=False)
    assert policy.answer_pending(app,'yes')=='approved'
    assert state['tasks'][-1]['s']=='done'
    receipt=first.last_result['receipt_id']
    undo=cm.FollowupRunner(app,runner.client)
    undo.start_undo(receipt);undo.read_page()
    assert state['tasks'][-1]['s']=='done'
    undo.stage_approval(listen=False)
    assert policy.answer_pending(app,'yes')=='approved'
    assert state['tasks'][-1]==before


def test_mirror_signals_share_origin_and_reject_edited_snapshot():
    now=datetime.fromisoformat(FIXTURE['now'])
    quick=cm.quick_source(dict(id='one',text='Urgent task:x.',at=FIXTURE['now']))
    mirror=dict(quick,memo_id='mirror',source_kind='legacy',upstream_id='quick:one')
    signals=cm.bounded_memo_signals([quick,mirror],[{'id':'task:x'}],now=now)
    assert {s['origin_id'] for s in signals}=={'quick:one'}
    quick['text']='Changed task:x'
    with pytest.raises(cm.MemoDisposition,match='changed'):
        cm.bounded_memo_signals([quick],[{'id':'task:x'}],now=now)



def test_event_move_previews_actual_date_and_time(app,runner,backend,monkeypatch):
    from plugins.commands import cockpit
    service,state,cc=backend
    state['sched']=[dict(id='e',t='Synthetic visit',date='2026-11-02',time='14:00',timezone=FIXTURE['timezone'])]
    state.pop('_stateHash');state['_stateHash']=cc._hash(state)
    def fresh():
        c=cc.normalize_context_v1(state,now=datetime.fromisoformat(FIXTURE['now']))
        c['sources']=dict(state=dict(revision=state['rev'],hash=state['_stateHash']))
        return c
    monkeypatch.setattr(runner.client,'context',fresh)
    app._cockpit_read_client=runner.client
    before=deepcopy(state)
    text=cockpit.followup(app,'move','sched:e',dict(date='2026-11-04',time='15:30'))
    assert '2026-11-02 14:00 to 2026-11-04 15:30' in text
    assert app._cockpit_memo_runner.draft['items'][0]['fields']==dict(date='2026-11-04',time='15:30')
    assert state==before
