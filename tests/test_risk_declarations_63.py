"""Queue 63: which write-rated commands still ask before running when spoken.

58 made an undeclared plugin take its commands_catalog.json rating on user
routes, and the policy lets `write` run unasked there. The owner's review:

  * must ask -- start print, record my screen, record this window, start
    capture, undo health log. Declared risk_class="destructive" (the only
    class the policy confirms on a spoken route; see execution_policy.authorize).
  * must not ask -- set a timer, take a memo, windows.send, add to list,
    complete task, complete alarm, mark here, note, remind me to, save layout,
    voice memo. Left undeclared: they keep the catalog's `write`.

The real handlers are never imported (printers, screen capture, input hooks):
each test registers a recording stub carrying the plugin file's own decorator
arguments, read from source, under the plugin's module name, and resolves the
risk against the real commands_catalog.json.
"""
import ast
import collections
import json
import sys
import threading
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins.commands import ask_ollama  # noqa: E402
from samsara import execution_policy as ep, plugin_commands  # noqa: E402
from samsara.command_registry import DispatchState  # noqa: E402
from samsara.commands import CommandExecutor  # noqa: E402
from samsara.session_modes import (  # noqa: E402
    CommandDispatchResult, SessionMode, SessionModeManager, UtteranceSignals,
)

GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.3,))

MUST_ASK = [
    ("flashforge_printer", "start print"),
    ("screen_gif", "record my screen"),
    ("screen_gif", "record this window"),
    ("workflow_capture", "start capture"),
    ("health_tracker", "undo health log"),
]

RUNS_DIRECTLY = [
    ("timer", "set a timer"),
    ("quick_memo", "take a memo"),
    ("windows", "send"),
    ("tasks", "add to list"),
    ("tasks", "complete task"),
    ("alarm_commands", "complete alarm"),
    ("text_marker", "mark here"),
    ("smart_actions", "note"),
    ("reminders", "remind me to"),
    ("windows", "save layout"),
    ("voice_memo", "voice memo"),
]


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


def _decorator(stem, spoken):
    """(primary phrase, keyword args) of the @command in plugins/commands/<stem>.py
    whose primary phrase or aliases include `spoken`."""
    tree = ast.parse((ROOT / "plugins" / "commands" / f"{stem}.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and getattr(dec.func, "id", None) == "command"):
                continue
            phrase = ast.literal_eval(dec.args[0])
            kwargs = {k.arg: ast.literal_eval(k.value) for k in dec.keywords}
            if spoken == phrase or spoken in (kwargs.get("aliases") or []):
                return phrase, kwargs
    raise AssertionError(f"{spoken!r} not found in {stem}")


def _register_stub(stem, spoken, calls):
    phrase, kwargs = _decorator(stem, spoken)

    def handler(app, remainder):
        calls.append((phrase, remainder))
        return True
    handler.__module__ = f"plugins.commands.{stem}"
    handler.__name__ = "handle_" + phrase.replace(" ", "_")
    plugin_commands.command(phrase, **kwargs)(handler)
    return phrase


def _app(stem, spoken):
    a = types.SimpleNamespace()
    pack = _decorator(stem, spoken)[1].get("pack", "core")
    a.config = {"command_packs": {pack: True}, "command_mode": {"tts_char_limit": 50}}
    a.audio_coordinator = Mock()
    a.command_mode_active = True
    a._ava_cmd_generation = 0
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


def _session(ex, app):
    """A command-mode session whose dispatch is the executor, converted the
    way dictation.py's command dispatch does."""
    def dispatch(text, *_a, **_k):
        result = ex.process_text(text, app, force_commands=True)
        if result.state is DispatchState.MISS:
            return CommandDispatchResult(matched=False)
        return CommandDispatchResult(
            matched=True, phrase=text, state=result.state.value,
            awaiting_confirmation=bool(result.detail.get("awaiting_confirmation")))

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


@pytest.mark.parametrize("stem,spoken", MUST_ASK)
def test_must_ask_commands_declare_destructive(stem, spoken):
    assert _decorator(stem, spoken)[1].get("risk_class") == "destructive"


@pytest.mark.parametrize("stem,spoken", RUNS_DIRECTLY)
def test_runs_directly_commands_classify_as_write(stem, spoken, tmp_path):
    """Declared non-destructive risks still run without confirmation."""
    phrase = _register_stub(stem, spoken, [])
    # `send` now correctly preserves its declared UI class; all other rows
    # retain their catalog write class. Neither class asks on a spoken route.
    assert ep.classify(phrase)[0] == ("ui" if phrase == "send" else "write")


@pytest.mark.parametrize("stem,spoken", MUST_ASK)
def test_spoken_must_ask_command_awaits_confirmation_and_runs_only_on_yes(stem, spoken, tmp_path, monkeypatch):
    # No catalog to fall back on (the packaged build): the declaration alone asks.
    monkeypatch.setattr(ep, "_catalog_index", ({}, {}))
    calls = []
    phrase = _register_stub(stem, spoken, calls)
    app = _app(stem, spoken)
    ex = _executor(tmp_path, app)

    outcome = _session(ex, app).dispatch_utterance(phrase, GOOD)

    assert outcome.kind == "command_awaiting_confirmation"
    assert calls == []
    assert app.audio_coordinator.speak.call_args.kwargs["category"] == "confirmation"
    pending = ep.pending_operation()
    assert pending is not None and pending.invocation.command_id == phrase
    assert pending.approve(app=app) is True
    assert calls == [(phrase, "")]


@pytest.mark.parametrize("stem,spoken", MUST_ASK)
def test_must_ask_command_rejected_never_runs(stem, spoken, tmp_path):
    calls = []
    phrase = _register_stub(stem, spoken, calls)
    app = _app(stem, spoken)
    ex = _executor(tmp_path, app)
    _session(ex, app).dispatch_utterance(phrase, GOOD)
    ep.pending_operation().reject()
    assert calls == []


@pytest.mark.parametrize("stem,spoken", RUNS_DIRECTLY)
def test_spoken_runs_directly_command_executes_without_asking(stem, spoken, tmp_path):
    calls = []
    phrase = _register_stub(stem, spoken, calls)
    app = _app(stem, spoken)
    ex = _executor(tmp_path, app)

    outcome = _session(ex, app).dispatch_utterance(phrase, GOOD)

    assert outcome.kind == "command_executed"
    assert calls == [(phrase, "")]
    assert ep.pending_operation() is None
    app.audio_coordinator.speak.assert_not_called()


def test_catalog_rows_carry_the_declared_ratings():
    rows = {r["canonical_id"]: r for r in json.loads((ROOT / "commands_catalog.json").read_text(encoding="utf-8"))["commands"]}
    for cid in ("flashforge_printer.start_print", "screen_gif.record_my_screen", "screen_gif.record_this_window",
                "workflow_capture.start_capture", "health_tracker.undo_health_log"):
        assert rows[cid]["risk"] == "destructive", cid
    assert rows["health_tracker.undo_health_log"]["undoable"] is False
    assert rows["quick_ask.ask"]["risk"] == "write"   # reported, not changed
