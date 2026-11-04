"""C4: real policy/adapters/plans with synthetic destinations, clocks and effects."""
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama as ava
from samsara import ava_capability_protocol as wire, ava_pending, commands
from samsara import effect_policy as effects, execution_policy as policy
from samsara import click_effect_policy as clicks
from samsara.ava_edit import session
from samsara.command_invocation_adapters import bind
from samsara.command_registry import CommandMatcher, DispatchState
from tests.test_ava_capabilities_1036 import make_executor
from tests.test_ava_plan_and_ask_624 import rig
from tests.test_ava_edit_targets_1036 import rig as edit_rig, REWRITE


@pytest.fixture
def runtime(rig, make_executor, monkeypatch):
    def ai_request(app, remainder):
        app.effects.append(('ai', remainder))
        return True

    ex = make_executor([dict(phrase='synthetic ai request', source='plugins.commands.ask_agent',
        func=ai_request, risk_class='write', param_schema={
            'remainder': {'type': 'str', 'required': True, 'max_len': 80}})], count=0)
    ex.commands = {
        'navigate': {'type': 'hotkey', 'keys': ['alt', 'tab']},
        'submit': {'type': 'press', 'key': 'enter'},
        'renamed submit': {'type': 'hotkey', 'keys': ['ctrl', 'enter']},
        'local write': {'type': 'text', 'text': 'Synthetic text'},
        'repeat that': {'type': 'method', 'method': 'repeat_last_command'},
        'two submits': {'type': 'macro', 'steps': [
            {'action': 'press', 'key': 'enter'}, {'action': 'press', 'key': 'enter'}]},
        'raw click': {'type': 'mouse', 'button': 'left'},
    }
    app = rig.app
    app.effects = []
    app.config['execution_policy'] = {'model_tool_extra': [
        'renamed submit', 'local write', 'two submits', 'raw click', 'synthetic ai request']}
    app.command_executor = ex
    ex._app = app
    ex._build_context = lambda _app: None
    matcher = CommandMatcher()
    matcher.load_builtins(ex.commands)
    matcher.load_plugins(ex._plugin_registry)
    matcher.set_enabled_packs({'core'})
    matcher.freeze()
    ex._matcher = matcher
    target = [effects.SubmitTarget('unknown', 42, 'synthetic.exe', 'composer', 'body-1',
        'account-1', 'conversation-1', 'recipient-1')]
    monkeypatch.setattr(effects, 'resolve_submit_target', lambda app=None: target[0])
    monkeypatch.setattr(policy, 'foreground_window_identity', lambda: {
        'hwnd': target[0].hwnd, 'pid': 1, 'process': target[0].process})
    monkeypatch.setattr(policy, 'listen_for_confirmation', lambda *_a: None)
    monkeypatch.setattr(commands, 'get_handler', lambda _kind: SimpleNamespace(
        execute=lambda cmd, _context: app.effects.append(cmd) or True))
    session.clear()
    yield SimpleNamespace(app=app, executor=ex, target=target, timers=rig.timers, spoken=rig.spoken)
    session.clear()


def request(r, utterance='submit'):
    return wire.new_request(r.app, utterance)


def offered(ctx, phrase):
    return next(cid for cid, descriptor in ctx.capabilities.offered.items()
                if descriptor.record()['canonical_phrase'] == phrase)


def plan(ctx, phrases):
    return ava.handle_response(ctx.app, wire.Response(json.dumps({
        'type': 'plan', 'plan': [offered(ctx, phrase) for phrase in phrases]}), ctx),
        original_text=ctx.utterance, generation=ctx.generation)


def test_discovery_declares_binding_without_probing_or_enabling_unresolved_wrappers(runtime, monkeypatch):
    monkeypatch.setattr(effects, 'resolve_submit_target', Mock(side_effect=AssertionError('discovery probe')))
    index = runtime.executor.capability_index()
    assert not index.error
    records = {d.record()['canonical_phrase']: d.record() for d in index.records}
    assert records['submit']['effect_binding'] == 'input_target/v1'
    assert records['navigate']['effect_binding'] == 'canonical/v1'
    for phrase in ('two submits', 'repeat that', 'raw click'):
        assert not records[phrase]['available']
        assert records[phrase]['reason'] == 'effect_binding_unavailable'
    assert all(r['effect_hook'] == 'samsara.click_effect_policy.handling_for' for r in records.values())
    assert runtime.app.effects == []


