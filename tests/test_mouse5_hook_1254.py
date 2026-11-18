"""1254: passive recovery. All native boundaries are fakes; no hook/input."""
import ctypes
import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import mouse_hook as mh
from tests.test_lost_recording_1042 import capture  # noqa: F401 - isolated real recording state

_FOREGROUND_CHECK = mh._foreground_not_elevated


@pytest.fixture
def rig(monkeypatch):
    s = SimpleNamespace(now=1000.0, events=[], posts=[], native=[])
    monkeypatch.setattr(mh, '_perf', lambda: s.now)
    monkeypatch.setattr(mh, '_input_status', lambda: (True, 0, 100))
    monkeypatch.setattr(mh, '_foreground_not_elevated', lambda: True)
    monkeypatch.setattr(mh, '_button_pressed', lambda _: False)
    monkeypatch.setattr(mh, '_call_next_hook_ex', lambda *_: 0)
    monkeypatch.setattr(mh, '_user32', SimpleNamespace(
        PostThreadMessageW=lambda *args: s.posts.append(args) or 1,
        UnhookWindowsHookEx=lambda handle: s.native.append(('unhook', handle)) or 1))
    monkeypatch.setattr(mh, '_kernel32', SimpleNamespace(
        GetTickCount=lambda: 1234, GetLastError=lambda: 0, GetCurrentThreadId=lambda: 123))
    monkeypatch.setattr(mh.thread_registry, 'spawn', Mock(side_effect=AssertionError('thread')))
    s.hook = mh.MouseHook(lambda *edge: s.events.append(edge), suppress_buttons={'mouse5'},
                          poll_buttons={'mouse5'})
    s.hook._hook_id, s.hook._thread_id = 0x123456789, 123
    return s


def ll(s, pressed, *, name='mouse5', tick=0):
    info = mh.MSLLHOOKSTRUCT()
    info.mouseData = (mh.XBUTTON2 if name == 'mouse5' else mh.XBUTTON1) << 16
    info.time = tick
    return s.hook._hook_callback(0, mh.WM_XBUTTONDOWN if pressed else mh.WM_XBUTTONUP,
                                 ctypes.pointer(info))


def raw(s, flags=0, *, device=1, motion=0):
    packet = mh.RAWINPUT()
    packet.header.dwSize = ctypes.sizeof(packet)
    packet.header.hDevice = device
    packet.mouse.usButtonFlags = flags
    packet.mouse.lLastX = motion
    s.hook._raw_mouse(packet)


def test_x64_native_contracts_preserve_full_handles(rig, monkeypatch):
    # A real ctypes call boundary to a Python fake proves argument conversion.
    s = rig
    handle = 0xFEDCBA987654321
    observed = []
    native_unhook = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.c_void_p)(
        lambda value: observed.append(value) or 1)
    native_set = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_int, mh.LowLevelMouseProc,
                                   ctypes.c_void_p, ctypes.wintypes.DWORD)(lambda *_: handle)
    native_set.argtypes = None
    native_set.restype = ctypes.c_int  # simulate untouched windll defaults
    monkeypatch.setattr(mh, '_user32', SimpleNamespace(
        SetWindowsHookExW=native_set, UnhookWindowsHookEx=native_unhook))
    assert mh._set_windows_hook_ex()(mh.WH_MOUSE_LL, s.hook._proc, None, 0) == handle
    s.hook._hook_id = handle
    s.hook._unhook('fake native boundary')
    assert observed == [handle]


def test_foreign_hook_contract_is_left_untouched(rig, monkeypatch):
    foreign = Mock()
    foreign.argtypes = (ctypes.c_int, object())
    foreign.restype = ctypes.c_int
    fresh = Mock(return_value=0x123456789)
    api = SimpleNamespace(SetWindowsHookExW=foreign)
    class Library:
        SetWindowsHookExW = api.SetWindowsHookExW
        def __getitem__(self, name):
            assert name == 'SetWindowsHookExW'
            return fresh
    monkeypatch.setattr(mh, '_user32', Library())
    assert mh._set_windows_hook_ex() is fresh
    assert foreign.restype is ctypes.c_int
    assert fresh.restype is ctypes.c_void_p


