"""Tests for exclusive voice-mode ownership (2026-07-19 incident root cause,
Fix 1 / P0a): AI-command-mode (left_alt) latched on, then Right-Alt Ava was
allowed to enter ON TOP of it because enter_ava_mode()'s guard omitted
ai_command_mode_active. Ava's clean 157ms ghost-tap exit then cleaned up
only Ava, leaving the AI-command latch nagging indefinitely.

enter_ava_mode() and enter_command_mode() now exit an active AI-command
session FIRST (with its own normal exit feedback) before proceeding, so a
different mode's key being pressed always resolves the overlap instead of
being silently rejected or left to coexist. The opposite direction is
unchanged: enter_ai_command_mode() still refuses while Ava/command mode
is active.

Exercises the REAL bound DictationApp methods via types.MethodType against
a minimal duck-typed `self`, same philosophy as
tests/test_wake_consumer_lifecycle.py -- a hand-copied reimplementation
would not reproduce wiring bugs like the incident's.
"""
import sys
import threading
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation
from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara import ai_command_mode as ai_command_mode_module


@pytest.fixture(autouse=True)
def _reset_ai_command_mode_module_state():
    ai_command_mode_module._pending_plan = None
    ai_command_mode_module._cancel.clear()
    yield
    ai_command_mode_module._pending_plan = None
    ai_command_mode_module._cancel.clear()


def _make_app(monkeypatch, command_mode='hold'):
    app = types.SimpleNamespace()

    # Shared mode booleans + locks
    app.command_mode_active = False
    app.ava_mode_active = False
    app.ai_command_mode_active = False
    app._command_mode_lock = threading.Lock()
    app._ava_mode_lock = threading.Lock()
    app._ai_cmd_mode_lock = threading.Lock()
    app.recording = False

    # AI-command-mode fields (mirrors DictationApp.__init__)
    app._ai_cmd_ready = threading.Event()
    app._ai_cmd_ready.set()
    app._ai_cmd_key_held = False

    app.config = {
        'command_mode': {
            'mode': command_mode,
            'enter_debounce_ms': 0,
            'exit_earcon': False,
            'inactivity_timeout_s': 300,
        },
        'ai_command_mode': {'enabled': True, 'key': 'right_ctrl'},
    }

    app.play_sound = Mock()
    app.start_recording = Mock(side_effect=lambda **kw: setattr(app, 'recording', True))
    app.stop_recording = Mock(side_effect=lambda: setattr(app, 'recording', False))

    # Toggle-session plumbing (only exercised when command_mode='toggle')
    app._wake_consumer = None
    app._wake_consumer_reasons = set()
    app._wake_consumer_lock = threading.Lock()
    app.wake_word_triggered = False
    app.process_wake_word_buffer = Mock()
    app._reset_command_mode_inactivity_timer = lambda timeout_s: None
    app._cancel_command_mode_inactivity_timer = lambda: None
    app._ensure_session_mode_manager = lambda: types.SimpleNamespace(reset=lambda **kw: None)
    app._update_mode_overlay = lambda mode: None
    app._update_streaming_preview = lambda mode: None
    app._release_streaming_preview = lambda: None
    app._session_mode_manager = None

    # No-op the async worker spawn entirely -- enter/exit's own boolean
    # flips and top-level play_sound() calls (what this suite asserts on)
    # all happen synchronously before spawn() is reached. Actually running
    # the spawned bodies would trigger _do_enter_ai_command_mode's real
    # Ollama warm-up HTTP call and winsound playback (ava_cues ready
    # cue) -- neither belongs in a unit test.
    monkeypatch.setattr(dictation.thread_registry, 'spawn', lambda *a, **k: None)

    app._ensure_wake_consumer = types.MethodType(dictation.DictationApp._ensure_wake_consumer, app)
    app._release_wake_consumer = types.MethodType(dictation.DictationApp._release_wake_consumer, app)

    app.enter_command_mode = types.MethodType(dictation.DictationApp.enter_command_mode, app)
    app._do_enter_command_mode = types.MethodType(dictation.DictationApp._do_enter_command_mode, app)
    app.exit_command_mode = types.MethodType(dictation.DictationApp.exit_command_mode, app)

    app.enter_ava_mode = types.MethodType(dictation.DictationApp.enter_ava_mode, app)
    app._do_enter_ava_mode = types.MethodType(dictation.DictationApp._do_enter_ava_mode, app)
    app.exit_ava_mode = types.MethodType(dictation.DictationApp.exit_ava_mode, app)

    app.enter_ai_command_mode = types.MethodType(dictation.DictationApp.enter_ai_command_mode, app)
    app._do_enter_ai_command_mode = types.MethodType(dictation.DictationApp._do_enter_ai_command_mode, app)
    app.exit_ai_command_mode = types.MethodType(dictation.DictationApp.exit_ai_command_mode, app)

    return app


