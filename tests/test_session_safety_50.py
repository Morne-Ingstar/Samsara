"""Queue 50 (ARC audit5b): session safety patches.

  1. an elevated (higher-integrity) foreground window gets its own outcome,
     earcon and chip -- nothing typed, text retained, never a success sound
  2. a stale inactivity timer whose generation no longer matches does not end
     the session
  3. scratch-that against a slowed target deletes exactly the intended span
  4. a suppressed chunk older than the retype TTL is refused and forgotten

The integrity check is mocked (no elevated process needed). dictation.py is
never imported: its methods under test are compiled from source with ast.
"""
import ast
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import injection_safety as inj
from samsara import session_modes as sm
from samsara.session_modes import SessionModeManager, SessionMode, UtteranceSignals

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _verdict(state):
    return inj.IntegrityVerdict(state, 0x2000, 0x3000 if state == inj.HIGHER else None,
                                "token" if state != inj.UNKNOWN else "none", "test", 0.01)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _manager(*, integrity=None, clock=None, inject_result="typed text", remove_result=True):
    injected, removed = [], []

    def inject_fn(text, guard=None):
        injected.append(text)
        return inject_result

    def remove_chars_fn(n):
        removed.append(n)
        return remove_result

    mgr = SessionModeManager(
        abort_phrases=["cancel"],
        foreground_exe_resolver=lambda: "notepad.exe",
        foreground_hwnd_resolver=lambda: 4242,
        inject_fn=inject_fn,
        remove_chars_fn=remove_chars_fn,
        command_dispatch_fn=lambda text: sm.CommandDispatchResult(matched=False, phrase=None),
        agent_dispatch_fn=lambda text, ctx: None,
        buffer_dictate_until_commit=True,
        window_integrity_fn=integrity,
        clock=clock or _Clock(),
    )
    mgr.reset(SessionMode.DICTATE)
    return mgr, injected, removed


# ---------------------------------------------------------------------------
# 1. elevated foreground window
# ---------------------------------------------------------------------------

def test_commit_into_higher_integrity_window_is_refused_with_its_own_outcome():
    mgr, injected, _ = _manager(integrity=lambda: _verdict(inj.HIGHER))
    mgr._dictate_pending_buffer = "a sentence for task manager"

    outcome = mgr.commit_pending_dictation()

    assert outcome.kind == "dictate_blocked_elevated"
    assert injected == [], "nothing may be typed into a window that silently drops it"
    assert mgr.dictate_pending_buffer == "a sentence for task manager", "the text is retained"
    label, chip_kind = sm.outcome_chip(outcome.kind, outcome.detail)
    assert chip_kind == "error" and "admin window" in label
    assert sm.is_mapped_outcome(outcome.kind)


def test_unbuffered_inject_into_higher_integrity_window_is_suppressed_for_retype():
    mgr, injected, _ = _manager(integrity=lambda: _verdict(inj.HIGHER))
    mgr._buffer_dictate_until_commit = False
    mgr._dictate_target_process = "notepad.exe"

    outcome = mgr._dispatch_dictate("hello there")

    assert outcome.kind == "dictate_blocked_elevated" and injected == []
    assert mgr._stack.peek().extra.get("suppressed") is True


def test_unknown_integrity_still_delivers_but_is_never_a_plain_success():
    mgr, injected, _ = _manager(integrity=lambda: _verdict(inj.UNKNOWN))
    mgr._dictate_pending_buffer = "text"

    outcome = mgr.commit_pending_dictation()

    assert injected == ["text"]
    assert outcome.kind == "dictate_committed" and outcome.detail.get("delivery_unverified") is True
    assert sm.outcome_chip(outcome.kind, outcome.detail) == ("sent, unconfirmed", "warning")


