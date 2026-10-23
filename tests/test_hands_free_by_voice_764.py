"""Voice entry uses the key's real session entry; no application bootstrap."""
from types import MethodType, SimpleNamespace
from unittest.mock import Mock
import threading

import numpy as np
import pytest

from plugins.commands import hands_free_voice as voice
from samsara import plugin_commands
from samsara import session_modes
from samsara.command_parser import parse_wake_command
from samsara.command_registry import DispatchState
from samsara.session_modes import (
    CommandDispatchResult, HANDS_FREE_ENTRY_PHRASES, SessionMode,
    SessionModeManager, UtteranceSignals,
)


@pytest.fixture
def rig(monkeypatch):
    # The gate adapter supplies the production class without executing startup.
    import dictation
    from tests.test_wake_opens_session import _app
    plugin_commands._reinstall_module_commands(voice)
    monkeypatch.setattr(session_modes, "DICTATE_COMMIT_PHRASE", "finish")
    monkeypatch.setattr(session_modes, "_DICTATE_COMMIT_HOMOPHONES", frozenset({"finish"}))
    config = {"wake_word_config": {"phrase": "jarvis"},
              "command_mode": {"mode": "toggle", "inactivity_timeout_s": 123}}
    app, log = _app(config, "")
    manager = SessionModeManager(
        abort_phrases=["halt", "cease"], foreground_exe_resolver=lambda: "notepad.exe", foreground_hwnd_resolver=lambda: 111,
        inject_fn=lambda *args: None, remove_chars_fn=Mock(),
        command_dispatch_fn=lambda text: CommandDispatchResult(False),
        agent_dispatch_fn=Mock(), buffer_dictate_until_commit=True,
        on_abort=Mock(),
    )
    app.ava_mode_active = False
    app._command_mode_lock = threading.Lock()
    app._session_mode_manager = manager
    app._ensure_session_mode_manager = Mock(return_value=manager)
    app._reset_command_mode_inactivity_timer = Mock()
    app._ensure_wake_consumer = Mock()
    app._update_mode_overlay = Mock()
    app._update_streaming_preview = Mock()
    app._show_outcome_chip = Mock()
    app.audio_coordinator = SimpleNamespace(speak=Mock())
    app.wake_word_timer = Mock()
    app._do_enter_command_mode = Mock()
    app._dispatch_session_transition = Mock(side_effect=lambda action: action())
    app._start_dictation_mode = Mock()
    app._resolve_command_miss = lambda outcome, text: outcome
    app._handle_session_dispatch_outcome = Mock()
    app._indicator_success_and_reset = Mock()
    app._indicator_reset = Mock()
    app.enter_command_mode = Mock(wraps=MethodType(dictation.DictationApp.enter_command_mode, app))
    app._process_wake_command = MethodType(dictation.DictationApp._process_wake_command, app)
    monkeypatch.setattr(dictation.thread_registry, "spawn", Mock())
    monkeypatch.setattr(dictation, "resample_audio", lambda audio, *args: audio)
    return app, manager


@pytest.mark.parametrize("phrase", sorted(HANDS_FREE_ENTRY_PHRASES))
@pytest.mark.parametrize("same_utterance", [True, False])
def test_wake_phrases_enter_real_key_session(rig, phrase, same_utterance):
    from samsara.audio_engine.wake_consumer import AdmittedWakeBuffer
    app, manager = rig
    transcript = f"Jarvis, {phrase}." if same_utterance else f"{phrase}."
    app.wake_word_triggered = not same_utterance
    app.model.transcribe = lambda *a, **kw: (
        [SimpleNamespace(text=transcript)], SimpleNamespace(language="en"))
    audio = AdmittedWakeBuffer([np.full(1600, .2, dtype=np.float32)],
                              primary_hit=True, profile_hits=())
    app._decode_wake_word_buffer(audio, src_rate=16000, oww_confirmed=True)
    app.enter_command_mode.assert_called_once_with()
    assert app.command_mode_active
    assert manager.mode is SessionMode.DICTATE
    assert not app.wake_word_triggered
    app.wake_word_timer.cancel.assert_called_once_with()
    app._ensure_wake_consumer.assert_called_once_with("toggle_session")
    app._reset_command_mode_inactivity_timer.assert_called_once_with(123)
    app._update_mode_overlay.assert_called_once_with(SessionMode.DICTATE)
    app._update_streaming_preview.assert_called_once_with(SessionMode.DICTATE)
    app._start_dictation_mode.assert_not_called()


