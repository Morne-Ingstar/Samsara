"""Tests for AI-command-mode's ghost-tap guard on its toggle key
(2026-07-19 incident report §4, Fix 3 / P1).

Right-Alt Ava has always required a hold of at least
command_mode.enter_debounce_ms (checked on release) before a tap counts
as a real activation. AI-command-mode's toggle key had no such check at
all -- toggling fired unconditionally on the PRESS edge, so a single
accidental tap (report's working theory: the user reaching for Ava's
right_alt and catching an adjacent key) was a successful, silent mode
activation.

The key now toggles on RELEASE, and only if held >= the debounce window
-- symmetric for both activation and deactivation presses, matching
Ava's existing protection and reusing the exact same config constant
(command_mode.enter_debounce_ms).

Exercises the REAL bound _check_command_mode_key/enter_ai_command_mode/
exit_ai_command_mode via types.MethodType against a minimal duck-typed
`self`, with dictation.time.monotonic() replaced by a controllable fake
clock for deterministic hold-duration timing (no real sleeps).
"""
import sys
import threading
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
from pynput.keyboard import Key

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation


class _FakeClock:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeWakeConsumer:
    def __init__(self):
        self._running = False

    def start(self):
        self._running = True

    def stop(self):
        self._running = False
        return []


def _make_app(monkeypatch, debounce_ms=200):
    app = types.SimpleNamespace()

    app.config = {
        'command_mode': {'enabled': False, 'enter_debounce_ms': debounce_ms},
        'ai_command_mode': {'enabled': True, 'key': 'right_ctrl'},
    }

    app.command_mode_active = False
    app.ava_mode_active = False
    app.ai_command_mode_active = False
    app._ai_cmd_mode_lock = threading.Lock()
    app._ai_cmd_key_held = False
    app._ai_cmd_key_press_time = 0.0
    app._ai_cmd_miss_count = 0
    app._ai_cmd_generation = 0
    app._ai_cmd_ready = threading.Event()
    app._ai_cmd_ready.set()

    app._wake_consumer = FakeWakeConsumer()
    app._wake_consumer_reasons = set()
    app._wake_consumer_lock = threading.Lock()
    app._ensure_wake_consumer = types.MethodType(dictation.DictationApp._ensure_wake_consumer, app)
    app._release_wake_consumer = types.MethodType(dictation.DictationApp._release_wake_consumer, app)

    app.play_sound = Mock()

    # No-op the async worker spawn -- irrelevant to this file's timing
    # assertions, and actually running it would trigger a real Ollama
    # warm-up call. _do_enter_ai_command_mode must still exist as an
    # attribute -- evaluated as spawn()'s argument even though unused.
    monkeypatch.setattr(dictation.thread_registry, 'spawn', lambda *a, **k: None)
    app._do_enter_ai_command_mode = lambda: None

    clock = _FakeClock()
    monkeypatch.setattr(dictation.time, 'monotonic', clock)

    app.enter_ai_command_mode = types.MethodType(dictation.DictationApp.enter_ai_command_mode, app)
    app.exit_ai_command_mode = types.MethodType(dictation.DictationApp.exit_ai_command_mode, app)
    app._check_command_mode_key = types.MethodType(dictation.DictationApp._check_command_mode_key, app)

    return app, clock


class TestGhostTapIgnoredOnActivation:
    def test_sub_debounce_tap_does_not_activate(self, monkeypatch, caplog):
        import logging
        app, clock = _make_app(monkeypatch, debounce_ms=200)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(0.157)  # incident's exact Ava ghost-tap duration
        with caplog.at_level(logging.DEBUG, logger="Samsara.dictation"):
            app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ai_command_mode_active is False
        assert any('Ghost tap' in r.message for r in caplog.records)

    def test_at_or_above_debounce_hold_activates(self, monkeypatch):
        app, clock = _make_app(monkeypatch, debounce_ms=200)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(0.2)
        app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ai_command_mode_active is True

    def test_well_above_debounce_hold_activates(self, monkeypatch):
        app, clock = _make_app(monkeypatch, debounce_ms=200)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(1.0)
        app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ai_command_mode_active is True

    def test_press_alone_never_activates(self, monkeypatch):
        """The old bug's exact shape: toggling used to fire on press.
        A press with no release yet must never change mode state."""
        app, clock = _make_app(monkeypatch, debounce_ms=200)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        assert app.ai_command_mode_active is False


class TestGhostTapIgnoredOnDeactivation:
    """Symmetric guard: a sub-debounce tap must not exit an active
    session either, matching the task's explicit requirement."""

    def _enter(self, app, clock, hold_s=1.0):
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(hold_s)
        app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ai_command_mode_active is True

    def test_sub_debounce_tap_does_not_deactivate(self, monkeypatch, caplog):
        import logging
        app, clock = _make_app(monkeypatch, debounce_ms=200)
        self._enter(app, clock)

        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(0.05)
        with caplog.at_level(logging.DEBUG, logger="Samsara.dictation"):
            app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ai_command_mode_active is True, \
            "an accidental tap while active must not exit the session"
        assert any('Ghost tap' in r.message for r in caplog.records)

    def test_at_or_above_debounce_hold_deactivates(self, monkeypatch):
        app, clock = _make_app(monkeypatch, debounce_ms=200)
        self._enter(app, clock)

        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(0.25)
        app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ai_command_mode_active is False


class TestAutoRepeatGuard:
    """Held-key OS auto-repeat must not re-trigger anything, and must not
    reset the original press timestamp used for the hold-duration check."""

    def test_repeat_press_events_while_held_are_ignored(self, monkeypatch):
        app, clock = _make_app(monkeypatch, debounce_ms=200)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)  # real press, arms
        press_time = app._ai_cmd_key_press_time

        clock.advance(0.05)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)  # OS repeat #1
        clock.advance(0.05)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)  # OS repeat #2

        assert app._ai_cmd_key_press_time == press_time, \
            "repeat presses must not reset the original press timestamp"
        assert app.ai_command_mode_active is False, \
            "repeats must never toggle mode state on their own"

    def test_hold_duration_measured_from_first_press_despite_repeats(self, monkeypatch):
        """Repeats must not reset the clock the ghost-tap check reads --
        total held time still counts from the FIRST press."""
        app, clock = _make_app(monkeypatch, debounce_ms=200)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(0.15)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)  # repeat at 150ms
        clock.advance(0.15)  # total: 300ms since the real press
        app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ai_command_mode_active is True, \
            "300ms total hold (from the real press) must clear the 200ms debounce"

    def test_phantom_release_with_no_prior_press_is_ignored(self, monkeypatch):
        app, clock = _make_app(monkeypatch, debounce_ms=200)
        app._check_command_mode_key(Key.ctrl_r, pressed=False)  # no matching press
        assert app.ai_command_mode_active is False

    def test_lost_release_leaves_key_stuck_held_next_press_is_a_noop(self, monkeypatch):
        """Report item 2 (pre-existing, not fixed by this task): if the
        release event is lost, _ai_cmd_key_held stays True and the NEXT
        press is swallowed as auto-repeat -- mode state cannot change
        again until a real release finally arrives. Documented here so a
        future fix has a test to update, not to assert this is desired
        behavior."""
        app, clock = _make_app(monkeypatch, debounce_ms=200)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(1.0)
        # Release is lost -- never delivered.
        assert app.ai_command_mode_active is False  # toggle never fired

        clock.advance(5.0)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)  # user's next real press
        assert app._ai_cmd_key_held is True  # still stuck from before
        assert app.ai_command_mode_active is False, \
            "swallowed as auto-repeat -- known limitation, not this task's fix"
