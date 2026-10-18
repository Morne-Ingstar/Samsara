"""400: exact production AST, fake model/OS seams, and the shared chain runner."""
import ast
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import ava_command_session as ava, ai_preferences, ava_readiness
from samsara import commands, config_schema, execution_policy as policy, handlers
from samsara.command_registry import DispatchResult, DispatchState
from samsara.session_modes import DispatchOutcome, outcome_chip

ROOT = Path(__file__).resolve().parents[1]


def production(name):
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = dict(time=time, logger=logging.getLogger('400.ast'))
    nodes = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), fn]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 '<ava-plain-requests-production-ast>', 'exec'), ns)
    return ns[name]


def proposal(*names):
    steps = tuple(ava.Proposal(name) for name in names)
    return SimpleNamespace(command_id=names[0], args={}, origin='model', steps=steps if len(steps) > 1 else ())


@pytest.fixture
def rig(monkeypatch):
    ava.reset_cancellations()
    executor = commands.CommandExecutor.__new__(commands.CommandExecutor)
    entries = {name: SimpleNamespace(phrase=name, data={}, handler=None)
               for name in ('pause', 'volume up', 'scroll down', 'close window', 'open')}
    executor._matcher = SimpleNamespace(_entries=entries, should_suppress=lambda _e: False)
    executor._registry_entry = lambda command_id: entries.get(command_id)
    executor._grammar_request_serial = 1
    calls, chips, speech, preflight = [], [], [], []
    app = SimpleNamespace(config={}, _ava_interpret_request=0, _ava_cmd_generation=0,
                          command_executor=executor, command_mode_active=False,
                          _command_mode_miss_count=2)
    app._show_dispatch_outcome_chip = lambda o: chips.append(outcome_chip(o.kind, o.detail))
    app._show_outcome_chip = lambda label, *_a, **_kw: chips.append(label)
    app._speak_session_notice = lambda text, **kw: speech.append((text, kw))
    monkeypatch.setattr(ai_preferences, 'ava_master_enabled', lambda _cfg: True)
    monkeypatch.setattr(policy, 'pending_operation', lambda: None)
    foreground = [10]
    monkeypatch.setattr(handlers, '_get_foreground_hwnd', lambda: foreground[0])
    monkeypatch.setattr(executor, '_grammar_foreground', lambda: 10)

    def authorize(invocation, **kw):
        preflight.append((invocation, kw))
        return policy.Allowed()
    monkeypatch.setattr(policy, 'authorize', authorize)

    def execute(name, host, **kw):
        calls.append((name, kw))
        if hasattr(host, '_show_outcome_chip'):
            host._show_outcome_chip('paused Spotify' if name == 'pause' else name, 'success')
        return DispatchResult(DispatchState.COMPLETED, name, name,
                              {'message': 'paused Spotify' if name == 'pause' else name})
    monkeypatch.setattr(executor, 'execute_canonical', execute)
    from plugins.commands import ask_ollama
    suggestions = []
    def suggest(_app, command_id, **kwargs):
        suggestions.append((command_id, kwargs))
        return DispatchResult(DispatchState.QUEUED, kwargs.get('source_text', ''), command_id,
                              {'awaiting_confirmation': True, 'prompt': f"Did you mean '{command_id}'? Say yes."})
    monkeypatch.setattr(ask_ollama, 'suggest_command_miss', suggest)
    resolver = Mock(return_value=proposal('pause'))
    monkeypatch.setattr(ava, 'resolve', resolver)
    miss = DispatchOutcome(kind='command_miss', detail={'heard': 'please do this'})
    yield SimpleNamespace(app=app, executor=executor, calls=calls, suggestions=suggestions, chips=chips, speech=speech,
                          preflight=preflight, resolver=resolver, miss=miss, foreground=foreground,
                          run=lambda text='please pause the music': production('_resolve_command_miss')(app, miss, text))
    ava.reset_cancellations()


