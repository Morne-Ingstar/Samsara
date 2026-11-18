"""1227: deterministic physical-edge fallback. Every native boundary is faked."""
import ctypes
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import mouse_hook as mh
from tests.test_mouse_hold_backstop_1052e import env  # noqa: F401 - isolated app boundaries


@pytest.fixture
def rig(monkeypatch):
    s = SimpleNamespace(now=1000.0, down=False, available=True, idle=0.0, tick=1,
                        events=[], notices=[], installs=[], refreshes=[], pos=(0, 0))
    monkeypatch.setattr(mh, '_perf', lambda: s.now)
    monkeypatch.setattr(mh, '_sleep', lambda seconds: setattr(s, 'now', s.now + seconds))
    monkeypatch.setattr(mh, '_input_status', lambda: (s.available, s.idle, s.tick))
    monkeypatch.setattr(mh, '_button_pressed', lambda _: s.down)
    monkeypatch.setattr(mh, '_call_next_hook_ex', lambda *_: 0)
    monkeypatch.setattr(mh, '_user32', SimpleNamespace(
        PostThreadMessageW=lambda *args: s.refreshes.append(args) or 1))
    monkeypatch.setattr(mh, '_raise_hook_priority', lambda: True)
    monkeypatch.setattr(mh.thread_registry, 'spawn', Mock(side_effect=AssertionError('thread')))
    s.hook = mh.MouseHook(lambda *event: s.events.append(event), suppress_buttons={'mouse5'},
                          poll_buttons={'mouse5'}, on_hook_interrupted=s.notices.append)
    s.hook._hook_id, s.hook._thread_id = 42, 123
    monkeypatch.setattr(s.hook, '_pump_responsive', lambda: True)
    monkeypatch.setattr(s.hook, '_stop_hook_thread', lambda: setattr(s.hook, '_hook_id', None))

    def install():
        s.installs.append(s.now)
        s.hook._hook_id = 43
        s.hook.install_error = ''

    monkeypatch.setattr(s.hook, '_start_hook_thread', install)
    s.hook._poll_hold_buttons()  # observed released before any test press
    return s


def edge(s, pressed):
    info = mh.MSLLHOOKSTRUCT()
    info.mouseData = mh.XBUTTON2 << 16
    return s.hook._hook_callback(0, mh.WM_XBUTTONDOWN if pressed else mh.WM_XBUTTONUP,
                                 ctypes.pointer(info))


def step(s, seconds=mh.BUTTON_POLL_S):
    s.now += seconds
    s.hook._poll_hold_buttons()


@pytest.mark.parametrize('duration', [10.300, 19.200, 36.361, 11.081, 10.973, 15.249,
                                     5.064, 12.186, 1.441, 27.040, .046, 4.055])
def test_all_twelve_outage_windows_retain_next_hold_without_hook_callbacks(rig, duration):
    s = rig
    s.hook._hook_id = None
    s.down = True
    step(s)
    assert s.events == [('mouse5', True)]
    step(s, duration)
    assert len(s.events) == 1
    s.down = False
    step(s)
    assert s.events == [('mouse5', True), ('mouse5', False)]


def test_removed_while_idle_detects_stationary_press_without_cursor_probe(rig):
    s = rig
    s.down = True
    step(s)
    s.hook.watchdog_tick()
    assert not s.installs and s.refreshes == [(123, mh.WM_HOOK_REFRESH, 0, 0)]  # 1254: gapless pump swap
    assert s.notices == ['physical mouse5 press missed hook callback']
    assert s.events == [('mouse5', True)]


def test_removed_while_held_does_not_invent_release_from_suppressed_async_zero(rig):
    s = rig
    edge(s, True)
    s.hook.dispatch_pending()
    s.hook._hook_id = None
    step(s, 1)
    s.hook.watchdog_tick()
    assert s.events == [('mouse5', True)]  # existing active-hold worker owns release
    assert s.installs