@pytest.mark.parametrize('kind', ['ai', 'unknown'])
def test_t0_navigation_plan_is_immediate_at_any_destination(runtime, kind):
    runtime.target[0] = replace(runtime.target[0], kind=kind)
    ctx = request(runtime, 'navigate')
    timers = len(runtime.timers)
    assert plan(ctx, ['navigate']).state == 'completed'
    assert len(runtime.app.effects) == 1
    assert policy.pending_operation() is None and len(runtime.timers) == timers


def test_reviewed_explicit_ai_request_has_no_generic_write_question(runtime):
    ctx = request(runtime, 'synthetic ai request Explain the fixture')
    timers = len(runtime.timers)
    binding = bind(ctx.capabilities, offered(ctx, 'synthetic ai request'),
                   {'remainder': 'Explain the fixture'}, ctx.utterance)
    inv = binding.invocation()
    assert runtime.executor.execute_canonical(inv.command_id, runtime.app, args=inv.args,
        route=inv.route, generation=inv.generation, source_text=inv.source_text,
        binding=binding).state is DispatchState.COMPLETED
    assert runtime.app.effects == [('ai', 'Explain the fixture')]
    assert policy.pending_operation() is None and len(runtime.timers) == timers


def test_generic_model_write_keeps_its_source_permission(runtime):
    assert plan(request(runtime, 'local write'), ['local write']).state == 'queued'
    assert policy.pending_operation() is not None and runtime.app.effects == []


@pytest.mark.parametrize('gate', ['generation', 'allowlist', 'schema', 'scope'])
def test_t0_immediacy_cannot_override_source_gates(runtime, monkeypatch, gate):
    app, ex = runtime.app, runtime.executor
    generation, args = 0, {}
    if gate == 'generation':
        generation = -1
    elif gate == 'allowlist':
        app.config['execution_policy']['model_tool_allowlist'] = []
    elif gate == 'schema':
        args = {'confirmed': True}
    else:
        monkeypatch.setattr(ex, 'command_availability', lambda *_: ('out_of_scope', 'fixture'))
    assert ex.execute_canonical('navigate', app, route=policy.Route.MODEL,
        generation=generation, args=args).state is DispatchState.REJECTED
    assert app.effects == []


@pytest.mark.parametrize('phrase', ['submit', 'renamed submit'])
@pytest.mark.parametrize('kind', ['ai', 'unknown', 'known_person'])
def test_model_keys_inherit_real_effect_and_private_send_is_promoted(runtime, phrase, kind):
    runtime.target[0] = replace(runtime.target[0], kind=kind)
    ctx = request(runtime, phrase)
    timers = len(runtime.timers)
    result = plan(ctx, [phrase])
    assert result.state == ('completed' if kind == 'ai' else 'queued')
    if kind == 'ai':
        assert len(runtime.app.effects) == 1 and len(runtime.timers) == timers
    else:
        op = policy.pending_operation()
        assert op.handling == effects.CONFIRM and not isinstance(op, policy.TimedOperation)
        assert runtime.app.effects == []
        assert ava_pending.consume(runtime.app, 'yes', source=policy.ANSWER_MODEL)
        assert runtime.app.effects == []
        assert ava_pending.consume(runtime.app, 'yes', capture_path='hold')
        assert len(runtime.app.effects) == 1 and policy.pending_operation() is None


@pytest.mark.parametrize('field', ['revision', 'control', 'account', 'conversation', 'recipient'])
def test_plan_approval_is_bound_to_exact_composer_not_just_same_window(runtime, field):
    assert plan(request(runtime), ['submit']).state == 'queued'
    runtime.target[0] = replace(runtime.target[0], **{field: 'changed'})
    assert ava_pending.consume(runtime.app, 'yes')
    assert runtime.app.effects == [] and policy.pending_operation() is None


