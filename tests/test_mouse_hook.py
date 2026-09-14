"""Tests for the Win32 low-level mouse hook (samsara/mouse_hook.py).

All Win32 API calls are mocked -- no actual hook is installed.

32 (2026-09-13): the hook callback only enqueues. on_button_event runs on the
dispatcher thread; tests deliver with hook.dispatch_pending() or a real
dispatcher thread, and measure that a slow handler never delays the callback.
"""

import ctypes
import ctypes.wintypes
import logging
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import samsara.mouse_hook as mh  # noqa: E402
from samsara.mouse_hook import (  # noqa: E402
    MouseHook,
    MSLLHOOKSTRUCT,
    WM_XBUTTONDOWN,
    WM_XBUTTONUP,
    XBUTTON1,
    XBUTTON2,
    WH_MOUSE_LL,
)

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lp_param(xbutton: int) -> ctypes.POINTER(MSLLHOOKSTRUCT):
    """Build a ctypes pointer to MSLLHOOKSTRUCT for a given X button."""
    info = MSLLHOOKSTRUCT()
    info.pt.x = 200
    info.pt.y = 300
    info.mouseData = ctypes.c_ulong(xbutton << 16).value
    return ctypes.pointer(info)


def _make_hook(on_event=None, suppress='mouse4', **kw):
    cb = on_event or MagicMock()
    hook = MouseHook(on_button_event=cb, suppress_button=suppress, **kw)
    # Pretend the hook is installed so CallNextHookEx doesn't crash
    hook._hook_id = 999
    return hook, cb


def _fire(hook, w_param, xbutton=XBUTTON1, n_code=0):
    with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0):
        return hook._hook_callback(n_code, w_param, _make_lp_param(xbutton))


# ---------------------------------------------------------------------------
# Callback: event routing (delivered by the dispatcher, never inline)
# ---------------------------------------------------------------------------

class TestHookCallback:

    @pytest.mark.parametrize("w_param, xbutton, expected", [
        (WM_XBUTTONDOWN, XBUTTON1, ('mouse4', True)),
        (WM_XBUTTONUP, XBUTTON1, ('mouse4', False)),
        (WM_XBUTTONDOWN, XBUTTON2, ('mouse5', True)),
        (WM_XBUTTONUP, XBUTTON2, ('mouse5', False)),
    ])
    def test_xbutton_events_are_queued_then_delivered(self, w_param, xbutton, expected):
        hook, cb = _make_hook(suppress=None)
        _fire(hook, w_param, xbutton)
        cb.assert_not_called()                       # nothing runs on the hook thread
        assert hook.dispatch_pending() == 1
        cb.assert_called_once_with(*expected)

    def test_queued_item_is_button_pressed_timestamp(self):
        hook, _cb = _make_hook(suppress=None)
        before = time.perf_counter()
        _fire(hook, WM_XBUTTONDOWN, XBUTTON2)
        name, pressed, stamp, injected = hook._events.get_nowait()
        assert (name, pressed, injected) == ('mouse5', True, False)
        assert before <= stamp <= time.perf_counter()

    def test_non_xbutton_event_does_not_queue(self):
        hook, cb = _make_hook()
        _fire(hook, WM_MOUSEMOVE, 0)
        assert hook.dispatch_pending() == 0
        cb.assert_not_called()

    def test_non_xbutton_event_calls_next_hook(self):
        hook, cb = _make_hook()
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0) as mock_next:
            hook._hook_callback(0, WM_MOUSEMOVE, _make_lp_param(0))
        mock_next.assert_called_once()

    def test_negative_n_code_delegates_immediately(self):
        hook, cb = _make_hook()
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0) as mock_next:
            result = hook._hook_callback(-1, WM_XBUTTONDOWN, _make_lp_param(XBUTTON1))
        mock_next.assert_called_once()
        assert result != 1
        assert hook.dispatch_pending() == 0
        cb.assert_not_called()


# ---------------------------------------------------------------------------
# 32: the hook thread never does work
# ---------------------------------------------------------------------------

