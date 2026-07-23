"""Tests for exclusive voice-mode ownership (2026-07-19 incident root cause,
Fix 1 / P0a): AI-command-mode (left_alt) latched on, then Right-Alt Ava was
allowed to enter ON TOP of it because enter_ava_mode()'s guard omitted
ai_command_mode_active. Ava's clean 157ms ghost-tap exit then cleaned up
only Ava, leaving the AI-command latch nagging indefinitely.

enter_ava_mode() and enter_command_mode() now exit an active Ava command
session FIRST (with its own normal exit feedback) before proceeding, so a
different mode's key being pressed always resolves the overlap instead of
being silently rejected or left to coexist. The opposite direction is
unchanged: enter_ava_command_session() still refuses while Ava/command mode
is active.

Ava Front Door P1: the old ai_command_mode module/attributes/methods this
suite exercised (ai_command_mode_active, enter_ai_command_mode, etc.) were
consolidated into the D3 waterfall session (samsara/ava_command_session.py,
DictationApp.enter_ava_command_session/exit_ava_command_session) -- renamed
in place here since the exclusive-ownership guard being tested is a PRESERVE
item, unchanged in substance.

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
from samsara import ava_command_session as ava_command_session_module


@pytest.fixture(autouse=True)
def _reset_ava_command_session_module_state():
    ava_command_session_module._cancel.clear()
    yield
    ava_command_session_module._cancel.clear()


def _make_app(monkeypatch, command_mode='hold'):
    app = types.SimpleNamespace()

    # Shared mode booleans + locks
    app.command_mode_active = False
    app.ava_mode_active = False
    app.ava_command_session_active = False
    app._command_mode_lock = threading.Lock()
    app._ava_mode_lock = threading.Lock()
    app._ava_cmd_mode_lock = threading.Lock()
    app.recording = False

    # Ava command session fields (mirrors DictationApp.__init__)
    app._ava_cmd_ready = threading.Event()
    app._ava_cmd_ready.set()
    app._ava_cmd_key_held = False
    app._ava_cmd_generation = 0
    # Inactivity timer plumbing is stubbed rather than bound -- this suite
    # asserts on the exclusive-ownership boolean flips and top-level
    # play_sound() calls only, not timer wiring (covered by its own tests).
    app._reset_ava_cmd_inactivity_timer = lambda timeout_s: None
    app._cancel_ava_cmd_inactivity_timer = lambda: None

    app.config = {
        'command_mode': {
            'mode': command_mode,
            'enter_debounce_ms': 0,
            'exit_earcon': False,
            'inactivity_timeout_s': 300,
        },
        'ava_command_session': {'enabled': True, 'key': 'right_ctrl'},
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
    # the spawned bodies would trigger _do_enter_ava_command_session's real
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

    app.enter_ava_command_session = types.MethodType(dictation.DictationApp.enter_ava_command_session, app)
    app._do_enter_ava_command_session = types.MethodType(dictation.DictationApp._do_enter_ava_command_session, app)
    app.exit_ava_command_session = types.MethodType(dictation.DictationApp.exit_ava_command_session, app)

    return app


class TestIncidentOrderRegression:
    """The exact 2026-07-19 sequence: Ava command session active -> Right-
    Alt Ava ghost tap -> the Ava command session must not survive."""

    def test_ava_cmd_active_then_ava_ghost_tap_exits_it(self, monkeypatch):
        app = _make_app(monkeypatch)

        app.enter_ava_command_session()
        assert app.ava_command_session_active is True

        # Right-Alt press: enters Ava, exiting the Ava command session first.
        app.enter_ava_mode()
        assert app.ava_command_session_active is False, \
            "Ava command session must be exited the instant a different mode is entered"
        assert app.ava_mode_active is True

        # Right-Alt release at 157ms (< 200ms debounce) -- ghost tap,
        # exits Ava only. The Ava command session was already exited above, on entry.
        app.exit_ava_mode()

        assert app.ava_command_session_active is False, \
            "Ava command session must not have re-latched after the Ava ghost exit"
        assert app.ava_mode_active is False

    def test_no_ava_cmd_utterance_handling_after_incident_sequence(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ava_command_session()
        app.enter_ava_mode()   # exits the Ava command session first
        app.exit_ava_mode()    # ghost tap

        # The WakeConsumer._flush() routing gate must be closed -- this is
        # the actual mechanism that would otherwise keep transcribing
        # ambient speech into the Ava command session queue.
        assert WakeConsumer._is_ai_cmd_mode(app) is False

    def test_ava_cmd_exit_feedback_played_not_a_silent_teardown(self, monkeypatch):
        """Task requirement: exit_ava_command_session() is called with its
        NORMAL exit feedback, not a bare flag clear."""
        app = _make_app(monkeypatch)
        app.enter_ava_command_session()
        app.play_sound.reset_mock()
        app.enter_ava_mode()
        app.play_sound.assert_any_call('stop')


class TestExitFirstOnAvaEntry:
    def test_ava_entry_while_ava_cmd_inactive_does_not_touch_ava_cmd_state(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ava_mode()
        assert app.ava_mode_active is True
        assert app.ava_command_session_active is False

    def test_ava_entry_is_still_blocked_by_command_mode(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.command_mode_active = True
        app.enter_ava_mode()
        assert app.ava_mode_active is False


class TestExitFirstOnCommandModeEntry:
    def test_hold_command_mode_entry_exits_active_ava_command_session(self, monkeypatch):
        app = _make_app(monkeypatch, command_mode='hold')
        app.enter_ava_command_session()
        assert app.ava_command_session_active is True
        app.enter_command_mode()
        assert app.ava_command_session_active is False
        assert app.command_mode_active is True

    def test_toggle_hands_free_session_entry_exits_active_ava_command_session(self, monkeypatch):
        app = _make_app(monkeypatch, command_mode='toggle')
        app.enter_ava_command_session()
        assert app.ava_command_session_active is True
        app.enter_command_mode()
        assert app.ava_command_session_active is False
        assert app.command_mode_active is True


class TestOppositeDirectionUnchanged:
    """enter_ava_command_session() must still REFUSE while Ava/command mode
    is active -- this task explicitly keeps that half of the guard as-is."""

    def test_ava_cmd_entry_rejected_while_ava_active(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.ava_mode_active = True
        app.enter_ava_command_session()
        assert app.ava_command_session_active is False

    def test_ava_cmd_entry_rejected_while_command_mode_active(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.command_mode_active = True
        app.enter_ava_command_session()
        assert app.ava_command_session_active is False