def test_unhook_exception_retains_old_handle_and_suppression(rig, monkeypatch):
    s = rig
    old = s.hook._hook_id
    monkeypatch.setattr(mh, '_set_windows_hook_ex', lambda: lambda *_: 0x123456790)
    monkeypatch.setattr(mh, '_unhook_windows_hook_ex', Mock(side_effect=ctypes.ArgumentError('fake')))
    s.hook._refresh_on_hook_thread()
    assert s.hook._retired_hooks == [old]
    assert s.hook._hook_id == 0x123456790
    assert s.hook.suppress_buttons == {'mouse5'}
    s.hook.dispatch_pending()
    assert s.hook._retry_due == s.now + 30


def test_cleanup_retry_survives_callbacks_and_failed_final_unhook(rig, monkeypatch):
    s = rig
    s.hook._retired_hooks = [77]
    s.hook._begin_recovery('cleanup pending')
    s.hook._schedule_retry('cleanup pending', refresh=True)
    due = s.hook._retry_due
    ll(s, True)
    s.hook.dispatch_pending()
    assert s.hook._retry_due == due and s.hook._refresh_backoff
    monkeypatch.setattr(mh, '_unhook_windows_hook_ex', Mock(side_effect=OSError('fake')))
    s.hook._unhook('failed cleanup')
    assert s.hook._retired_hooks == [77, 0x123456789]


@pytest.mark.parametrize('injected', [False, True])
def test_partial_host_release_reaches_recording_finalization(capture, monkeypatch, injected):
    # Re-execute the existing 1042 regression body against the initialized
    # edge-state guard, with its original PCM/state fixture and assertions.
    from tests.test_lost_recording_1042 import test_delivered_mouse_release_ends_hold_regardless_of_injection_flag
    test_delivered_mouse_release_ends_hold_regardless_of_injection_flag(capture, monkeypatch, injected)


def test_refresh_budget_backoff_and_install_before_unhook(rig, monkeypatch):
    s = rig
    def install(*_):
        s.native.append('install')
        return s.hook._hook_id + 1
    monkeypatch.setattr(mh, '_set_windows_hook_ex', lambda: install)
    for _ in range(mh.REINSTALL_LIMIT):
        s.hook._request_refresh('physical raw loss')
        assert s.hook._refresh_pending
        s.hook._refresh_on_hook_thread()
        s.hook.dispatch_pending()
        assert s.native[-2] == 'install' and s.native[-1][0] == 'unhook'
    s.hook._request_refresh('repeated loss')
    due = s.hook._retry_due
    assert due == s.now + 30 and len(s.posts) == 3
    for _ in range(20):
        s.now += 1
        s.hook._request_refresh('repeated loss')
        s.hook.watchdog_tick()
    assert len(s.posts) == 3 and s.hook._retry_due == due
    s.now = due
    s.hook.watchdog_tick()
    assert len(s.posts) == 4
    s.hook._refresh_on_hook_thread()
    s.hook.dispatch_pending()
    s.hook._request_refresh('still silent')
    assert s.hook._retry_due == s.now + 30  # active cap; no 3-second churn
    assert s.hook._backoff_step == 2 and s.hook.suppress_buttons == {'mouse5'}


@pytest.mark.parametrize('name,down,up', [('mouse4', 0x40, 0x80), ('mouse5', 0x100, 0x200)])
@pytest.mark.parametrize('raw_first', [False, True])
def test_duplicate_native_edges_collapse_for_all_bindings(rig, name, down, up, raw_first):
    s = rig
    s.hook.poll_buttons = frozenset()  # toggle and command bindings also deduplicate
    for pressed, flag in ((True, down), (False, up)):
        if raw_first:
            raw(s, flag)
            ll(s, pressed, name=name)
        else:
            ll(s, pressed, name=name)
            raw(s, flag)
        s.hook.dispatch_pending()
        s.now += .1
    assert s.events == [(name, True), (name, False)]