def test_dictate_payload_keeps_one_shot(rig):
    app, _ = rig
    app._process_wake_command("dictate hello")
    app._start_dictation_mode.assert_called_once_with("quick_dictation", initial_content="hello")
    app.enter_command_mode.assert_not_called()
    assert parse_wake_command("dictate")["content"] is None


@pytest.mark.parametrize("phrase", ["hands free", "hands-free", "start hands free", "hands free mode"])
def test_command_key_registry_path(rig, phrase):
    app, manager = rig
    match = plugin_commands.find_command(phrase)
    assert match is not None
    # Exercise the registered handler, including aliases and metadata.
    entry = plugin_commands._REGISTRY[phrase]
    assert entry["pack"] == "core" and entry["risk_class"] == "safe"
    result = entry["func"](app, "")
    assert result.state is DispatchState.QUEUED
    assert manager.mode is SessionMode.DICTATE
    app.enter_command_mode.assert_called_once_with()


@pytest.mark.parametrize("phrase", sorted(HANDS_FREE_ENTRY_PHRASES))
def test_already_latched_preserves_draft_and_session(rig, phrase):
    app, manager = rig
    voice.handle_hands_free(app)
    signals = UtteranceSignals(True, (1.0,), True)
    manager.dispatch_utterance("a complete thought", signals)
    pending = manager.dictate_pending_buffer

    def dispatch(text):
        result = voice.handle_hands_free(app)
        return CommandDispatchResult(True, "hands free", result.state, detail=result.detail)

    manager._command_dispatch_fn = dispatch
    outcome = manager.dispatch_utterance(phrase, signals)
    assert outcome.kind == "command_executed"
    assert manager.dictate_pending_buffer == pending
    app.enter_command_mode.assert_called_once_with()
    app._show_outcome_chip.assert_called_with("Already hands-free.", "accent", 3500)
    app.audio_coordinator.speak.assert_called_with("Already hands-free.", category="command")


@pytest.mark.parametrize("exit_word", ["halt", "cease", "go to sleep"])
def test_finish_and_existing_exits(rig, exit_word):
    app, manager = rig
    voice.handle_hands_free(app)
    manager._on_abort = lambda *args: setattr(app, "command_mode_active", False)
    signals = UtteranceSignals(True, (1.0,), True)
    manager.dispatch_utterance("a complete thought", signals)
    assert manager.dictate_pending_buffer
    assert manager.dispatch_utterance("finish", signals).kind == "dictate_committed"
    assert app.command_mode_active
    manager.dispatch_utterance(exit_word, signals)
    assert not app.command_mode_active


def test_hold_mode_is_refused_without_changing_config(rig):
    app, _ = rig
    app.config["command_mode"]["mode"] = "hold"
    assert voice.handle_hands_free(app).state is DispatchState.REJECTED
    assert app.config["command_mode"]["mode"] == "hold"
    assert not app.command_mode_active
    app.enter_command_mode.assert_not_called()


@pytest.mark.parametrize("phrase", ["hands free tomorrow", "dictate mode tomorrow", "dictate hello"])
def test_whole_utterance_only(rig, phrase):
    app, _ = rig
    app._process_wake_command(phrase)
    app.enter_command_mode.assert_not_called()


def test_active_ava_hold_is_refused(rig):
    app, _ = rig
    app.ava_mode_active = True
    assert voice.handle_hands_free(app).state is DispatchState.REJECTED
    app.enter_command_mode.assert_not_called()