def test_game_cursor_recentering_and_blocked_probe_do_not_condemn_working_hook(rig):
    s = rig
    s.hook.watchdog_tick()
    for _ in range(12):
        s.now += 1
        s.pos = (s.pos[0] + 1, 0)
        s.hook.watchdog_tick()
    assert not s.notices and not s.installs and not s.hook.recovering
    assert s.hook.suppress_buttons == {'mouse5'}
    assert s.refreshes == []  # 1254: cursor-only movement is never evidence


def test_delayed_probe_under_cpu_load_is_inconclusive(rig, monkeypatch):
    s = rig
    assert s.hook._self_test() == 'inconclusive'
    assert not s.notices and not s.installs


def test_press_spanning_reinstall_is_delivered_once_then_release_is_retained(rig, monkeypatch):
    s = rig
    install = s.hook._start_hook_thread

    def pressed_during_install():
        s.down = True
        install()

    monkeypatch.setattr(s.hook, '_start_hook_thread', pressed_during_install)
    s.hook._hook_id = None
    s.hook.watchdog_tick()
    assert s.events == [('mouse5', True)]
    s.now += .01
    assert edge(s, True) == 1
    s.hook.dispatch_pending()
    assert s.events == [('mouse5', True)]
    # Down passed through Windows while hook was absent; its up must pass too.
    assert edge(s, False) == 0
    s.hook.dispatch_pending()
    s.down = False
    step(s)
    assert s.events == [('mouse5', True), ('mouse5', False)]


def test_pump_probe_wait_keeps_sampling_missing_press_and_release(rig, monkeypatch):
    # 1254: passive Raw Input supersedes injected self-tests.
    s = rig
    s.down = True
    step(s)
    assert s.hook._self_test() == 'inconclusive'
    s.down = False
    step(s)
    assert s.events == [('mouse5', True), ('mouse5', False)]


def test_delayed_hook_down_cannot_restart_already_released_sampled_hold(rig):
    s = rig
    s.down = True
    step(s)
    s.hook._events.put(('mouse5', True, s.now, False))
    s.down = False
    step(s)
    s.hook._events.put(('mouse5', True, s.now - .02, False))
    s.hook.dispatch_pending()
    assert s.events == [('mouse5', True), ('mouse5', False)]


def test_callback_racing_native_sample_wins(rig, monkeypatch):
    s = rig

    def racing(_):
        edge(s, True)
        return True

    monkeypatch.setattr(mh, '_button_pressed', racing)
    step(s)
    assert not s.events
    s.hook.dispatch_pending()
    assert s.events == [('mouse5', True)]
    assert not s.hook._polled_down


@pytest.mark.parametrize('injected', [False, True])
@pytest.mark.parametrize('sampled_start', [False, True])
@pytest.mark.parametrize('old_stamp', [False, True])
def test_delivered_release_stops_immediately_and_stale_poll_cannot_restart(
        rig, env, injected, sampled_start, old_stamp):
    from tests.test_mouse_hold_backstop_1052e import App
    s = rig
    app = App()
    app._mouse_hook = s.hook
    s.hook.on_button_event = app._on_mouse_button
    s.down = True
    if sampled_start:
        step(s)
    else:
        edge(s, True)
        s.hook.dispatch_pending()
    assert app.calls == ['start'] and app.recording

    # Even a delayed up older than the last sampled down must end capture.
    stamp = s.now - 1 if old_stamp else s.now
    s.hook._deliver(('mouse5', False, stamp, injected))
    assert app.calls == ['start', 'stop'] and not app.recording
    assert not app._hold_down_mouse

    for _ in range(5):
        step(s, .1)  # stale physical down outlasts both poll and bounce windows
    assert app.calls == ['start', 'stop'] and not app.recording
    assert not s.hook._polled_down

    # A fresh observed physical edge can still recover a missed next press.
    s.down = False
    step(s)
    s.down = True
    step(s)
    assert app.calls == ['start', 'stop', 'start'] and app.recording
    s.down = False
    step(s)
    assert app.calls == ['start', 'stop', 'start', 'stop'] and not app.recording


def test_secure_desktop_and_explicit_stop_never_synthesize_edges(rig):
    s = rig
    s.down, s.available = True, False
    step(s)
    assert not s.events
    s.available = True
    s.hook._stopping.set()
    step(s)
    assert not s.events


