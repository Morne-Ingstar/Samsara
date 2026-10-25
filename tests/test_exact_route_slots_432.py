"""432: exact speech uses the handler's slot grammar before authorization.

No dictation import, live profile, model, or window effect is needed. The
production registry supplies declarations; monitor state and effects are fake.
"""
import json
import logging
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara import arg_types, execution_policy as ep, plugin_commands
from samsara.command_registry import DispatchResult, DispatchState
from samsara.command_scope import MatchContext
from tools.dump_command_metadata import build_executor


VOICE_SLOTS = {
    'send': ('Chrome to left screen', {'app_name': 'chrome', 'monitor': 'left screen'}),
    'play playlist': ('Foo', {'remainder': 'Foo'}),
    'play album': ('Foo', {'remainder': 'Foo'}),
    'play artist': ('Foo', {'remainder': 'Foo'}),
    'insert': ('Foo', {'remainder': 'Foo'}),
    'save snippet': ('Foo', {'remainder': 'Foo'}),
    'find text': ('Foo', {'remainder': 'Foo'}),
    'tab number': ('two', {'remainder': 'two'}),
    'font size': ('twelve', {'remainder': 'twelve'}),
    'line spacing': ('two', {'remainder': 'two'}),
    'set font': ('Arial', {'remainder': 'Arial'}),
    'claude message submit': ('Foo', {'remainder': 'Foo'}),
    'chatgpt message submit': ('Foo', {'remainder': 'Foo'}),
    # 604 adds named-control actions and selection-scoped disambiguation.
    'choose control': ('two', {'remainder': 'two'}),
    'toggle': ('Foo', {'remainder': 'Foo'}),
    'expand': ('Foo', {'remainder': 'Foo'}),
    'collapse': ('Foo', {'remainder': 'Foo'}),
    'focus control': ('Foo', {'remainder': 'Foo'}),
    # 887: required text slots from landed 632/823 and keyboard selection.
    'make selection': ('Foo', {'remainder': 'make selection Foo'}),
    'change selection': ('Foo', {'remainder': 'change selection Foo'}),
    'edit selection': ('Foo', {'remainder': 'edit selection Foo'}),
    'reformat selection': ('Foo', {'remainder': 'reformat selection Foo'}),
    'rewrite selection': ('Foo', {'remainder': 'rewrite selection Foo'}),
    'reword selection': ('Foo', {'remainder': 'reword selection Foo'}),
    'rephrase selection': ('Foo', {'remainder': 'rephrase selection Foo'}),
    'select previous': ('three words', {'remainder': 'three words'}),
}
CONTROLLER_SLOTS = {
    'interact focus': {'subject_token': 'opaque-controller-token'},
    'interact minimize': {'subject_token': 'opaque-controller-token'},
    'interact maximize': {'subject_token': 'opaque-controller-token'},
    'interact move': {'subject_token': 'opaque-controller-token', 'monitor': 2},
    'interact snap': {'subject_token': 'opaque-controller-token', 'side': 'left'},
    'interact close': {'subject_token': 'opaque-controller-token'},
}


@pytest.fixture(scope='module')
def production():
    executor = build_executor()
    required = {entry.phrase for entry in executor._matcher._sorted
                if any(spec.get('required') for spec in entry.param_schema.values())}
    # 887: 820b's scoped read-window command has an integer, not a text slot.
    assert required == set(VOICE_SLOTS) | set(CONTROLLER_SLOTS) | {'read window'}
    return executor, dict(plugin_commands._REGISTRY)