def test_raw_release_ends_suppressed_hook_hold_even_while_moving(rig):
    s = rig
    assert ll(s, True) == 1
    s.hook.dispatch_pending()
    for _ in range(80):
        raw(s, motion=1)
        s.now += .001
    s.hook._poll_hold_buttons()  # async zero must not release a suppressed down
    assert s.events == [('mouse5', True)]
    raw(s, 0x200)
    s.hook.dispatch_pending()
    assert s.events == [('mouse5', True), ('mouse5', False)]
    raw(s, 0x100)
    s.hook.dispatch_pending()
    assert s.events[-1] == ('mouse5', True)  # re-armed by physical edge, despite motion


def test_raw_press_and_release_retain_hold_when_hook_is_silent(rig):
    s = rig
    raw(s, 0x100)
    s.hook.dispatch_pending()
    raw(s, 0x200)
    s.hook.dispatch_pending()
    assert s.events == [('mouse5', True), ('mouse5', False)]


def test_raw_xbutton_waits_twenty_ms_and_late_ll_prevents_loss(rig):
    s = rig
    raw(s, 0x100)
    assert s.hook._hook_lost() == ''
    s.now += .019
    assert s.hook._hook_lost() == ''
    ll(s, True)
    s.now += .002
    assert s.hook._hook_lost() == ''
    raw(s, 0x200)
    s.now += .021
    assert '20 ms' in s.hook._hook_lost()


@pytest.mark.parametrize('safe_foreground', [False, True])
def test_physical_motion_threshold_and_foreground_guard(rig, monkeypatch, safe_foreground):
    s = rig
    monkeypatch.setattr(mh, '_foreground_not_elevated', lambda: safe_foreground)
    for _ in range(49):
        raw(s, motion=1)
    assert s.hook._hook_lost() == ''
    raw(s, motion=1)
    assert bool(s.hook._hook_lost()) is safe_foreground
    assert s.hook._hook_lost() == ''  # consume evidence once


def test_injected_null_device_and_old_raw_motion_never_condemn_hook(rig):
    s = rig
    for _ in range(100):
        raw(s, 0x100, device=None, motion=1)
    assert not s.hook._raw_activity and not s.hook._raw_edges
    assert not s.events and s.hook._hook_lost() == ''
    for _ in range(50):
        raw(s, motion=1)
    s.now += 1.001
    assert s.hook._hook_lost() == ''


def test_real_callback_prevents_motion_loss(rig):
    s = rig
    for _ in range(50):
        raw(s, motion=1)
    ll(s, True)
    assert s.hook._hook_lost() == ''


def test_mouse_movement_during_poll_does_not_prevent_rearming(rig, monkeypatch):
    s = rig
    def sample(_):
        info = mh.MSLLHOOKSTRUCT()
        s.hook._hook_callback(0, 0x0200, ctypes.pointer(info))
        return False
    monkeypatch.setattr(mh, '_button_pressed', sample)
    s.hook._poll_hold_buttons()
    assert s.hook._poll_armed == {'mouse5'}


