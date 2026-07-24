"""Tests for the Ava command session's Left-Alt tap-toggle debounce.

2026-07-23 G3 live-test finding: D3 (Left-Alt) is a TAP-to-toggle control
-- a clean, fast press-release IS the intended activation/deactivation
gesture -- NOT a hold control like Right-Alt Ava. The 2026-07-19 incident's
Fix 3 / P1 ghost-tap guard (reusing Right-Alt's >=200ms
command_mode.enter_debounce_ms hold requirement) was carried over into D3
"verbatim per spec" without noticing this distinction, and silently ate
legitimate fast taps as a result -- log-confirmed: "Ghost tap (188ms) —
ignored" for what was a real, intentional toggle attempt.

Fix: the Left-Alt session now uses its own _AVA_CMD_TAP_DEBOUNCE_MS
(~40ms) floor instead of command_mode.enter_debounce_ms -- just enough to
reject genuine keyboard-hardware contact bounce / phantom double-fire,
not human tap speed. Right-Alt Ava's own >=200ms HOLD requirement (a
DIFFERENT code path, _ava_mode_key handling, not exercised by this file)
is unchanged -- see dictation.py's _check_command_mode_key.

Ava Front Door P1: migrated from tests/test_ai_command_mode_ghost_tap.py
(deleted) -- ai_command_mode.py was replaced by
samsara/ava_command_session.py + DictationApp.enter_ava_command_session/
exit_ava_command_session, but this guard lives in the SAME method
(_check_command_mode_key) it always did, now with the D3 "surgical Alt
guard" (spec-new) checked first. That new guard only force-exits an
ALREADY-active session on a NON-session key, so it does not interfere
with any assertion below (every event here uses the session's own
configured key).

Exercises the REAL bound _check_command_mode_key/enter_ava_command_session/
exit_ava_command_session via types.MethodType against a minimal duck-typed
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


def _make_app(monkeypatch):
    app = types.SimpleNamespace()

    # command_mode.enter_debounce_ms is left at the default here only
    # because the config shape expects the key to exist -- the Left-Alt
    # tap-toggle path no longer reads it at all (see dictation.py's
    # _AVA_CMD_TAP_DEBOUNCE_MS); it's set to an implausibly large value
    # so any test that accidentally started reading it again would fail
    # loudly instead of silently passing.
    app.config = {
        'command_mode': {'enabled': False, 'enter_debounce_ms': 200000},
        'ava_command_session': {'enabled': True, 'key': 'right_ctrl'},
    }

    app.command_mode_active = False
    app.ava_mode_active = False
    app.ava_command_session_active = False
    app._ava_cmd_mode_lock = threading.Lock()
    app._ava_cmd_key_held = False
    app._ava_cmd_key_press_time = 0.0
    app._ava_cmd_miss_count = 0
    app._ava_cmd_generation = 0
    app._ava_cmd_ready = threading.Event()
    app._ava_cmd_ready.set()
    # Inactivity timer plumbing is stubbed rather than bound -- this file
    # asserts on tap-debounce hold-duration timing only, not timer wiring.
    app._reset_ava_cmd_inactivity_timer = lambda timeout_s: None
    app._cancel_ava_cmd_inactivity_timer = lambda: None

    app._wake_consumer = FakeWakeConsumer()
    app._wake_consumer_reasons = set()
    app._wake_consumer_lock = threading.Lock()
    app._ensure_wake_consumer = types.MethodType(dictation.DictationApp._ensure_wake_consumer, app)
    app._release_wake_consumer = types.MethodType(dictation.DictationApp._release_wake_consumer, app)

    app.play_sound = Mock()

    # No-op the async worker spawn -- irrelevant to this file's timing
    # assertions, and actually running it would trigger a real Ollama
    # warm-up call. _do_enter_ava_command_session must still exist as an
    # attribute -- evaluated as spawn()'s argument even though unused.
    monkeypatch.setattr(dictation.thread_registry, 'spawn', lambda *a, **k: None)
    app._do_enter_ava_command_session = lambda: None

    clock = _FakeClock()
    monkeypatch.setattr(dictation.time, 'monotonic', clock)

    app.enter_ava_command_session = types.MethodType(dictation.DictationApp.enter_ava_command_session, app)
    app.exit_ava_command_session = types.MethodType(dictation.DictationApp.exit_ava_command_session, app)
    app._check_command_mode_key = types.MethodType(dictation.DictationApp._check_command_mode_key, app)

    return app, clock


class TestFastTapsToggleTheSession:
    """The task's explicit requirement: an 80ms and a 150ms tap both
    toggle the session -- these used to be silently swallowed as "ghost
    taps" under the old 200ms (Right-Alt-hold-derived) floor."""

    def test_80ms_tap_activates(self, monkeypatch):
        app, clock = _make_app(monkeypatch)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(0.080)
        app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ava_command_session_active is True

    def test_150ms_tap_activates(self, monkeypatch):
        """188ms is the exact incident duration logged as a rejected
        "ghost tap" -- 150ms is comfortably inside that same "clearly a
        real, fast tap" range."""
        app, clock = _make_app(monkeypatch)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(0.150)
        app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ava_command_session_active is True

    def test_80ms_tap_also_deactivates_an_active_session(self, monkeypatch):
        app, clock = _make_app(monkeypatch)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(1.0)
        app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ava_command_session_active is True

        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(0.080)
        app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ava_command_session_active is False