@pytest.fixture
def live(production, monkeypatch):
    from plugins.commands import windows

    executor, registry = production
    monkeypatch.setattr(plugin_commands, '_REGISTRY', dict(registry))
    packs = {entry.pack for entry in executor._matcher._sorted}
    executor._matcher.set_enabled_packs(packs)
    monkeypatch.setattr(executor._matcher, 'current_context',
                        lambda: MatchContext.for_app('chrome.exe'))
    app = SimpleNamespace(config={'command_packs': dict.fromkeys(packs, True),
                                  'commands': {'grammar_tier': False}},
                          command_mode_active=False, _ava_cmd_generation=0)
    monitors = [
        {'handle': 1, 'index': 1, 'rect': (0, 0, 1920, 1080), 'primary': True, 'device': 'left'},
        {'handle': 2, 'index': 2, 'rect': (1920, 0, 3840, 1080), 'primary': False, 'device': 'right'},
    ]
    monkeypatch.setattr(windows, 'get_monitors', lambda: monitors)
    monkeypatch.setattr(windows.win32api, 'EnumDisplayMonitors',
                        lambda: [(m['handle'], None, m['rect']) for m in monitors])
    monkeypatch.setattr(windows.win32api, 'GetCursorPos', lambda: (300, 400))

    def forbidden(*_args, **_kwargs):
        raise AssertionError('validation must not cause a window/cursor effect')

    monkeypatch.setattr(windows, 'move_window_to_monitor', forbidden)
    monkeypatch.setattr(windows, '_windows_for', forbidden)
    monkeypatch.setattr(windows, 'teleport_cursor', forbidden)
    return executor, app, windows, monitors


def _capture(executor, app, monkeypatch, text):
    captured = {}

    def capture(command_id, app_instance=None, **kwargs):
        captured.update(command_id=command_id, **kwargs)
        return DispatchResult(DispatchState.REJECTED, command_id, command_id)

    monkeypatch.setattr(executor, 'execute_canonical', capture)
    result = executor.process_text(text, app, force_commands=True)
    return captured, result


def _decision(executor, app, phrase, args, *, route=ep.Route.EXACT, generation=0):
    return ep.authorize(ep.Invocation(phrase, args, route, generation),
                        executor=executor, app=app, quiet=True)


@pytest.mark.parametrize('phrase', VOICE_SLOTS)
def test_every_required_voice_slot_has_the_same_policy_decision_as_typed(live, monkeypatch, phrase):
    executor, app, _windows, _monitors = live
    remainder, typed = VOICE_SLOTS[phrase]
    entry = executor._registry_entry(phrase)
    scope = entry.scope
    if scope is not None:
        exe = next(iter(scope.apps), 'chrome.exe')
        monkeypatch.setattr(executor._matcher, 'current_context',
                            lambda: MatchContext.for_app(exe, tags=scope.tags))
    captured, _result = _capture(executor, app, monkeypatch, f'{phrase} {remainder}')
    assert captured['command_id'] == phrase
    assert captured['route'] is ep.Route.EXACT
    # 887: selection requests retain the whole utterance for their intent parser.
    expected_remainder = typed.get('remainder', remainder)
    assert captured['args']['remainder'] == expected_remainder
    assert _decision(executor, app, phrase, captured['args']) == _decision(executor, app, phrase, typed)
    assert not isinstance(_decision(executor, app, phrase, typed), ep.Denied)


@pytest.mark.parametrize('phrase', CONTROLLER_SLOTS)
def test_controller_tokens_are_never_inferred_from_voice(live, monkeypatch, phrase):
    executor, app, _windows, _monitors = live
    assert executor._registry_entry(phrase).voice_triggerable is False
    slots = CONTROLLER_SLOTS[phrase]
    # Explicit controller slots retain the same decision with spoken text
    # attached. The exact route cannot manufacture an opaque subject token.
    assert _decision(executor, app, phrase, {**slots, 'remainder': 'left'}) == _decision(executor, app, phrase, slots)
    captured, result = _capture(executor, app, monkeypatch, f'{phrase} left')
    if captured:
        assert captured['args'] == {'remainder': 'left'}
        decision = _decision(executor, app, phrase, captured['args'])
        assert isinstance(decision, ep.Denied)
        assert decision.reason == 'invalid_args'
        assert decision.detail == "missing required argument 'subject_token'"
    else:
        assert result.state is DispatchState.MISS


