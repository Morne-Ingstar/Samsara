"""Dispatch removal leaves ordinary commands and the Ava substance gate usable."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import dispatch_voice
from samsara.commands import CommandExecutor
from samsara.commands import DispatchState
from samsara.session_modes import SessionModeManager

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def clean_dispatch(monkeypatch):
    def absent(name):
        raise ModuleNotFoundError(name=name)
    monkeypatch.setattr(dispatch_voice, 'import_module', absent)


@pytest.mark.parametrize('text', ['send it', 'send message to the team'])
def test_send_reaches_ordinary_matching(tmp_path, monkeypatch, clean_dispatch, text):
    commands = tmp_path / 'commands.json'
    commands.write_text('{"commands": {}}')
    plugins = tmp_path / 'plugins'
    plugins.mkdir()
    executor = CommandExecutor(commands, plugins_dir=plugins)
    app = SimpleNamespace(command_matching_enabled=True, config={})
    monkeypatch.setattr(executor, '_session_control_effect', lambda *a: None)
    monkeypatch.setattr(executor, '_grammar_enabled', lambda *a: False)
    match = Mock(return_value=(None, ''))
    monkeypatch.setattr(executor._matcher, 'match', match)
    result = executor.process_text(text, app_instance=app, force_commands=True)
    assert result.state is DispatchState.MISS
    match.assert_any_call(text, executor._matcher.current_context())


def test_short_ava_with_dispatch_owner_is_rejected(clean_dispatch):
    class Owner:
        def dispatch(self, *args):
            pytest.fail('noise must not reach the agent')
    manager = SessionModeManager(
        abort_phrases=['cancel'], foreground_exe_resolver=lambda: 'editor.exe',
        inject_fn=Mock(), remove_chars_fn=Mock(), command_dispatch_fn=Mock(),
        agent_dispatch_fn=Owner().dispatch)
    assert manager._dispatch_ava('uh').kind == 'ava_rejected_not_substantive'


def test_shipped_dispatch_imports_exist():
    # Scan source in an export too: no git executable or external files needed.
    checked = []
    for path in ROOT.rglob('*.py'):
        parts = path.relative_to(ROOT).parts
        if (path.name.startswith('local_') or '__pycache__' in parts
                or any(part.startswith('.') for part in parts)):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
            if isinstance(node, ast.ImportFrom) and node.module == 'plugins.commands.dispatch_voice':
                for alias in node.names:
                    assert hasattr(dispatch_voice, alias.name), (str(path), node.lineno, alias.name)
                    checked.append(alias.name)
    assert {'route_ava_utterance', 'is_dispatch_shaped', 'match_send', 'awaiting_follow_up'} <= set(checked)


@pytest.mark.parametrize('name,args', [
    ('route_ava_utterance', (object(), 'send prompt')),
    ('is_dispatch_shaped', ('send 123 to sol',)),
    ('match_send', ('send 123 to sol',)),
    ('awaiting_follow_up', (object(), 'uh')),
])
def test_each_helper_forwards_to_local_plugin(monkeypatch, name, args):
    sentinel = object()
    handler = Mock(return_value=sentinel)
    local = SimpleNamespace(**{name: handler})
    monkeypatch.setattr(dispatch_voice, 'import_module', lambda _: local)
    assert getattr(dispatch_voice, name)(*args) is sentinel
    handler.assert_called_once_with(*args)
