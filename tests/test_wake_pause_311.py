"""Exercise the production dispatch seam without importing the live app."""
import ast
import copy
import logging
import string
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import music
from samsara import command_catalog, config_defaults, plugin_commands
from samsara.command_registry import DispatchResult, DispatchState
from samsara.commands import CommandExecutor
from samsara.session_modes import (
    CommandDispatchResult, HandsFreeCommandMatch, PendingTextPolicy,
    SessionMode, SessionModeManager, UtteranceSignals, match_whole_utterance,
    normalize_utterance, outcome_chip,
)
from samsara.stage_timing import StageTiming


@pytest.fixture(scope="module")
def production():
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    app_class = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "DictationApp")
    methods = {node.name: node for node in app_class.body if isinstance(node, ast.FunctionDef)}
    adapter = copy.deepcopy(next(node for node in ast.walk(methods["_ensure_session_mode_manager"])
                                 if isinstance(node, ast.FunctionDef) and node.name == "_command_dispatch_fn"))
    adapter.args.args.insert(0, ast.arg(arg="self"))
    decoder = methods["_decode_wake_word_buffer"]
    dispatch_body = next(node.body for node in ast.walk(decoder)
                         if isinstance(node, ast.Try) and any(
                             isinstance(child, ast.Assign) and any(
                                 isinstance(target, ast.Name) and target.id == "media_match"
                                 for target in child.targets) for child in node.body))
    start = next(index for index, node in enumerate(dispatch_body)
                 if isinstance(node, ast.Assign) and any(
                     isinstance(target, ast.Name) and target.id == "media_match" for target in node.targets))
    route = ast.parse("def route(self, text):\n    text_lower = text.lower()\n"
                      "    ww_config = self.config['wake_word_config']\n    stage_timing = None\n").body[0]
    route.body.extend(copy.deepcopy(dispatch_body[start:start + 3]))
    namespace = {
        "CommandDispatchResult": CommandDispatchResult, "HandsFreeCommandMatch": HandsFreeCommandMatch,
        "PendingTextPolicy": PendingTextPolicy, "SessionMode": SessionMode, "StageTiming": StageTiming,
        "normalize_utterance": normalize_utterance, "apply_phonetic_wash": lambda text: text,
        "logger": logging.getLogger(__name__), "string": string, "config_defaults": config_defaults,
        "increment_command_count": Mock(), "_is_repeat_blacklisted": lambda *args: True,
        "_WAKE_SESSION_SEND_WORDS": ["over"],
    }
    constants = [node for node in tree.body if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id.startswith("_HANDS_FREE_")
        and ("COMMAND" in target.id or target.id.endswith("PREFIXES")) for target in node.targets)]
    module = ast.Module(body=copy.deepcopy(constants) + [copy.deepcopy(methods[name]) for name in (
        "_probe_media_transport", "_probe_hands_free_command",
        "_handle_session_dispatch_outcome")] + [adapter, route], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace


@pytest.fixture
def app(tmp_path, monkeypatch, production):
    plugin_commands._reinstall_module_commands(music)
    commands = tmp_path / "commands.json"
    commands.write_text('{"commands": {}}', encoding="utf-8")
    instance = SimpleNamespace(
        config={"wake_word_config": {"pause_words": ["pause", "hold on", "wait"],
                                    "resume_words": ["resume", "continue", "go on"]}},
        app_state="long_dictation", _dictation_paused=False, _dictation_require_end=True,
        wake_dictation_buffer=["existing draft"], command_mode_active=False,
        _session_mode_manager=None, listening_indicator=Mock(), _schedule_ui=Mock(),
        play_sound=Mock(), _emit_wake_trace=Mock(), _output_dictation=Mock(),
        _reset_wake_dictation=Mock(), _log_command_dispatch=Mock(), add_to_history=Mock(),
        _log_history=Mock(), inject=Mock(return_value=True), outcomes=[],
        _touch_session_activity=Mock(),
    )
    instance.command_executor = CommandExecutor(commands)
    for name in ("_probe_media_transport", "_probe_hands_free_command", "_command_dispatch_fn", "route"):
        setattr(instance, name, MethodType(production[name], instance))
    manager = SessionModeManager(
        abort_phrases=["cancel dictation"], foreground_exe_resolver=lambda: "editor.exe",
        inject_fn=instance.inject, remove_chars_fn=Mock(return_value=True),
        command_dispatch_fn=instance._command_dispatch_fn, agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=True, hands_free_command_probe_fn=instance._probe_hands_free_command,
        fragment_clear_gap_s=30,
    )
    manager.reset(SessionMode.DICTATE)
    instance._session_mode_manager = manager
    instance._ensure_session_mode_manager = lambda: manager
    instance._show_dispatch_outcome_chip = instance.outcomes.append
    instance._handle_session_dispatch_outcome = MethodType(
        production["_handle_session_dispatch_outcome"], instance)
    instance.transport = Mock(side_effect=lambda action: DispatchResult(
        DispatchState.COMPLETED, phrase=action, detail={"media_action": action, "message": {
            "pause": "paused music", "play": "resumed music", "next": "next track",
            "previous": "previous track"}[action]}))
    monkeypatch.setattr(music, "_media_transport", instance.transport)
    monkeypatch.setattr(music, "_spotify_mute_toggle", Mock(return_value=True))
    return instance


@pytest.mark.parametrize("text,expected", [
    ("pause", "pause"), (" PAUSE! ", "pause"), ("hold on...", "hold on"),
    ("pause music", None), ("unpause music", None), ("I'll continue tomorrow", None),
    ("please pause", None), ("pause, music", None), ("", None),
])
def test_pause_match_is_whole_utterance(text, expected):
    assert match_whole_utterance(text, ["pause", "hold on"]) == expected


@pytest.mark.parametrize("text", ["pause", "Pause!", "hold on..."])
def test_bare_pause_preserves_buffer_and_trace(app, text):
    app.route(text)
    assert app._dictation_paused is True
    assert app.wake_dictation_buffer == ["existing draft"]
    assert any(call.args[0]["stage"] == "pause" for call in app._emit_wake_trace.call_args_list)
    app.transport.assert_not_called()


def test_sentence_with_continue_is_dictated(app):
    app.route("I'll continue tomorrow")
    assert app.wake_dictation_buffer == ["existing draft", "I'll continue tomorrow"]
    assert app._dictation_paused is False


def test_paused_dictation_requires_whole_resume(app):
    app._dictation_paused = True
    app.route("I'll continue tomorrow")
    assert app._dictation_paused is True
    assert app.wake_dictation_buffer == ["existing draft"]
    app.route("Continue!")
    assert app._dictation_paused is False


@pytest.mark.parametrize("state", ["long_dictation", "wake_session", "asleep"])
@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("phrase,action", [
    ("pause music!", "pause"), ("stop music", "pause"), ("unpause music", "play"),
    ("resume music", "play"), ("play music", "play"),
    ("next track", "next"), ("previous track", "previous"),
])
def test_wake_transport_preserves_state_draft_and_indicator(app, state, paused, phrase, action):
    app.app_state, app._dictation_paused = state, paused
    original_buffer = app.wake_dictation_buffer
    app.route(phrase)
    app.transport.assert_called_once_with(action)
    assert app.app_state == state and app._dictation_paused is paused
    assert app.wake_dictation_buffer is original_buffer
    assert original_buffer == ["existing draft"]
    app._output_dictation.assert_not_called()
    app._schedule_ui.assert_not_called()
    assert outcome_chip(app.outcomes[0].kind, app.outcomes[0].detail)[1] == "success"


def test_mute_is_claimed_without_touching_dictation(app):
    app._dictation_paused = True
    app.route("mute")
    music._spotify_mute_toggle.assert_called_once()
    assert app._dictation_paused and app.wake_dictation_buffer == ["existing draft"]


@pytest.mark.parametrize("phrase,action,label", [
    ("pause music", "pause", "paused music"), ("unpause music", "play", "resumed music"),
])
@pytest.mark.parametrize("combined", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_combined_draft_and_media_failure_detail_survive_dispatch(app, combined, failed, phrase, action, label):
    manager = app._session_mode_manager
    signals = UtteranceSignals(True, (1.0,))
    manager.dispatch_utterance("unfinished draft", signals)
    pending = manager.dictate_pending_buffer
    if failed:
        app.transport.side_effect = None
        app.transport.return_value = DispatchResult(
            DispatchState.FAILED, phrase="pause", detail={"reason": "nothing is playing"})
    if combined:
        outcome = manager.dispatch_utterance(phrase, signals)
    else:
        app.route(phrase)
        outcome = app.outcomes[-1]
    app.transport.assert_called_once_with(action)
    assert manager.dictate_pending_buffer == pending
    assert manager.mode is SessionMode.DICTATE
    app.inject.assert_not_called()
    chip_label, kind = outcome_chip(outcome.kind, outcome.detail)
    assert "nothing is playing" in chip_label if failed else chip_label == label
    assert kind == ("error" if failed else "success")


def test_transport_allowlist_is_catalog_backed_and_exact(app, monkeypatch):
    phrases = command_catalog.hands_free_media_phrases()
    assert set(phrases) == {"pause music", "stop music", "unpause music", "resume music",
                            "play music", "next track", "previous track", "mute"}
    for text in ("pause", "resume", "play", "next", "pause music later", "please mute"):
        assert app._probe_media_transport(text) is None
    monkeypatch.setattr(command_catalog, "load_catalog_json", lambda: [])
    command_catalog.hands_free_media_phrases.cache_clear()
    try:
        assert app._probe_media_transport("pause music") is None
    finally:
        command_catalog.hands_free_media_phrases.cache_clear()


def test_dictation_module_is_not_imported():
    assert "dictation" not in sys.modules