def test_attach_while_down_requires_release_before_first_press(rig):
    s = rig
    s.hook._poll_armed.clear()
    s.down = True
    step(s)
    assert not s.events
    s.down = False
    step(s)
    s.down = True
    step(s)
    assert s.events == [('mouse5', True)]


def test_polling_failure_does_not_kill_dispatcher(rig, monkeypatch, caplog):
    monkeypatch.setattr(mh, '_button_pressed', Mock(side_effect=OSError('fake refusal')))
    rig.hook._poll_if_due()
    assert 'physical button sample failed' in caplog.text


@pytest.mark.parametrize('mode,expected', [('hold', {'mouse5'}), ('toggle', set()), ('continuous', set())])
def test_app_enables_polling_only_for_main_hold_and_updates_on_mode_change(monkeypatch, mode, expected):
    from tests.test_mouse_hold_backstop_1052e import App
    app = App(mode=mode)
    monkeypatch.setattr(mh.MouseHook, 'start', lambda self: setattr(self, '_hook_id', 42))
    app._install_mouse_listener()
    assert app._mouse_hook.poll_buttons == expected
    app.config['mode'] = 'toggle' if mode == 'hold' else 'hold'
    app.refresh_mouse_hook()
    assert app._mouse_hook.poll_buttons == (set() if mode == 'hold' else {'mouse5'})


def test_unhook_logs_native_result_and_all_time_callback_max(rig, monkeypatch, caplog):
    s = rig
    s.hook.callback_max_ms = 12.3
    monkeypatch.setattr(mh, '_user32', SimpleNamespace(UnhookWindowsHookEx=lambda _: 0))
    monkeypatch.setattr(mh, '_kernel32', SimpleNamespace(GetCurrentThreadId=lambda: 123,
                                                       GetLastError=lambda: 1404))
    with caplog.at_level('INFO'):
        s.hook._unhook('test')
    assert 'removed=False error=1404 callback_max_ms=12.300' in caplog.text


def test_priority_uses_pointer_sized_current_thread_and_above_normal_only(monkeypatch):
    current = Mock(return_value=0x123456789)
    priority = Mock(return_value=1)
    monkeypatch.setattr(mh, '_kernel32', {'GetCurrentThread': current,
                                        'SetThreadPriority': priority})
    assert mh._raise_hook_priority()
    priority.assert_called_once_with(0x123456789, 1)
    assert current.restype is ctypes.c_void_p


def test_sampled_hold_reaches_real_hotkey_start_and_stop_without_audio_or_bootstrap(rig, env):
    from tests.test_mouse_hold_backstop_1052e import App
    s = rig
    app = App()
    app._mouse_hook = s.hook
    s.hook.on_button_event = app._on_mouse_button
    s.down = True
    step(s)
    assert app.calls == ['start'] and app.recording
    s.hook._hook_id = None
    s.hook.watchdog_tick()
    assert app.calls == ['start'] and app.recording
    s.down = False
    step(s)
    assert app.calls == ['start', 'stop'] and not app.recording


def test_callback_worst_case_with_faked_native_chain(rig, monkeypatch, record_property):
    s = rig
    monkeypatch.setattr(mh, '_perf', time.perf_counter)
    info = mh.MSLLHOOKSTRUCT()
    info.mouseData = mh.XBUTTON2 << 16
    pointer = ctypes.pointer(info)
    durations = []
    for _ in range(2000):
        began = time.perf_counter()
        s.hook._hook_callback(0, mh.WM_XBUTTONDOWN, pointer)
        durations.append((time.perf_counter() - began) * 1000)
    record_property('callbacks_measured', len(durations))
    record_property('worst_outer_ms', max(durations))
    record_property('worst_callback_ms', s.hook.callback_max_ms)
    assert not s.events  # callback never executes the app handler
    assert max(durations) < 50  # generous scheduler allowance; report actual maximum


