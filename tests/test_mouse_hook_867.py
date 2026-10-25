"""867: persistent recovery and load-aware probes; no real mouse input/hooks."""

import ctypes
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import samsara.mouse_hook as mh
from samsara.mouse_hook import MouseHook
from tests.test_mouse_hook import fallback_app  # noqa: F401 - shared isolated app fixture


class Clock:
    def __init__(self):
        self.now = 1000.0

    def perf(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def recovery(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(mh, '_perf', clock.perf)
    monkeypatch.setattr(mh, '_sleep', clock.sleep)
    monkeypatch.setattr(mh, '_cursor_pos', lambda: (0, 0))
    notices, recovered, installs = [], [], []
    hook = MouseHook(lambda *_: None, suppress_buttons={'mouse4', 'mouse5'},
                     on_hook_failed=notices.append, on_hook_recovered=lambda: recovered.append(True))
    hook._hook_id = 42

    def install():
        installs.append(clock.now)
        hook._hook_id = 42
        hook.install_error = ''

    def uninstall():
        hook._hook_id = None

    monkeypatch.setattr(hook, '_start_hook_thread', install)
    monkeypatch.setattr(hook, '_stop_hook_thread', uninstall)
    return SimpleNamespace(hook=hook, clock=clock, notices=notices, recovered=recovered,
                           installs=installs)


def lose(state):
    state.hook._hook_id = None
    state.hook.watchdog_tick()


def test_four_losses_back_off_then_reinstall_without_permanent_disable(recovery):
    s = recovery
    for _ in range(4):
        lose(s)
    assert len(s.installs) == 3 and len(s.notices) == 1
    assert s.hook._retry_due == s.clock.now + 30
    assert not s.hook.gave_up and not s.hook._stopping.is_set()
    assert not s.hook.suppress_buttons
    s.clock.sleep(29.9)
    s.hook.watchdog_tick()
    assert len(s.installs) == 3
    s.clock.sleep(0.1)
    s.hook.watchdog_tick()
    assert len(s.installs) == 4 and s.hook.installed
    assert not s.hook.suppress_buttons  # a new handle alone is not proof of health
    s.hook.callback_count += 1
    s.hook.watchdog_tick()
    assert s.hook.suppress_buttons == frozenset({'mouse4', 'mouse5'})
    assert s.recovered == [True] and len(s.notices) == 1


def test_repeated_losses_reach_five_minute_retries_and_keep_going(recovery):
    s = recovery
    for _ in range(3):
        lose(s)
    for delay in (30, 60, 120, 300, 300, 300):
        lose(s)
        assert s.hook._retry_due == s.clock.now + delay
        before = len(s.installs)
        s.clock.sleep(delay)
        s.hook.watchdog_tick()
        assert len(s.installs) == before + 1
    assert len(s.notices) == 1 and not s.hook._stopping.is_set()


def test_install_failure_retries_slowly_and_notifies_once(recovery, monkeypatch):
    s = recovery

    def fail():
        s.installs.append(s.clock.now)
        s.hook._hook_id = None
        s.hook.install_error = 'SetWindowsHookExW returned 0'

    monkeypatch.setattr(s.hook, '_start_hook_thread', fail)
    lose(s)
    for _ in range(4):
        assert s.hook._retry_due == s.clock.now + mh.INSTALL_RETRY_S
        s.clock.sleep(mh.INSTALL_RETRY_S - 1)
        before = len(s.installs)
        s.hook.watchdog_tick()
        assert len(s.installs) == before
        s.clock.sleep(1)
        s.hook.watchdog_tick()
        assert len(s.installs) == before + 1
    assert len(s.notices) == 1 and not s.hook.suppress_buttons


def test_initial_install_failure_keeps_dispatcher_retry_and_notice(recovery, monkeypatch):
    s = recovery
    monkeypatch.setattr(mh.thread_registry, 'spawn', lambda *_args, **_kw: None)
    monkeypatch.setattr(s.hook, '_start_hook_thread', lambda: None)
    s.hook._hook_id = None
    s.hook.install_error = 'SetWindowsHookExW returned 0'
    s.hook.start()
    try:
        assert s.hook in mh._live_hooks
        s.hook.watchdog_tick()
        s.hook.watchdog_tick()
        assert s.notices == ['SetWindowsHookExW returned 0']
        assert s.hook._retry_due == s.clock.now + 300
    finally:
        s.hook.stop()


def test_late_install_is_recognized_before_slow_retry_deadline(recovery):
    s = recovery
    s.hook._hook_id = None
    s.hook._begin_recovery('install still pending')
    s.hook._schedule_retry('install still pending', install_failed=True)
    s.hook._hook_id = 42
    s.hook.callback_count += 1
    s.hook.watchdog_tick()
    assert not s.hook.recovering and s.hook._retry_due is None
    assert s.hook.suppress_buttons == frozenset({'mouse4', 'mouse5'})


def test_probe_can_confirm_stationary_recovery_without_notice(recovery, monkeypatch):
    s = recovery
    lose(s)
    monkeypatch.setattr(s.hook, '_self_test', lambda: 'alive')
    s.clock.sleep(3)
    s.hook.watchdog_tick()
    assert not s.hook.recovering and s.recovered == [True]
    assert len(s.notices) == 1


def test_inconclusive_probe_keeps_suppression_off_without_another_install(recovery, monkeypatch):
    s = recovery
    lose(s)
    monkeypatch.setattr(s.hook, '_self_test', lambda: 'inconclusive')
    s.clock.sleep(3)
    s.hook.watchdog_tick()
    assert s.hook.recovering and not s.hook.suppress_buttons
    assert len(s.installs) == 1 and len(s.notices) == 1


def test_new_notice_requires_a_full_healthy_minute(recovery):
    s = recovery
    lose(s)
    s.hook.callback_count += 1
    s.hook.watchdog_tick()
    s.clock.sleep(59)
    lose(s)
    assert len(s.notices) == 1
    s.hook.callback_count += 1
    s.hook.watchdog_tick()
    s.clock.sleep(61)
    s.hook.watchdog_tick()
    lose(s)
    assert len(s.notices) == 2


def test_explicit_release_cancels_all_retries(recovery):
    s = recovery
    for _ in range(4):
        lose(s)
    s.hook.release('test user release')
    s.clock.sleep(10000)
    s.hook.watchdog_tick()
    assert len(s.installs) == 3 and s.hook._retry_due is None
    assert s.hook._stopping.is_set() and not s.hook.suppress_buttons


def test_explicit_restart_resets_backoff_and_restores_requested_buttons(recovery, monkeypatch):
    s = recovery
    monkeypatch.setattr(mh.thread_registry, 'spawn', lambda *_args, **_kw: None)
    for _ in range(4):
        lose(s)
    s.hook.stop()
    s.hook.start()
    try:
        assert s.hook.installed and not s.hook.recovering
        assert s.hook._retry_due is None and s.hook._backoff_step == 0
        assert s.hook.suppress_buttons == frozenset({'mouse4', 'mouse5'})
    finally:
        s.hook.stop()


@pytest.mark.parametrize('pump_responsive, verdict', [(True, 'dead'), (False, 'inconclusive')])
def test_missed_injection_needs_responsive_pump(recovery, monkeypatch, pump_responsive, verdict):
    monkeypatch.setattr(mh, '_send_zero_move', lambda: True)
    pump = MagicMock(return_value=pump_responsive)
    monkeypatch.setattr(recovery.hook, '_pump_responsive', pump)
    assert recovery.hook._self_test() == verdict
    pump.assert_called_once()


def test_dispatcher_starvation_makes_missed_probe_inconclusive(recovery, monkeypatch):
    s = recovery
    monkeypatch.setattr(mh, '_send_zero_move', lambda: True)
    monkeypatch.setattr(mh, '_sleep', lambda _seconds: s.clock.sleep(5))
    pump = MagicMock(return_value=True)
    monkeypatch.setattr(s.hook, '_pump_responsive', pump)
    assert s.hook._self_test() == 'inconclusive'
    pump.assert_not_called()


def test_callback_during_pump_check_prevents_false_loss(recovery, monkeypatch):
    monkeypatch.setattr(mh, '_send_zero_move', lambda: True)

    def ping():
        recovery.hook.callback_count += 1
        return True

    monkeypatch.setattr(recovery.hook, '_pump_responsive', ping)
    assert recovery.hook._self_test() == 'alive'


@pytest.mark.parametrize('acknowledge', [True, False])
def test_pump_round_trip_measures_the_current_request(recovery, monkeypatch, acknowledge):
    s = recovery
    s.hook._thread_id = 123
    s.hook._pump_ack = -1  # an old response must not satisfy a fresh ping

    def post(thread_id, message, sequence, _param):
        assert thread_id == 123 and message == mh.WM_HOOK_PING
        if acknowledge:
            s.hook._pump_ack = sequence
        return 1

    monkeypatch.setattr(mh._user32, 'PostThreadMessageW', post)
    assert s.hook._pump_responsive() is acknowledge
    assert s.clock.now - 1000 < mh.PROBE_WAIT_S if acknowledge else s.clock.now - 1000 >= mh.PROBE_WAIT_S


def test_pump_message_is_acknowledged_by_real_run_loop(recovery, monkeypatch):
    s = recovery
    monkeypatch.setattr(mh, '_set_windows_hook_ex', lambda: lambda *_: 42)
    monkeypatch.setattr(mh._kernel32, 'GetCurrentThreadId', lambda: 123)
    calls = []

    def get_message(pointer, *_):
        if calls:
            return 0
        msg = ctypes.cast(pointer, ctypes.POINTER(ctypes.wintypes.MSG)).contents
        msg.message, msg.wParam = mh.WM_HOOK_PING, 17
        calls.append(True)
        return 1

    monkeypatch.setattr(mh._user32, 'GetMessageW', get_message)
    monkeypatch.setattr(mh._user32, 'UnhookWindowsHookEx', lambda *_: 1)
    s.hook._run()
    assert s.hook._pump_ack == 17 and s.hook._hook_id is None


def test_starved_old_thread_is_not_overlapped(monkeypatch):
    hook = MouseHook(lambda *_: None)
    hook._thread = SimpleNamespace(is_alive=lambda: True)
    spawn = MagicMock()
    monkeypatch.setattr(mh.thread_registry, 'spawn', spawn)
    hook._start_hook_thread()
    spawn.assert_not_called()


def test_app_retains_retrying_hook_and_silently_clears_fallback(fallback_app, recovery, monkeypatch):
    app, s = fallback_app, recovery
    s.hook.on_hook_failed = app._on_mouse_hook_failed

    def factory(**kwargs):
        s.hook.on_hook_recovered = kwargs['on_hook_recovered']
        s.hook.requested_suppress_buttons = frozenset(kwargs['suppress_buttons'])
        s.hook.suppress_buttons = s.hook.requested_suppress_buttons
        return s.hook

    monkeypatch.setattr(mh, 'MouseHook', factory)
    app._install_mouse_listener()
    try:
        lose(s)
        assert app._mouse_hook is s.hook and app._main_hotkey_override == 'ctrl+shift'
        assert app.mouse_hotkey_status()['state'] == 'recovering'
        assert len(app.balloons) == 1 and 'automatically' in app.balloons[0][1]
        app.refresh_mouse_hook(rearm=False)
        assert app._mouse_hook is s.hook
        before = list(app.calls)
        s.hook.callback_count += 1
        s.hook.watchdog_tick()
        assert app.mouse_hotkey_status()['state'] == 'active'
        assert app._main_hotkey_override is None and app._mouse_hotkey_disabled_reason is None
        assert app.calls == before and len(app.balloons) == 1
        assert app.config['hotkey'] == 'mouse4'
    finally:
        s.hook.stop()


def test_app_keeps_initial_install_failure_for_slow_retry(fallback_app, recovery, monkeypatch):
    app, s = fallback_app, recovery
    s.hook._hook_id = None
    s.hook.install_error = 'SetWindowsHookExW returned 0'
    s.hook.on_hook_failed = app._on_mouse_hook_failed
    monkeypatch.setattr(s.hook, '_start_hook_thread', lambda: None)
    monkeypatch.setattr(mh, 'MouseHook', lambda **_kw: s.hook)
    app._install_mouse_listener()
    try:
        s.hook.watchdog_tick()
        assert app._mouse_hook is s.hook
        assert app.mouse_hotkey_status()['state'] == 'disabled'
        assert len(app.balloons) == 1 and s.hook._retry_due is not None
    finally:
        s.hook.stop()


def test_recovering_tray_has_release_without_reenable(qapp):
    from tests.test_mouse_hook import TestTrayMouseHotkeyItems

    helper = TestTrayMouseHotkeyItems()
    app = helper._app({'state': 'recovering', 'reason': ''}, object())
    _tray, texts = helper._menu(app)
    assert 'Re-enable mouse hotkey' not in texts
    assert 'Release mouse buttons' in texts
    assert not any(t.startswith('Mouse hotkey disabled') for t in texts)