@pytest.mark.parametrize('verb', ['send', 'move', 'put', 'throw'])
def test_alias_runs_after_policy_and_keeps_original_remainder(live, monkeypatch, verb):
    executor, app, _windows, _monitors = live
    calls = []

    def handler(app_instance, remainder):
        calls.append((app_instance, remainder))
        return True

    handler.__module__ = 'plugins.commands.windows'
    monkeypatch.setattr(executor._registry_entry('send'), 'handler', handler)
    result = executor.process_text(f'{verb} Chrome to Left Screen', app, force_commands=True)
    assert result.state is DispatchState.COMPLETED
    assert calls == [(app, 'Chrome to Left Screen')]


@pytest.mark.parametrize('remainder,detail', [
    ('', "missing required argument 'monitor'"),
    ('chrome to nowhere', "argument 'monitor' must be a monitor"),
    ('chrome to monitor 3', "argument 'monitor' must be a monitor"),
    ('chrome to middle screen', "argument 'monitor' must be a monitor"),
    ('chrome to left and firefox to monitor 3', "argument 'monitor' must be a monitor"),
])
def test_missing_unrecognized_nonexistent_and_compound_destinations_refuse(live, monkeypatch, remainder, detail):
    executor, app, _windows, _monitors = live
    calls = []

    def handler(_app, original):
        calls.append(original)
        return True

    handler.__module__ = 'plugins.commands.windows'
    monkeypatch.setattr(executor._registry_entry('send'), 'handler', handler)
    result = executor.process_text(f'send {remainder}'.strip(), app, force_commands=True)
    assert result.state is DispatchState.REJECTED
    assert result.detail == {'reason': 'invalid_args', 'detail': detail}
    assert calls == []


@pytest.mark.parametrize('destination', ['left', 'right screen', 'monitor 2', 'second screen',
                                         'primary', 'tv', 'here', 'other'])
def test_monitor_validator_uses_the_handler_destination_parser(live, destination):
    executor, app, windows, monitors = live
    assert windows._parse_destination(destination, app, monitors)
    assert windows._parse_destination(destination, app, monitors, strict=True) is windows._parse_destination(destination, app, monitors)
    assert ep.validate_args(executor._registry_entry('send').param_schema,
                            {'monitor': destination}, app=app) is None


def test_all_compound_slots_are_checked_and_extraction_reads_no_system_state(live, monkeypatch):
    executor, app, windows, _monitors = live
    original = 'Chrome to Left Screen and Firefox to Right Screen'
    captured, _result = _capture(executor, app, monkeypatch, f'send {original}')
    assert captured['args'] == {'remainder': original, 'app_name': ('chrome', 'firefox'),
                                'monitor': ('left screen', 'right screen')}
    assert isinstance(_decision(executor, app, 'send', captured['args']), ep.Allowed)

    def forbidden():
        raise AssertionError('slot extraction must not read live state')

    monkeypatch.setattr(windows, 'get_monitors', forbidden)
    assert windows.parse_send_slots(original)['monitor'] == ('left screen', 'right screen')


@pytest.mark.parametrize('failure', ['enumeration', 'empty', 'partial', 'cursor'])
def test_failed_live_state_reads_refuse_with_existing_wording(live, monkeypatch, failure):
    executor, app, windows, monitors = live

    def unavailable(*_args):
        raise OSError('synthetic read failure')

    destination = 'left'
    if failure == 'enumeration':
        monkeypatch.setattr(windows.win32api, 'EnumDisplayMonitors', unavailable)
    elif failure == 'empty':
        monkeypatch.setattr(windows, 'get_monitors', lambda: [])
    elif failure == 'partial':
        monkeypatch.setattr(windows, 'get_monitors', lambda: monitors[:1])
    else:
        destination = 'here'
        monkeypatch.setattr(windows.win32api, 'GetCursorPos', unavailable)
    assert _decision(executor, app, 'send', {'monitor': destination}) == ep.Denied(
        'invalid_args', risk='ui', detail="argument 'monitor' must be a monitor")