@pytest.mark.parametrize('elevated,success', [(False, True), (True, True), (False, False)])
def test_foreground_elevation_query_fails_closed_and_closes_handles(rig, monkeypatch, elevated, success):
    closed = []
    def native(lib, name, args, result):
        if name == 'CloseHandle':
            return lambda handle: closed.append(handle.value if hasattr(handle, 'value') else handle) or 1
        if name in ('GetForegroundWindow', 'OpenProcess'):
            return lambda *_: 0x123456789
        if name == 'GetWindowThreadProcessId':
            return lambda *_: 1
        if name == 'OpenProcessToken':
            def token(_process, _access, output):
                ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 88
                return 1
            return token
        assert name == 'GetTokenInformation'
        def information(_token, kind, output, _size, _needed):
            assert kind == 20
            ctypes.cast(output, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = elevated
            return success
        return information
    monkeypatch.setattr(mh, '_native', native)
    assert _FOREGROUND_CHECK() is (success and not elevated)
    assert closed == [88, 0x123456789]


def test_no_cursor_or_injected_probe_in_product_source(rig, monkeypatch):
    s = rig
    assert 'SendInput' not in inspect.getsource(mh)
    assert 'GetCursorPos' not in inspect.getsource(mh)
    for _ in range(120):
        s.now += 1
        s.hook.watchdog_tick()
    assert not s.posts and not s.hook.recovering


def test_event_time_logs_pre_gil_latency_and_unsigned_wrap(rig, monkeypatch, caplog):
    s = rig
    monkeypatch.setattr(mh._kernel32, 'GetTickCount', lambda: 12)
    ll(s, True, tick=0xFFFFFFF0)
    assert s.hook.hook_latency_max_ms == 28
    with caplog.at_level('WARNING'):
        s.hook._report_counters()
    assert 'event-to-Python latency=28 ms' in caplog.text


def test_raw_struct_layout_matches_win64():
    assert ctypes.sizeof(mh.RAWMOUSE) == 24
    assert mh.RAWMOUSE.u.offset == 4
    assert ctypes.sizeof(mh.RAWINPUTHEADER) == 24
    assert mh.RAWINPUT.mouse.offset == 24


def test_window_registration_decode_cleanup_and_native_widths(rig, monkeypatch):
    s = rig
    calls = []
    packet = mh.RAWINPUT()
    packet.header.hDevice, packet.header.dwSize = 55, ctypes.sizeof(packet)
    packet.mouse.usButtonFlags = 0x100
    def native(lib, name, args, result):
        calls.append((name, args, result))
        if name == 'GetModuleHandleW':
            return lambda *_: 0x123456789
        if name == 'RegisterClassW':
            return lambda *_: 1
        if name == 'CreateWindowExW':
            def create(*values):
                assert values[8] == -3 and values[10] == 0x123456789
                return 0x223456789
            return create
        if name == 'RegisterRawInputDevices':
            def register(pointer, count, size):
                value = ctypes.cast(pointer, ctypes.POINTER(mh.RAWINPUTDEVICE)).contents
                assert count == 1 and size == ctypes.sizeof(value)
                assert (value.usUsagePage, value.usUsage) == (1, 2)
                assert (value.dwFlags, value.hwndTarget) in ((mh.RIDEV_INPUTSINK, 0x223456789), (1, None))
                return 1
            return register
        if name == 'GetRawInputData':
            def read(handle, command, output, size, header_size):
                assert handle == 0x323456789 and command == mh.RID_INPUT
                assert header_size == ctypes.sizeof(mh.RAWINPUTHEADER)
                ctypes.memmove(output, ctypes.byref(packet), ctypes.sizeof(packet))
                return ctypes.sizeof(packet)
            return read
        return lambda *_: 0
    monkeypatch.setattr(mh, '_native', native)
    s.hook._start_raw_input()
    s.hook._raw_window_proc(s.hook._raw_window, mh.WM_INPUT, 0, 0x323456789)
    s.hook.dispatch_pending()
    assert s.events == [('mouse5', True)]
    s.hook._stop_raw_input()
    assert not s.hook._raw_window and not s.hook._raw_class
    assert any(name == 'DefWindowProcW' and result is ctypes.c_ssize_t for name, _, result in calls)
    assert [name for name, _, _ in calls][-3:] == ['RegisterRawInputDevices', 'DestroyWindow', 'UnregisterClassW']
