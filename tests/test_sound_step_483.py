"""483 regression proof: real config assertion, no dictation bootstrap."""
from samsara.log import describe_text, safe_exception
import ast
import collections
import copy
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.config_schema import apply_feedback_level
from samsara.command_registry import DispatchState
from samsara.commands import CommandExecutor
from samsara import execution_policy, plugin_commands

ROOT = Path(__file__).resolve().parents[1]


def config_methods():
    tree = ast.parse((ROOT / "dictation.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    names = {"save_config", "update_config"}
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in methods} == names
    namespace = dict(describe_text=describe_text, safe_exception=safe_exception,
                     json=json, os=os, shutil=shutil, time=time, copy=copy,
                     logger=logging.getLogger(__name__))
    exec(compile(ast.Module(body=methods, type_ignores=[]), "dictation-config-483", "exec"), namespace)
    return namespace


class App:
    def __init__(self, tmp_path, volume=.5):
        self.config = {"sound_volume": volume}
        apply_feedback_level(self.config, "brief")
        self.config_path = tmp_path / "config.json"
        self._config_lock = threading.Lock()
        self.chips, self.sounds, self.speech = [], [], []
        self.audio_coordinator = SimpleNamespace(speak=lambda text, **kw: self.speech.append((text, kw)))
        methods = config_methods()
        self.save_config = methods["save_config"].__get__(self)
        self.update_config = methods["update_config"].__get__(self)
        self.set_feedback_level = None

    def play_sound(self, key):
        self.sounds.append((key, self.config["sound_volume"]))

    def _show_outcome_chip(self, text, *args, **kwargs):
        self.chips.append(text)


@pytest.mark.parametrize("handler,start,end,already", [
    ("handle_sounds_quieter", .5, .4, False),
    ("handle_sounds_louder", .5, .6, False),
    ("handle_sounds_quieter", .05, 0, False),
    ("handle_sounds_louder", .95, 1, False),
    ("handle_sounds_quieter", 0, 0, True),
    ("handle_sounds_louder", 1, 1, True),
])
def test_sound_step_uses_real_locked_save(tmp_path, handler, start, end, already):
    from plugins.commands import feedback_level
    app = App(tmp_path, start)
    with pytest.raises(AssertionError, match="without holding _config_lock"):
        app.save_config()
    assert getattr(feedback_level, handler)(app)
    assert app.config["sound_volume"] == pytest.approx(end)
    assert json.loads(app.config_path.read_text())["sound_volume"] == pytest.approx(end)
    assert app.sounds == [("action_complete", end)]
    assert app.chips == [f"Samsara sounds {int(end * 100)}%" + (" (already)" if already else "")]
    assert not app._config_lock.locked()


@pytest.mark.parametrize("level", ["silent", "brief", "full"])
def test_feedback_fallback_uses_real_locked_save(tmp_path, level):
    from plugins.commands import feedback_level
    app = App(tmp_path)
    app.config["tts"]["engine"] = "winrt"
    assert getattr(feedback_level, "handle_feedback_" + level)(app)
    assert json.loads(app.config_path.read_text())["tts"] == {
        "feedback_level": level, "enabled": level != "silent", "engine": "winrt"}


def test_plugin_exception_emits_one_failure_outcome(tmp_path, monkeypatch, caplog):
    @plugin_commands.command("broken test", risk_class="ui")
    def broken(app, remainder):
        raise RuntimeError("synthetic device failure")

    path = tmp_path / "commands.json"
    path.write_text('{"commands": {}}')
    executor = CommandExecutor(path, plugins_dir=tmp_path / "none")
    monkeypatch.setattr(execution_policy, "authorize", lambda *a, **kw: execution_policy.Allowed())
    app = failure_app(tmp_path)
    with caplog.at_level(logging.INFO):
        result = executor.execute_canonical("broken test", app)
    assert result.state is DispatchState.FAILED
    assert sum("Plugin 'broken test' failed" in r.getMessage() for r in caplog.records) == 1
    assert len(app.chips) == 1
    assert "broken test" in app.chips[0]
    assert "synthetic device failure" not in app.chips[0]
    assert app.sounds == [("error", .5)]
    app.history_db.add.assert_called_once()
    assert app.history_db.add.call_args.kwargs["display_text"] == app.chips[0]


def production_methods(names):
    tree = ast.parse((ROOT / "dictation.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in methods} == names
    from samsara import outcome_ring
    ns = dict(describe_text=describe_text, safe_exception=safe_exception,
              logger=logging.getLogger("483.production"), collections=collections,
              time=time, outcome_ring=outcome_ring)
    exec(compile(ast.Module(body=methods, type_ignores=[]), "dictation-feedback-483", "exec"), ns)
    return ns


def failure_app(tmp_path):
    app = App(tmp_path)
    names = {"_handle_session_dispatch_outcome", "_show_dispatch_outcome_chip", "_show_outcome_chip"}
    for name, method in production_methods(names).items():
        if name in names:
            setattr(app, name, method.__get__(app))
    app.listening_indicator = SimpleNamespace(show_outcome=lambda label, *a: app.chips.append(label))
    app._schedule_ui = lambda fn, *a: fn(*a)
    app.history_db = SimpleNamespace(add=Mock())
    app._touch_session_activity = Mock()
    app._log_command_dispatch = Mock()
    app.add_to_history = Mock()
    app._log_history = Mock()
    app._tutorial_hooks = {"command": Mock()}
    app.command_mode_active = False
    return app


def hold_dispatch():
    """Execute the actual hold result-handling block, without ASR/bootstrap."""
    tree = ast.parse((ROOT / "dictation.py").read_text(encoding="utf-8"))
    lane = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                and ast.unparse(n.test).startswith("is_command_mode or"))
    start = next(i for i, n in enumerate(lane.body) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "stage_timing is not None")
    wrapper = ast.parse("def run(self, text, audio_duration=1.0, stage_timing=None): pass").body[0]
    wrapper.body = lane.body[start:]
    ns = dict(describe_text=describe_text, safe_exception=safe_exception,
              logger=logging.getLogger("483.hold"),
              # The enclosing finalizer freezes provenance before this
              # dispatch block; this slice represents the command hold key.
              ownership=SimpleNamespace(capture_path='command_key'),
              _is_repeat_blacklisted=Mock(return_value=False), increment_command_count=Mock())
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                 "dictation-hold-result-483", "exec"), ns)
    return ns["run"], ns


