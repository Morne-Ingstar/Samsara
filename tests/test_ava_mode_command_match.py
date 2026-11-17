"""Targeted regression test: Ava mode (Right Alt hold-to-talk) must try the
command registry BEFORE falling through to the ask_ollama LLM path.

Bug: _route_to_ava() unconditionally spawned an Ava/Ollama worker for every
utterance, so a literal command like "show numbers" -- which matches fine
through the exact same CommandExecutor/CommandMatcher wake word mode uses --
was swallowed whole into a natural-language LLM prompt instead of dispatching
to plugins/commands/show_numbers.py:handle_show_numbers.

This test builds a REAL CommandExecutor/CommandMatcher -- no mocking of the
matcher itself -- so it exercises the actual production registry/dispatch
code, not a re-implementation of it. Per conftest.py's autouse
_isolate_plugin_registry fixture (which blocks CommandExecutor's automatic
plugins/ directory scan so tests don't leak the real plugin set), plugin
commands are brought into the registry the way that fixture's docstring
prescribes: import/reload the real plugin module so its @command decorators
fire against the freshly-cleared registry.
"""
import ast
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

class _FakeApp:
    """Minimal stand-in exposing only what _route_to_ava touches.

    Deliberately NOT a Mock/MagicMock: a MagicMock auto-creates any attribute
    on access (including `notification_manager`), which trips process_text's
    `hasattr(effective_app, 'notification_manager')` reminder-parsing branch
    and blows up trying to unpack a MagicMock as a 2-tuple. A plain object
    that simply lacks the attribute reproduces real DictationApp instances
    without reminders configured.
    """

    def __init__(self, command_executor):
        self.command_executor = command_executor
        self._last_command = None
        self._last_command_name = None
        self.history_calls = []
        self.log_calls = []

    def add_to_history(self, text, is_command=False):
        self.history_calls.append((text, is_command))

    def _log_history(self, **kwargs):
        self.log_calls.append(kwargs)

    def _try_cancel_pending_ava_utterance(self, text):
        return False

    def _try_stop_utterance(self, text, lane):
        return False   # execution policy stop path: not a stop word here


def _build_real_command_executor():
    """Real CommandExecutor with the real "show numbers" plugin registered.

    reload() re-runs plugins/commands/show_numbers.py's @command decorators
    against the registry conftest's autouse fixture just cleared, so the
    executor's matcher picks up the SAME handler function object the app
    uses everywhere else -- nothing hardcoded or duplicated here.
    """
    import plugins.commands.show_numbers as show_numbers
    importlib.reload(show_numbers)

    from samsara.commands import CommandExecutor
    return CommandExecutor(), show_numbers


def _routing_source():
    # 1233: execute the exact routing definition, without desktop bootstrap.
    from samsara.runtime import thread_registry
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8-sig'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    route = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_route_to_ava')
    blacklist = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_is_repeat_blacklisted')
    constants = [n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id in {'_REPEAT_BLACKLIST_NAMES', '_REPEAT_BLACKLIST_TYPES'}
        for t in n.targets)]
    namespace = dict(thread_registry=thread_registry, increment_command_count=Mock())
    exec(compile(ast.Module(body=[*constants, blacklist, route], type_ignores=[]),
                 '<ava-routing-1233>', 'exec'), namespace)
    return SimpleNamespace(DictationApp=SimpleNamespace(_route_to_ava=namespace['_route_to_ava']),
                           thread_registry=thread_registry)


def test_ava_mode_dispatches_show_numbers_command_not_llm():
    dictation = _routing_source()
    ce, show_numbers = _build_real_command_executor()
    fake_app = _FakeApp(ce)

    # Sanity: the registry's "show numbers" entry really is the canonical
    # handler -- proves the fix routes to the EXISTING handler rather than
    # a duplicated/hardcoded copy.
    entry, remainder = ce._matcher.match("show numbers")
    assert entry is not None
    assert entry.phrase == "show numbers"
    assert entry.handler is show_numbers.handle_show_numbers

    # 1233: keep the real handler, with an inert DOM/foreground boundary.
    # Stubbing Qt alone still enumerates the owner's desktop and can launch
    # services. Assert the real handler reaches the fake DOM exactly once.
    from samsara.command_scope import MatchContext
    with patch.object(dictation.thread_registry, 'spawn') as mock_spawn, \
         patch('samsara.command_scope.capture_context', return_value=MatchContext.for_app('synthetic.exe')), \
         patch('plugins.commands.click_by_name.cancel'), \
         patch('win32gui.GetForegroundWindow', return_value=0), \
         patch.object(show_numbers, '_foreground_info', return_value={}), \
         patch.object(show_numbers, '_try_show_dom_numbers', return_value=(True, None)) as dom:
        dictation.DictationApp._route_to_ava(fake_app, "show numbers")
    dom.assert_called_once_with(fake_app)

    # The Ava/Ollama worker must NOT have been spawned -- the command
    # matched and dispatched directly instead of falling through to the LLM.
    spawned_names = [call.args[0] for call in mock_spawn.call_args_list]
    assert "Ava-worker" not in spawned_names

    # Bookkeeping updated as a real matched command (mirrors the wake-word
    # and toggle-command-mode dispatch sites).
    assert fake_app._last_command_name == "show numbers"
    assert fake_app.history_calls == [("show numbers", True)]


def test_ava_mode_falls_through_to_llm_for_unmatched_text():
    dictation = _routing_source()
    ce, _show_numbers = _build_real_command_executor()
    fake_app = _FakeApp(ce)

    from samsara.command_scope import MatchContext
    with patch.object(dictation.thread_registry, 'spawn') as mock_spawn, \
         patch('samsara.command_scope.capture_context', return_value=MatchContext.for_app('synthetic.exe')):
        dictation.DictationApp._route_to_ava(
            fake_app, "what is the capital of france")

    # Genuinely unmatched text must still fall through to Ava, unchanged.
    spawned_names = [call.args[0] for call in mock_spawn.call_args_list]
    assert "Ava-worker" in spawned_names
    assert fake_app._last_command_name is None
    assert fake_app.history_calls == []


if __name__ == "__main__":
    test_ava_mode_dispatches_show_numbers_command_not_llm()
    print("test_ava_mode_dispatches_show_numbers_command_not_llm: PASS")
    test_ava_mode_falls_through_to_llm_for_unmatched_text()
    print("test_ava_mode_falls_through_to_llm_for_unmatched_text: PASS")