def test_monitor_handle_wrappers_are_compared_as_numeric_windows_handles(live, monkeypatch):
    _executor, _app, windows, monitors = live

    class Handle:
        def __init__(self, value):
            self.value = value

        def __int__(self):
            return self.value

    # pywin32 returns PyHANDLE wrappers; ctypes returns integer handles.
    monkeypatch.setattr(windows.win32api, 'EnumDisplayMonitors',
                        lambda: [(Handle(m['handle']), None, m['rect']) for m in monitors])
    assert windows._strict_monitors() == monitors


def test_unknown_semantic_type_and_rejecting_or_raising_validator_fail_closed(monkeypatch):
    schema = {'slot': {'type': 'unregistered_432', 'required': True}}
    error = ('invalid_args', "argument 'slot' must be a unregistered_432")
    assert ep.validate_args(schema, {'slot': 'left'}) == error
    with monkeypatch.context() as patch:
        patch.setitem(arg_types._ARG_TYPE_VALIDATORS, 'unregistered_432', lambda _v, **_k: False)
        assert ep.validate_args(schema, {'slot': 'left'}) == error
    with monkeypatch.context() as patch:
        def raises(_value, **_kwargs):
            raise RuntimeError('synthetic parser failure')
        patch.setitem(arg_types._ARG_TYPE_VALIDATORS, 'unregistered_432', raises)
        assert ep.validate_args(schema, {'slot': 'left'}) == error
    assert ep.validate_args(schema, {'slot': 'left'}) == error


@pytest.mark.parametrize('type_name', ['int', 'float', 'bool', 'str'])
def test_static_registration_cannot_override_primitive_validation(type_name):
    with pytest.raises(ValueError):
        arg_types.register_arg_type(type_name, lambda _value, **_kwargs: True)


def test_registered_types_are_owned_by_the_window_module(live):
    _executor, _app, windows, _monitors = live
    assert arg_types._ARG_TYPE_VALIDATORS['monitor'] is windows._validate_monitor
    assert arg_types._ARG_TYPE_VALIDATORS['app_name'] is windows._validate_app_name
    with pytest.raises(ValueError):
        arg_types.register_arg_type('monitor', lambda _value, **_kwargs: True)


@pytest.mark.parametrize('value', ['', 'this', 1, {}, [], [''], ['this']])
def test_app_name_validator_rejects_non_names(live, value):
    _executor, _app, _windows, _monitors = live
    assert ep.validate_args({'app': {'type': 'app_name'}}, {'app': value}) == (
        'invalid_args', "argument 'app' must be a app_name")


@pytest.mark.parametrize('value', ['chrome', 'the chrome', 'VS Code', 'unknown app title',
                                  'chrome to monitor 3', ['chrome'], ('chrome',)])
def test_app_name_validator_reuses_single_name_normalization_without_window_lookup(live, value):
    _executor, _app, _windows, _monitors = live
    assert ep.validate_args({'app': {'type': 'app_name'}}, {'app': value}) is None


@pytest.mark.parametrize('value', [2, True, {}, {'index': 1}, [], [''], (), None])
def test_monitor_validator_rejects_values_the_destination_parser_cannot_read(live, value):
    _executor, _app, _windows, _monitors = live
    assert ep.validate_args({'monitor': {'type': 'monitor'}}, {'monitor': value}) == (
        'invalid_args', "argument 'monitor' must be a monitor")


def test_authorization_forwards_app_context_to_the_existing_tv_parser(live, monkeypatch):
    executor, app, windows, monitors = live
    app.config['window_manager'] = {'tv_device': 'left'}
    original = windows.get_tv_monitor
    contexts = []

    def capture(context, snapshot):
        contexts.append(context)
        target = original(context, snapshot)
        assert target is monitors[0]
        return target

    monkeypatch.setattr(windows, 'get_tv_monitor', capture)
    assert isinstance(_decision(executor, app, 'send', {'monitor': 'tv'}), ep.Allowed)
    assert contexts == [app]