class TestGenuineContactBounceStillIgnored:
    """The floor still exists -- it's just far below human tap speed now
    (~40ms, not 200ms): a sub-floor duration is real hardware contact
    bounce / phantom double-fire, not an intentional tap."""

    def test_sub_floor_tap_does_not_activate(self, monkeypatch, caplog):
        import logging
        app, clock = _make_app(monkeypatch)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(0.020)
        with caplog.at_level(logging.DEBUG, logger="Samsara.dictation"):
            app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ava_command_session_active is False
        assert any('Ghost tap' in r.message for r in caplog.records)

    def test_sub_floor_tap_does_not_deactivate(self, monkeypatch, caplog):
        import logging
        app, clock = _make_app(monkeypatch)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(1.0)
        app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ava_command_session_active is True

        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(0.020)
        with caplog.at_level(logging.DEBUG, logger="Samsara.dictation"):
            app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ava_command_session_active is True, \
            "a sub-floor tap while active must not exit the session"
        assert any('Ghost tap' in r.message for r in caplog.records)

    def test_well_above_floor_hold_activates(self, monkeypatch):
        app, clock = _make_app(monkeypatch)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(1.0)
        app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ava_command_session_active is True

    def test_press_alone_never_activates(self, monkeypatch):
        """The old bug's exact shape: toggling used to fire on press.
        A press with no release yet must never change mode state."""
        app, clock = _make_app(monkeypatch)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        assert app.ava_command_session_active is False


class TestAutoRepeatGuard:
    """Held-key OS auto-repeat must not re-trigger anything, and must not
    reset the original press timestamp used for the hold-duration check."""

    def test_repeat_press_events_while_held_are_ignored(self, monkeypatch):
        app, clock = _make_app(monkeypatch)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)  # real press, arms
        press_time = app._ava_cmd_key_press_time

        clock.advance(0.05)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)  # OS repeat #1
        clock.advance(0.05)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)  # OS repeat #2

        assert app._ava_cmd_key_press_time == press_time, \
            "repeat presses must not reset the original press timestamp"
        assert app.ava_command_session_active is False, \
            "repeats must never toggle mode state on their own"

    def test_hold_duration_measured_from_first_press_despite_repeats(self, monkeypatch):
        """Repeats must not reset the clock the tap-debounce check reads --
        total held time still counts from the FIRST press."""
        app, clock = _make_app(monkeypatch)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(0.015)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)  # repeat at 15ms
        clock.advance(0.015)  # total: 30ms since the real press -- still sub-floor
        app._check_command_mode_key(Key.ctrl_r, pressed=False)
        assert app.ava_command_session_active is False, \
            "30ms total hold (from the real press) is still below the ~40ms floor"

    def test_phantom_release_with_no_prior_press_is_ignored(self, monkeypatch):
        app, clock = _make_app(monkeypatch)
        app._check_command_mode_key(Key.ctrl_r, pressed=False)  # no matching press
        assert app.ava_command_session_active is False

    def test_lost_release_leaves_key_stuck_held_next_press_is_a_noop(self, monkeypatch):
        """Report item 2 (pre-existing, not fixed by this task): if the
        release event is lost, _ava_cmd_key_held stays True and the NEXT
        press is swallowed as auto-repeat -- mode state cannot change
        again until a real release finally arrives. Documented here so a
        future fix has a test to update, not to assert this is desired
        behavior."""
        app, clock = _make_app(monkeypatch)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)
        clock.advance(1.0)
        # Release is lost -- never delivered.
        assert app.ava_command_session_active is False  # toggle never fired

        clock.advance(5.0)
        app._check_command_mode_key(Key.ctrl_r, pressed=True)  # user's next real press
        assert app._ava_cmd_key_held is True  # still stuck from before
        assert app.ava_command_session_active is False, \
            "swallowed as auto-repeat -- known limitation, not this task's fix"
