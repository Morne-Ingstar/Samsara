"""Queue 58: a command asked for confirmation nobody could hear, then logged
itself as executed.

2026-09-14, command mode, "show windows":
    [POLICY] CONFIRM show windows route=exact risk=unknown gen=0
    AudioCoordinator: TTS suppressed (55 chars > 50) in command mode
    [SESSION] mode=command outcome=command_executed detail={... 'state': 'queued'}

Covers the four fixes:
  1. an undeclared plugin takes commands_catalog.json's risk on user routes;
     show windows / show numbers declare read themselves
  2. confirmation questions are exempt from command_mode.tts_char_limit
  3. held-for-confirmation is command_awaiting_confirmation, never command_executed
  4. a disabled pack's exact phrase is a miss naming the pack, not "show" + "windows"

Never imports dictation (the app may be running from source).
"""
import ast
import collections
import json
import sys
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins.commands import ask_ollama  # noqa: E402
from samsara import commands as commands_mod, execution_policy as ep, plugin_commands  # noqa: E402
from samsara.command_registry import DispatchState  # noqa: E402
from samsara.commands import CommandExecutor  # noqa: E402
from samsara.execution_policy import Allowed, Invocation, NeedsConfirmation, Route  # noqa: E402
from samsara.session_modes import (  # noqa: E402
    CommandDispatchResult, SessionMode, SessionModeManager, UtteranceSignals,
    chip_ttl_ms, is_mapped_outcome, outcome_chip,
)
from samsara.tts.coordinator import AudioCoordinator  # noqa: E402
from samsara.tts.engine_base import SpeechHandle  # noqa: E402

GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.3,))


@pytest.fixture(autouse=True)
def _clean():
    saved = dict(plugin_commands._REGISTRY)
    saved_modules = {k: dict(v) for k, v in plugin_commands._MODULE_ENTRIES.items()}
    ask_ollama.clear_pending_action()
    ep.clear_catalog_risk_cache()
    yield
    ask_ollama.clear_pending_action()
    ep.clear_catalog_risk_cache()
    plugin_commands._REGISTRY.clear()
    plugin_commands._REGISTRY.update(saved)
    plugin_commands._MODULE_ENTRIES.clear()
    plugin_commands._MODULE_ENTRIES.update(saved_modules)


def _register(phrase, module, calls, **kw):
    def handler(app, remainder):
        calls.append((phrase, remainder))
        return True
    handler.__module__ = module
    handler.__name__ = "handle_" + phrase.replace(" ", "_")
    plugin_commands.command(phrase, **kw)(handler)


def _app(packs=None, generation=0):
    a = types.SimpleNamespace()
    a.config = {"command_packs": packs or {}, "command_mode": {"tts_char_limit": 50}}
    a.audio_coordinator = Mock()
    a.command_mode_active = True
    a._ava_cmd_generation = generation
    a._ava_cmd_mode_lock = threading.Lock()
    a._ava_session_dispatch_queue = collections.deque(maxlen=3)
    a._ava_session_dispatch_lock = threading.Lock()
    a._ava_session_request_in_flight = False
    a._show_outcome_chip = Mock()
    return a


def _executor(tmp_path, app):
    path = tmp_path / "commands.json"
    path.write_text(json.dumps({"commands": {}}), encoding="utf-8")
    ex = CommandExecutor(path, plugins_dir=tmp_path / "no_plugins")
    ex._app = app
    ex.rebuild_matcher()
    app.command_executor = ex
    return ex


# ---------------------------------------------------------------------------
# 1. Risk class
# ---------------------------------------------------------------------------