def test_two_t3_plan_steps_each_require_one_local_approval_and_no_timer(runtime):
    ctx = request(runtime, 'submit and submit')
    assert plan(ctx, ['submit', 'submit']).state == 'queued'
    first = policy.pending_operation()
    assert first._ttl_s == 30 and first.handling == effects.CONFIRM
    assert ava_pending.consume(runtime.app, 'yes')
    second = policy.pending_operation()
    assert second is not None and second.op_id != first.op_id
    assert len(runtime.app.effects) == 1
    assert ava_pending.consume(runtime.app, 'yes')
    assert len(runtime.app.effects) == 2 and policy.pending_operation() is None
    assert not ava_pending.consume(runtime.app, 'yes')
    # Existing reply-window timers may poll; no send-grace TimedOperation exists.
    assert not any(isinstance(op, policy.TimedOperation) for op in (first, second))


@pytest.mark.parametrize('change', ['none', 'body', 'expired'])
def test_t3_email_plan_reuses_native_preview_and_attempts_once(runtime, monkeypatch, change):
    from samsara.message_adapters import Message
    message = Message('outlook', 'fixture@example.invalid', 'draft-1',
        ('recipient@example.invalid',), 'Synthetic recipient', 'Synthetic email body',
        subject='Synthetic subject', revision='draft-1')
    runtime.target[0] = replace(runtime.target[0], kind='email', message=message)
    assert plan(request(runtime), ['submit']).state == 'queued'
    op = policy.pending_operation()
    assert message.preview() in op.prompt and 'Send?' in op.prompt
    assert op.handling == effects.CONFIRM and op._ttl_s == 30
    assert callable(op.message_readback) and runtime.app.effects == []
    if change == 'body':
        runtime.target[0] = replace(runtime.target[0], message=replace(message, body='Changed body'))
    elif change == 'expired':
        monkeypatch.setattr(op, '_clock', lambda: op.deadline + .01)
    assert ava_pending.consume(runtime.app, 'yes')
    assert len(runtime.app.effects) == int(change == 'none')
    if change == 'none':
        # Native key transport has an unknown provider outcome; it cannot retry.
        receipt = next(iter(runtime.app._message_attempts.values()))
        assert receipt.state == 'unknown'
        assert plan(request(runtime), ['submit']).state == 'queued'
        assert ava_pending.consume(runtime.app, 'yes')
        assert len(runtime.app.effects) == 1


@pytest.mark.parametrize('change', ['unchanged', 'schema', 'allowlist', 'generation', 'contract'])
def test_input_confirmation_callback_preserves_capability_binding(runtime, change):
    ctx = request(runtime)
    binding = bind(ctx.capabilities, offered(ctx, 'submit'), {}, ctx.utterance)
    result = runtime.executor.execute_canonical('submit', runtime.app, route=policy.Route.MODEL,
        generation=0, source_text=ctx.utterance, binding=binding)
    assert result.state is DispatchState.QUEUED
    if change == 'schema':
        runtime.executor.commands['submit'] = {'type': 'text', 'text': 'changed effect'}
    elif change == 'allowlist':
        runtime.app.config['execution_policy']['model_tool_allowlist'] = []
    elif change == 'generation':
        runtime.app._ava_cmd_generation += 1
    elif change == 'contract':
        runtime.executor._capability_generation += 1
    policy.answer_pending(runtime.app, 'yes')
    assert len(runtime.app.effects) == int(change == 'unchanged')
    policy.answer_pending(runtime.app, 'yes')
    assert len(runtime.app.effects) == int(change == 'unchanged')


@pytest.mark.parametrize('action', ['elapsed', 'onset cancel', 'send now', 'changed'])
def test_t2_native_simulator_uses_one_second_and_holds_on_speech_onset(runtime, action):
    runtime.target[0] = replace(runtime.target[0], kind='known_person')
    result = runtime.executor.execute_canonical('submit', runtime.app)
    op = policy.pending_operation()
    assert result.state is DispatchState.QUEUED and result.detail['send_grace_s'] == 1
    assert op.handling == effects.PRIVATE_SEND
    timer = runtime.timers[-1]
    assert timer.delay == 1
    if action == 'onset cancel':
        policy.note_speech_onset(runtime.app)
        timer.fire()
        assert op.held and runtime.app.effects == []
        assert policy.answer_pending(runtime.app, 'cancel') == 'rejected'
    elif action == 'send now':
        assert policy.answer_pending(runtime.app, 'send now') == 'approved'
    elif action == 'changed':
        runtime.target[0] = replace(runtime.target[0], revision='changed')
    timer.fire()
    timer.fire()
    assert len(runtime.app.effects) == int(action in {'elapsed', 'send now'})


