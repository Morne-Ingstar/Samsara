"""869s: matching, execution, and command feedback remain distinct."""
import json
from types import SimpleNamespace

import pytest

from samsara import commands, plugin_commands
from samsara.command_registry import DispatchResult, DispatchState, adapt_handler_return
from samsara.execution_policy import Route
from samsara.session_feedback import outcome_chip
from samsara.session_modes import CommandDispatchResult, SessionModeManager


@pytest.fixture
def executor(tmp_path, monkeypatch):
    # Import real declarations before isolating their replay table: imports are
    # cached, so later tests must retain the original metadata, not our overrides.
    from plugins.commands import core_utils, volume  # noqa: F401

    monkeypatch.setattr(plugin_commands, '_REGISTRY', dict(plugin_commands._REGISTRY))
    monkeypatch.setattr(plugin_commands, '_MODULE_ENTRIES',
                        {name: dict(entries) for name, entries in plugin_commands._MODULE_ENTRIES.items()})
    monkeypatch.setattr(plugin_commands, '_shared_matcher', None)
    monkeypatch.setattr(commands.execution_policy, 'authorize', lambda *a, **k: commands.execution_policy.Allowed())
    path = tmp_path / 'commands.json'
    path.write_text(json.dumps({'commands': {}}))
    def build(phrase, handler):
        plugin_commands.command(phrase, risk_class='ui')(handler)
        return commands.CommandExecutor(path, plugins_dir=tmp_path / 'absent')
    return build


@pytest.mark.parametrize('route', list(Route))
def test_false_after_chosen_command_is_failed_with_reason(executor, route):
    def fail(app, remainder):
        return False
    result = executor('synthetic wheel', fail).execute_canonical('synthetic wheel',
        SimpleNamespace(config={}), route=route, source_text='Synthetic wheel.')
    assert result.state is DispatchState.FAILED
    assert result.claimed and result.detail['reason']
    label, kind = outcome_chip('command_failed', dict(result.detail, phrase=result.phrase,
                                                    state=result.state.value, heard='Synthetic wheel.'))
    assert "Couldn't synthetic wheel:" in label and kind == 'error'
    assert 'Synthetic wheel.' not in label and 'not a command' not in label


def test_real_remainder_guard_declines_only_nonwhole_spoken_command(executor):
    from plugins.commands.core_utils import reset_floating_windows
    ex = executor('reset floating windows', reset_floating_windows)
    result = ex.execute_canonical('reset floating windows', SimpleNamespace(config={}),
        args={'remainder': 'in this sentence'}, source_text='reset floating windows in this sentence')
    assert result.state is DispatchState.MISS
    assert result.detail == {'declined_by': 'reset floating windows'}


def test_false_with_an_argument_is_failure_unless_audited_guard(executor):
    def fail(app, remainder):
        return False
    result = executor('synthetic wheel', fail).execute_canonical('synthetic wheel',
        SimpleNamespace(config={}), args={'remainder': 'a little'}, source_text='synthetic wheel a little')
    assert result.state is DispatchState.FAILED


def test_explicit_decline_preserves_legacy_remainder_handler(executor):
    def decline(app, remainder):
        return DispatchResult.miss(remainder)
    result = executor('inspect remainder', decline).execute_canonical('inspect remainder',
        SimpleNamespace(config={}), args={'remainder': 'ordinary prose'}, source_text='inspect remainder ordinary prose')
    assert result.state is DispatchState.MISS


@pytest.mark.parametrize('route', list(Route))
def test_legacy_callback_decline_returns_original_words(executor, monkeypatch, route):
    ex = executor('inspect remainder', lambda app, remainder: True)
    entry = ex._registry_entry('inspect remainder')
    monkeypatch.setattr(entry, 'handler', lambda app, remainder: False)
    heard = 'Inspect remainder: Original Words.'
    result = ex.execute_canonical('inspect remainder', SimpleNamespace(config={}),
        route=route, args={'remainder': 'Original Words'}, source_text=heard)
    assert result.state is DispatchState.MISS and not result.claimed
    assert result.result == heard
    assert result.detail == {'declined_by': 'inspect remainder'}


def test_legacy_callback_explicit_failure_stays_claimed(executor, monkeypatch):
    ex = executor('inspect remainder', lambda app, remainder: True)
    entry = ex._registry_entry('inspect remainder')
    monkeypatch.setattr(entry, 'handler', lambda app, remainder: DispatchResult(
        DispatchState.FAILED, detail={'reason': 'target disappeared'}))
    result = ex.process_text('Inspect remainder.', SimpleNamespace(config={}), force_commands=True)
    assert result.state is DispatchState.FAILED and result.claimed
    assert result.detail['reason'] == 'target disappeared'


def test_registered_volume_device_failure_stays_claimed(executor, monkeypatch):
    from plugins.commands import volume
    monkeypatch.setattr(volume._audio, 'get_volume', lambda: None)
    ex = executor('volume up', volume.handle_volume_up)
    result = ex.process_text('Volume up.', SimpleNamespace(config={}), force_commands=True)
    assert result.state is DispatchState.FAILED and result.claimed
    assert result.detail['reason']


@pytest.mark.parametrize('tail', ['.', ',', '!', '?', ';', ':', '...', '…', ' . ! ? ; : … '])
@pytest.mark.parametrize('matched', [False, True])
def test_shared_command_lane_cleans_before_dispatch_and_history(tail, matched):
    calls = []
    def dispatch(text):
        calls.append(text)
        return CommandDispatchResult(matched=matched, phrase='scroll down' if matched else None,
                                     state='failed' if matched else 'miss', detail={'reason': 'no foreground window'})
    manager = SessionModeManager(command_dispatch_fn=dispatch, abort_phrases=[],
        foreground_exe_resolver=lambda: 'synthetic.exe', inject_fn=lambda *a: None,
        remove_chars_fn=lambda n: None, agent_dispatch_fn=lambda *a: None)
    outcome = manager.dispatch_command_utterance('Scroll down' + tail)
    assert calls == ['Scroll down']
    assert outcome.detail['heard'] == 'Scroll down'
    label, _ = outcome_chip(outcome.kind, outcome.detail)
    assert label.startswith('heard: Scroll down — ')
    assert ('not a command' in label) is (not matched)


def test_adapter_preserves_async_and_explicit_states():
    assert adapt_handler_return(False) is DispatchState.FAILED
    assert adapt_handler_return(False, declined=True) is DispatchState.MISS
    assert adapt_handler_return(None) is DispatchState.QUEUED
    assert adapt_handler_return(DispatchState.REJECTED) is DispatchState.REJECTED


def test_period_still_matches_real_command_without_side_effect(executor):
    calls = []
    def fail(app, remainder):
        calls.append(remainder)
        return False
    ex = executor('scroll down', fail)
    result = ex.process_text('Scroll down.', SimpleNamespace(config={}), force_commands=True)
    assert calls == ['']
    assert result.state is DispatchState.FAILED