@pytest.mark.parametrize("level", ["silent", "brief", "full"])
@pytest.mark.parametrize("display", ["compact", "full"])
def test_hold_failed_plugin_reports_once_without_success(tmp_path, monkeypatch, caplog, level, display):
    @plugin_commands.command("broken hold test", risk_class="ui")
    def broken(app, remainder):
        raise RuntimeError("synthetic device failure")

    path = tmp_path / "commands.json"
    path.write_text('{"commands": {}}')
    app = failure_app(tmp_path)
    apply_feedback_level(app.config, level)
    app.config["ui"] = {"listening_display": display, "live_surface": {"enabled": display == "full"}}
    app.command_executor = CommandExecutor(path, plugins_dir=tmp_path / "none")
    monkeypatch.setattr(execution_policy, "authorize", lambda *a, **kw: execution_policy.Allowed())
    run, ns = hold_dispatch()
    with caplog.at_level(logging.INFO):
        run(app, "broken hold test")
    assert len(app.chips) == 1 and "broken hold test" in app.chips[0]
    assert "synthetic device failure" not in app.chips[0] and "Traceback" not in app.chips[0]
    assert app.sounds == [("error", .5)] and not app.speech
    app.history_db.add.assert_called_once()
    assert app.history_db.add.call_args.kwargs["status"] == "error"
    app._log_history.assert_called_once()
    assert app._log_history.call_args.kwargs["status"] == "failed"
    app._log_command_dispatch.assert_called_once_with("broken hold test", "broken hold test", "failed")
    app._tutorial_hooks["command"].assert_not_called()
    ns["increment_command_count"].assert_not_called()
    assert not hasattr(app, "_last_command_name")
    assert sum("outcome=command_failed" in r.getMessage() for r in caplog.records) == 1
    assert sum("Plugin 'broken hold test' failed" in r.getMessage() for r in caplog.records) == 1