class TestHookThreadNeverWorks:

    def test_slow_handler_does_not_delay_the_callback(self):
        started = threading.Event()

        def _slow(name, pressed):
            started.set()
            time.sleep(0.5)

        hook, _ = _make_hook(on_event=_slow, suppress='mouse4')
        hook._dispatcher = mh.thread_registry.spawn('test-mouse-dispatch', hook._dispatch_loop, daemon=True)
        try:
            durations = []
            with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0):
                for w_param in (WM_XBUTTONDOWN, WM_XBUTTONUP, WM_XBUTTONDOWN, WM_XBUTTONUP):
                    t0 = time.perf_counter()
                    result = hook._hook_callback(0, w_param, _make_lp_param(XBUTTON1))
                    durations.append((time.perf_counter() - t0) * 1000)
                    assert result == 1
                    started.wait(1.0)         # the dispatcher is busy sleeping now
            assert started.is_set()
            assert max(durations) < 5.0, durations          # never the handler's 500 ms
            assert hook.last_callback_ms < 5.0
        finally:
            hook._stopping.set()
            hook._events.put(mh._STOP)
            hook._dispatcher.join(timeout=3)

    def test_callback_cost_is_microseconds(self):
        hook, _ = _make_hook(suppress='mouse4')
        lp = _make_lp_param(XBUTTON1)
        times = []
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0):
            for i in range(200):
                t0 = time.perf_counter()
                hook._hook_callback(0, WM_XBUTTONDOWN if i % 2 == 0 else WM_MOUSEMOVE, lp)
                times.append((time.perf_counter() - t0) * 1e6)
        times.sort()
        assert times[len(times) // 2] < 500, times[:5]      # median well under a millisecond
        assert hook.slow_callbacks == 0

    def test_callback_takes_no_lock_and_never_logs(self, caplog):
        hook, _ = _make_hook(suppress='mouse4')
        with caplog.at_level(logging.DEBUG):
            _fire(hook, WM_XBUTTONDOWN)
            _fire(hook, WM_MOUSEMOVE, 0)
        assert caplog.records == []
        assert type(hook._events).__name__ == 'SimpleQueue'


# ---------------------------------------------------------------------------
# Suppression scope: ONLY the configured button's X events return 1
# ---------------------------------------------------------------------------

_PASS_THROUGH = [
    ('move', WM_MOUSEMOVE, 0), ('left down', WM_LBUTTONDOWN, 0), ('left up', WM_LBUTTONUP, 0),
    ('right down', WM_RBUTTONDOWN, 0), ('right up', WM_RBUTTONUP, 0),
    ('middle down', WM_MBUTTONDOWN, 0), ('middle up', WM_MBUTTONUP, 0),
    ('wheel', WM_MOUSEWHEEL, 0), ('h-wheel', WM_MOUSEHWHEEL, 0),
    ('mouse5 down', WM_XBUTTONDOWN, XBUTTON2), ('mouse5 up', WM_XBUTTONUP, XBUTTON2),
]


class TestSuppressionScope:

    @pytest.mark.parametrize("w_param", [WM_XBUTTONDOWN, WM_XBUTTONUP])
    def test_configured_button_returns_1(self, w_param):
        hook, _ = _make_hook(suppress='mouse4')
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0) as mock_next:
            assert hook._hook_callback(0, w_param, _make_lp_param(XBUTTON1)) == 1
        mock_next.assert_not_called()

    @pytest.mark.parametrize("label, w_param, xbutton", _PASS_THROUGH, ids=[p[0] for p in _PASS_THROUGH])
    def test_every_other_message_passes_to_call_next_hook(self, label, w_param, xbutton):
        hook, _ = _make_hook(suppress='mouse4')
        lp = _make_lp_param(xbutton)
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=7) as mock_next:
            result = hook._hook_callback(0, w_param, lp)
        mock_next.assert_called_once_with(999, 0, w_param, lp)
        assert result == 7

    @pytest.mark.parametrize("label, w_param, xbutton", _PASS_THROUGH, ids=[p[0] for p in _PASS_THROUGH])
    def test_every_other_message_passes_on_the_exception_path(self, label, w_param, xbutton):
        hook, _ = _make_hook(suppress='mouse4')

        class _Broken:
            def put(self, item):
                raise RuntimeError("queue broke")

            def qsize(self):
                raise RuntimeError("queue broke")

        hook._events = _Broken()
        lp = _make_lp_param(xbutton)
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=7) as mock_next:
            assert hook._hook_callback(0, w_param, lp) == 7
        mock_next.assert_called_once_with(999, 0, w_param, lp)

    @pytest.mark.parametrize("w_param", [WM_XBUTTONDOWN, WM_XBUTTONUP])
    def test_configured_button_passes_through_when_the_callback_breaks(self, w_param):
        hook, _ = _make_hook(suppress='mouse4')
        with patch.object(mh.ctypes, 'cast', side_effect=RuntimeError("bad lParam")), \
                patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0) as mock_next:
            result = hook._hook_callback(0, w_param, _make_lp_param(XBUTTON1))
        assert result != 1 and hook.callback_errors == 1
        mock_next.assert_called_once()

    def test_call_next_hook_raising_still_returns(self):
        hook, _ = _make_hook(suppress='mouse4')
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', side_effect=OSError("gone")):
            assert hook._hook_callback(0, WM_LBUTTONDOWN, _make_lp_param(0)) == 0
        assert hook.callback_errors == 1

    def test_suppress_mouse5_returns_1(self):
        hook, cb = _make_hook(suppress='mouse5')
        assert hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON2)) == 1

    def test_suppress_none_passes_mouse4_through(self):
        hook, cb = _make_hook(suppress=None)
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0) as mock_next:
            result = hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON1))
        mock_next.assert_called_once()
        assert result != 1

    def test_suppressed_press_is_still_delivered(self):
        called = []
        hook, _ = _make_hook(on_event=lambda b, p: called.append((b, p)), suppress='mouse4')
        assert hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON1)) == 1
        hook.dispatch_pending()
        assert called == [('mouse4', True)]


class TestSuppressionSet:
    """One hook serves command mode and the main hotkey: a set of buttons."""

    def test_set_suppresses_both_buttons(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_buttons={'mouse4', 'mouse5'})
        hook._hook_id = 999
        assert hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON1)) == 1
        assert hook._hook_callback(0, WM_XBUTTONUP, _make_lp_param(XBUTTON2)) == 1

    def test_set_with_one_button_passes_the_other_through(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_buttons={'mouse5'})
        hook._hook_id = 999
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0) as mock_next:
            result = hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON1))
        mock_next.assert_called_once()
        assert result != 1
        assert hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON2)) == 1

    def test_empty_set_suppresses_nothing_but_still_reports(self):
        cb = MagicMock()
        hook = MouseHook(on_button_event=cb, suppress_buttons=set())
        hook._hook_id = 999
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=0):
            assert hook._hook_callback(0, WM_XBUTTONDOWN, _make_lp_param(XBUTTON1)) != 1
        hook.dispatch_pending()
        cb.assert_called_once_with('mouse4', True)

    @pytest.mark.parametrize("value, expected", [
        ('mouse4', frozenset({'mouse4'})),
        (None, frozenset()),
        (['mouse4', 'mouse5'], frozenset({'mouse4', 'mouse5'})),
    ])
    def test_legacy_suppress_button_str_none_and_iterables_are_coerced(self, value, expected):
        assert MouseHook(MagicMock(), suppress_button=value).suppress_buttons == expected

    def test_suppress_buttons_str_is_coerced(self):
        assert MouseHook(MagicMock(), suppress_buttons='mouse5').suppress_buttons == frozenset({'mouse5'})

    def test_legacy_default_still_suppresses_mouse4(self):
        assert MouseHook(MagicMock()).suppress_buttons == frozenset({'mouse4'})