@pytest.mark.parametrize('handling', [effects.IMMEDIATE, effects.CONFIRM])
@pytest.mark.parametrize('command_id', ['navigate', 'submit'])
def test_native_click_dispatch_preserves_bound_effect_and_source_gate(runtime, handling, command_id):
    target = clicks.ClickTarget(handling, runtime.target[0], 'control', 'page-1', 'Synthetic control')
    # Use the native shared dispatch, not a second click adapter or desktop sender.
    inv = policy.Invocation(command_id, route=policy.Route.MODEL, generation=0)
    calls = []
    result = clicks.dispatch(runtime.app, target, lambda: target,
        lambda: calls.append('clicked') or True, invocation=inv)
    if handling == effects.IMMEDIATE and command_id == 'navigate':
        assert result is True and calls == ['clicked'] and policy.pending_operation() is None
    else:
        assert result.state is DispatchState.QUEUED and calls == []
        assert policy.answer_pending(runtime.app, 'yes') == 'approved'
        assert calls == ['clicked']
        policy.answer_pending(runtime.app, 'yes')
        assert calls == ['clicked']
    assert not runtime.timers


def test_repeat_cannot_reuse_a_send_approval(runtime):
    app, ex = runtime.app, runtime.executor
    app._last_command_name, app._last_command = 'submit', dict(ex.commands['submit'])
    result = ex.execute_canonical('repeat that', app, route=policy.Route.MODEL, generation=0,
                                  confirmed=True)
    assert result.state is DispatchState.REJECTED and app.effects == []


def test_schedule_route_never_inherits_direct_private_send_privilege(runtime):
    runtime.target[0] = replace(runtime.target[0], kind='known_person')
    for _ in range(2):
        result = runtime.executor.execute_canonical('submit', runtime.app,
            route=policy.Route.SCHEDULE, generation=0)
        assert result.state is DispatchState.QUEUED and runtime.app.effects == []
        assert policy.pending_operation().handling == effects.CONFIRM
        assert policy.answer_pending(runtime.app, 'no') == 'rejected'
    assert not runtime.timers


def test_edit_proposal_is_t0_and_apply_is_the_only_t1_approval(edit_rig, monkeypatch):
    monkeypatch.setattr(ava, '_ai_state', lambda _app: SimpleNamespace(allowed=True))
    ctx = wire.new_request(edit_rig.app, 'Fix punctuation in the selection')
    response = wire.Response(json.dumps({'type': 'action', 'id': 'edit_text',
        'args': {'target': 'selection', 'instruction': 'Fix punctuation only.'}}), ctx)
    turn = ava.handle_response(edit_rig.app, response, original_text=ctx.utterance,
                               generation=ctx.generation)
    assert turn.state == 'queued' and len(edit_rig.calls) == 1
    assert not edit_rig.mutations and policy.pending_operation() is None
    assert edit_rig.app._ava_question.kind == 'edit'
    assert ava_pending.consume(edit_rig.app, 'yes')
    assert ava_pending.consume(edit_rig.app, 'apply', source=policy.ANSWER_MODEL)
    assert not edit_rig.mutations and session.pending() is not None
    assert ava_pending.consume(edit_rig.app, 'apply', capture_path='hold')
    assert edit_rig.mutations == [REWRITE] and session.pending() is None
    assert not ava_pending.consume(edit_rig.app, 'apply')


@pytest.mark.parametrize('wrapper', ['two submits', 'repeat that', 'raw click'])
def test_unresolved_wrapper_cannot_be_forged_into_a_plan(runtime, wrapper):
    ctx = request(runtime, wrapper)
    descriptor = next(d for d in ctx.capabilities.index.records
                      if d.record()['canonical_phrase'] == wrapper)
    response = wire.Response(json.dumps({'type': 'plan', 'plan': [descriptor.id]}), ctx)
    assert ava.handle_response(runtime.app, response, original_text=ctx.utterance,
                               generation=0).state == 'refused'
    assert runtime.app.effects == [] and policy.pending_operation() is None
