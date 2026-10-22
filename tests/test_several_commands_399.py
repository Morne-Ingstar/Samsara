"""Offline chain dispatch: real live registry/policy, inert OS handlers."""
import json
import os
import statistics
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import commands, execution_policy as policy, grammar
from samsara.command_catalog import build_catalog
from samsara.command_registry import DispatchResult, DispatchState as State
from samsara.command_scope import MatchContext
from tools.dump_command_metadata import build_executor

ROOT = Path(__file__).resolve().parents[1]
LIVE_LOAD_PLUGINS = commands._plugin_commands.load_plugins


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(commands._plugin_commands, 'load_plugins', LIVE_LOAD_PLUGINS)
    monkeypatch.setattr(commands._command_scope, 'capture_context',
                        lambda: MatchContext.for_app('chrome.exe'))
    monkeypatch.setattr('plugins.commands.app_verbs.resolve_window', lambda name: None)
    monkeypatch.setattr(policy, 'pending_operation', lambda: None)
    executor = build_executor()
    app = SimpleNamespace(config={'commands': {'grammar_tier': True}},
                          command_mode_active=False, command_matching_enabled=True,
                          _ava_cmd_generation=0,
                          _show_dispatch_outcome_chip=Mock(), _show_outcome_chip=Mock(),
                          _speak_session_notice=Mock())
    executor._app = app
    app.command_executor = executor
    calls = []
    seen = set()
    for entry in executor._matcher._sorted:
        if id(entry) in seen or entry.handler is None:
            continue
        seen.add(id(entry))
        def fake(host, remainder, phrase=entry.phrase):
            calls.append((phrase, remainder))
            return True
        fake.__module__ = entry.handler.__module__
        fake.__wrapped__ = entry.handler
        fake.__doc__ = entry.handler.__doc__
        monkeypatch.setattr(entry, 'handler', fake)
    class Builtin:
        def execute(self, data, context):
            phrase = next(p for p, d in executor.commands.items() if d is data)
            calls.append((phrase, ''))
            return True
    monkeypatch.setattr(commands, 'get_handler', lambda kind: Builtin())
    monkeypatch.setattr(executor, '_build_context', lambda app: app)
    monkeypatch.setattr(executor, '_stage_confirmation', Mock())
    # Never query the real foreground, even on the regression before this method exists.
    monkeypatch.setattr(executor, '_grammar_foreground', lambda: 10, raising=False)
    monkeypatch.setattr(executor, '_grammar_wait_target', lambda *args: True, raising=False)
    return executor, app, calls


def test_two_owner_chains_in_order_and_route(rig, monkeypatch):
    executor, app, calls = rig
    execute = Mock(wraps=executor.execute_canonical)
    monkeypatch.setattr(executor, 'execute_canonical', execute)
    result = executor.process_text('open notepad and volume down', force_commands=True)
    assert result.state is State.COMPLETED
    assert calls == [('open', 'notepad'), ('volume down', '')]
    assert all(c.kwargs['route'] is policy.Route.GRAMMAR for c in execute.call_args_list)
    assert len({c.kwargs['generation'] for c in execute.call_args_list}) == 1
    calls.clear()
    result = executor.process_text('new tab then scroll down', force_commands=True)
    assert result.state is State.COMPLETED
    assert [c[0] for c in calls] == ['new tab', 'scroll down']


def test_numeric_owner_chain(rig):
    executor, app, calls = rig
    result = executor.process_text('open notepad and volume thirty', force_commands=True)
    assert result.state is State.COMPLETED
    assert calls == [('open', 'notepad'), ('set volume', 'thirty')]


@pytest.mark.parametrize('forced,active,matching', [(False, False, True), (False, False, False),
                                                   (True, False, False), (False, True, True)])
def test_explicit_context_only(rig, monkeypatch, forced, active, matching):
    executor, app, calls = rig
    app.command_mode_active = active
    app.command_matching_enabled = matching
    spy = Mock(wraps=grammar.parse)
    monkeypatch.setattr(grammar, 'parse', spy)
    executor.process_text('new tab then scroll down', force_commands=forced)
    if forced or active:
        assert spy.called
        assert [c[0] for c in calls] == ['new tab', 'scroll down']
    else:
        spy.assert_not_called()
        # 407: a no-argument prefix cannot discard the rest of an unforced sentence.
        assert calls == []