# ---------------------------------------------------------------------------
# Dispatcher: exceptions and back-pressure
# ---------------------------------------------------------------------------

class TestDispatcher:

    def test_handler_exception_does_not_kill_delivery_or_the_hook(self, caplog):
        seen = []

        def _handler(btn, pressed):
            seen.append((btn, pressed))
            if pressed:
                raise RuntimeError("test error")

        hook, _ = _make_hook(on_event=_handler, suppress='mouse4')
        assert _fire(hook, WM_XBUTTONDOWN) == 1
        assert _fire(hook, WM_XBUTTONUP) == 1
        with caplog.at_level(logging.ERROR, logger='samsara.mouse_hook'):
            assert hook.dispatch_pending() == 2
        assert seen == [('mouse4', True), ('mouse4', False)]
        assert "callback error" in caplog.text
        with patch.object(ctypes.windll.user32, 'CallNextHookEx', return_value=5) as mock_next:
            assert hook._hook_callback(0, WM_LBUTTONDOWN, _make_lp_param(0)) == 5
        mock_next.assert_called_once()

    def test_dispatcher_thread_survives_a_raising_handler(self):
        delivered = threading.Event()
        calls = []

        def _handler(btn, pressed):
            calls.append(pressed)
            if pressed:
                raise RuntimeError("boom")
            delivered.set()

        hook, _ = _make_hook(on_event=_handler)
        hook._dispatcher = mh.thread_registry.spawn('test-mouse-dispatch', hook._dispatch_loop, daemon=True)
        try:
            _fire(hook, WM_XBUTTONDOWN)
            _fire(hook, WM_XBUTTONUP)
            assert delivered.wait(2.0)
            assert calls == [True, False] and hook._dispatcher.is_alive()
        finally:
            hook._stopping.set()
            hook._events.put(mh._STOP)
            hook._dispatcher.join(timeout=3)

    def test_overflow_drops_oldest(self):
        hook, _ = _make_hook(suppress=None)
        for i in range(mh.QUEUE_BOUND + 8):
            _fire(hook, WM_XBUTTONDOWN if i % 2 == 0 else WM_XBUTTONUP)
        assert hook._events.qsize() == mh.QUEUE_BOUND
        assert hook.dropped_events == 8
        first = hook._events.get_nowait()
        assert first[1] is True and hook._events.qsize() == mh.QUEUE_BOUND - 1   # event #8 (a press) survived

    def test_overflow_logs_once_per_burst(self, caplog):
        hook, _ = _make_hook(suppress=None)
        with caplog.at_level(logging.WARNING, logger='samsara.mouse_hook'):
            for _ in range(mh.QUEUE_BOUND + 4):
                _fire(hook, WM_XBUTTONDOWN)
            hook._report_counters()
            for _ in range(10):
                _fire(hook, WM_XBUTTONDOWN)
            hook._report_counters()
            assert sum("dropping oldest" in r.message for r in caplog.records) == 1
            hook.dispatch_pending()
            hook._report_counters()                  # burst over: queue drained
            for _ in range(mh.QUEUE_BOUND + 1):
                _fire(hook, WM_XBUTTONDOWN)
            hook._report_counters()
        assert sum("dropping oldest" in r.message for r in caplog.records) == 2


# ---------------------------------------------------------------------------
# Watchdog: slow callbacks, lost hook, reinstall, 3-strike fallback
# ---------------------------------------------------------------------------

class TestWatchdog:

    def test_slow_callback_is_logged_with_its_duration(self, caplog, monkeypatch):
        hook, _ = _make_hook()
        clock = iter([100.0, 100.012])                 # 12 ms inside the callback
        monkeypatch.setattr(mh, '_perf', lambda: next(clock))
        _fire(hook, WM_MOUSEMOVE, 0)
        assert hook.slow_callbacks == 1 and hook.slow_callback_max_ms == pytest.approx(12.0)
        with caplog.at_level(logging.WARNING, logger='samsara.mouse_hook'):
            hook._report_counters()
            hook._report_counters()
        warnings = [r for r in caplog.records if "exceeded" in r.message]
        assert len(warnings) == 1 and "12.00 ms" in warnings[0].getMessage()

    def _lost_hook(self, on_failed=None):
        hook, _ = _make_hook(on_hook_failed=on_failed)
        hook._hook_id = None                           # Windows removed it
        restarts = []

        def _fake_start():
            restarts.append('start')
            hook._hook_id = None                       # ...and it is lost again every time

        hook._start_hook_thread = _fake_start
        hook._stop_hook_thread = MagicMock()
        return hook, restarts

    def test_lost_hook_is_reinstalled_and_logged(self, caplog):
        hook, restarts = self._lost_hook()
        with caplog.at_level(logging.WARNING, logger='samsara.mouse_hook'):
            hook.watchdog_tick()
        assert restarts == ['start'] and not hook.gave_up
        assert "reinstalling (1/3" in caplog.text

    def test_three_reinstalls_in_a_minute_then_give_up(self, caplog):
        failed = []
        hook, restarts = self._lost_hook(on_failed=failed.append)
        with caplog.at_level(logging.WARNING, logger='samsara.mouse_hook'):
            for _ in range(4):
                hook.watchdog_tick()
        assert restarts == ['start'] * 3
        assert hook.gave_up and hook.suppress_buttons == frozenset()
        assert len(failed) == 1 and "lost 4 times" in failed[0]
        assert hook._stopping.is_set()
        hook.watchdog_tick()                           # given up: no further reinstalls
        assert restarts == ['start'] * 3

    def test_reinstalls_older_than_a_minute_do_not_count(self, monkeypatch):
        hook, restarts = self._lost_hook()
        now = [1000.0]
        monkeypatch.setattr(mh, '_perf', lambda: now[0])
        for _ in range(3):
            hook.watchdog_tick()
        now[0] += mh.REINSTALL_WINDOW_S + 1
        hook.watchdog_tick()
        assert restarts == ['start'] * 4 and not hook.gave_up


