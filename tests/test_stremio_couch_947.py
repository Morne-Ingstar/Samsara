"""Couch command repairs with the live router and inert desktop effects."""
import ast
import collections
import logging
import time
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import outcome_ring
from samsara.command_registry import DispatchState
from samsara.command_scope import MatchContext
from samsara.history import HistoryManager
from samsara.live_surface.feedback import remember_feedback
from samsara.session_modes import DispatchOutcome
from tests.test_builtin_vocabulary_801 import store
from tests.test_several_commands_399 import rig


@pytest.mark.parametrize("exe", ["stremio-shell-ng.exe", "stremio.exe"])
@pytest.mark.parametrize("heard,expected", [
    ("Paul Stremio", "pause stremio"),
    ("pull stremio", "pause stremio"),
    ("postremia", "pause stremio"),
    ("pause stremio", "pause stremio"),
    ("pazdremium", "pause stremio"),
    ("paz strenio", "pause stremio"),
    ("Resume Strami out", "resume stremio"),
    ("resume stremia", "resume stremio"),
    ("play stream io", "resume stremio"),
    ("pause stremio's", "pause stremio"),
    ("pause strenio", "pause stremio"),
    ("pause", "pause stremio"), ("resume", "resume stremio"),
    ("play", "resume stremio"), ("unpause", "resume stremio"),
    ("skip forward", "skip forward"), ("skip back", "skip back"),
    ("skip forward stremio", "skip forward"),
    ("skip forward ten", "skip forward"),
], ids=lambda _: "case")
def test_couch_routes(rig, exe, heard, expected):
    executor, app, calls = rig
    executor._matcher.set_context_provider(lambda: MatchContext.for_app(exe))
    executor._matcher.set_enabled_packs({"core", "media", "stremio"})
    result = executor.process_text(heard, force_commands=True)
    assert result.state is DispatchState.COMPLETED
    assert result.phrase == expected
    assert [phrase for phrase, _ in calls] == [expected]


@pytest.mark.parametrize("exe", ["spotify.exe", "Music.UI.exe", None])
@pytest.mark.parametrize("heard", ["play", "resume"], ids=lambda _: "case")
def test_music_keeps_bare_transport(rig, exe, heard):
    executor, app, calls = rig
    context = MatchContext.for_app(exe) if exe else MatchContext.unresolved("test")
    executor._matcher.set_context_provider(lambda: context)
    executor._matcher.set_enabled_packs({"core", "media", "stremio"})
    assert executor.process_text(heard, force_commands=True).phrase == "play"
    assert [phrase for phrase, _ in calls] == ["play"]


@pytest.mark.parametrize("heard", ["resume strami out", "pause strenio", "postremia"], ids=lambda _: "case")
def test_explicit_stremio_repairs_outside_foreground(rig, heard):
    executor, app, calls = rig
    executor._matcher.set_enabled_packs({"core", "media", "stremio"})
    result = executor.process_text(heard, force_commands=True)
    assert result.phrase in {"resume stremio", "pause stremio"}
    assert all(phrase != "play" for phrase, _ in calls)


@pytest.mark.parametrize("heard", [
    "postremia is a strange word", "pause the discussion please",
    "skip forward ten chapters", "volume down the street",
    "Paul Stremio wrote a letter", "resume streaming documentaries",
], ids=lambda _: "case")
def test_prose_is_not_a_stremio_command(rig, heard):
    executor, app, calls = rig
    executor._matcher.set_context_provider(lambda: MatchContext.for_app("stremio-shell-ng.exe"))
    executor._matcher.set_enabled_packs({"core", "media", "stremio"})
    executor.process_text(heard, force_commands=True)
    assert all("stremio" not in phrase and not phrase.startswith("skip ") for phrase, _ in calls)


@pytest.mark.parametrize("heard", ["postremia", "resume strami out", "pause", "skip forward ten"], ids=lambda _: "case")
def test_disabled_pack_cannot_be_repaired(rig, heard):
    executor, app, calls = rig
    executor._matcher.set_context_provider(lambda: MatchContext.for_app("stremio-shell-ng.exe"))
    executor._matcher.set_enabled_packs({"core", "media"})
    executor.process_text(heard, force_commands=True)
    assert all("stremio" not in phrase and not phrase.startswith("skip ") for phrase, _ in calls)


def test_stremio_hints_use_existing_command_prompt_only(tmp_path):
    dictionary = store(tmp_path, {"command_packs": {"stremio": True}})
    assert "stremio" in dictionary.get_initial_prompt().lower()
    assert dictionary.get_initial_prompt(False) is None
    dictionary.app.config["command_packs"]["stremio"] = False
    assert "stremio" not in dictionary.get_initial_prompt().lower()