def test_defaults_and_settings_fallback():
    assert ava._DEFAULTS['interpret_misses'] is True
    entry = config_schema.SETTINGS_SCHEMA['ava_command_session.interpret_misses']
    assert entry['default'] is True
    assert 'on by default' in entry['help']['why']
    tree = ast.parse((ROOT / 'samsara/ui/settings/ava_cloud_qt.py').read_text(encoding='utf-8'))
    gets = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == 'get' and n.args and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == 'interpret_misses']
    assert len(gets) == 1 and ast.literal_eval(gets[0].args[1]) is True


@pytest.mark.parametrize('old', [None, False, True])
def test_one_time_migration_preserves_later_opt_out(old):
    cfg = {'tts': {'feedback_level': 'brief', 'enabled': True}}
    if old is not None:
        cfg['ava_command_session'] = {'interpret_misses': old, 'model': 'keep-this'}
    assert config_schema.migrate_removed_settings(cfg)
    assert cfg['ava_command_session']['interpret_misses'] is True
    cfg['ava_command_session']['interpret_misses'] = False
    assert not config_schema.migrate_removed_settings(cfg)
    assert cfg['ava_command_session']['interpret_misses'] is False
    if old is not None:
        assert cfg['ava_command_session']['model'] == 'keep-this'


def test_retired_migration_fills_true_without_overwriting_opt_out():
    app = SimpleNamespace(config={'ava_command_session': {}}, save_config=Mock())
    migrate = production('_migrate_retired_ava_command_session_config')
    migrate(app)
    assert app.config['ava_command_session']['interpret_misses'] is True
    app.config['ava_command_session']['interpret_misses'] = False
    app.save_config.reset_mock()
    migrate(app)
    assert app.config['ava_command_session']['interpret_misses'] is False
    app.save_config.assert_not_called()


@pytest.mark.parametrize('words, expected', [(8, True), (9, True), (16, True), (17, False)])
def test_dispatcher_word_limit(rig, words, expected):
    result = rig.run(' '.join(['word'] * words))
    assert bool(rig.suggestions) is expected
    assert rig.resolver.called is expected
    if expected:
        assert result.kind == 'command_suggestion_pending'


@pytest.mark.parametrize('names', [('pause',), ('pause', 'volume up'), ('pause', 'volume up', 'scroll down')])
def test_one_two_three_steps_route_and_feedback(rig, names):
    rig.resolver.return_value = proposal(*names)
    result = rig.run()
    if len(names) == 1:
        assert not rig.calls
        assert result.kind == 'command_suggestion_pending'
        assert rig.suggestions[0][0] == names[0]
        assert result.detail['phrase'] == names[0]
        assert result.detail['state'] == 'queued'
    else:
        assert result is rig.miss
        assert not rig.calls and not rig.suggestions


@pytest.mark.parametrize('decision', [policy.NeedsConfirmation('confirm'), policy.Denied('not_allowed_for_model')])
def test_chain_preflight_refuses_everything_before_effects(rig, monkeypatch, decision):
    rig.resolver.return_value = proposal('pause', 'close window')
    monkeypatch.setattr(policy, 'authorize', lambda inv, **kw: decision if inv.command_id == 'close window' else policy.Allowed())
    result = rig.run()
    assert result is rig.miss
    assert not rig.calls and not rig.suggestions


def test_single_keeps_confirmation_path(rig, monkeypatch):
    monkeypatch.setattr(rig.executor, 'execute_canonical', lambda *a, **kw:
                        DispatchResult(DispatchState.QUEUED, 'close window', 'close window'))
    rig.resolver.return_value = proposal('close window')
    assert rig.run().kind == 'command_suggestion_pending'


def test_four_steps_are_dropped(rig):
    rig.resolver.return_value = proposal('pause', 'volume up', 'scroll down', 'pause')
    assert rig.run() is rig.miss
    assert not rig.calls