class _Clock:
    """Deterministic mh._perf/_sleep: sleep advances time."""

    def __init__(self, start=1000.0):
        self.now = start

    def perf(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _moving_cursor():
    """Cursor position that changes on every read (the owner kept moving the mouse)."""
    state = {'i': 0}

    def _pos():
        state['i'] += 1
        return (state['i'] * 7, state['i'] * 3)

    return _pos


class TestLivenessCounter:
    """35 regression: EVERY callback is liveness, not just the x-buttons."""

    @pytest.mark.parametrize("label, w_param, xbutton", [
        ('move', WM_MOUSEMOVE, 0), ('wheel', WM_MOUSEWHEEL, 0), ('h-wheel', WM_MOUSEHWHEEL, 0),
        ('left down', WM_LBUTTONDOWN, 0), ('left up', WM_LBUTTONUP, 0),
        ('right down', WM_RBUTTONDOWN, 0), ('right up', WM_RBUTTONUP, 0),
        ('middle down', WM_MBUTTONDOWN, 0),
        ('mouse4 down (suppressed)', WM_XBUTTONDOWN, XBUTTON1), ('mouse4 up (suppressed)', WM_XBUTTONUP, XBUTTON1),
        ('mouse5 down', WM_XBUTTONDOWN, XBUTTON2), ('mouse5 up', WM_XBUTTONUP, XBUTTON2),
    ])
    def test_counter_increments_for_every_message(self, label, w_param, xbutton):
        hook, _ = _make_hook(suppress='mouse4')
        before = hook.callback_count
        _fire(hook, w_param, xbutton)
        assert hook.callback_count == before + 1, label

    def test_counter_increment_is_the_first_thing_the_callback_does(self):
        import inspect
        body = inspect.getsource(MouseHook._hook_callback).split("try:", 1)[1]
        first = next(line.strip() for line in body.splitlines() if line.strip())
        assert first.startswith("self.callback_count += 1")


class TestLivenessDetector:
    """The watchdog must not condemn a hook that is receiving traffic (35)."""

    def _hook(self, monkeypatch, *, probe='alive', cursor=None):
        hook, _ = _make_hook(suppress='mouse4', on_hook_failed=MagicMock())
        clock = _Clock()
        monkeypatch.setattr(mh, '_perf', clock.perf)
        monkeypatch.setattr(mh, '_sleep', clock.sleep)
        monkeypatch.setattr(mh, '_cursor_pos', cursor or _moving_cursor())
        probes = []

        def _send():
            probes.append(clock.now)
            if probe == 'refused':
                return False
            if probe == 'alive':
                hook.callback_count += 1          # our own injected move reached the hook
            return True

        monkeypatch.setattr(mh, '_send_zero_move', _send)
        restarts = []

        def _fake_start():
            restarts.append(clock.now)
            hook._hook_id = 1000 + len(restarts)

        hook._start_hook_thread = _fake_start
        hook._stop_hook_thread = MagicMock()
        return hook, clock, probes, restarts

    def _run(self, hook, clock, seconds, traffic_per_tick=0, step=mh.WATCHDOG_INTERVAL_S):
        ticks = int(seconds / step)
        for _ in range(ticks):
            for _ in range(traffic_per_tick):
                _fire(hook, WM_MOUSEMOVE, 0)
            hook.watchdog_tick()
            clock.now += step

    def test_a_minute_of_move_traffic_without_xbuttons_is_never_lost(self, monkeypatch, caplog):
        """The owner's log: a hook that dispatched every real press, condemned
        on cursor movement. Real rate at p10 is 40 callbacks/s (20 per tick);
        here a meagre 1 callback per 0.5 s tick."""
        hook, clock, probes, restarts = self._hook(monkeypatch)
        with caplog.at_level(logging.WARNING, logger='samsara.mouse_hook'):
            self._run(hook, clock, 60.0, traffic_per_tick=1)
        assert restarts == [] and probes == [] and not hook.gave_up
        assert "hook lost" not in caplog.text

    def test_short_silences_are_noise(self, monkeypatch):
        hook, clock, probes, restarts = self._hook(monkeypatch)
        for _ in range(30):                       # 2 silent seconds, then traffic, repeatedly
            self._run(hook, clock, 2.0, traffic_per_tick=0)
            self._run(hook, clock, 1.0, traffic_per_tick=3)
        assert probes == [] and restarts == []

    def test_silent_movement_needs_three_checks_over_three_seconds_before_the_self_test(self, monkeypatch):
        hook, clock, probes, restarts = self._hook(monkeypatch, probe='alive')
        start = clock.now
        self._run(hook, clock, 3.4)
        assert probes == []
        self._run(hook, clock, 1.0)
        assert len(probes) == 1 and probes[0] - start >= 3.0
        assert mh.SILENT_CHECKS == 3 and mh.LIVENESS_CHECK_S == 1.0

    def test_self_test_answered_is_a_false_alarm_not_a_loss(self, monkeypatch, caplog):
        hook, clock, probes, restarts = self._hook(monkeypatch, probe='alive')
        with caplog.at_level(logging.INFO, logger=mh.logger.name):
            self._run(hook, clock, 60.0)          # SetCursorPos-style movement for a minute
        assert restarts == [] and not hook.gave_up
        assert len(probes) >= 10 and hook.false_alarms == len(probes)
        assert sum("answered its self-test" in r.getMessage() for r in caplog.records) == 1   # once per episode

    def test_self_test_refused_is_inconclusive_not_a_loss(self, monkeypatch):
        hook, clock, probes, restarts = self._hook(monkeypatch, probe='refused')
        self._run(hook, clock, 30.0)
        assert probes and restarts == [] and not hook.gave_up

    def test_genuinely_dead_hook_is_lost_reinstalled_and_gives_up_on_the_fourth_loss(self, monkeypatch, caplog):
        hook, clock, probes, restarts = self._hook(monkeypatch, probe='dead')
        with caplog.at_level(logging.WARNING, logger='samsara.mouse_hook'):
            self._run(hook, clock, 30.0)
        assert len(restarts) == 3
        assert all(b - a >= 3.0 for a, b in zip(restarts, restarts[1:]))   # the sustained window each time
        assert hook.gave_up and hook.suppress_buttons == frozenset()
        hook.on_hook_failed.assert_called_once()
        assert "missed its own injected self-test" in hook.on_hook_failed.call_args.args[0]
        assert "reinstalling (1/3" in caplog.text and "giving up" in caplog.text

    def test_stationary_cursor_is_never_evidence(self, monkeypatch):
        hook, clock, probes, restarts = self._hook(monkeypatch, probe='dead', cursor=lambda: (10, 10))
        self._run(hook, clock, 60.0)
        assert probes == [] and restarts == []

    def test_real_self_test_injection_is_a_zero_motion_relative_move(self, monkeypatch):
        sent = []

        def _fake_send(count, inputs, size):
            inp = ctypes.cast(inputs, ctypes.POINTER(mh._INPUT)).contents
            sent.append((count, inp.type, inp.mi.dx, inp.mi.dy, inp.mi.dwFlags, size))
            return 1

        monkeypatch.setattr(ctypes.windll.user32, 'SendInput', _fake_send)
        assert mh._send_zero_move() is True
        assert sent == [(1, mh.INPUT_MOUSE, 0, 0, mh.MOUSEEVENTF_MOVE, ctypes.sizeof(mh._INPUT))]


class TestCallNextHookExContract:
    def test_64bit_lparam_without_declared_argtypes_uses_the_private_pointer(self, monkeypatch):
        """No argtypes on the shared function: ctypes would overflow on a
        64-bit LPARAM and cut the chain (what the 35 observer did)."""
        bare = ctypes.windll.user32['CallNextHookEx']          # fresh pointer, argtypes None
        monkeypatch.setattr(ctypes.windll.user32, 'CallNextHookEx', bare)
        calls = []
        monkeypatch.setattr(mh, '_call_next_fresh', lambda *a: calls.append(a) or 0)
        big_lparam = 0x7FFF_1234_5678
        assert mh._call_next_hook_ex(None, 0, WM_MOUSEMOVE, big_lparam) == 0
        assert calls == [(None, 0, WM_MOUSEMOVE, big_lparam)]
        assert mh._call_next_fresh is not bare

    def test_declared_argtypes_or_a_patch_are_used_as_they_are(self, monkeypatch):
        mock_next = MagicMock(return_value=3)
        monkeypatch.setattr(ctypes.windll.user32, 'CallNextHookEx', mock_next)
        assert mh._call_next_hook_ex(1, 0, 2, 3) == 3
        mock_next.assert_called_once_with(1, 0, 2, 3)


# ---------------------------------------------------------------------------
# Lifecycle: start / stop / panic release
# ---------------------------------------------------------------------------

class TestLifecycle:

    def _patched_hook(self, hook_id=42):
        """Context: patch Win32 calls so start() returns immediately."""
        return (
            patch.object(ctypes.windll.user32, 'SetWindowsHookExW',
                         return_value=hook_id),
            patch.object(ctypes.windll.user32, 'GetMessageW',
                         return_value=0),               # WM_QUIT -> loop exits
            patch.object(ctypes.windll.user32, 'UnhookWindowsHookEx'),
            patch.object(ctypes.windll.user32, 'PostThreadMessageW'),
            patch.object(ctypes.windll.kernel32, 'GetCurrentThreadId',
                         return_value=1234),
        )

    def test_start_calls_set_windows_hook_ex_and_spawns_the_dispatcher(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        p1, p2, p3, p4, p5 = self._patched_hook()
        with p1 as mock_set, p2, p3, p4, p5:
            hook.start()
            hook._thread.join(timeout=1)
            assert hook._dispatcher.is_alive()
            assert hook._dispatcher.name.startswith('mouse-hook-dispatch')
            assert hook in mh._live_hooks
            hook.stop()
        mock_set.assert_called_once_with(WH_MOUSE_LL, hook._proc, None, 0)
        assert not hook._dispatcher.is_alive() and hook not in mh._live_hooks

    def test_stop_calls_unhook(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        p1, p2, p3, p4, p5 = self._patched_hook()
        with p1, p2, p3 as mock_unhook, p4, p5:
            hook.start()
            hook._thread.join(timeout=1)
            hook.stop()
        mock_unhook.assert_called_once()

    def test_stop_posts_wm_quit(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        p1, p2, p3, p4, p5 = self._patched_hook()
        from samsara.mouse_hook import WM_QUIT
        with p1, p2, p3, p4 as mock_post, p5:
            hook.start()
            hook._thread.join(timeout=1)
            hook.stop()
        # PostThreadMessageW called with WM_QUIT
        assert any(c.args[1] == WM_QUIT for c in mock_post.call_args_list)

    def test_hook_id_none_after_stop(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        p1, p2, p3, p4, p5 = self._patched_hook()
        with p1, p2, p3, p4, p5:
            hook.start()
            hook._thread.join(timeout=1)
            hook.stop()
        assert hook._hook_id is None and not hook.installed

    def test_stop_without_start_does_not_raise(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        hook.stop()   # must not raise

    def test_start_fails_gracefully_when_set_hook_returns_zero(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        with patch.object(ctypes.windll.user32, 'SetWindowsHookExW', return_value=0), \
             patch.object(ctypes.windll.kernel32, 'GetCurrentThreadId', return_value=111):
            hook.start()
            hook._thread.join(timeout=1)
            assert not hook.installed and hook.install_error
            hook.stop()
        assert hook._hook_id is None or hook._hook_id == 0

    def _blocking_win32(self, hang=False):
        """Win32 fakes with a real message loop: GetMessageW blocks until
        PostThreadMessageW(WM_QUIT) (or forever when hang=True, until .unblock())."""
        quit_posted = threading.Event()
        unblock = threading.Event()

        def _get_message(*a):
            (unblock if hang else quit_posted).wait(5)
            return 0

        patches = (
            patch.object(ctypes.windll.user32, 'SetWindowsHookExW', return_value=77),
            patch.object(ctypes.windll.user32, 'GetMessageW', side_effect=_get_message),
            patch.object(ctypes.windll.user32, 'UnhookWindowsHookEx', return_value=1),
            patch.object(ctypes.windll.user32, 'PostThreadMessageW', side_effect=lambda *a: quit_posted.set()),
            patch.object(ctypes.windll.kernel32, 'GetCurrentThreadId', side_effect=threading.get_ident),
        )
        return patches, unblock

    def test_unhook_runs_on_the_installing_thread(self):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        patches, _ = self._blocking_win32()
        with patches[0], patches[1], patches[2] as mock_unhook, patches[3], patches[4]:
            hook.start()
            installing_thread = hook._thread_id
            assert hook.installed
            hook.stop()
        mock_unhook.assert_called_once_with(77)
        assert hook.unhooked_on_thread == installing_thread != threading.get_ident()

    def test_hung_hook_thread_is_unhooked_from_the_caller_and_logged(self, caplog):
        hook = MouseHook(on_button_event=MagicMock(), suppress_button='mouse4')
        patches, unblock = self._blocking_win32(hang=True)
        with patches[0], patches[1], patches[2] as mock_unhook, patches[3], patches[4]:
            hook.start()
            with caplog.at_level(logging.WARNING, logger='samsara.mouse_hook'):
                hook._stop_hook_thread(timeout=0.1)
            mock_unhook.assert_called_once_with(77)
            assert hook.unhooked_on_thread == threading.get_ident()
            assert "did not unhook" in caplog.text
            unblock.set()
            hook._thread.join(timeout=2)
            hook.stop()
        mock_unhook.assert_called_once()           # the hook thread found nothing left to unhook

    def test_release_stops_suppressing_and_unhooks(self):
        hook, _ = _make_hook(suppress='mouse4')
        hook.stop = MagicMock()
        hook.release("test")
        assert hook.suppress_buttons == frozenset()
        hook.stop.assert_called_once()

    def test_atexit_releases_every_live_hook(self):
        import atexit as _atexit
        hook, _ = _make_hook()
        hook.stop = MagicMock()
        mh._live_hooks.add(hook)
        try:
            mh.release_all_hooks()
            hook.stop.assert_called_once()
        finally:
            mh._live_hooks.discard(hook)
        source = Path(mh.__file__).read_text(encoding='utf-8')
        assert "atexit.register(release_all_hooks)" in source and _atexit is not None

    def test_hook_callback_source_does_only_three_things(self):
        import inspect
        src = inspect.getsource(MouseHook._hook_callback)
        for forbidden in ("on_button_event", "logger", "print(", "with ", ".acquire", "Lock"):
            assert forbidden not in src, forbidden


# ---------------------------------------------------------------------------
# _on_command_button integration (DictationApp level)
# ---------------------------------------------------------------------------

class TestOnCommandButton:
    """Verify the dictation.py _on_command_button callback logic in isolation."""

    class _App:
        def __init__(self, button='mouse4', enabled=True, mode='hold'):
            from dictation import DictationApp

            self._on_command_button = DictationApp._on_command_button.__get__(self)
            self.config = {'command_mode': {
                'button': button, 'enabled': enabled, 'mode': mode,
            }}
            self.command_mode_active = False
            self._enter_count = 0
            self._exit_count = 0

        def enter_command_mode(self):
            self.command_mode_active = True
            self._enter_count += 1

        def exit_command_mode(self):
            self.command_mode_active = False
            self._exit_count += 1

    def test_mouse4_press_enters_hold_mode(self):
        app = self._App(button='mouse4', mode='hold')
        app._on_command_button('mouse4', True)
        assert app.command_mode_active is True
        assert (app._enter_count, app._exit_count) == (1, 0)

    def test_mouse4_release_exits_hold_mode(self):
        app = self._App(button='mouse4', mode='hold')
        app._on_command_button('mouse4', True)
        app._on_command_button('mouse4', False)
        assert app.command_mode_active is False
        assert (app._enter_count, app._exit_count) == (1, 1)

    def test_wrong_button_ignored(self):
        app = self._App(button='mouse4', mode='hold')
        app._on_command_button('mouse5', True)
        assert app.command_mode_active is False
        assert (app._enter_count, app._exit_count) == (0, 0)

    def test_disabled_config_ignored(self):
        app = self._App(button='mouse4', enabled=False, mode='hold')
        app._on_command_button('mouse4', True)
        assert app.command_mode_active is False
        assert (app._enter_count, app._exit_count) == (0, 0)

    def test_toggle_first_press_enters(self):
        app = self._App(button='mouse4', mode='toggle')
        app._on_command_button('mouse4', True)
        assert app.command_mode_active is True
        assert (app._enter_count, app._exit_count) == (1, 0)

    def test_toggle_second_press_exits(self):
        app = self._App(button='mouse4', mode='toggle')
        app._on_command_button('mouse4', True)
        app._on_command_button('mouse4', True)
        assert app.command_mode_active is False
        assert (app._enter_count, app._exit_count) == (1, 1)

    def test_mouse5_configured(self):
        app = self._App(button='mouse5', mode='hold')
        app._on_command_button('mouse5', True)
        assert app.command_mode_active is True
        app._on_command_button('mouse4', False)  # wrong button — no effect
        assert app.command_mode_active is True
        assert (app._enter_count, app._exit_count) == (1, 0)

    def test_missing_button_defaults_to_rctrl(self):
        app = self._App()
        del app.config['command_mode']['button']
        # The old inline copy defaulted to mouse4; production defaults to rctrl.
        app._on_command_button('mouse4', True)
        assert (app._enter_count, app._exit_count) == (0, 0)
        assert app.command_mode_active is False
        app._on_command_button('rctrl', True)
        assert app.command_mode_active is True
        assert (app._enter_count, app._exit_count) == (1, 0)
        app._on_command_button('rctrl', False)
        assert app.command_mode_active is False
        assert (app._enter_count, app._exit_count) == (1, 1)

    def test_toggle_release_does_not_exit(self):
        app = self._App(mode='toggle')
        app._on_command_button('mouse4', True)
        app._on_command_button('mouse4', False)
        assert app.command_mode_active is True
        assert (app._enter_count, app._exit_count) == (1, 0)


# ---------------------------------------------------------------------------
# 35: loud, reversible fallback -- chip, tray balloon, tray item, Settings row
# ---------------------------------------------------------------------------

class _InstallableHook:
    """Stand-in MouseHook with an installed flag the app can read."""
    created = []
    install_ok = True

    def __init__(self, on_button_event=None, suppress_buttons=None, **kw):
        self.suppress_buttons = frozenset(suppress_buttons or ())
        self.on_hook_failed = kw.get('on_hook_failed')
        self.installed = False
        self.install_error = ''
        self.stopped = False
        _InstallableHook.created.append(self)

    def start(self):
        self.installed = _InstallableHook.install_ok
        self.install_error = '' if self.installed else 'SetWindowsHookExW returned 0'

    def stop(self):
        self.stopped = True
        self.installed = False

    def release(self, why=''):
        self.stop()


@pytest.fixture
def fallback_app(monkeypatch):
    import types
    import dictation
    from tests.test_mouse_hotkey import _App

    monkeypatch.setattr(mh, 'MouseHook', _InstallableHook)
    monkeypatch.setattr(dictation.thread_registry, 'spawn', lambda name, target, daemon=True, **kw: None)
    _InstallableHook.created = []
    _InstallableHook.install_ok = True
    app = _App(hotkey='mouse4')
    for name in ('mouse_hotkey_status', 'reenable_mouse_hotkey'):
        setattr(app, name, getattr(dictation.DictationApp, name).__get__(app))
    app.balloons = []
    app.tray_icon = types.SimpleNamespace(notify_warning=lambda title, text: app.balloons.append((title, text)))
    return app


class TestLoudReversibleFallback:

    def test_give_up_is_loud_and_leaves_the_saved_choice_alone(self, fallback_app):
        app = fallback_app
        app._install_mouse_listener()
        assert app.mouse_hotkey_status()['state'] == 'active'
        app._mouse_hook.installed = False
        app._on_mouse_hook_failed("no hook callbacks for 3 checks ... missed its own injected self-test")
        status = app.mouse_hotkey_status()
        assert status['state'] == 'disabled' and 'self-test' in status['reason']
        assert status['fallback'] == 'ctrl+shift'
        assert app.config['hotkey'] == 'mouse4'                              # never rewritten
        assert any(c[0] == 'chip' and c[2] == 'warning' for c in app.calls)  # chip
        assert app.balloons and 'disabled' in app.balloons[0][0]             # tray balloon
        assert 'Re-enable mouse hotkey' in app.balloons[0][1]

    def test_reenable_reinstalls_and_clears_the_fallback(self, fallback_app):
        app = fallback_app
        app._install_mouse_listener()
        app._mouse_hook.installed = False
        app._on_mouse_hook_failed("lost 4 times within 60s")
        assert app.reenable_mouse_hotkey() == 'active'
        assert len(_InstallableHook.created) == 2 and app._mouse_hook is _InstallableHook.created[1]
        assert app._main_hotkey_override is None and app._mouse_hotkey_disabled_reason is None
        assert app._mouse_hook_released_bindings is None
        assert app.mouse_hotkey_status()['state'] == 'active'
        assert any(c[0] == 'chip' and c[1] == 'mouse hotkey active' for c in app.calls)

    def test_reenable_is_a_noop_when_already_active(self, fallback_app):
        app = fallback_app
        app._install_mouse_listener()
        hook = app._mouse_hook
        assert app.reenable_mouse_hotkey() == 'active'
        assert len(_InstallableHook.created) == 1 and app._mouse_hook is hook and not hook.stopped

    def test_reenable_that_cannot_install_stays_disabled_and_says_why(self, fallback_app):
        app = fallback_app
        app._install_mouse_listener()
        app._mouse_hook.installed = False
        app._on_mouse_hook_failed("lost 4 times within 60s")
        _InstallableHook.install_ok = False
        assert app.reenable_mouse_hotkey() == 'disabled'
        status = app.mouse_hotkey_status()
        assert status['state'] == 'disabled' and 'did not install' in status['reason']
        assert app._main_hotkey_override == 'ctrl+shift' and app.config['hotkey'] == 'mouse4'

    def test_no_mouse_binding_means_nothing_to_show(self, fallback_app):
        app = fallback_app
        app.config['hotkey'] = 'ctrl+shift'
        assert app.mouse_hotkey_status()['state'] == 'n/a'
        assert app.reenable_mouse_hotkey() == 'n/a'

    def test_keyboard_path_reads_the_runtime_override(self):
        import inspect
        import dictation
        for name in ('on_key_press', 'on_key_release'):
            src = inspect.getsource(getattr(dictation.DictationApp, name))
            assert "main_hotkey = getattr(self, '_main_hotkey_override', None) or self.config['hotkey']" in src, name
            assert "main_hotkey = self.config['hotkey']" not in src, name

    def test_config_watcher_does_not_rearm_but_settings_apply_does(self):
        import inspect
        import dictation
        src = inspect.getsource(dictation.DictationApp._apply_disk_config)
        assert "self.refresh_mouse_hook(rearm=False)" in src
        assert inspect.signature(dictation.DictationApp.refresh_mouse_hook).parameters['rearm'].default is True


class TestTrayMouseHotkeyItems:

    def _menu(self, app):
        from samsara.ui.tray_qt import SamsaraTrayQt
        tray = SamsaraTrayQt(app)
        tray._rebuild_menu()
        return tray, [a.text() for a in tray._menu.actions()]

    def _app(self, status, hook):
        from tests.test_tray_qt import _make_app
        app = _make_app()
        app.mouse_hotkey_status = MagicMock(return_value=status)
        app._mouse_hook = hook
        return app

    def test_disabled_shows_why_and_reenable(self, qapp):
        app = self._app({'state': 'disabled', 'reason': 'lost 4 times within 60s', 'fallback': 'ctrl+shift'}, None)
        tray, texts = self._menu(app)
        assert "Mouse hotkey disabled: lost 4 times within 60s" in texts
        assert "Re-enable mouse hotkey" in texts and "Release mouse buttons" not in texts
        action = next(a for a in tray._menu.actions() if a.text() == "Re-enable mouse hotkey")
        action.trigger()
        app.reenable_mouse_hotkey.assert_called_once()

    def test_active_offers_reenable_and_release(self, qapp):
        app = self._app({'state': 'active', 'reason': ''}, object())
        _tray, texts = self._menu(app)
        assert "Re-enable mouse hotkey" in texts and "Release mouse buttons" in texts
        assert not any(t.startswith("Mouse hotkey disabled") for t in texts)

    def test_no_mouse_binding_shows_nothing(self, qapp):
        app = self._app({'state': 'n/a'}, None)
        _tray, texts = self._menu(app)
        assert "Re-enable mouse hotkey" not in texts and "Release mouse buttons" not in texts

    def test_balloon_is_a_thread_safe_signal_to_a_warning_message(self, qapp):
        from samsara.ui.tray_qt import SamsaraTrayQt
        from tests.test_tray_qt import _make_app
        tray = SamsaraTrayQt(_make_app())
        shown = []
        tray._tray.showMessage = lambda *a: shown.append(a)
        tray.notify_warning("Samsara mouse hotkey disabled", "why")
        qapp.processEvents()
        assert shown and shown[0][0] == "Samsara mouse hotkey disabled" and shown[0][1] == "why"


class TestSettingsHotkeyRowReflectsReality:

    def _window(self, status, reenable_state='active'):
        from samsara.ui.settings_qt import _SettingsWindow
        from tests.test_settings import _StubApp
        app = _StubApp()
        app.config = {'hotkey': 'mouse4'}
        app.mouse_hotkey_status = lambda: status
        app.reenable_mouse_hotkey = MagicMock(return_value=reenable_state)
        return _SettingsWindow(app), app

    def test_disabled_mouse_hotkey_shows_inline_notice_without_touching_config(self, qapp):
        from PySide6.QtWidgets import QPushButton
        win, app = self._window({'state': 'disabled', 'reason': 'lost 4 times within 60s',
                                 'fallback': 'ctrl+shift'})
        try:
            notice = win.findChild(QPushButton, "mouseHotkeyStatus")
            assert notice is not None
            assert "not active" in notice.text() and "click to retry" in notice.text()
            assert "lost 4 times" in notice.toolTip() and "ctrl+shift" in notice.toolTip()
            assert app.config['hotkey'] == 'mouse4'
            notice.click()
            app.reenable_mouse_hotkey.assert_called_once()
            assert notice.text() == "active" and not notice.isEnabled()
        finally:
            win.close()

    def test_failed_retry_says_so(self, qapp):
        from PySide6.QtWidgets import QPushButton
        win, app = self._window({'state': 'disabled', 'reason': 'x', 'fallback': 'ctrl+shift'},
                                reenable_state='disabled')
        try:
            notice = win.findChild(QPushButton, "mouseHotkeyStatus")
            notice.click()
            assert "still not active" in notice.text() and notice.isEnabled()
        finally:
            win.close()

    def test_active_mouse_hotkey_shows_no_notice(self, qapp):
        from PySide6.QtWidgets import QPushButton
        win, _app = self._window({'state': 'active', 'reason': ''})
        try:
            assert win.findChild(QPushButton, "mouseHotkeyStatus") is None
        finally:
            win.close()
