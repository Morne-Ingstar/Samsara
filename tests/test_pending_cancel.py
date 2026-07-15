"""Focused tests for context-specific nevermind cancellation."""

import collections
import threading
import time
from unittest.mock import Mock

import pytest

import dictation
from plugins.commands import ask_ollama
from samsara.session_modes import (
    CommandDispatchResult,
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
)


GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))


@pytest.fixture(autouse=True)
def _clear_pending_action():
    ask_ollama.clear_pending_action()
    yield
    ask_ollama.clear_pending_action()


def _set_pending_action():
    with ask_ollama._pending_action_lock:
        ask_ollama._pending_action = {
            "type": "action",
            "command": "close notepad",
            "confirm_text": "Close Notepad?",
            "expires": time.time() + 30,
        }


def _app():
    app = object.__new__(dictation.DictationApp)
    app.audio_coordinator = Mock()
    app.play_sound = Mock()
    app._indicator_reset = Mock()
    app._emit_wake_trace = Mock()
    app._ava_session_dispatch_lock = threading.Lock()
    app._ava_session_dispatch_queue = collections.deque(maxlen=3)
    app._ava_session_request_in_flight = False
    app._start_ava_session_worker = Mock()
    return app


@pytest.mark.parametrize("phrase", ["nevermind", "never mind", "Never mind!"])
def test_jarvis_waiting_exact_cancel_clears_actual_ava_pending_action(phrase):
    app = _app()
    timer = Mock()
    app.wake_word_timer = timer
    app.wake_word_triggered = True
    app.app_state = "command_window"
    _set_pending_action()

    assert app._try_cancel_pending_wake_command(phrase) is True

    assert ask_ollama.get_pending_action() is None
    assert app.wake_word_triggered is False
    assert app.app_state == "asleep"
    assert app.wake_word_timer is None
    timer.cancel.assert_called_once_with()
    app._indicator_reset.assert_called_once_with()


def test_jarvis_waiting_nonsole_phrase_does_not_cancel():
    app = _app()
    app.wake_word_timer = Mock()
    app.wake_word_triggered = True
    app.app_state = "command_window"
    _set_pending_action()

    assert app._try_cancel_pending_wake_command("please never mind that") is False

    assert ask_ollama.get_pending_action() is not None
    assert app.wake_word_triggered is True
    assert app.app_state == "command_window"


def test_right_alt_ava_exact_cancel_bypasses_command_and_agent_routes():
    app = _app()
    app.command_executor = Mock()
    _set_pending_action()

    app._route_to_ava("never mind")

    assert ask_ollama.get_pending_action() is None
    app.command_executor.process_text.assert_not_called()


def test_latched_ava_exact_cancel_consumed_only_while_action_pending():
    app = _app()
    _set_pending_action()

    app._ava_session_agent_dispatch_fn("nevermind", None)

    assert ask_ollama.get_pending_action() is None
    app._start_ava_session_worker.assert_not_called()
    assert app._ava_session_request_in_flight is False


def test_latched_ava_nevermind_without_pending_remains_ordinary_agent_prose():
    app = _app()

    app._ava_session_agent_dispatch_fn("nevermind", None)

    app._start_ava_session_worker.assert_called_once_with("nevermind")
    assert app._ava_session_request_in_flight is True


def test_nevermind_is_not_a_global_substring_abort():
    agent = Mock()
    abort = Mock()
    manager = SessionModeManager(
        abort_phrases=["cancel", "abort", "stop listening"],
        foreground_exe_resolver=lambda: "editor.exe",
        foreground_hwnd_resolver=lambda: 1,
        inject_fn=Mock(),
        remove_chars_fn=Mock(),
        command_dispatch_fn=lambda text: CommandDispatchResult(False),
        agent_dispatch_fn=agent,
        on_abort=abort,
    )
    manager.reset(initial_mode=SessionMode.AVA)

    outcome = manager.dispatch_utterance(
        "I never mind ordinary prose here", GOOD,
    )

    assert outcome.kind == "ava_dispatched"
    agent.assert_called_once_with("I never mind ordinary prose here", None)
    abort.assert_not_called()
