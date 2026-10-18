"""363: real hands-free orchestration/registry, recording handlers, no bootstrap."""
import json
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.command_scope import MatchContext
from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals, outcome_chip
from samsara.streaming import DictatePreviewSession
from tests.test_wake_pause_311 import production as _production
from tools.dump_command_metadata import build_executor

SIGNALS = UtteranceSignals(True, (1.0,))
# Empty draft unless the final column supplies the immediately preceding thought.
ROWS = [
    ('scroll down', 'hands_free_command_executed', None),
    ('page up', 'hands_free_command_executed', None),
    ('submit', 'hands_free_command_executed', None),
    ('press tab', 'hands_free_command_executed', None),
    ('show commands', 'hands_free_command_executed', None),
    ('hide commands', 'hands_free_command_executed', None),
    ('bold', 'hands_free_command_executed', None),
    ('italic', 'hands_free_command_executed', None),
    ('command reference', 'hands_free_command_executed', None),
    ('switch window', 'hands_free_command_executed', None),
    ('I told him to switch window', 'dictate_staged', None),
    ('open', 'hands_free_command_executed', None),
    ('open notepad', 'dictate_staged', None),
    ('find tab', 'hands_free_command_executed', None),
    ('find tab github', 'dictate_staged', None),
    ('show commands', 'dictate_staged', 'An unfinished thought'),
    ('show commands', 'hands_free_command_executed', 'A finished thought.'),
    ('copy', 'hands_free_command_executed', None),
    ('Um, SHOW COMMANDS!', 'hands_free_command_executed', None),
    ('open the window for me', 'dictate_staged', None),
    ('open notepad and volume thirty', 'dictate_staged', None),
    ('please invent a command', 'dictate_staged', None),
]
# 415's default literal-only policy supersedes these older admission rows.
# Keep their existing node IDs so CI can compare every original case.
ROWS = [pytest.param(text, 'dictate_staged' if text in {
    'scroll down', 'page up', 'open', 'find tab', 'Um, SHOW COMMANDS!',
} else expected, previous, id=f'{text}-{expected}-{previous}')
        for text, expected, previous in ROWS]


@pytest.fixture(scope='module')
def production():
    from samsara.commands import apply_phonetic_wash
    definitions = _production.__wrapped__()
    definitions['apply_phonetic_wash'] = apply_phonetic_wash
    return definitions


@pytest.fixture
def lane(production, monkeypatch, registered_executor):
    from samsara import plugin_commands
    executor, registry = registered_executor
    # Execution policy checks the global live registry too; restore the same
    # real declarations cleared by conftest, without bypassing authorization.
    plugin_commands._REGISTRY.update(registry)
    executor._matcher.set_context_provider(lambda: MatchContext.for_app('chrome.exe'))
    effects = []
    # Replace only effect handlers; matching, policy and the production dispatch
    # adapter remain real. No keyboard, mouse, process, window or provider calls.
    monkeypatch.setattr('samsara.commands.get_handler', lambda kind: SimpleNamespace(
        execute=lambda data, context: effects.append(('builtin', data)) or True))
    for entry in set(executor._matcher._entries.values()):
        if entry.handler is not None:
            monkeypatch.setattr(entry, 'handler', lambda app, remainder, phrase=entry.phrase: effects.append(
                (phrase, remainder)) or True)
        monkeypatch.setattr(entry, 'debounce', 0)
    executor._matcher._last_executions.clear()
    app = SimpleNamespace(
        config={}, command_executor=executor, command_mode_active=False,
        _session_mode_manager=None, _log_command_dispatch=Mock(), add_to_history=Mock(),
        _log_history=Mock(), _current_utterance_duration_s=0,
    )
    for name in ('_probe_media_transport', '_probe_hands_free_command', '_command_dispatch_fn'):
        setattr(app, name, MethodType(production[name], app))
    clock = [100.0]
    injected = []

    def make(live=False, exact=True):
        app.config = {'hands_free': {'exact_commands': exact},
                      'ui': {'live_surface': {'enabled': live}}}
        manager = SessionModeManager(
            abort_phrases=['cancel dictation'], foreground_exe_resolver=lambda: 'chrome.exe',
            foreground_hwnd_resolver=lambda: 123, inject_fn=lambda text, *args, **kw: injected.append(text) or True,
            remove_chars_fn=lambda count: True, command_dispatch_fn=app._command_dispatch_fn,
            agent_dispatch_fn=Mock(side_effect=AssertionError('Ava must not receive dictation')),
            buffer_dictate_until_commit=True, hands_free_command_probe_fn=app._probe_hands_free_command,
            fragment_clear_gap_s=2, clock=lambda: clock[0], live_surface_enabled=live,
        )
        manager.reset(SessionMode.DICTATE)
        app._session_mode_manager = manager
        app._ensure_session_mode_manager = lambda: manager
        return manager

    return app, make, clock, effects, injected


