"""E5/C3/C5/R1: real parsers/editor/executor, synthetic providers and effects."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama as ava
from samsara import ava_capability_protocol as wire, ava_pending, ava_plan, execution_policy as policy
from samsara.command_registry import DispatchState
from samsara.command_invocation_adapters import bind
from samsara.ava_edit import actions, model, selection, session, targets
from tests.test_ava_plan_and_ask_624 import rig
from tests.test_ava_capabilities_1036 import make_executor
from tests.test_ava_edit_targets_1036 import rig as edit_rig, SOURCE, REWRITE


@pytest.fixture
def runtime(rig, make_executor, monkeypatch):
    def effect(app, remainder):
        app.synthetic_effects.append(remainder)
        return True
    ex = make_executor([
        dict(phrase='reviewed remainder', func=effect, param_schema={
            'remainder': {'type': 'str', 'required': True, 'max_len': 100}}),
        dict(phrase='reviewed navigation', func=effect),
        dict(phrase='unsupported level', func=effect, param_schema={
            'level': {'type': 'int', 'min': 0, 'max': 100}}),
    ], count=40)
    rig.app.command_executor = rig.executor = ex
    ex._app = rig.app
    rig.app.synthetic_effects = []
    monkeypatch.setattr(selection, 'capture_result', lambda *_: targets.SelectionCapture('verified_empty'))
    session.clear()
    yield rig
    session.clear()


def context(r, utterance='reviewed remainder Keep Case'):
    return wire.new_request(r.app, utterance)


def offered(ctx, phrase):
    return next(cid for cid, desc in ctx.capabilities.offered.items()
                if desc.record()['canonical_phrase'] == phrase)


def deliver(ctx, data, *, app=None, utterance=None, generation=None):
    return ava.handle_response(app or ctx.app, wire.Response(json.dumps(data), ctx),
        original_text=ctx.utterance if utterance is None else utterance,
        generation=ctx.generation if generation is None else generation)


def test_typed_remainder_reaches_real_handler_exactly_without_fuzzy_dispatch(runtime):
    ctx = context(runtime)
    runtime.executor.process_text = Mock(side_effect=AssertionError('no fuzzy redispatch'))
    result = deliver(ctx, {'type': 'plan', 'plan': [
        {'id': offered(ctx, 'reviewed remainder'), 'args': {'remainder': 'Keep Case'}}]})
    assert result.state == 'completed'
    assert runtime.app.synthetic_effects == ['Keep Case']


@pytest.mark.parametrize('bad', [
    {'remainder': 'invented'}, {'remainder': 'Keep Case', 'confirmed': True},
    {'remainder': 12}, {'remainder': ''}, {'remainder': 'Keep Case\0'},
    {'level': 50}, {'remainder': {'shell': 'ignored'}},
])
def test_bad_or_ungrounded_arguments_never_reach_handler(runtime, bad):
    ctx = context(runtime)
    assert deliver(ctx, {'type': 'plan', 'plan': [
        {'id': offered(ctx, 'reviewed remainder'), 'args': bad}]}).state == 'refused'
    assert runtime.app.synthetic_effects == []


@pytest.mark.parametrize('change', ['generation', 'schema', 'handler', 'pack', 'new_turn', 'foreign_app'])
def test_stale_or_borrowed_response_has_zero_effects(runtime, change):
    ctx = context(runtime)
    data = {'type': 'plan', 'plan': [{'id': offered(ctx, 'reviewed remainder'),
                                     'args': {'remainder': 'Keep Case'}}]}
    app, utterance = runtime.app, ctx.utterance
    if change == 'generation':
        policy.bump_generation(app, 'synthetic cancel')
    elif change == 'schema':
        runtime.executor._plugin_registry['reviewed remainder']['param_schema']['remainder']['max_len'] = 5
    elif change == 'handler':
        runtime.executor._plugin_registry['reviewed remainder']['func'] = lambda *_: True
    elif change == 'pack':
        runtime.executor._matcher.set_enabled_packs({'other'})
    elif change == 'new_turn':
        utterance = 'different user request'
    else:
        app = SimpleNamespace(config=runtime.app.config, _ava_cmd_generation=0)
    assert deliver(ctx, data, app=app, utterance=utterance).state == 'refused'
    assert runtime.app.synthetic_effects == []


def test_response_and_bound_invocation_are_rechecked_at_execution(runtime):
    ctx = context(runtime)
    cid = offered(ctx, 'reviewed remainder')
    binding = bind(ctx.capabilities, cid, {'remainder': 'Keep Case'}, ctx.utterance)
    runtime.executor._plugin_registry['reviewed remainder']['param_schema']['remainder']['max_len'] = 4
    result = runtime.executor.execute_canonical('reviewed remainder', runtime.app, route=policy.Route.MODEL,
        generation=0, args={'remainder': 'Keep Case'}, source_text=ctx.utterance, binding=binding)
    assert result.state is DispatchState.REJECTED and runtime.app.synthetic_effects == []


def test_duplicate_delivery_is_not_a_second_effect(runtime):
    ctx = context(runtime, 'reviewed navigation')
    data = {'type': 'plan', 'plan': [offered(ctx, 'reviewed navigation')]}
    assert deliver(ctx, data).state == 'completed'
    assert deliver(ctx, data).state == 'refused'
    assert runtime.app.synthetic_effects == ['']


def test_forged_id_is_refused_before_an_earlier_valid_step(runtime):
    ctx = context(runtime, 'reviewed navigation')
    assert deliver(ctx, {'type': 'plan', 'plan': [offered(ctx, 'reviewed navigation'), 'cap_forged']}).state == 'refused'
    assert runtime.app.synthetic_effects == []


def test_unsupported_schema_cannot_call_a_handler_that_ignores_it(runtime):
    ctx = context(runtime, 'unsupported level')
    record = next(d.record() for d in ctx.capabilities.index.records
                  if d.record()['canonical_phrase'] == 'unsupported level')
    assert record['reason'] == 'adapter_unavailable'
    assert deliver(ctx, {'type': 'plan', 'plan': [{'id': record['id'], 'args': {'level': 50}}]}).state == 'refused'
    result = runtime.executor.execute_canonical('unsupported level', runtime.app, route=policy.Route.MODEL,
        generation=0, args={'level': 50})
    assert result.state is DispatchState.REJECTED and runtime.app.synthetic_effects == []


@pytest.mark.parametrize('response', [
    '{"type":"plan","type":"text","text":"forged"}',
    '{"type":"plan","plan":[{"id":"x","args":{"remainder":"a","remainder":"b"}}]}',
    'before {"type":"plan","plan":["reviewed navigation"]}',
    '```json\n{"type":"plan","plan":["reviewed navigation"]}\n```',
    '{"type":"text","text":"ok"} {"type":"plan","plan":[]}',
    '{"type":"plan","plan":[NaN]}',
    '[{"type":"plan","plan":[]}]',
    '{"type":"plan","plan":[],"confirmed":true}',
    '{"type":"text","text":"' + ('x' * 16384) + '"}',
], ids=['duplicate_root', 'duplicate_args', 'mixed_prose', 'markdown', 'multiple',
        'nonfinite', 'array', 'extra', 'too_large'])
def test_strict_control_response_is_refused_before_effect(runtime, response):
    result = ava.handle_response(runtime.app, response, original_text='reviewed navigation', generation=0)
    assert result.state == 'refused'
    assert runtime.app.synthetic_effects == []


@pytest.mark.parametrize('search', [False, True])
def test_custom_persona_and_search_setting_always_offer_protocol(runtime, monkeypatch, search):
    payloads = []
    class Reply:
        def raise_for_status(self): pass
        def json(self): return {'message': {'content': '{"type":"text","text":"Ready."}'}}
    def post(*_a, **kw):
        payloads.append(kw['json'])
        return Reply()
    monkeypatch.setattr(ava.ai_transport, 'post_local', post)
    reply = ava.ask_model('reviewed navigation', runtime.app, system='Custom persona.', allow_search=search)
    system = payloads[0]['messages'][0]['content']
    assert 'Custom persona.' in system and 'AVA_CAPABILITIES_1036' in system
    assert 'edit_text' in system and 'CAPABILITIES=' in system and payloads[0]['format'] == 'json'
    assert isinstance(reply.text, wire.Response)
    assert reply.text.context.snapshot.released


def test_serial_catalog_lookup_can_offer_a_command_beyond_initial_page(runtime):
    ctxs, outputs = [], []
    def provider(prompt, app, **_):
        ctx = wire._local.context
        ctxs.append(ctx)
        if len(ctxs) == 1:
            outputs.append(set(ctx.capabilities.offered))
            return ava.AvaReply(json.dumps({'type': 'catalog_query', 'query': 'reviewed remainder',
                'category': None, 'cursor': None}), 'fake', 0)
        assert 'HOST_CATALOG_RESULT=' in prompt and app.synthetic_effects == []
        cid = offered(ctx, 'reviewed remainder')
        assert cid not in outputs[0]
        return ava.AvaReply(json.dumps({'type': 'plan', 'plan': [
            {'id': cid, 'args': {'remainder': 'Keep Case'}}]}), 'fake', 0)
    reply = wire.model_request(provider)('fixture operation Keep Case', runtime.app)
    assert ctxs[0] is ctxs[1] and ctxs[0].capabilities.rounds == 1
    assert ava.handle_response(runtime.app, reply.text, original_text='fixture operation Keep Case', generation=0).state == 'completed'
    assert runtime.app.synthetic_effects == ['Keep Case']


def test_lookup_budget_asks_instead_of_asserting_absence(runtime):
    calls = []
    def provider(*_a, **_kw):
        calls.append(1)
        return ava.AvaReply(json.dumps({'type': 'catalog_query', 'query': 'fixture',
            'category': None, 'cursor': None}), 'fake', 0)
    reply = wire.model_request(provider)('fixture', runtime.app)
    assert len(calls) == 4 and reply.text.context.capabilities.rounds == 3
    data = wire.parse(reply.text)
    assert data['type'] == 'ask' and data['kind'] == 'clarify'
    assert runtime.app.synthetic_effects == []


def test_legacy_action_uses_actual_pinned_menu_not_later_bigger_menu(runtime, monkeypatch):
    monkeypatch.setattr(ava, '_conversational_refusal', lambda app, *_: wire.refusal(app, 'unoffered'))
    ctx = context(runtime, 'fixture operation')
    runtime.executor.ava_menu = lambda *_a, **_kw: ['reviewed navigation']
    result = ava.handle_response(runtime.app, wire.Response('CONFIRM Go.\nACTION reviewed navigation', ctx),
        original_text=ctx.utterance, generation=0)
    assert result.state != 'completed' and runtime.app.synthetic_effects == []


def test_edit_both_doors_share_selection_and_one_user_reply_owner(edit_rig, monkeypatch):
    r = edit_rig
    monkeypatch.setattr(ava, '_ai_state', lambda _app: SimpleNamespace(allowed=True))
    assert session.handle_utterance(r.app, 'fix this')
    first = session.pending()
    expected = (first.bound_target.selection.span, first.proposal.original, first.proposal.rewritten)
    assert ava_pending.consume(r.app, 'discard', capture_path='hold')
    utterance = 'Could you fix the punctuation in the selection?'
    ctx = wire.new_request(r.app, actions.ingress_payload(r.app, utterance))
    result = deliver(ctx, {'type': 'action', 'id': 'edit_text',
        'args': {'target': 'selection', 'instruction': 'Fix punctuation only.'}})
    assert result.state == 'queued'
    staged = session.pending()
    assert (staged.bound_target.selection.span, staged.proposal.original, staged.proposal.rewritten) == expected
    assert r.calls[-1][1] == 'Fix punctuation only.' and not r.mutations
    assert r.app._ava_question.kind == 'edit'
    assert ava_pending.consume(r.app, 'apply', source=policy.ANSWER_MODEL)
    assert ava_pending.consume(r.app, 'apply', capture_path='echo')
    assert ava_pending.consume(r.app, 'yes')
    assert not r.mutations and session.pending() is staged
    assert ava_pending.consume(r.app, 'apply', capture_path='hold')
    assert r.mutations == [REWRITE] and session.pending() is None
    assert not ava_pending.consume(r.app, 'apply')


@pytest.mark.parametrize('extra', [{'confirmed': True}, {'source': SOURCE}, {'proposal_id': 'forged'}])
def test_forged_edit_authority_fields_are_inert(edit_rig, monkeypatch, extra):
    monkeypatch.setattr(ava, '_ai_state', lambda _app: SimpleNamespace(allowed=True))
    ctx = wire.new_request(edit_rig.app, 'fix the selection')
    assert deliver(ctx, {'type': 'action', 'id': 'edit_text',
        'args': {'target': 'selection', 'instruction': 'clean up', **extra}}).state == 'refused'
    assert not edit_rig.calls and not edit_rig.mutations


def test_pending_edit_blocks_second_edit_and_question_but_allows_conversation(edit_rig, monkeypatch):
    monkeypatch.setattr(ava, '_ai_state', lambda _app: SimpleNamespace(allowed=True))
    assert session.handle_utterance(edit_rig.app, 'fix this')
    staged, q = session.pending(), edit_rig.app._ava_question
    assert ava_pending.consume(edit_rig.app, 'make that shorter')
    assert ava_pending.consume(edit_rig.app, 'make that shorter')
    assert edit_rig.app._ava_question is q and q.answer_guidance_given
    assert not ava_pending.consume(edit_rig.app, 'what does punctuation mean')
    assert ava_plan.handle_json(edit_rig.app, '{"type":"text","text":"Explanation."}', 'question', 0).state == 'spoken'
    assert ava_plan.handle_json(edit_rig.app, json.dumps({'type': 'ask', 'kind': 'clarify',
        'question': 'Which?', 'plan': []}), 'question', 0).state == 'refused'
    assert not actions.propose_and_stage(edit_rig.app, actions.from_utterance(edit_rig.app, 'fix this'))
    assert session.pending() is staged and not edit_rig.mutations


def test_edit_expiry_retires_capture_source_and_visible_pending(edit_rig, monkeypatch):
    assert session.handle_utterance(edit_rig.app, 'fix this')
    staged, q = session.pending(), edit_rig.app._ava_question
    monkeypatch.setattr(ava_pending, 'time', SimpleNamespace(monotonic=lambda: q.deadline + 1))
    ava_pending._tick(edit_rig.app, q)
    assert edit_rig.app._ava_question is None and session.pending() is None
    assert staged.request.snapshot.released and not edit_rig.mutations


@pytest.mark.parametrize('shape', [
    {'type': 'plan', 'plan': ['edit_text']},
    {'type': 'plan', 'plan': [{'id': 'edit_text', 'args': {'target': 'auto', 'instruction': 'clean up'}}]},
    {'type': 'schedule', 'seconds': 300, 'command': 'edit_text'},
    {'type': 'action', 'id': 'apply', 'args': {}},
])
def test_edits_and_apply_cannot_be_plan_or_schedule_effects(runtime, shape):
    ctx = context(runtime)
    assert deliver(ctx, shape).state == 'refused'
    assert runtime.app.synthetic_effects == [] and session.pending() is None


def test_command_miss_preserves_context_until_real_dispatch(runtime):
    ctx = context(runtime)
    response = wire.Response(json.dumps({'type': 'plan', 'plan': [
        {'id': offered(ctx, 'reviewed remainder'), 'args': {'remainder': 'Keep Case'}}]}), ctx)
    proposal = wire.miss_proposal(runtime.app, response, ctx.utterance, 'fake')
    assert proposal is not None and runtime.app.synthetic_effects == []
    result = ava.suggest_command_miss(runtime.app, proposal.command_id, generation=0,
                                    args=proposal.args, source_text=ctx.utterance)
    assert result.state is DispatchState.COMPLETED
    assert runtime.app.synthetic_effects == ['Keep Case']


@pytest.mark.parametrize('search', [False, True])
def test_natural_edit_router_excludes_passage_and_uses_same_staged_editor(runtime, edit_rig, monkeypatch, search):
    from samsara import ai_preferences
    app = runtime.app
    app._session_mode_manager = edit_rig.manager
    app.config['ava']['editing_enabled'] = True
    provider = ai_preferences.provider_for(app.config, 'local')
    consent = ai_preferences.accept_consent(app.config, features=('ava', 'editing'), provider=provider,
        cloud=False, accepted_at='2026-11-03T00:00:00Z')
    app.config = ai_preferences.apply_preferences(app.config, policy='local', ava=True,
        editing=True, consent=consent, local_model='synthetic-model')
    payloads = []
    class Reply:
        def raise_for_status(self): pass
        def json(self): return {'message': {'content': json.dumps({'type': 'action', 'id': 'edit_text',
            'args': {'target': 'selection', 'instruction': 'Fix punctuation only.'}})}}
    def post(*_a, **kw):
        payloads.append(kw['json'])
        return Reply()
    monkeypatch.setattr(ava.ai_transport, 'post_local', post)
    utterance = 'Could you fix punctuation in the selection?'
    reply = ava.ask_model(actions.ingress_payload(app, utterance), app, system='Custom persona.', allow_search=search)
    assert SOURCE not in json.dumps(payloads) and not edit_rig.calls
    turn = ava.handle_response(app, reply.text, original_text=utterance, generation=0)
    assert turn.state == 'queued' and session.pending().proposal.original == SOURCE
    assert edit_rig.calls == [(SOURCE, 'Fix punctuation only.')]
    assert not edit_rig.mutations


@pytest.mark.parametrize('reason', ['pack_disabled', 'out_of_scope', 'adapter_unavailable', 'registry_unavailable'])
def test_inability_outcomes_are_distinct_grounded_local_reasons(runtime, reason):
    result = wire.refusal(runtime.app, reason)
    assert result.reason == reason and runtime.app.synthetic_effects == []
    assert runtime.spoken[-1] != 'I could not use that action. Nothing was run.'


def test_same_words_from_an_older_request_have_no_new_turn_authority(runtime):
    old = context(runtime, 'reviewed navigation')
    data = {'type': 'plan', 'plan': [offered(old, 'reviewed navigation')]}
    context(runtime, 'reviewed navigation')
    assert deliver(old, data).state == 'refused'
    assert runtime.app.synthetic_effects == []


def test_retiring_capture_after_ui_apply_does_not_replace_success_with_expired(edit_rig, monkeypatch):
    chips = []
    monkeypatch.setattr(session, '_chip', lambda _app, kind, *_args: chips.append(kind))
    assert session.handle_utterance(edit_rig.app, 'fix this')
    q = edit_rig.app._ava_question
    assert session.apply(edit_rig.app)['applied']
    ava_pending._tick(edit_rig.app, q)
    assert edit_rig.app._ava_question is None
    assert chips[-1] == 'edit_applied'


def test_discard_during_a_rewrite_invalidates_the_late_worker(edit_rig, monkeypatch):
    def rewrite(*_a, **_kw):
        assert session.handle_utterance(edit_rig.app, 'discard')
        return REWRITE, None
    monkeypatch.setattr(model, 'rewrite', rewrite)
    assert session.handle_utterance(edit_rig.app, 'fix this')
    assert session.pending() is None and not edit_rig.mutations


def test_model_sourced_yes_cannot_approve_an_existing_plan(runtime):
    runner = ava_plan.Plan(runtime.app, (), (), 'synthetic', 0)
    runner.run = Mock()
    assert ava_pending.ask(runtime.app, runner, 'yes_no', 'Run?', ()).state == 'queued'
    assert ava_pending.consume(runtime.app, 'yes', source=policy.ANSWER_MODEL)
    runner.run.assert_not_called()


def test_unchanged_edit_releases_unused_source_snapshot(edit_rig, monkeypatch):
    monkeypatch.setattr(model, 'rewrite', lambda text, *_a, **_kw: (text, None))
    request = actions.from_utterance(edit_rig.app, 'fix this')
    assert not actions.propose_and_stage(edit_rig.app, request)
    assert request.snapshot.released and session.pending() is None


@pytest.mark.parametrize('name', ['chrome invented', 'chrome', '!!!'])
def test_json_app_uses_exact_grounded_app_adapter(runtime, name):
    ctx = context(runtime, 'open chromebook')
    assert deliver(ctx, {'type': 'app', 'verb': 'open', 'argument': name}).state == 'refused'
    assert runtime.app.synthetic_effects == []


def test_schedule_binds_offered_id_and_rejects_a_changed_contract_before_start(runtime, monkeypatch):
    ctx = context(runtime, 'reviewed navigation')
    start = Mock()
    monkeypatch.setattr(ava, '_start_schedule', start)
    # 1036d / 1031e: safe repeats start immediately, without reusable consent.
    assert deliver(ctx, {'type': 'schedule', 'seconds': 300,
        'command': offered(ctx, 'reviewed navigation')}).state == 'completed'
    start.assert_called_once()
    assert policy.pending_operation() is None
    ctx = context(runtime, 'reviewed navigation')
    runtime.executor._plugin_registry['reviewed navigation']['func'] = lambda *_: True
    start.reset_mock()
    assert deliver(ctx, {'type': 'schedule', 'seconds': 300,
        'command': offered(ctx, 'reviewed navigation')}).state == 'refused'
    start.assert_not_called()
    assert runtime.app.synthetic_effects == []


def test_scheduled_repeat_snapshots_payload_and_preserves_live_request_authority(runtime, monkeypatch):
    ctx = context(runtime, 'reviewed navigation')
    task = {'capabilities': ctx.capabilities, 'command': 'reviewed navigation', 'generation': 0}
    queued = []
    monkeypatch.setattr(runtime.app, '_schedule_ui', queued.append)
    stop = Mock()
    monkeypatch.setattr(ava, '_stop_schedule', stop)
    ava._execute_safe(runtime.app, task)
    task['command'] = 'unsupported level'
    assert len(queued) == 1 and runtime.app.synthetic_effects == []
    queued.pop()()
    assert runtime.app.synthetic_effects == ['']
    stop.assert_not_called()


def test_scheduled_repeats_recheck_the_pinned_contract_at_the_effect(runtime, monkeypatch):
    ctx = context(runtime, 'reviewed navigation')
    task = {'capabilities': ctx.capabilities, 'command': 'reviewed navigation', 'generation': 0}
    stop = Mock()
    monkeypatch.setattr(ava, '_stop_schedule', stop)
    runtime.executor._plugin_registry['reviewed navigation']['func'] = lambda *_: True
    ava._execute_safe(runtime.app, task)
    stop.assert_called_once()
    assert runtime.app.synthetic_effects == []


@pytest.mark.parametrize('shape', ['legacy', 'plan', 'schedule'])
def test_older_executor_keeps_its_pinned_no_argument_command_route(runtime, monkeypatch, shape):
    calls = []
    executor = SimpleNamespace(ava_menu=lambda *_a, **_kw: ['reviewed navigation'],
        execute_canonical=lambda cid, *_a, **_kw: calls.append(cid) or SimpleNamespace(
            state=DispatchState.COMPLETED, detail={}))
    runtime.app.command_executor = executor
    ctx = context(runtime, 'reviewed navigation')
    assert ctx.capabilities is None
    executor.ava_menu = lambda *_a, **_kw: ['reviewed navigation', 'invented command']
    if shape == 'legacy':
        result = ava.handle_response(runtime.app, wire.Response('CONFIRM Go.\nACTION reviewed navigation', ctx),
            original_text=ctx.utterance, generation=0)
    elif shape == 'plan':
        result = deliver(ctx, {'type': 'plan', 'plan': ['reviewed navigation']})
    else:
        monkeypatch.setattr(ava, '_start_schedule', lambda *_a: calls.append('scheduled'))
        result = deliver(ctx, {'type': 'schedule', 'seconds': 300, 'command': 'reviewed navigation'})
        # 1036d / 1031e: immediate safe-repeat handling also applies to old executors.
        assert result.state == 'completed'
        assert policy.pending_operation() is None
        assert calls == ['scheduled']
        return
    assert result.state == 'completed' and calls == ['reviewed navigation']


def test_older_executor_cannot_expand_its_menu_after_the_request(runtime, monkeypatch):
    monkeypatch.setattr(ava, '_conversational_refusal', lambda app, *_: wire.refusal(app, 'unoffered'))
    execute = Mock()
    runtime.app.command_executor = SimpleNamespace(
        ava_menu=lambda *_a, **_kw: ['reviewed navigation'], execute_canonical=execute)
    ctx = context(runtime)
    runtime.app.command_executor.ava_menu = lambda *_a, **_kw: ['reviewed navigation', 'invented command']
    result = ava.handle_response(runtime.app, wire.Response('CONFIRM Go.\nACTION invented command', ctx),
        original_text=ctx.utterance, generation=0)
    assert result.state == 'refused'
    execute.assert_not_called()


def test_broken_capability_api_does_not_fall_back_to_an_executable_name_menu(runtime, monkeypatch):
    monkeypatch.setattr(type(runtime.executor), 'capability_request', lambda *_a, **_kw: object())
    ctx = context(runtime, 'reviewed navigation')
    assert ctx.capabilities is None and ctx.legacy_ids is None and ctx.menu == ()
    assert deliver(ctx, {'type': 'plan', 'plan': ['reviewed navigation']}).state == 'refused'
    assert runtime.app.synthetic_effects == []


def test_source_snapshot_release_cancels_expiry_and_is_idempotent(edit_rig):
    request = actions.from_utterance(edit_rig.app, 'fix this')
    timer = Mock()
    request.snapshot.expiry_timer = timer
    request.snapshot.release()
    request.snapshot.release()
    timer.cancel.assert_called_once_with()
    assert request.snapshot.released and request.snapshot.expiry_timer is None
