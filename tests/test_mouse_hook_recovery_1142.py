"""1142: a missed injection cannot turn idle/keyboard activity into an outage.

All native boundaries are faked; no live hook, input injection or app bootstrap.
"""

import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import samsara.mouse_hook as mh


@pytest.fixture
def mouse(monkeypatch):
    state = SimpleNamespace(now=1000.0, pos=(0, 0), available=True, idle=0,
                            tick=100, installs=[], stops=[], notices=[], events=[])

    def sleep(seconds):
        state.now += seconds

    monkeypatch.setattr(mh, '_perf', lambda: state.now)
    monkeypatch.setattr(mh, '_sleep', sleep)
    monkeypatch.setattr(mh, '_cursor_pos', lambda: state.pos)
    monkeypatch.setattr(mh, '_input_status',
                        lambda: (state.available, state.idle, state.tick))
    monkeypatch.setattr(mh, '_send_zero_move', lambda: True)
    monkeypatch.setattr(mh, '_call_next_hook_ex', lambda *_: 0)
    # No real user32 call or thread creation can escape these fake boundaries.
    monkeypatch.setattr(mh, '_user32', SimpleNamespace())
    monkeypatch.setattr(mh.thread_registry, 'spawn', Mock(side_effect=AssertionError('native thread')))
    state.hook = mh.MouseHook(
        lambda *event: state.events.append(event), suppress_buttons={'mouse5'},
        on_hook_interrupted=state.notices.append,
        on_hook_recovered=lambda: state.events.append('recovered'))
    state.hook._hook_id = 42
    state.hook._thread_id = 123
    monkeypatch.setattr(state.hook, '_pump_responsive', lambda: True)

    def install():
        state.installs.append(state.now)
        state.hook._hook_id = 42
        state.hook._thread = None
        state.hook.install_error = ''

    def stop():
        state.stops.append(state.now)
        state.hook._hook_id = None

    monkeypatch.setattr(state.hook, '_start_hook_thread', install)
    monkeypatch.setattr(state.hook, '_stop_hook_thread', stop)
    state.advance = sleep
    return state


def fire(state, message=0x0200):
    info = mh.MSLLHOOKSTRUCT()
    info.mouseData = mh.XBUTTON2 << 16
    return state.hook._hook_callback(0, message, ctypes.pointer(info))


def move_without_callbacks(state):
    state.advance(mh.LIVENESS_CHECK_S)
    state.tick += 1
    state.pos = (state.pos[0] + 1, 0)
    state.hook.watchdog_tick()


@pytest.mark.parametrize('callback_before_probe_s', [0.0242, 0.1533])
def test_log_replay_real_mouse_return_does_not_disable_binding(mouse, monkeypatch,
                                                            callback_before_probe_s):
    s = mouse
    s.idle = 90
    s.hook.watchdog_tick()
    s.idle, s.tick = 0, 101
    fire(s)
    s.advance(callback_before_probe_s)
    probe = Mock(wraps=s.hook._self_test)
    monkeypatch.setattr(s.hook, '_self_test', probe)
    s.hook.watchdog_tick()
    probe.assert_not_called()
    assert not s.notices and not s.stops and not s.installs
    assert not s.hook.recovering and s.hook.suppress_buttons == {'mouse5'}
    assert fire(s, mh.WM_XBUTTONDOWN) == 1
    s.hook.dispatch_pending()
    assert s.events == [('mouse5', True)]


@pytest.mark.parametrize('available', [False, True])
def test_keyboard_return_without_mouse_input_does_not_start_recovery(mouse, monkeypatch, available):
    s = mouse
    probe = Mock(return_value='dead')
    monkeypatch.setattr(s.hook, '_self_test', probe)
    # Repeated away/lock and typing cycles do not consume the reinstall budget.
    for _ in range(66):
        s.available, s.idle = available, 90
        s.hook.watchdog_tick()
        s.advance(90)
        s.available, s.idle = True, 0
        s.tick += 1
        s.hook.watchdog_tick()
        s.advance(1)
    probe.assert_not_called()
    assert not s.installs and not s.notices
    assert s.hook._backoff_step == 0 and s.hook._retry_due is None
    assert s.hook.suppress_buttons == {'mouse5'}