def test_off_switch_has_legacy_result_and_no_parse(rig, monkeypatch):
    executor, app, calls = rig
    app.config['commands']['grammar_tier'] = False
    spy = Mock(side_effect=AssertionError('grammar disabled'))
    monkeypatch.setattr(grammar, 'parse', spy)
    result = executor.process_text('new tab then scroll down', force_commands=True)
    # 407: grammar off still requires the whole sentence for no-argument commands.
    assert result.state is State.MISS
    assert calls == []
    spy.assert_not_called()


def test_understanding_is_all_or_nothing_on_a_miss(rig):
    executor, app, calls = rig
    result = executor.process_text('please new tab then purple monkey', force_commands=True)
    assert result.state is State.MISS
    assert not calls


def test_resolution_none_falls_through_to_legacy_prefix(rig):
    executor, app, calls = rig
    result = executor.process_text('new tab then purple monkey', force_commands=True)
    # 407: the no-argument prefix no longer consumes unrecognized trailing words.
    assert result.state is State.MISS
    assert calls == []


@pytest.mark.parametrize('decision', [policy.NeedsConfirmation('Ask first'),
                                    policy.Denied('pack_disabled')])
def test_quiet_preflight_checks_all_steps_before_any_effect(rig, monkeypatch, decision):
    executor, app, calls = rig
    authorizer = Mock(side_effect=[policy.Allowed(), decision])
    monkeypatch.setattr(policy, 'authorize', authorizer)
    stage = Mock(side_effect=AssertionError('must not stage a chain'))
    monkeypatch.setattr(executor, '_stage_confirmation', stage)
    result = executor.process_text('new tab then scroll down', force_commands=True)
    assert result.state is State.REJECTED
    assert calls == []
    assert len(authorizer.call_args_list) == 2
    assert all(c.kwargs.get('quiet') is True for c in authorizer.call_args_list)
    stage.assert_not_called()


def test_real_policy_confirmation_prevents_first_step(rig):
    executor, app, calls = rig
    result = executor.process_text('volume down then scratch everything', force_commands=True)
    assert result.state is State.REJECTED
    assert calls == []
    assert result.detail['reason'] == 'confirmation_required'


def test_more_than_four_runs_nothing(rig):
    executor, app, calls = rig
    result = executor.process_text(' then '.join(['volume down'] * 5), force_commands=True)
    assert result.state is State.REJECTED
    assert calls == []


@pytest.mark.parametrize('failure', [State.FAILED, State.MISS, State.QUEUED, State.REJECTED])
def test_stop_at_first_unsuccessful_step(rig, monkeypatch, failure):
    executor, app, calls = rig
    execute = Mock(side_effect=[DispatchResult(State.COMPLETED, 'volume down'),
                               DispatchResult(failure, 'scroll down')])
    monkeypatch.setattr(executor, 'execute_canonical', execute)
    result = executor.process_text('volume down then scroll down then volume up', force_commands=True)
    assert execute.call_count == 2
    assert result.state is not State.COMPLETED
    assert len(result.detail['chain_done']) == 1
    assert result.detail['chain_stopped'] == 'scroll down'


def test_target_wait_precedes_next_step_and_timeout_stops(rig, monkeypatch):
    executor, app, calls = rig
    events = []
    execute = executor.execute_canonical
    def run(*args, **kwargs):
        events.append(args[0])
        return execute(*args, **kwargs)
    monkeypatch.setattr(executor, 'execute_canonical', run)
    def wait(*args):
        events.append('wait')
        return False
    monkeypatch.setattr(executor, '_grammar_wait_target', wait)
    result = executor.process_text('open notepad then volume down', force_commands=True)
    assert events == ['open', 'wait']
    assert result.state is State.FAILED
    assert result.detail['reason'] == 'target_timeout'
    assert result.detail['chain_done'] == ['opened Notepad']


def test_new_generation_cancels_unrun_steps(rig, monkeypatch):
    executor, app, calls = rig
    execute = executor.execute_canonical
    def run(*args, **kwargs):
        result = execute(*args, **kwargs)
        policy.bump_generation(app, 'new utterance or stop')
        return result
    monkeypatch.setattr(executor, 'execute_canonical', run)
    result = executor.process_text('volume down then volume up', force_commands=True)
    assert calls == [('volume down', '')]
    assert result.state is State.CANCELLED


def test_new_process_text_cancels_remaining_chain(rig, monkeypatch):
    executor, app, calls = rig
    execute = executor.execute_canonical
    def run(*args, **kwargs):
        result = execute(*args, **kwargs)
        # Real newer utterance, without manually incrementing the app counter.
        executor.process_text('purple monkey', force_commands=True)
        return result
    monkeypatch.setattr(executor, 'execute_canonical', run)
    result = executor.process_text('volume down then volume up', force_commands=True)
    assert calls == [('volume down', '')]
    assert result.state is State.CANCELLED