def test_extraction_parser_failure_does_not_execute(live, monkeypatch, caplog):
    executor, app, windows, _monitors = live

    def broken(_remainder):
        raise RuntimeError('synthetic parser failure')

    monkeypatch.setattr(windows, 'parse_send_slots', broken)
    with caplog.at_level(logging.DEBUG, logger='Samsara.samsara.commands'):
        result = executor.process_text('send chrome to left', app, force_commands=True)
    assert result.state is DispatchState.REJECTED
    assert result.detail == {'reason': 'invalid_args', 'detail': "missing required argument 'monitor'"}
    assert 'send slot parser failed: RuntimeError' in caplog.text


def test_only_declared_slots_are_forwarded(live, monkeypatch):
    executor, app, windows, _monitors = live
    real_parser = windows.parse_send_slots
    monkeypatch.setattr(windows, 'parse_send_slots',
                        lambda text: {**real_parser(text), 'undeclared': 'must not be forwarded'})
    captured, _result = _capture(executor, app, monkeypatch, 'send chrome to left')
    assert set(captured['args']) == {'remainder', 'app_name', 'monitor'}


@pytest.mark.parametrize('blocked', ['stale', 'pack_disabled', 'out_of_scope', 'model_allowlist'])
def test_valid_semantic_slots_do_not_bypass_other_policy_refusals(live, monkeypatch, blocked):
    executor, app, _windows, _monitors = live
    kwargs = {}
    expected = blocked
    if blocked == 'stale':
        kwargs['generation'] = -1
    elif blocked == 'pack_disabled':
        executor._matcher.set_enabled_packs(set())
    elif blocked == 'out_of_scope':
        from samsara.command_scope import parse_scope
        monkeypatch.setattr(executor._registry_entry('send'), 'scope',
                            parse_scope({'apps': ['other.exe']}))
    else:
        app.config['execution_policy'] = {'model_tool_allowlist': []}
        kwargs['route'] = ep.Route.MODEL
        expected = 'invalid_args'
    decision = _decision(executor, app, 'send', {'app_name': 'chrome', 'monitor': 'left'}, **kwargs)
    assert isinstance(decision, ep.Denied)
    assert decision.reason == expected


SEMANTIC_COMMANDS = ('find window', 'cursor to', 'window move', 'bring',
                     'focus', 'open', 'close', 'send', 'snap')


def _head_type_ok(typ, value, **_kwargs):
    """The primitive-only predicates at HEAD before 432 (no Git dependency)."""
    if typ == 'int':
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == 'float':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typ == 'bool':
        return isinstance(value, bool)
    if typ == 'str':
        return isinstance(value, str)
    return False


def _decision_bytes(decision):
    return json.dumps({'kind': type(decision).__name__, **asdict(decision)},
                      sort_keys=True).encode('utf-8')


@pytest.mark.parametrize('phrase', SEMANTIC_COMMANDS)
@pytest.mark.parametrize('route', [ep.Route.MODEL, ep.Route.SMART_ACTION])
@pytest.mark.parametrize('confirmed', [False, True])
@pytest.mark.parametrize('allowlist', [None, [], list(SEMANTIC_COMMANDS)])
def test_every_semantic_command_preserves_model_decision_byte_for_byte(
        live, monkeypatch, phrase, route, confirmed, allowlist):
    executor, app, _windows, _monitors = live
    affected = {entry.phrase for entry in executor._matcher._sorted
                if any(spec.get('type') in {'app_name', 'monitor'}
                       for spec in entry.param_schema.values())}
    assert affected == set(SEMANTIC_COMMANDS)
    schema = executor._registry_entry(phrase).param_schema
    args = {key: 'left' if spec['type'] == 'monitor' else 'chrome'
            for key, spec in schema.items() if spec.get('type') in {'app_name', 'monitor'}}
    if allowlist is not None:
        app.config['execution_policy'] = {'model_tool_allowlist': allowlist}
    inv = ep.Invocation(phrase, args, route, 0)
    with monkeypatch.context() as patch:
        patch.setattr(ep, '_type_ok', _head_type_ok)
        old = ep.authorize(inv, executor=executor, app=app, confirmed=confirmed, quiet=True)
    new = ep.authorize(inv, executor=executor, app=app, confirmed=confirmed, quiet=True)
    assert isinstance(new, ep.Denied) and new.reason == 'invalid_args'
    assert _decision_bytes(old) == _decision_bytes(new)