def _bind_chip_methods(app):
    # Compile only the two production methods; never import the live app module.
    tree = ast.parse((Path(__file__).parents[1] / "dictation.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name in {"_show_outcome_chip", "_show_dispatch_outcome_chip"}]
    namespace = dict(collections=collections, time=time, outcome_ring=outcome_ring,
                     logger=logging.getLogger(__name__))
    exec(compile(ast.Module(body=methods, type_ignores=[]), "chip_methods", "exec"), namespace)
    for method in methods:
        setattr(app, method.name, MethodType(namespace[method.name], app))


@pytest.mark.parametrize("display", ["missing", "legacy", "modern"], ids=lambda _: "case")
def test_miss_history_survives_missing_or_dropped_display(tmp_path, rig, display):
    executor, app, calls = rig
    heard = "synthetic unmatched command with an intentionally long tail for recovery"
    assert executor.process_text(heard, force_commands=True).state is DispatchState.MISS
    history = HistoryManager(tmp_path / "history.db")
    app.history_db = history
    app._schedule_ui = Mock()  # UI delivery is deliberately never run.
    if display == "modern":
        class Indicator:
            _records_feedback = True
            def show_outcome(self, *args): pass
            def show_feedback(self, *args): pass
        app.listening_indicator = Indicator()
    elif display == "legacy":
        app.listening_indicator = SimpleNamespace(show_outcome=Mock())
    _bind_chip_methods(app)
    app._show_dispatch_outcome_chip(DispatchOutcome("command_miss", {"heard": heard}))
    rows = history._conn.execute("SELECT * FROM history").fetchall()
    assert len(rows) == 1
    assert rows[0]["entry_type"] == "feedback"
    assert rows[0]["raw_text"] == heard
    if display == "modern":
        # Even if the modern display later accepts the event, retain it once.
        _fn, label, _kind, _ttl, feedback = app._schedule_ui.call_args.args
        remember_feedback(app, label, feedback)
        assert history._conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 1
    history._conn.close()


def test_windows_alias_targets_shell_build():
    from plugins.commands.windows import APP_ALIASES
    assert APP_ALIASES["stremio"] == "stremio-shell-ng.exe"


def test_window_lookup_keeps_both_builds(monkeypatch):
    from plugins.commands import windows
    names = {1: "stremio-shell-ng.exe", 2: "stremio.exe", 3: "stremio-runtime.exe"}
    monkeypatch.setattr(windows, "_WINDOWS_AVAILABLE", True)
    monkeypatch.setattr(windows, "is_valid_app_window", lambda *_: True)
    monkeypatch.setattr(windows.win32gui, "EnumWindows",
                        lambda callback, arg: [callback(hwnd, arg) for hwnd in names], raising=False)
    monkeypatch.setattr(windows.win32process, "GetWindowThreadProcessId",
                        lambda hwnd: (0, hwnd), raising=False)
    monkeypatch.setattr(windows.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: names[pid]))
    assert windows.find_windows_by_app("stremio") == [1, 2]


@pytest.mark.parametrize("blocked", [False, True])
def test_real_modern_adapter_never_duplicates_or_loses_miss(tmp_path, monkeypatch, blocked):
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.feedback import schedule_feedback, Feedback, FeedbackKind, FeedbackPurpose
    monkeypatch.setattr("samsara.live_surface.controller.qt_runtime.post", lambda *_: None)
    history = HistoryManager(tmp_path / "history.db")
    app = SimpleNamespace(config={}, history_db=history, _schedule_ui=lambda fn, *args: fn(*args))
    controller = LiveSurfaceController(app)
    app.listening_indicator = LiveSurfaceIndicatorAdapter(controller)
    if blocked:
        app.listening_indicator.show_feedback("Synthetic active question", "pending", None,
            Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, owner="synthetic"))
        controller.drain()
    schedule_feedback(app, "Synthetic unmatched speech", "error", 4000,
                      source="command_miss", detail={"heard": "synthetic unheard request"})
    controller.drain()
    rows = history._conn.execute("SELECT * FROM history").fetchall()
    assert len(rows) == 1
    assert rows[0]["raw_text"] == "synthetic unheard request"
    history._conn.close()


def test_hands_free_probe_uses_same_repair(rig):
    from samsara.phonetic_wash import prepare_hands_free_command
    executor, app, calls = rig
    executor._matcher.set_context_provider(lambda: MatchContext.for_app("stremio-shell-ng.exe"))
    executor._matcher.set_enabled_packs({"core", "media", "stremio"})
    repaired, reserved = prepare_hands_free_command("resume strami out", app)
    assert repaired == "resume stremio" and reserved is None