@pytest.mark.parametrize('condition', ['disabled', 'master_off', 'pending', 'pack', 'ordinary', 'late', 'foreground', 'cancelled', 'newer'])
def test_ineligible_or_stale_proposal_does_nothing(rig, monkeypatch, condition):
    if condition == 'disabled':
        rig.app.config['ava_command_session'] = {'interpret_misses': False}
    elif condition == 'master_off':
        monkeypatch.setattr(ai_preferences, 'ava_master_enabled', lambda _cfg: False)
    elif condition == 'pending':
        monkeypatch.setattr(policy, 'pending_operation', lambda: object())
    elif condition == 'pack':
        rig.miss.detail['pack'] = 'Utilities'
    elif condition == 'ordinary':
        rig.miss = DispatchOutcome(kind='dictate_buffered')
        assert production('_resolve_command_miss')(rig.app, rig.miss, 'please pause') is rig.miss
        assert not rig.resolver.called
        return
    else:
        def changed(app, text, request, deadline):
            if condition == 'late':
                monkeypatch.setattr(ava.time, 'monotonic', lambda: deadline + .01)
            elif condition == 'foreground':
                rig.foreground[0] = 20
            elif condition == 'cancelled':
                ava.cancel(request)
            elif condition == 'newer':
                app._ava_interpret_request += 1
            return proposal('pause')
        rig.resolver.side_effect = changed
    assert rig.run() is rig.miss
    assert not rig.calls


@pytest.mark.parametrize('stop', ['failure', 'newer', 'generation', 'cancelled', 'interpreter_newer'])
def test_chain_stops_remaining_steps(rig, monkeypatch, stop):
    rig.resolver.return_value = proposal('pause', 'volume up', 'scroll down')
    def execute(name, host, **kw):
        rig.calls.append((name, kw))
        if stop == 'newer':
            rig.executor._grammar_request_serial += 1
        if stop == 'generation':
            rig.app._ava_cmd_generation += 1
        if stop == 'cancelled':
            ava.cancel(str(rig.app._ava_interpret_request))
        if stop == 'interpreter_newer':
            rig.app._ava_interpret_request += 1
        state = DispatchState.FAILED if stop == 'failure' else DispatchState.COMPLETED
        return DispatchResult(state, name, name)
    monkeypatch.setattr(rig.executor, 'execute_canonical', execute)
    assert rig.run() is rig.miss
    assert not rig.calls


def test_chain_follows_opened_foreground(rig, monkeypatch):
    rig.resolver.return_value = proposal('open', 'volume up')
    wait = Mock(return_value=True)
    monkeypatch.setattr(rig.executor, '_grammar_wait_target', wait)
    assert rig.run() is rig.miss
    assert not rig.calls
    wait.assert_not_called()


@pytest.fixture
def model(monkeypatch):
    ava.reset_cancellations()
    monkeypatch.setattr(ai_preferences, 'runtime_state', lambda *_a: SimpleNamespace(
        allowed=True, provider=SimpleNamespace(identity='ollama')))
    monkeypatch.setattr(ava_readiness, 'readiness_for', lambda _app: SimpleNamespace(ready=True))
    from plugins.commands import ask_ollama
    ask = Mock(return_value='CONFIRM done.\nACTION pause')
    monkeypatch.setattr(ask_ollama, 'ask_ollama', ask)
    monkeypatch.setattr(ask_ollama, 'get_system_prompt', lambda _app: 'existing {COMMAND_LIST}')
    app = SimpleNamespace(config={}, command_executor=SimpleNamespace(
        ava_menu=lambda *_a, **_kw: ['pause', 'volume up', 'scroll down', 'open']))
    yield app, ask
    ava.reset_cancellations()