@pytest.mark.parametrize('route', [ep.Route.MODEL, ep.Route.SMART_ACTION])
def test_model_matching_keeps_original_remainder_and_never_calls_semantic_validator(
        live, monkeypatch, route):
    executor, app, windows, _monitors = live
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append('parser or validator invoked')
        raise AssertionError('model route must not extract or validate semantic slots')

    monkeypatch.setattr(windows, 'parse_send_slots', forbidden)
    monkeypatch.setitem(arg_types._ARG_TYPE_VALIDATORS, 'monitor', forbidden)
    captured = {}

    def capture(command_id, app_instance=None, **kwargs):
        captured.update(kwargs)
        return DispatchResult(DispatchState.REJECTED, command_id, command_id)

    monkeypatch.setattr(executor, 'execute_canonical', capture)
    executor.process_text('send chrome to left', app, force_commands=True, route=route, generation=0)
    assert captured['args'] == {'remainder': 'chrome to left'}
    assert calls == []
    assert ep.validate_args({'monitor': {'type': 'monitor'}}, {'monitor': 'left'},
                            route=route, app=app) == (
                                'invalid_args', "argument 'monitor' must be a monitor")
    assert calls == []


@pytest.mark.parametrize('route', [ep.Route.EXACT, ep.Route.GRAMMAR, ep.Route.MACRO, ep.Route.SCHEDULE])
@pytest.mark.parametrize('container', [list, tuple])
@pytest.mark.parametrize('destinations', [('left', 'right'), ('left', 'monitor 3')])
def test_json_lists_and_tuples_have_identical_user_route_decisions(live, route, container, destinations):
    executor, app, _windows, _monitors = live
    args = {'app_name': container(('chrome', 'firefox')), 'monitor': container(destinations)}
    actual = _decision(executor, app, 'send', args, route=route)
    expected = _decision(executor, app, 'send',
                         {'app_name': ('chrome', 'firefox'), 'monitor': destinations}, route=route)
    assert actual == expected
    assert isinstance(actual, ep.Allowed if destinations[-1] == 'right' else ep.Denied)


@pytest.mark.parametrize('name', ['command and conquer', 'photo to pdf', 'back to the future'])
@pytest.mark.parametrize('phrase', ['send', 'focus', 'bring'])
def test_one_app_name_keeps_reserved_words_on_exact_route(live, monkeypatch, phrase, name):
    executor, app, windows, _monitors = live
    from plugins.commands import app_verbs

    # A single typed name has no placement syntax; normalization is the
    # same helper used by the send parser. No lookup/effect is needed here.
    with monkeypatch.context() as patch:
        patch.setattr(windows, '_parse_placements', lambda _text: pytest.fail('single-name validator split placements'))
        assert windows._validate_app_name(name)
        typed = {'app_name': name, **({'monitor': 'left'} if phrase == 'send' else {})}
        assert isinstance(_decision(executor, app, phrase, typed), ep.Allowed)

    if phrase == 'send':
        original = f'{name} to left'
        placements = windows._parse_placements(original)
        captured, _result = _capture(executor, app, monkeypatch, f'send {original}')
        assert captured['args']['remainder'] == original
        assert captured['args']['monitor'] == placements[0][1]
        if placements[0][0] is not None:
            assert captured['args']['app_name'] == placements[0][0]
        accepts_destination = bool(windows._parse_destination(placements[0][1], app))
        assert isinstance(_decision(executor, app, 'send', captured['args']), ep.Allowed) == accepts_destination
        # Existing send grammar reserves the first " to "; authorization
        # must follow the handler's parse, never invent a different split.
    else:
        with monkeypatch.context() as patch:
            captured, _result = _capture(executor, app, patch, f'{phrase} {name}')
        expected_name = captured['args']['remainder']
        names = []
        if phrase == 'focus':
            monkeypatch.setattr(app_verbs, 'do_focus',
                                lambda value: names.append(value) or app_verbs.ActionResult.DONE)
        else:
            monkeypatch.setattr(windows, 'get_monitor_under_cursor', lambda: _monitors[0])
            monkeypatch.setattr(windows, 'find_windows_by_app', lambda value, _ignore: names.append(value) or [])
        result = executor.process_text(f'{phrase} {name}', app, force_commands=True)
        assert result.state is DispatchState.COMPLETED
        assert names == [expected_name]