@pytest.fixture(scope='module')
def registered_executor():
    # Load real definitions before conftest's function-scoped registry isolation.
    # Effect handlers are patched per row and restored, so other files see no fakes.
    from samsara import plugin_commands
    executor = build_executor()
    return executor, dict(plugin_commands._REGISTRY)


@pytest.mark.parametrize('live', [False, True], ids=['indicator', 'live-surface'])
@pytest.mark.parametrize('text,expected,previous', ROWS)
def test_today_table(lane, live, text, expected, previous, record_property):
    app, make, clock, effects, injected = lane
    manager = make(live)
    if previous:
        manager.dispatch_utterance(previous, SIGNALS)
        clock[0] += .5
    probe = app._probe_hands_free_command(text)
    outcome = manager.dispatch_utterance(text, SIGNALS)
    remainder = app.command_executor._matcher.match(probe.dispatch_text)[1] if probe else None
    path = ('prefix command' if remainder else 'exact command') if effects else 'stage'
    row = dict(text=text, previous=previous, live=live, outcome=outcome.kind, path=path,
               chain=False, ava_miss=False, canonical=outcome.detail.get('phrase'))
    record_property('hands_free_row', json.dumps(row))
    assert outcome.kind == expected
    assert manager.mode is SessionMode.DICTATE
    if outcome.kind == 'hands_free_command_executed':
        assert text.lower() not in manager.dictate_pending_buffer.lower()
        assert text.lower() not in manager.draft_document.text.lower()
        assert outcome_chip(outcome.kind, outcome.detail) == (
            f"→ command: {outcome.detail['phrase']}", 'success')
        assert DictatePreviewSession._is_reserved_hands_free_command(manager, text)
    else:
        assert not effects
        assert text in manager.dictate_pending_buffer


@pytest.mark.parametrize('live', [False, True])
def test_opt_out_preserves_reserved_set(lane, live):
    app, make, clock, effects, injected = lane
    manager = make(live, exact=False)
    assert app._probe_hands_free_command('show commands') is None
    assert manager.dispatch_utterance('show commands', SIGNALS).kind == 'dictate_staged'
    assert not DictatePreviewSession._is_reserved_hands_free_command(manager, 'show commands')
    assert manager.dispatch_utterance('scratch that', SIGNALS).kind == 'scratch_success'
    assert not manager.dictate_pending_buffer
    clock[0] += 3
    assert manager.dispatch_utterance('scroll down', SIGNALS).kind == 'dictate_staged'
    assert manager.dictate_pending_buffer == 'scroll down'
    assert DictatePreviewSession._is_reserved_hands_free_command(manager, 'scratch that')


def test_low_confidence_command_is_refused_without_staging(lane):
    app, make, clock, effects, injected = lane
    manager = make()
    outcome = manager.dispatch_utterance('show commands', UtteranceSignals(False, (1.0,)))
    assert outcome.kind == 'hands_free_command_refused'
    assert not manager.dictate_pending_buffer and not effects


def test_alias_and_config_contract():
    from samsara.config_schema import SETTINGS_SCHEMA
    commands = json.loads((Path(__file__).resolve().parents[1] / 'commands.json').read_text(encoding='utf-8'))
    assert 'command reference' in commands['commands']['show commands'].get('aliases', [])
    assert SETTINGS_SCHEMA['hands_free.exact_commands']['default'] is True


def test_replay_shadow_input_uses_only_selected_synthetic_rows(tmp_path):
    from tools.replay_hands_free_363 import load_rows
    source = tmp_path / 'intent-2099-01-01.jsonl'
    source.write_text('\n'.join(json.dumps(row) for row in [
        {'text': 'Synthetic prose about a window', 'would': 'dictate', 'app': 'chrome.exe'},
        {'text': 'show commands', 'would': 'command:builtin.show_commands', 'app': None},
    ]), encoding='utf-8')
    rows = load_rows(tmp_path)['shadow']
    assert len(rows) == 2
    assert [row['dictation'] for row in rows] == [True, False]


def test_replay_fixture_other_command_is_not_a_dictation_label():
    from tools.replay_hands_free_363 import load_rows
    groups = load_rows()
    # gen_intent_eval's other_command rows reject their named id only.
    other = [row for row in groups['intent_eval'] if row['text'] == 'turn on alarm']
    assert other and not any(row['dictation'] for row in other)
    assert len(groups['398_dictation']) == 40
