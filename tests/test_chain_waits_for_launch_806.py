"""806: inert desktop/clock, production chain parser, policy and hotkeys."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import chain_settle as settle, commands, execution_policy as policy
from samsara.chain_recovery import parse_command_chain
from samsara.command_registry import DispatchState as State
from samsara.handlers import CommandContext, HotkeyHandler
from tests.test_several_commands_399 import rig


class Clock:
    now = 0.0

    def __init__(self):
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def window(hwnd=20, exe='notepad.exe', rect=(0, 0, 100, 100)):
    return settle.Window(hwnd, hwnd + 100, exe, rect)


def real_wait(executor, monkeypatch, clock):
    # 888a: a fake chain clock must not advance real background workers' time.
    monkeypatch.setattr(settle, 'time', SimpleNamespace(
        monotonic=lambda: clock.now, sleep=clock.sleep))
    monkeypatch.setattr(executor, '_grammar_wait_target',
                        commands.CommandExecutor._grammar_wait_target.__get__(executor))


@pytest.mark.parametrize('delay', [.3, .6, .9])
def test_delayed_launch_snap_then_numbers_targets_new_window(rig, monkeypatch, delay):
    executor, app, calls = rig
    clock = Clock()
    real_wait(executor, monkeypatch, clock)
    move_started = []

    def foreground():
        if clock.now < .1:
            return window(10, 'brave.exe')
        if clock.now < .2:
            return window(11, 'unrelated.exe')
        if clock.now < delay:
            return window(12)  # Old Notepad must not satisfy a launch wait.
        if move_started:
            elapsed = clock.now - move_started[0]
            if elapsed < .2:
                return window(rect=(0, 0, int(100 + elapsed * 1000), 100))
            return window(rect=(0, 0, 500, 100))
        return window()

    desktop = SimpleNamespace(foreground=foreground, windows=lambda: (window(12),))
    monkeypatch.setattr(executor, '_grammar_chain_target',
                        lambda entry, args, values: settle.prepare(entry, args, values, desktop=desktop))
    execute = executor.execute_canonical
    observations = []

    def run(phrase, *args, **kwargs):
        observations.append((phrase, clock.now, foreground().hwnd))
        if phrase == 'snap':
            assert kwargs['args']['remainder'] == 'left'
            move_started.append(clock.now)
        return execute(phrase, *args, **kwargs)

    monkeypatch.setattr(executor, 'execute_canonical', run)
    result = executor.process_text('open notepad then snap it left then show numbers', force_commands=True)
    assert result.state is State.COMPLETED
    assert [item[0] for item in observations] == ['open', 'snap', 'show numbers']
    assert observations[1][1] >= delay
    assert observations[1][2] == observations[2][2] == 20
    assert .35 <= observations[2][1] - move_started[0] <= .4
    assert all(0 < seconds <= .025 for seconds in clock.sleeps)
    print(f'806 launch={delay:.3f}s snap={observations[1][1]:.3f}s numbers={observations[2][1]:.3f}s')


@pytest.mark.parametrize('sentence', [
    'Focus notepad and snap it left and show numbers',
    'Focus notepad, then snap it left, then show numbers',
    'switch to notepad and then snap it left then show numbers',
])
def test_owner_focus_sentences_are_three_steps(rig, sentence):
    executor, app, calls = rig
    result = executor.process_text(sentence, force_commands=True)
    assert result.state is State.COMPLETED
    assert calls == [('focus', 'notepad'), ('snap', 'left'), ('show numbers', '')]


@pytest.mark.parametrize('phrase', ['show numbers', 'snap left', 'maximize window', 'close window', 'paste'])
def test_foreground_actions_wait_for_launch(rig, monkeypatch, phrase):
    executor, app, calls = rig
    if phrase == 'close window':
        # 806: isolate foreground timing; real close refusal is checked below.
        authorize = policy.authorize
        monkeypatch.setattr(policy, 'authorize',
                            lambda invocation, **kwargs: policy.Allowed()
                            if invocation.command_id == 'close window'
                            else authorize(invocation, **kwargs))
    clock = Clock()
    real_wait(executor, monkeypatch, clock)
    desktop = SimpleNamespace(foreground=lambda: window() if clock.now >= .6 else window(10, 'brave.exe'))
    monkeypatch.setattr(executor, '_grammar_chain_target',
                        lambda *args: settle.Target(desktop, frozenset({'notepad.exe'})))
    # The registered maximize command is bare; retain the original case id.
    spoken = 'maximize' if phrase == 'maximize window' else phrase
    result = executor.process_text('open notepad then ' + spoken, force_commands=True)
    assert result.state is State.COMPLETED
    assert len(calls) == 2
    assert .6 <= clock.now < .65


def test_close_chain_keeps_real_confirmation_policy(rig, monkeypatch):
    executor, app, calls = rig
    observe = Mock(side_effect=AssertionError('refused chain must not observe desktop'))
    monkeypatch.setattr(executor, '_grammar_chain_target', observe)
    result = executor.process_text('open notepad then close window', force_commands=True)
    assert result.state is State.REJECTED
    assert result.detail['reason'] == 'confirmation_required'
    assert not calls
    observe.assert_not_called()


def test_legacy_foreground_observer_tolerates_missing_handler(monkeypatch):
    from samsara import handlers
    monkeypatch.delattr(handlers, '_get_foreground_hwnd')
    assert commands.CommandExecutor._grammar_foreground() is None


def test_timeout_continues_with_log_and_display_note(rig, monkeypatch, caplog):
    executor, app, calls = rig
    clock = Clock()
    real_wait(executor, monkeypatch, clock)
    desktop = SimpleNamespace(foreground=lambda: window(10, 'brave.exe'))
    monkeypatch.setattr(executor, '_grammar_chain_target',
                        lambda *args: settle.Target(desktop, frozenset({'notepad.exe'})))
    result = executor.process_text('open notepad then show numbers', force_commands=True)
    assert result.state is State.COMPLETED
    assert calls == [('open', 'notepad'), ('show numbers', '')]
    assert clock.now == pytest.approx(3)
    assert 'continuing' in caplog.text
    detail = app._show_dispatch_outcome_chip.call_args.args[0].detail
    assert 'window wait timed out; continued' in detail['chain_done'][0]


def test_already_foreground_focus_and_open_do_not_wait(rig, monkeypatch):
    executor, app, calls = rig
    clock = Clock()
    monkeypatch.setattr('plugins.commands.app_verbs.resolve_window', lambda name: (20, 'Notes', 'notepad.exe'))
    desktop = SimpleNamespace(foreground=window, window=lambda hwnd: window())
    for phrase in ['focus', 'open']:
        target = settle.prepare(executor._registry_entry(phrase), {'remainder': 'notepad'}, desktop=desktop)
        assert target.hwnd == 20 and not target.new_window
        assert settle.wait(target, lambda: True, clock=lambda: clock.now, sleep=clock.sleep)
    assert not clock.sleeps


def test_builtin_launch_requires_new_hwnd_even_if_same_process_is_front(rig, monkeypatch):
    executor, app, calls = rig
    monkeypatch.setattr('plugins.commands.app_verbs.resolve_window', lambda name: (20, 'Notes', 'notepad.exe'))
    desktop = SimpleNamespace(foreground=window, windows=lambda: (window(),))
    entry = SimpleNamespace(phrase='open notepad', data={'type': 'launch', 'target': 'notepad.exe'})
    target = settle.prepare(entry, {}, desktop=desktop)
    assert target.new_window and target.existing == frozenset({20})
    clock = Clock()
    assert not settle.wait(target, lambda: True, clock=lambda: clock.now, sleep=clock.sleep)
    assert clock.now == pytest.approx(3)


def test_three_volume_steps_do_not_observe_or_sleep(rig, monkeypatch):
    executor, app, calls = rig
    monkeypatch.setattr(executor, '_grammar_chain_target',
                        lambda entry, args, values: settle.prepare(entry, args, values))
    monkeypatch.setattr(settle, 'Desktop', Mock(side_effect=AssertionError('non-focus desktop read')))
    monkeypatch.setattr(settle, 'time', SimpleNamespace(
        monotonic=settle.time.monotonic, sleep=Mock(side_effect=AssertionError('non-focus delay'))))
    result = executor.process_text('volume down then volume down then volume down', force_commands=True)
    assert result.state is State.COMPLETED
    assert calls == [('volume down', '')] * 3


def test_wait_cancellation_never_runs_next_step(rig, monkeypatch):
    executor, app, calls = rig
    clock = Clock()
    real_wait(executor, monkeypatch, clock)
    def foreground():
        app._ava_cmd_generation = 1
        return None
    desktop = SimpleNamespace(foreground=foreground)
    monkeypatch.setattr(executor, '_grammar_chain_target',
                        lambda *args: settle.Target(desktop, frozenset({'notepad.exe'})))
    result = executor.process_text('open notepad then show numbers', force_commands=True)
    assert result.state is State.CANCELLED
    assert calls == [('open', 'notepad')]


def test_maximize_waits_for_animation_and_snap_assist():
    clock = Clock()
    def foreground():
        if clock.now < .3:
            return window(30, 'explorer.exe')  # Snap Assist temporarily has focus.
        return window(rect=(0, 0, 500, 500))
    target = settle.Target(SimpleNamespace(foreground=foreground), frozenset({'notepad.exe'}), hwnd=20, moving=True)
    assert settle.wait(target, lambda: True, clock=lambda: clock.now, sleep=clock.sleep)
    assert .45 <= clock.now <= .5


@pytest.mark.parametrize('verb', ['focus', 'open', 'switch to', 'ask', 'click'])
def test_argument_boundary_only_before_known_command(verb):
    records = [
        {'canonical_id': 'argument', 'aliases': [verb], 'args': [{'name': 'name', 'type': 'text'}]},
        {'canonical_id': 'numbers', 'aliases': ['show numbers'], 'args': []},
    ]
    result = parse_command_chain(verb + ' Salt and Pepper and then show numbers', records)
    assert [(cid, values) for cid, values, *_ in result] == [
        ('argument', {'name': 'salt and pepper'}), ('numbers', {})]
    assert len(parse_command_chain(verb + ' Salt and Pepper', records)) <= 1


def test_named_app_with_and_is_not_split(rig):
    executor, app, calls = rig
    result = executor.process_text('focus Salt and Pepper then show numbers', force_commands=True)
    assert result.state is State.COMPLETED
    assert calls == [('focus', 'Salt and Pepper'), ('show numbers', '')]


@pytest.mark.parametrize('sentence', ['tab one', 'new tab then tab one'])
def test_tab_one_real_hotkey_path_sends_ctrl_before_digit(rig, monkeypatch, sentence):
    executor, app, calls = rig
    keyboard = Mock()
    context = CommandContext(keyboard_controller=keyboard, key_map={'ctrl': 'CTRL'})
    monkeypatch.setattr(commands, 'get_handler', lambda kind: HotkeyHandler())
    monkeypatch.setattr(executor, '_build_context', lambda app: context)
    result = executor.process_text(sentence, force_commands=True)
    assert result.state is State.COMPLETED
    events = [(call[0], call.args[0]) for call in keyboard.method_calls]
    expected = [('press', 'CTRL'), ('press', '1'), ('release', '1'), ('release', 'CTRL')]
    assert events[-4:] == expected
    assert len(events) == (8 if sentence.startswith('new tab') else 4)


def test_dark_mode_catalog_describes_settings_launch():
    data = json.loads((Path(__file__).resolve().parents[1] / 'commands.json').read_text(encoding='utf-8'))
    # This is an ownership-excluded behavior report, not a live settings action.
    entry = data.get('commands', data)['dark mode']
    assert entry['type'] == 'launch'
    assert entry['target'] == 'ms-settings:personalization-colors'
    assert entry['description'] == 'Opens color/theme settings (toggle dark mode).'