@pytest.mark.parametrize('call,destination', [('EnumDisplayMonitors', 'left'), ('GetCursorPos', 'here')])
def test_windows_errors_refuse_and_next_authorization_carries_on(live, monkeypatch, call, destination):
    import pywintypes

    executor, app, windows, _monitors = live

    def unavailable(*_args):
        raise pywintypes.error(5, call, 'synthetic secure desktop read failure')

    with monkeypatch.context() as patch:
        patch.setattr(windows.win32api, call, unavailable)
        assert _decision(executor, app, 'send', {'monitor': destination}) == ep.Denied(
            'invalid_args', risk='ui', detail="argument 'monitor' must be a monitor")
    assert isinstance(_decision(executor, app, 'send', {'monitor': destination}), ep.Allowed)


@pytest.mark.parametrize('call,destination', [('EnumDisplayMonitors', 'left'), ('GetCursorPos', 'here')])
def test_validator_intentionally_refuses_when_handler_reader_would_guess(live, monkeypatch, call, destination):
    executor, app, windows, monitors = live

    def unavailable(*_args):
        raise OSError('synthetic failed read')

    monkeypatch.setattr(windows.win32api, call, unavailable)
    # Handler best-effort parsing can use the available/primary monitor.
    assert windows._parse_destination(destination, app) is monitors[0]
    assert isinstance(_decision(executor, app, 'send', {'monitor': destination}), ep.Denied)


def test_fresh_interpreter_requires_plugin_registration_before_authorize(tmp_path):
    # The subprocess inherits the gate's isolated environment. Read only
    # repository source; no Git, owner profile, service, or window effect.
    script = r'''
import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from samsara import execution_policy as ep, plugin_commands
assert 'plugins.commands.windows' not in sys.modules
tree = ast.parse(Path('plugins/commands/windows.py').read_text(encoding='utf-8'))
handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'handle_send')
declaration = handler.decorator_list[0]
metadata = {kw.arg: ast.literal_eval(kw.value) for kw in declaration.keywords}
plugin_commands._REGISTRY['send'] = {'phrase': 'send', 'metadata': metadata}
app = SimpleNamespace(config={}, _ava_cmd_generation=0)
inv = ep.Invocation('send', {'monitor': 'monitor 1'}, ep.Route.EXACT, 0)
before = ep.authorize(inv, app=app, quiet=True)
assert before == ep.Denied('invalid_args', risk='ui', detail="argument 'monitor' must be a monitor"), before
assert 'plugins.commands.windows' not in sys.modules
plugin_commands._REGISTRY.pop('send')  # remove the pre-import declaration stub
from plugins.commands import windows
assert windows._strict_monitors(), 'readable real monitor required by this test'
after = ep.authorize(inv, app=app, quiet=True)
assert isinstance(after, ep.Allowed), after
print('before: refused; after plugin import: allowed with real monitor 1; no handler called')
'''
    completed = subprocess.run([sys.executable, '-B', '-c', script],
                               cwd=Path(__file__).resolve().parents[1],
                               text=True, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert 'after plugin import: allowed with real monitor 1' in completed.stdout