def test_missed_replacement_probes_cannot_create_the_log_reinstall_storm(mouse):
    s = mouse
    s.hook._begin_recovery('prior confirmed loss')
    # Replay more missed probes than the 21 in the retained log. Neither
    # elapsed time nor keyboard activity corroborates a missing mouse event.
    for _ in range(22):
        s.advance(10)
        s.tick += 1
        s.hook.watchdog_tick()
    assert s.hook.installed and s.hook.recovering
    assert not s.installs and not s.stops and len(s.notices) == 1
    assert s.hook._retry_due is None and s.hook._backoff_step == 0
    assert fire(s, mh.WM_XBUTTONDOWN) == 1
    s.hook.dispatch_pending()
    assert s.events == ['recovered', ('mouse5', True)]
    assert not s.hook.recovering and s.hook.suppress_buttons == {'mouse5'}


def test_actual_silent_movement_still_replaces_lost_hook(mouse):
    s = mouse
    s.hook.watchdog_tick()
    started = s.now
    for _ in range(mh.SILENT_CHECKS):
        move_without_callbacks(s)
    assert len(s.installs) == 1 and len(s.stops) == 1
    assert s.now - started <= 3.5  # three samples plus the failed probe
    assert s.hook.recovering and not s.hook.suppress_buttons
    assert fire(s, mh.WM_XBUTTONDOWN) == 1
    s.hook.dispatch_pending()
    assert s.events == ['recovered', ('mouse5', True)]


@pytest.mark.parametrize('loss', ['silent_motion', 'thread_ended', 'handle_cleared'])
def test_active_confirmed_loss_bypasses_old_thirty_second_backoff(mouse, loss):
    s = mouse
    s.hook.watchdog_tick()
    s.hook._backoff_step = 99
    if loss == 'silent_motion':
        for _ in range(mh.SILENT_CHECKS):
            move_without_callbacks(s)
    else:
        if loss == 'thread_ended':
            s.hook._thread = SimpleNamespace(is_alive=lambda: False)
        else:
            s.hook._hook_id = None
        s.hook.watchdog_tick()
    detected = s.now
    assert s.hook._retry_due == detected + mh.ACTIVE_LOSS_RETRY_S
    s.advance(.5)
    s.hook.watchdog_tick()
    assert not s.installs
    s.advance(.5)
    s.hook.watchdog_tick()
    assert s.installs == [detected + 1]
    assert fire(s, mh.WM_XBUTTONDOWN) == 1
    s.hook.dispatch_pending()
    assert s.events == ['recovered', ('mouse5', True)]


def test_confirmed_loss_retries_are_bounded_when_thread_keeps_dying(mouse):
    s = mouse
    s.hook._backoff_step = 99
    for _ in range(20):
        s.hook._hook_id = None
        s.hook.watchdog_tick()
        before = len(s.installs)
        for _ in range(9):
            s.advance(.1)
            s.hook.watchdog_tick()
        assert len(s.installs) == before
        s.advance(.101)
        s.hook.watchdog_tick()
        assert len(s.installs) == before + 1
    assert all(b - a >= 1 for a, b in zip(s.installs, s.installs[1:]))
    assert len(s.notices) == 1


def test_native_install_failure_keeps_separate_slow_retry(mouse, monkeypatch):
    s = mouse

    def fail():
        s.hook.install_error = 'fake install refusal'

    monkeypatch.setattr(s.hook, '_start_hook_thread', fail)
    s.hook._hook_id = None
    s.hook.watchdog_tick()
    assert s.hook._retry_due == s.now + mh.ACTIVE_RETRY_S
    assert not s.hook.suppress_buttons


def test_callback_at_failed_probe_boundary_prevents_loss(mouse, monkeypatch):
    s = mouse

    def late():
        fire(s)
        return 'dead'

    monkeypatch.setattr(s.hook, '_self_test', late)
    s.hook.watchdog_tick()
    for _ in range(mh.SILENT_CHECKS):
        move_without_callbacks(s)
    assert not s.notices and not s.installs and not s.hook.recovering


@pytest.mark.parametrize('available,idle', [(False, 0), (True, 90)])
def test_idle_or_secure_desktop_still_pauses_confirmed_loss_retry(mouse, available, idle):
    s = mouse
    s.hook._backoff_step = 99
    s.hook._hook_id = None
    s.hook.watchdog_tick()
    s.available, s.idle = available, idle
    s.advance(10)
    s.hook.watchdog_tick()
    assert not s.installs
    s.available, s.idle, s.tick = True, 0, 101
    s.hook.watchdog_tick()
    assert len(s.installs) == 1


def test_explicit_release_cancels_fast_retry(mouse):
    s = mouse
    s.hook._hook_id = None
    s.hook._backoff_step = 99
    s.hook.watchdog_tick()
    s.hook.release('test release')
    s.advance(100)
    s.hook.watchdog_tick()
    assert not s.installs and s.hook._retry_due is None