def test_single_grammar_only_phrase(rig):
    executor, app, calls = rig
    assert executor._matcher.match('please scroll down')[0] is None
    result = executor.process_text('please scroll down', force_commands=True)
    assert result.state is State.COMPLETED
    assert calls == [('scroll down', '')]


def test_ambiguous_first_reading_keeps_prefix(rig, monkeypatch, caplog):
    executor, app, calls = rig
    monkeypatch.setattr(grammar, 'parse', lambda *args: [
        ('volume.volume_down', {}, (0, 2), .99),
        ('scroll.scroll_down', {}, (3, 5), .99)])
    with caplog.at_level('INFO'):
        result = executor.process_text('new tab then scroll down', force_commands=True)
    # 407: a conflicting chain cannot fall back to a no-argument partial prefix.
    assert result.state is State.MISS
    assert calls == []
    assert 'new tab' in caplog.text and 'volume down' in caplog.text


TEXT_SLOT_FIXTURE = json.loads(
    (ROOT/'tests/fixtures/several_commands_399_text_slots.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('canonical_id', sorted(TEXT_SLOT_FIXTURE))
def test_every_text_slot_keeps_head_behavior(rig, canonical_id):
    executor, app, calls = rig
    expected = TEXT_SLOT_FIXTURE[canonical_id]
    result = executor.process_text(expected['text'], force_commands=True)
    assert result.state.value == expected['state']
    assert result.detail == expected['detail']
    assert [list(call) for call in calls] == expected['calls']


def test_text_slot_fixture_covers_catalog():
    records = json.loads((ROOT/'commands_catalog.json').read_text(encoding='utf-8'))['commands']
    ids = {r['canonical_id'] for r in records
           if not r['canonical_id'].startswith('demo_commands.')
           and any(a.get('type', 'text') == 'text' for a in r.get('args', []))}
    assert ids
    assert set(TEXT_SLOT_FIXTURE) == ids


def test_free_text_and_is_content(rig):
    executor, app, calls = rig
    executor.process_text('search for cats and dogs', force_commands=True)
    assert len(calls) <= 1
    assert not any(phrase == 'volume down' for phrase, rest in calls)


@pytest.mark.parametrize('spoken,number', [('thirty',30), ('to fifty',50), ('50',50),
                                        ('0',0), ('100',100), ('one hundred',100)])
def test_numeric_volume(spoken, number, monkeypatch):
    from plugins.commands import volume
    audio = Mock()
    audio.set_volume.return_value = True
    monkeypatch.setattr(volume, '_audio', audio)
    app = SimpleNamespace(_show_outcome_chip=Mock())
    assert volume.handle_set_volume(app, spoken) is True
    audio.set_volume.assert_called_once_with(number / 100)
    assert app._show_outcome_chip.call_args.args[0] == f'Windows volume {number}%'


@pytest.mark.parametrize('spoken', ['-1','101','one hundred one','50 cats','', '3.5','-0'])
def test_volume_bounds_decline_without_scalar_call(spoken, monkeypatch):
    from plugins.commands import volume
    audio = Mock()
    monkeypatch.setattr(volume, '_audio', audio)
    assert volume.handle_set_volume(SimpleNamespace(), spoken) is False
    audio.set_volume.assert_not_called()


def test_volume_grammar_inclusive_upper_bound():
    record = {'canonical_id': 'volume.set_volume', 'aliases': ['volume'],
              'args': [{'name': 'level', 'type': 'int', 'required': True}]}
    assert grammar.parse('volume 100', [record])[0][1] == {'level': 100}
    assert grammar.parse('volume one hundred', [record])[0][1] == {'level': 100}


@pytest.mark.parametrize('realistic', [False, True])
def test_miss_latency_with_stub_and_forty_windows(rig, monkeypatch, realistic):
    executor, app, calls = rig
    def resolver(name):
        if realistic:
            for _ in range(40): time.sleep(.0005)
    monkeypatch.setattr('plugins.commands.app_verbs.resolve_window', resolver)
    records = [r.to_dict() for r in build_catalog(executor)]
    assert grammar.parse('please open imaginary app', records)
    # Warm all imports/indexes; compare app-name parsing and an ordinary miss.
    executor.process_text('please open imaginary app then purple monkey', force_commands=True)
    samples=[]
    resampled = 0
    for _ in range(1000):
        start=time.perf_counter()
        executor.process_text('please open imaginary app then purple monkey', force_commands=True)
        elapsed = (time.perf_counter()-start)*1000
        if elapsed >= 30:
            # Like 720, retry only scheduler outliers in this warmed process.
            # A repeatable slow path must still meet the same 30 ms budget.
            retries = []
            for _ in range(2):
                start = time.perf_counter()
                executor.process_text('please open imaginary app then purple monkey', force_commands=True)
                retries.append((time.perf_counter()-start)*1000)
            elapsed = min(elapsed, *retries)
            resampled += 1
        samples.append(elapsed)
    print(f'399 realistic={realistic} median={statistics.median(samples):.4f}ms worst={max(samples):.4f}ms resampled={resampled}')
    budget_ms = 60 if os.environ.get('CI') else 30
    assert max(samples) < budget_ms


def test_nonchain_and_precheck_latency(rig):
    executor, app, calls = rig
    context = MatchContext.for_app('chrome.exe')
    executor._grammar_chain('search for cats and dogs', 'search for cats and dogs', context, app, 0)
    samples=[]
    for _ in range(1000):
        start=time.perf_counter()
        executor._grammar_chain('search for cats and dogs', 'search for cats and dogs', context, app, 0)
        samples.append((time.perf_counter()-start)*1000)
    print(f'399 nonchain-and median={statistics.median(samples):.4f}ms worst={max(samples):.4f}ms')
    assert max(samples) < 30


def test_foreground_wait_real_loop_has_deadline_and_cancellation(rig, monkeypatch):
    executor, app, calls = rig
    clock = [0.0]
    monkeypatch.setattr(commands.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(commands.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    wait = commands.CommandExecutor._grammar_wait_target.__get__(executor)
    monkeypatch.setattr(executor, '_grammar_foreground', lambda: 10)
    assert wait(10, app, 0) is False
    assert 1.5 <= clock[0] < 1.55
    monkeypatch.setattr(executor, '_grammar_foreground', lambda: 11)
    assert wait(10, app, 0) is True
    app._ava_cmd_generation = 1
    assert wait(10, app, 0) is False


def test_four_steps_allowed_and_reload_invalidates_catalog(rig):
    executor, app, calls = rig
    result = executor.process_text(' then '.join(['volume down'] * 4), force_commands=True)
    assert result.state is State.COMPLETED and len(calls) == 4
    before, _ = executor._grammar_catalog()
    executor.rebuild_matcher()
    after, _ = executor._grammar_catalog()
    assert before is not after


def test_one_aggregate_chip_and_feedback_category(rig, monkeypatch):
    from plugins.commands import volume
    from samsara.session_modes import outcome_chip
    executor, app, calls = rig
    entry = executor._registry_entry('set volume')
    monkeypatch.setattr(entry, 'handler', volume.handle_set_volume)
    monkeypatch.setattr(volume._audio, 'set_volume', lambda number: True)
    shown = []
    app._show_dispatch_outcome_chip = lambda outcome: shown.append(outcome_chip(outcome.kind, outcome.detail))
    result = executor.process_text('volume thirty then volume fifty', force_commands=True)
    assert result.state is State.COMPLETED
    assert shown == [('Done: Windows volume 30% · Windows volume 50%', 'success')]
    app._show_outcome_chip.assert_not_called()
    assert outcome_chip('command_executed', result.detail) is None
    assert app._speak_session_notice.call_args.kwargs == {'category': 'command'}


def test_policy_pack_and_scope_refusal_runs_nothing(rig):
    executor, app, calls = rig
    executor._matcher.set_enabled_packs({'core', 'window-management'})
    assert executor.process_text('volume down then volume up', force_commands=True).state is State.REJECTED
    assert calls == []
    executor._matcher.set_enabled_packs({'core', 'browsers', 'media'})
    executor._matcher.set_context_provider(lambda: MatchContext.for_app('notepad.exe'))
    assert executor.process_text('volume down then new tab', force_commands=True).state is State.REJECTED
    assert calls == []


def test_single_miss_outside_command_context_never_parses(rig, monkeypatch):
    executor, app, calls = rig
    spy = Mock(side_effect=AssertionError('ambient dictation must not parse'))
    monkeypatch.setattr(grammar, 'parse', spy)
    assert executor.process_text('please scroll down').state is State.MISS
    spy.assert_not_called()


def _ready_steps(executor, phrases):
    return [(executor._registry_entry(phrase), {}, phrase, None, None) for phrase in phrases]


def test_ready_chain_model_route_reaches_policy_and_execution(rig, monkeypatch):
    executor, app, calls = rig
    authorizer = Mock(return_value=policy.Allowed())
    execute = Mock(wraps=executor.execute_canonical)
    monkeypatch.setattr(policy, 'authorize', authorizer)
    monkeypatch.setattr(executor, 'execute_canonical', execute)
    result = executor.run_chain(_ready_steps(executor, ['volume down', 'volume up']),
                                text='ready steps', app=app, generation=0, route=policy.Route.MODEL)
    assert result.state is State.COMPLETED
    assert calls == [('volume down', ''), ('volume up', '')]
    preflight = [c for c in authorizer.call_args_list if c.kwargs.get('quiet') is True]
    assert len(preflight) == 2
    assert all(c.args[0].route is policy.Route.MODEL for c in authorizer.call_args_list)
    assert execute.call_count == 2
    assert all(c.kwargs['route'] is policy.Route.MODEL for c in execute.call_args_list)
    app._show_dispatch_outcome_chip.assert_called_once()


def test_ready_model_chain_confirmation_refuses_before_any_effect(rig, monkeypatch):
    executor, app, calls = rig
    authorizer = Mock(side_effect=[policy.Allowed(), policy.NeedsConfirmation('Ask first')])
    monkeypatch.setattr(policy, 'authorize', authorizer)
    execute = Mock(wraps=executor.execute_canonical)
    monkeypatch.setattr(executor, 'execute_canonical', execute)
    result = executor.run_chain(_ready_steps(executor, ['volume down', 'volume up']),
                                text='ready steps', app=app, generation=0, route=policy.Route.MODEL)
    assert result.state is State.REJECTED
    assert calls == []
    execute.assert_not_called()
    assert authorizer.call_count == 2
    assert all(c.kwargs['quiet'] is True and c.args[0].route is policy.Route.MODEL
               for c in authorizer.call_args_list)
    app._show_dispatch_outcome_chip.assert_called_once()
    app._show_outcome_chip.assert_not_called()


def test_ready_chain_five_steps_refused_before_preflight(rig, monkeypatch):
    executor, app, calls = rig
    authorizer = Mock(side_effect=AssertionError('too many steps must never reach policy'))
    monkeypatch.setattr(policy, 'authorize', authorizer)
    result = executor.run_chain(_ready_steps(executor, ['volume down'] * 5),
                                text='five steps', app=app, generation=0, route=policy.Route.MODEL)
    assert result.state is State.REJECTED
    assert result.detail['reason'] == 'too_many_steps'
    assert result.detail['chain_count'] == 5
    assert calls == []
    authorizer.assert_not_called()
    app._show_dispatch_outcome_chip.assert_called_once()


@pytest.mark.parametrize('empty', [None, {}])
def test_ready_chain_empty_metadata_uses_handler_chip_or_phrase(rig, monkeypatch, empty):
    executor, app, calls = rig
    monkeypatch.setattr(policy, 'authorize', Mock(return_value=policy.Allowed()))
    entry = executor._registry_entry('volume down')
    def with_chip(host, remainder):
        calls.append(('volume down', remainder))
        host._show_outcome_chip('Windows volume 30%')
        return True
    monkeypatch.setattr(entry, 'handler', with_chip)
    steps = [(executor._registry_entry(phrase), {}, phrase, empty, empty)
             for phrase in ['volume down', 'volume up']]
    result = executor.run_chain(steps, text='ready steps', app=app, generation=0)
    assert result.state is State.COMPLETED
    assert calls == [('volume down', ''), ('volume up', '')]
    assert result.detail['chain_done'] == ['Windows volume 30%', 'volume up']
    app._show_dispatch_outcome_chip.assert_called_once()
    app._show_outcome_chip.assert_not_called()


def test_ready_chain_without_serial_still_checks_generation(rig, monkeypatch):
    executor, app, calls = rig
    monkeypatch.setattr(policy, 'authorize', Mock(return_value=policy.Allowed()))
    execute = executor.execute_canonical
    def newer_request(*args, **kwargs):
        result = execute(*args, **kwargs)
        executor._grammar_request_serial = getattr(executor, '_grammar_request_serial', 0) + 1
        return result
    monkeypatch.setattr(executor, 'execute_canonical', newer_request)
    steps = _ready_steps(executor, ['volume down', 'volume up'])
    assert executor.run_chain(steps, text='ready steps', app=app, generation=0).state is State.COMPLETED
    assert len(calls) == 2
    calls.clear()
    app._ava_cmd_generation = 1
    result = executor.run_chain(steps, text='stale steps', app=app, generation=0)
    assert result.state is State.CANCELLED
    assert calls == []