class TestIncidentOrderRegression:
    """The exact 2026-07-19 sequence: AI-command-mode active -> Right-Alt
    Ava ghost tap -> AI-command-mode must not survive."""

    def test_ai_active_then_ava_ghost_tap_exits_ai(self, monkeypatch):
        app = _make_app(monkeypatch)

        app.enter_ai_command_mode()
        assert app.ai_command_mode_active is True

        # Right-Alt press: enters Ava, exiting AI-command-mode first.
        app.enter_ava_mode()
        assert app.ai_command_mode_active is False, \
            "AI-command-mode must be exited the instant a different mode is entered"
        assert app.ava_mode_active is True

        # Right-Alt release at 157ms (< 200ms debounce) -- ghost tap,
        # exits Ava only. AI-command-mode was already exited above, on entry.
        app.exit_ava_mode()

        assert app.ai_command_mode_active is False, \
            "AI-command-mode must not have re-latched after the Ava ghost exit"
        assert app.ava_mode_active is False

    def test_no_ai_command_utterance_handling_after_incident_sequence(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ai_command_mode()
        app.enter_ava_mode()   # exits AI-command-mode first
        app.exit_ava_mode()    # ghost tap

        # The WakeConsumer._flush() routing gate must be closed -- this is
        # the actual mechanism that would otherwise keep transcribing
        # ambient speech into the AI-command queue.
        assert WakeConsumer._is_ai_cmd_mode(app) is False

    def test_ai_exit_feedback_played_not_a_silent_teardown(self, monkeypatch):
        """Task requirement: exit_ai_command_mode() is called with its
        NORMAL exit feedback, not a bare flag clear."""
        app = _make_app(monkeypatch)
        app.enter_ai_command_mode()
        app.play_sound.reset_mock()
        app.enter_ava_mode()
        app.play_sound.assert_any_call('stop')


class TestExitFirstOnAvaEntry:
    def test_ava_entry_while_ai_inactive_does_not_touch_ai_state(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ava_mode()
        assert app.ava_mode_active is True
        assert app.ai_command_mode_active is False

    def test_ava_entry_is_still_blocked_by_command_mode(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.command_mode_active = True
        app.enter_ava_mode()
        assert app.ava_mode_active is False


class TestExitFirstOnCommandModeEntry:
    def test_hold_command_mode_entry_exits_active_ai_command_mode(self, monkeypatch):
        app = _make_app(monkeypatch, command_mode='hold')
        app.enter_ai_command_mode()
        assert app.ai_command_mode_active is True
        app.enter_command_mode()
        assert app.ai_command_mode_active is False
        assert app.command_mode_active is True

    def test_toggle_hands_free_session_entry_exits_active_ai_command_mode(self, monkeypatch):
        app = _make_app(monkeypatch, command_mode='toggle')
        app.enter_ai_command_mode()
        assert app.ai_command_mode_active is True
        app.enter_command_mode()
        assert app.ai_command_mode_active is False
        assert app.command_mode_active is True


class TestOppositeDirectionUnchanged:
    """enter_ai_command_mode() must still REFUSE while Ava/command mode is
    active -- this task explicitly keeps that half of the guard as-is."""

    def test_ai_entry_rejected_while_ava_active(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.ava_mode_active = True
        app.enter_ai_command_mode()
        assert app.ai_command_mode_active is False

    def test_ai_entry_rejected_while_command_mode_active(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.command_mode_active = True
        app.enter_ai_command_mode()
        assert app.ai_command_mode_active is False