@pytest.mark.parametrize('wire, ids', [
    ('CONFIRM done.\nACTION pause', ['pause']),
    ('CONFIRM done.\nACTION pause\nACTION volume up', ['pause', 'volume up']),
    ('CONFIRM done.\nAPP open | notepad\nACTION volume up\nACTION scroll down', ['open', 'volume up', 'scroll down']),
])
def test_repeated_existing_wire_tags_are_a_closed_menu_list(model, wire, ids):
    app, ask = model
    ask.return_value = wire
    result = ava.resolve(app, 'could you launch notepad and make it louder', 'list', 10**12)
    assert result is not None
    assert [p.command_id for p in (getattr(result, 'steps', ()) or (result,))] == ids


@pytest.mark.parametrize('wire', [
    'CONFIRM done.\nACTION pause\nACTION erase the disk',
    'CONFIRM done.\nAPP open | calculator\nACTION pause',
    'CONFIRM done.\nACTION pause\nACTION volume up\nACTION scroll down\nACTION pause',
    'CONFIRM done.\nACTION pause\nSCHEDULE 3 volume up',
    'CONFIRM done.\nACTION pause\nAPP open |',
    'CONFIRM done.\nACTION pause\nACTION volume up\nI will also erase the disk.',
])
def test_bad_step_rejects_whole_model_answer(model, wire):
    app, ask = model
    ask.return_value = wire
    assert ava.resolve(app, 'launch notepad and pause', 'invalid', 10**12) is None


def test_single_step_model_prompt_is_byte_identical(model):
    app, ask = model
    result = ava.resolve(app, 'please pause the music', 'single', 10**12)
    assert result.command_id == 'pause'
    assert ask.call_args.kwargs['system'] == 'existing pause, volume up, scroll down, open'


@pytest.mark.parametrize('words, expected', [(16, True), (17, False)])
def test_resolver_word_limit(model, words, expected):
    app, ask = model
    result = ava.resolve(app, ' '.join(['word'] * words), 'limit', 10**12)
    assert (result is not None) is expected
    assert ask.called is expected


def test_all_three_callers_are_inside_command_boundaries():
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    callers = {}
    for fn in cls.body:
        if isinstance(fn, ast.FunctionDef):
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == '_resolve_command_miss']
            if calls:
                callers[fn.name] = calls
    assert set(callers) == {'_handle_command_mode_utterance', '_process_wake_command', '_stop_recording_impl'}
    wake = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_process_wake_command')
    command_branch = next(n for n in ast.walk(wake) if isinstance(n, ast.If)
                          and ast.unparse(n.test) == "intent['type'] == 'command_text'")
    assert callers[wake.name][0] in list(ast.walk(command_branch))
    hold = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_stop_recording_impl')
    branch = next(n for n in ast.walk(hold) if isinstance(n, ast.If)
                  and 'is_command_mode' in ast.unparse(n.test)
                  and callers[hold.name][0] in list(ast.walk(n)))
    assert callers[hold.name][0] in list(ast.walk(branch))


def test_ava_chip_examples_and_miss_unchanged():
    assert outcome_chip('ava_command_chain', dict(state='completed', chain_count=2,
        chain_done=['opened Notepad', 'Windows volume 30%'])) == (
        'Completed (Ava): opened Notepad · Windows volume 30%', 'success')
    assert outcome_chip('command_miss', {'heard': 'purple cloud'}) == (
        'heard: purple cloud — not a command', 'error')


def test_real_resolver_rejects_expired_and_late_model_answers(model, monkeypatch):
    app, ask = model
    assert ava.resolve(app, 'pause the music', 'expired', 0) is None
    ask.assert_not_called()
    now = [1.0]
    monkeypatch.setattr(ava.time, 'monotonic', lambda: now[0])
    def late(*_a, **_kw):
        now[0] = 4.0
        return 'CONFIRM done.\nACTION pause'
    ask.side_effect = late
    assert ava.resolve(app, 'pause the music', 'late', 3.5) is None


def test_bad_registry_step_cannot_start_chain(rig):
    rig.resolver.return_value = proposal('pause', 'invented')
    assert rig.run() is rig.miss
    assert not rig.calls and not rig.preflight