def test_confirmed_ok_window_is_a_normal_success():
    mgr, injected, _ = _manager(integrity=lambda: _verdict(inj.OK))
    mgr._dictate_pending_buffer = "text"
    outcome = mgr.commit_pending_dictation()
    assert outcome.kind == "dictate_committed" and "delivery_unverified" not in outcome.detail
    assert sm.outcome_chip(outcome.kind, outcome.detail) == ("typed", "success")


def test_retype_into_higher_integrity_window_is_refused():
    clock = _Clock()
    mgr, injected, _ = _manager(integrity=lambda: _verdict(inj.HIGHER), clock=clock)
    mgr._stack.push(sm.StackItem(kind="dictation_chunk", payload="kept", mode=SessionMode.DICTATE,
                                 timestamp=clock.t, extra={"suppressed": True, "target_process": "notepad.exe"}))
    assert mgr.retype_last_suppressed() is False and injected == []


# -- dictation.py side: earcon for the new outcome, compiled from source ------

def _dictation_methods(*names):
    tree = ast.parse((ROOT / "dictation.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    found = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names}
    assert set(found) == set(names), set(names) - set(found)
    consts = [n for n in cls.body if isinstance(n, ast.Assign)
              and any(getattr(t, "id", "") == "WINDOW_LOCKED_CHIP" for t in n.targets)]
    harness = ast.ClassDef(name="H", bases=[], keywords=[], body=consts + [found[n] for n in names],
                           decorator_list=[])
    module = ast.Module(body=[harness], type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {"logger": logging.getLogger("test.session50"), "time": __import__("time"),
          "thread_registry": None}
    exec(compile(module, str(ROOT / "dictation.py"), "exec"), ns)
    return ns


def test_blocked_outcome_plays_window_locked_never_success():
    ns = _dictation_methods("_handle_session_dispatch_outcome", "_play_window_locked")
    app = ns["H"]()
    app._sound_cache = {"window_locked": object(), "success": object()}
    app.play_sound = Mock()
    app._show_dispatch_outcome_chip = Mock()
    app._touch_session_activity = Mock()

    app._handle_session_dispatch_outcome(sm.DispatchOutcome(kind="dictate_blocked_elevated",
                                                            detail={"integrity": "test"}), "x")

    sounds = [c.args[0] for c in app.play_sound.call_args_list]
    assert sounds == ["window_locked"]
    app._show_dispatch_outcome_chip.assert_called_once()


def test_window_locked_falls_back_to_error_not_silence():
    ns = _dictation_methods("_play_window_locked")
    app = ns["H"]()
    app._sound_cache = {"error": object()}
    app.play_sound = Mock()
    app._play_window_locked()
    app.play_sound.assert_called_once_with("error")


def test_every_theme_ships_a_window_locked_earcon():
    themes = [p for p in (ROOT / "sounds" / "themes").iterdir() if p.is_dir()]
    assert themes
    for theme in themes:
        assert (theme / "window_locked.wav").is_file(), theme.name


# ---------------------------------------------------------------------------
# 2. timer generations
# ---------------------------------------------------------------------------

class _FakeTimer:
    def __init__(self, fn, args):
        self.fn, self.args, self.cancelled = fn, args, False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # A real Timer that has already started running ignores cancel().
        self.fn(*self.args)


def _timer_app():
    ns = _dictation_methods("_reset_command_mode_inactivity_timer",
                            "_cancel_command_mode_inactivity_timer",
                            "_cancel_command_mode_inactivity_timer_locked",
                            "_on_command_mode_inactivity")
    timers = []
    ns["thread_registry"] = SimpleNamespace(
        timer=lambda name, delay, fn, args=(), daemon=True: timers.append(_FakeTimer(fn, args)) or timers[-1])
    app = ns["H"]()
    app._command_mode_timer_lock = threading.Lock()
    app._command_mode_lock = threading.Lock()
    app._command_mode_inactivity_timer = None
    app._command_mode_inactivity_deadline = None
    app._timer_generation = 0
    app.command_mode_active = True
    app._session_mode_manager = None
    app.exit_command_mode = Mock()
    app.play_sound = Mock()
    return app, timers


def test_stale_timer_whose_generation_no_longer_matches_does_not_exit():
    app, timers = _timer_app()
    app._reset_command_mode_inactivity_timer(300)       # generation A
    stale = timers[-1]
    app._reset_command_mode_inactivity_timer(300)       # speech onset re-armed: generation B
    assert stale.cancelled

    stale.fire()                                        # the cancelled callback ran anyway

    app.exit_command_mode.assert_not_called()


def test_current_timer_still_ends_the_session():
    app, timers = _timer_app()
    app._reset_command_mode_inactivity_timer(300)
    timers[-1].fire()
    app.exit_command_mode.assert_called_once()


def test_cancel_invalidates_an_already_running_timer():
    app, timers = _timer_app()
    app._reset_command_mode_inactivity_timer(300)
    running = timers[-1]
    app._cancel_command_mode_inactivity_timer()         # e.g. hold-suspend / device recovery
    running.fire()
    app.exit_command_mode.assert_not_called()


def test_a_fired_timer_cannot_fire_twice():
    app, timers = _timer_app()
    app._reset_command_mode_inactivity_timer(300)
    timer = timers[-1]
    timer.fire()
    timer.fire()
    assert app.exit_command_mode.call_count == 1


def test_direct_call_without_generation_still_works():
    app, _ = _timer_app()
    app._on_command_mode_inactivity()
    app.exit_command_mode.assert_called_once()


# ---------------------------------------------------------------------------
# 3. scratch-that against a slowed target
# ---------------------------------------------------------------------------

class _SlowLateShiftTarget:
    """A text box that processes keys LATE and samples Shift only when it
    gets to each key (like a remote-desktop or busy target). Keys are queued
    and applied in order after a backlog delay."""

    def __init__(self, text, lag_events=6):
        self.text = text
        self.caret = len(text)
        self.sel_anchor = None
        self.queue = []
        self.shift_down = False          # live key state at the time of sampling
        self.lag = lag_events

    def send(self, events):
        for vk, _scan, flags in events:
            up = bool(flags & 0x0002)
            self.queue.append((vk, up))
            if vk == 0x10:
                self.shift_down = not up
            if len(self.queue) > self.lag:
                self._apply(self.queue.pop(0))
        return True

    def drain(self):
        while self.queue:
            self._apply(self.queue.pop(0))

    def _apply(self, key):
        vk, up = key
        if up:
            return
        if vk == 0x08:                                  # Backspace
            if self.sel_anchor is not None:
                lo, hi = sorted((self.sel_anchor, self.caret))
                self.text = self.text[:lo] + self.text[hi:]
                self.caret, self.sel_anchor = lo, None
            elif self.caret:
                self.text = self.text[:self.caret - 1] + self.text[self.caret:]
                self.caret -= 1
        elif vk == 0x25:                                # Left, Shift sampled NOW (late)
            if self.shift_down:
                self.sel_anchor = self.caret if self.sel_anchor is None else self.sel_anchor
            else:
                self.sel_anchor = None
            self.caret = max(0, self.caret - 1)
        elif vk == 0x2E:                                # Delete
            if self.sel_anchor is not None:
                lo, hi = sorted((self.sel_anchor, self.caret))
                self.text = self.text[:lo] + self.text[hi:]
                self.caret, self.sel_anchor = lo, None
            else:
                self.text = self.text[:self.caret] + self.text[self.caret + 1:]


def test_scratch_that_against_a_slowed_target_deletes_exactly_the_span():
    keep, dictated = "keep this part ", "DELETE ME, this was dictated just now ok"
    target = _SlowLateShiftTarget(keep + dictated, lag_events=12)
    sleeps = []

    deleted, why = inj.delete_backwards(len(dictated), send_input=target.send, sleep=sleeps.append)
    target.drain()

    assert (deleted, why) == (len(dictated), "done")
    assert target.text == keep
    assert sleeps and all(s == inj.DELETE_BATCH_PAUSE_S for s in sleeps), "batches are paced"


def test_the_old_shift_left_idiom_mis_deletes_on_the_same_target():
    """Documents the defect the change removes: Shift+Left pairs sampled late
    turn into bare Left moves, and Delete removes the wrong character."""
    keep, dictated = "keep this part ", "DELETE ME, this was dictated just now ok"
    target = _SlowLateShiftTarget(keep + dictated, lag_events=12)
    VK_SHIFT, VK_LEFT, VK_DELETE, UP = 0x10, 0x25, 0x2E, 0x0002
    for _ in range(len(dictated)):
        target.send([(VK_SHIFT, 0, 0), (VK_LEFT, 0, 0), (VK_LEFT, 0, UP), (VK_SHIFT, 0, UP)])
    target.send([(VK_DELETE, 0, 0), (VK_DELETE, 0, UP)])
    target.drain()
    assert target.text != keep


def test_backspace_events_carry_no_modifier_and_a_scan_code():
    events = inj.build_backspace_events(3)
    assert len(events) == 6 and {vk for vk, _s, _f in events} == {0x08}
    assert all(scan == 0x0E for _vk, scan, _f in events)


def test_scratch_that_stops_when_focus_moves_mid_deletion():
    target = _SlowLateShiftTarget("x" * 40, lag_events=0)
    checks = iter([True, True, False])
    deleted, why = inj.delete_backwards(40, send_input=target.send, sleep=lambda s: None,
                                        still_target=lambda: next(checks, False))
    assert why == "focus_changed" and deleted == 2 * inj.DELETE_BATCH


def test_session_reports_an_incomplete_scratch_as_refused():
    clock = _Clock()
    mgr, _, removed = _manager(clock=clock, remove_result=False)
    mgr._stack.push(sm.StackItem(kind="dictation_chunk", payload="hello", mode=SessionMode.DICTATE,
                                 timestamp=clock.t, extra={"target_process": "notepad.exe", "hwnd": 4242}))
    assert mgr._do_scratch_that() is False and removed == [5]


# ---------------------------------------------------------------------------
# 4. retype TTL
# ---------------------------------------------------------------------------

def _suppressed(mgr, clock, age_s, payload="my password is swordfish"):
    mgr._stack.push(sm.StackItem(kind="dictation_chunk", payload=payload, mode=SessionMode.DICTATE,
                                 timestamp=clock.t - age_s,
                                 extra={"suppressed": True, "target_process": "notepad.exe"}))
    return mgr._stack.peek()


def test_suppressed_chunk_older_than_ttl_is_refused_and_forgotten():
    clock = _Clock()
    mgr, injected, _ = _manager(clock=clock)
    item = _suppressed(mgr, clock, sm.SUPPRESSED_RETYPE_TTL_S + 1)

    assert mgr.retype_last_suppressed() is False
    assert injected == []
    assert item.payload == "" and item.extra.get("expired") is True


def test_suppressed_chunk_within_ttl_is_retyped():
    clock = _Clock()
    mgr, injected, _ = _manager(clock=clock)
    _suppressed(mgr, clock, sm.SUPPRESSED_RETYPE_TTL_S - 1, payload="retype me")
    assert mgr.retype_last_suppressed() is True and injected == ["retype me"]


def test_expired_suppressed_text_is_dropped_at_the_next_utterance():
    clock = _Clock()
    mgr, _, _ = _manager(clock=clock)
    item = _suppressed(mgr, clock, sm.SUPPRESSED_RETYPE_TTL_S + 5)
    mgr.dispatch_utterance("", UtteranceSignals(has_contiguous_speech=True))
    assert item.payload == ""


def test_ttl_is_the_documented_value():
    assert sm.SUPPRESSED_RETYPE_TTL_S == 20.0