def _decorator_kwargs(stem, phrase):
    tree = ast.parse((ROOT / "plugins" / "commands" / f"{stem}.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and getattr(dec.func, "id", None) == "command"
                        and ast.literal_eval(dec.args[0]) == phrase):
                    return {k.arg: ast.literal_eval(k.value) for k in dec.keywords}
    raise AssertionError(f"{phrase} not found in {stem}")


@pytest.mark.parametrize("stem,phrase", [("window_switcher", "show windows"),
                                         ("show_numbers", "show numbers")])
def test_overlay_commands_declare_read(stem, phrase):
    assert _decorator_kwargs(stem, phrase).get("risk_class") == "read"


def test_catalog_rates_the_overlays_read():
    assert ep.catalog_risk({"source": "plugins.commands.window_switcher", "phrase": "show windows"}) == "read"
    assert ep.catalog_risk({"source": "plugins.commands.show_numbers", "phrase": "show numbers"}, "show") == "read"
    assert ep.catalog_risk({"source": "tests.nowhere", "phrase": "show windows"}) is None


def test_undeclared_command_with_catalog_read_executes_without_confirmation(monkeypatch):
    calls = []
    _register("peek at things", "plugins.commands.peek", calls)          # no risk_class
    monkeypatch.setattr(ep, "_catalog_index", ({"peek.peek_at_things": "read"}, {}))
    app = _app()
    assert plugin_commands._REGISTRY["peek at things"]["metadata"]["risk_class"] == "unknown"
    assert ep.classify("peek at things")[0] == "read"
    d = ep.authorize(Invocation("peek at things", {}, Route.EXACT, 0), app=app)
    assert isinstance(d, Allowed) and d.risk == "read"
    # A model still sees only what the command itself declares.
    assert ep.classify("peek at things", declared_only=True)[0] == "unknown"


def test_undeclared_command_without_a_catalog_row_still_confirms(monkeypatch):
    _register("mystery verb", "plugins.commands.mystery", [])
    monkeypatch.setattr(ep, "_catalog_index", ({}, {}))
    d = ep.authorize(Invocation("mystery verb", {}, Route.EXACT, 0), app=_app())
    assert isinstance(d, NeedsConfirmation) and d.risk == "unknown"


def test_show_windows_with_no_declaration_runs_without_confirmation_through_the_executor(tmp_path):
    """The live case end to end, against the real commands_catalog.json."""
    calls = []
    _register("show windows", "plugins.commands.window_switcher", calls,
              aliases=["label windows"], pack="window-management")
    app = _app(packs={"window-management": True})
    ex = _executor(tmp_path, app)
    result = ex.process_text("show windows", app, force_commands=True)
    assert result.state is DispatchState.COMPLETED
    assert calls == [("show windows", "")]
    assert ep.pending_operation() is None
    app.audio_coordinator.speak.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Confirmation speech is never truncated away
# ---------------------------------------------------------------------------

def _coordinator(limit=50):
    app = MagicMock()
    app.command_mode_active = True
    app.config = {"tts": {}, "command_mode": {"tts_char_limit": limit}}
    engine = MagicMock()
    engine.speak.return_value = SpeechHandle(utterance_id="spoken")
    engine.get_engine_state.return_value = "idle"
    return AudioCoordinator(app, engine), engine


def test_confirmation_longer_than_the_char_limit_is_spoken():
    coord, engine = _coordinator()
    question = "Show windows? -- say yes to confirm, or say ava cancel."
    assert len(question) > 50
    handle = coord.speak(question, category="confirmation")
    engine.speak.assert_called_once()
    assert engine.speak.call_args[0][0] == question
    assert handle.utterance_id == "spoken"


def test_other_long_command_mode_speech_is_still_suppressed():
    coord, engine = _coordinator()
    coord.speak("x" * 55, category="general")
    engine.speak.assert_not_called()


def test_executor_speaks_the_confirmation_as_category_confirmation(tmp_path, monkeypatch):
    _register("mystery verb", "plugins.commands.mystery", [])
    monkeypatch.setattr(ep, "_catalog_index", ({}, {}))
    app = _app()
    ex = _executor(tmp_path, app)
    ex.process_text("mystery verb", app, force_commands=True)
    assert app.audio_coordinator.speak.call_args.kwargs["category"] == "confirmation"


# ---------------------------------------------------------------------------
# 3. Awaiting confirmation is not executed
# ---------------------------------------------------------------------------

def _manager(dispatch):
    mgr = SessionModeManager(
        abort_phrases=["cancel"],
        foreground_exe_resolver=Mock(return_value="notepad.exe"),
        foreground_hwnd_resolver=Mock(return_value=1),
        inject_fn=Mock(return_value=True),
        remove_chars_fn=Mock(),
        command_dispatch_fn=dispatch,
        agent_dispatch_fn=Mock(),
        clock=lambda: 1000.0,
    )
    mgr.force_mode(SessionMode.COMMAND)
    return mgr


def test_plugin_held_for_confirmation_reports_awaiting(tmp_path, monkeypatch):
    calls = []
    _register("mystery verb", "plugins.commands.mystery", calls)
    monkeypatch.setattr(ep, "_catalog_index", ({}, {}))
    app = _app()
    ex = _executor(tmp_path, app)
    result = ex.process_text("mystery verb", app, force_commands=True)
    assert result.state is DispatchState.QUEUED
    assert result.detail.get("awaiting_confirmation") is True
    assert calls == []


def test_builtin_held_for_confirmation_is_awaiting_not_failed(tmp_path, monkeypatch):
    path = tmp_path / "commands.json"
    path.write_text(json.dumps({"commands": {"quit app": {"type": "hotkey", "keys": ["ctrl", "q"]}}}),
                    encoding="utf-8")
    app = _app()
    ex = CommandExecutor(path, plugins_dir=tmp_path / "no_plugins")
    app.command_executor = ex
    effects = []
    monkeypatch.setattr(commands_mod, "get_handler",
                        lambda _t: types.SimpleNamespace(execute=lambda cmd, ctx: effects.append(cmd) or True))
    result = ex.process_text("quit app", app, force_commands=True)
    assert result.state is DispatchState.QUEUED
    assert result.detail.get("awaiting_confirmation") is True
    assert effects == []


def test_awaiting_confirmation_outcome_is_distinct_and_never_command_executed():
    mgr = _manager(Mock(return_value=CommandDispatchResult(
        matched=True, phrase="show windows", state="queued", awaiting_confirmation=True)))
    outcome = mgr.dispatch_utterance("show windows", GOOD)
    assert outcome.kind == "command_awaiting_confirmation"
    assert outcome.kind != "command_executed"
    assert outcome.detail == {"phrase": "show windows", "state": "queued"}
    assert list(mgr._stack._items) == []    # nothing ran, nothing to undo


def test_queued_without_confirmation_is_still_command_executed():
    mgr = _manager(Mock(return_value=CommandDispatchResult(matched=True, phrase="ask ava", state="queued")))
    assert mgr.dispatch_utterance("ask ava", GOOD).kind == "command_executed"


def test_awaiting_confirmation_chip_is_pending_and_stays_up():
    label, kind = outcome_chip("command_awaiting_confirmation", {"phrase": "show windows"})
    assert kind == "pending" and "show windows" in label and "yes" in label
    assert chip_ttl_ms("command_awaiting_confirmation", kind) is None
    assert is_mapped_outcome("command_awaiting_confirmation")


# ---------------------------------------------------------------------------
# 4. A disabled pack's exact phrase is a miss
# ---------------------------------------------------------------------------

def test_disabled_window_management_show_windows_is_a_miss_naming_the_pack(tmp_path):
    calls = []
    _register("show windows", "plugins.commands.window_switcher", calls,
              aliases=["label windows"], pack="window-management", risk_class="read")
    _register("show numbers", "plugins.commands.show_numbers", calls,
              aliases=["show"], pack="accessibility", risk_class="read")
    app = _app(packs={"window-management": False, "accessibility": True})
    ex = _executor(tmp_path, app)

    result = ex.process_text("show windows", app, force_commands=True)
    assert result.state is DispatchState.MISS
    assert result.detail == {"reason": "pack_disabled", "pack": "window-management"}
    assert calls == []                                 # show numbers did NOT run

    # The enabled bare alias still works on its own.
    assert ex.process_text("show", app, force_commands=True).state is DispatchState.COMPLETED
    assert calls == [("show numbers", "")]


def test_disabled_pack_miss_outcome_and_chip_name_the_pack():
    mgr = _manager(Mock(return_value=CommandDispatchResult(matched=False, disabled_pack="window-management")))
    outcome = mgr.dispatch_utterance("show windows", GOOD)
    assert outcome.kind == "command_miss"
    assert outcome.detail == {"reason": "pack_disabled", "pack": "window-management"}
    label, kind = outcome_chip(outcome.kind, outcome.detail)
    assert "window-management" in label and kind == "warning"
    assert outcome_chip("command_miss", {}) == ("MISS", "error")