@pytest.mark.parametrize('old_present', [False, True])
def test_refresh_installs_before_unhooking_and_preserves_delivery(rig, monkeypatch, old_present, caplog):
    s = rig
    operations = []
    monkeypatch.setattr(mh, '_set_windows_hook_ex', lambda: lambda *_: operations.append('install') or 43)
    monkeypatch.setattr(mh._user32, 'UnhookWindowsHookEx',
                        lambda handle: operations.append(('unhook', handle)) or old_present, raising=False)
    monkeypatch.setattr(mh, '_kernel32', SimpleNamespace(GetLastError=lambda: 1404))
    s.hook._request_refresh('probe silence unconfirmed')
    s.hook._refresh_on_hook_thread()
    assert operations == ['install', ('unhook', 42)]
    assert s.hook._hook_id == 43 and not s.hook._refresh_pending
    with caplog.at_level('INFO'):
        assert s.hook.dispatch_pending() == 0
    assert f'old_removed={old_present}' in caplog.text
    assert 'error=1404' in caplog.text if not old_present else 'error=0' in caplog.text
    assert edge(s, True) == 1
    s.hook.dispatch_pending()
    assert s.events == [('mouse5', True)] and not s.notices


def test_refresh_failure_keeps_old_hook_and_stop_cancels_request(rig, monkeypatch):
    s = rig
    monkeypatch.setattr(mh, '_set_windows_hook_ex', lambda: lambda *_: 0)
    monkeypatch.setattr(mh, '_kernel32', SimpleNamespace(GetLastError=lambda: 5))
    s.hook._refresh_on_hook_thread()
    assert s.hook._hook_id == 42 and not s.hook._refresh_pending
    s.hook.dispatch_pending()
    s.hook._stopping.set()
    s.hook._request_refresh('stopped')
    assert not s.refreshes


def test_real_pump_dispatches_refresh_on_installing_thread(rig, monkeypatch):
    s = rig
    messages = iter([mh.WM_HOOK_REFRESH, None])

    def message(pointer, *_):
        value = next(messages)
        if value is None:
            return 0
        ctypes.cast(pointer, ctypes.POINTER(ctypes.wintypes.MSG)).contents.message = value
        return 1

    monkeypatch.setattr(mh, '_set_windows_hook_ex', lambda: lambda *_: 42)
    monkeypatch.setattr(mh, '_kernel32', SimpleNamespace(GetCurrentThreadId=lambda: 123))
    monkeypatch.setattr(mh._user32, 'GetMessageW', message, raising=False)
    monkeypatch.setattr(mh._user32, 'UnhookWindowsHookEx', lambda _: 1, raising=False)
    refresh = Mock()
    monkeypatch.setattr(s.hook, '_refresh_on_hook_thread', refresh)
    s.hook._run()
    refresh.assert_called_once_with()


def test_failed_old_hook_cleanup_retains_both_handles_until_dispatcher_restarts(rig, monkeypatch):
    s = rig
    released = []
    monkeypatch.setattr(mh, '_set_windows_hook_ex', lambda: lambda *_: 43)
    monkeypatch.setattr(mh._user32, 'UnhookWindowsHookEx', lambda _: 0, raising=False)
    monkeypatch.setattr(mh, '_kernel32', SimpleNamespace(GetLastError=lambda: 5,
                                                       GetCurrentThreadId=lambda: 123))
    s.hook._refresh_on_hook_thread()
    assert s.hook._retired_hooks == [42] and s.hook._hook_id == 43
    monkeypatch.setattr(mh._user32, 'UnhookWindowsHookEx', lambda handle: released.append(handle) or 1)
    monkeypatch.setattr(s.hook, '_stop_hook_thread', lambda: s.hook._unhook('test'))
    s.hook.dispatch_pending()
    # 1254: cleanup is retried on the pump under backoff, without a gap.
    assert released == [] and s.hook._retired_hooks == [42]
    assert s.hook._retry_due == s.now + 30 and not s.notices
    s.hook._refresh_on_hook_thread()
    assert released == [42, 43] and not s.hook._retired_hooks

from tests.test_mouse_hook import fake_raw_window  # noqa: F401 - 1254 native isolation
