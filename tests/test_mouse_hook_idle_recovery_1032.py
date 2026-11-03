"""1032: returning real input outranks an injected probe; native calls are faked."""

import ctypes
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import samsara.mouse_hook as mh
from tests.test_mouse_hook import fallback_app  # noqa: F401
from tests.test_mouse_hook_867 import recovery  # noqa: F401
from tests.test_mouse_blip_quiet_1019 import notice_clock  # noqa: F401


def fire(hook, monkeypatch, message=0x0200, *, flags=0, tag=0, button=mh.XBUTTON2):
    info = mh.MSLLHOOKSTRUCT()
    info.mouseData, info.flags, info.dwExtraInfo = button << 16, flags, tag
    monkeypatch.setattr(mh, '_call_next_hook_ex', lambda *_: 0)
    return hook._hook_callback(0, message, ctypes.pointer(info))


def waiting(s, *, step=3):
    s.hook._begin_recovery('missed injected probe')
    s.hook._backoff_step = step
    s.hook._schedule_retry('missed injected probe')
    assert s.hook.installed


def activity(s, monkeypatch, *, available=True, idle=0, tick=100):
    s.hook._thread_id = 123
    monkeypatch.setattr(mh, '_input_status', lambda: (available, idle, tick))


def test_failed_replacement_keeps_listening_through_five_minute_backoff(recovery, monkeypatch):
    s = recovery
    s.hook._begin_recovery('lost')
    s.hook._backoff_step = 3
    stop = MagicMock()
    monkeypatch.setattr(s.hook, '_stop_hook_thread', stop)
    monkeypatch.setattr(s.hook, '_self_test', lambda: 'dead')
    s.hook.watchdog_tick()
    assert s.hook.installed and not s.hook.suppress_buttons
    assert s.hook._retry_due == s.clock.now + 300
    stop.assert_not_called()
    assert not s.installs and len(s.notices) == 1


@pytest.mark.parametrize('message', [0x0200, 0x0201, 0x020A, mh.WM_XBUTTONDOWN])
def test_any_real_event_wakes_dispatcher_and_restores_binding(recovery, monkeypatch, message):
    s = recovery
    waiting(s)
    fire(s.hook, monkeypatch, message)
    s.hook.dispatch_pending()  # no watchdog interval or retry-deadline wait
    assert not s.hook.recovering and s.hook._retry_due is None
    assert s.hook.suppress_buttons == frozenset({'mouse4', 'mouse5'})
    assert s.recovered == [True] and not s.installs


def test_first_mouse5_press_recovers_app_before_recording_and_cancels_notice(
        fallback_app, recovery, monkeypatch, notice_clock):
    app, s = fallback_app, recovery
    app.config['hotkey'] = 'mouse5'
    delivered = []

    def factory(**kwargs):
        s.hook.on_hook_interrupted = kwargs['on_hook_interrupted']
        s.hook.on_hook_recovered = kwargs['on_hook_recovered']

        def route(*event):
            delivered.append(app._main_hotkey_override)
            kwargs['on_button_event'](*event)

        s.hook.on_button_event = route
        return s.hook

    monkeypatch.setattr(mh, 'MouseHook', factory)
    monkeypatch.setattr(s.hook, 'start', lambda: None)
    app._install_mouse_listener()
    waiting(s)
    assert app._main_hotkey_override == 'ctrl+shift'
    assert fire(s.hook, monkeypatch, mh.WM_XBUTTONDOWN) == 1
    assert not app.recording  # no application work on the native callback
    assert s.hook.dispatch_pending() == 1
    assert delivered == [None] and app.recording
    assert app.calls == [('start', False)]
    assert app.mouse_hotkey_status()['state'] == 'active'
    notice_clock.advance(10)
    assert app.balloons == [] and app.config['hotkey'] == 'mouse5'


def test_unrequested_button_passes_through_on_first_returning_event(recovery, monkeypatch):
    s = recovery
    s.hook.requested_suppress_buttons = frozenset({'mouse5'})
    waiting(s)
    assert fire(s.hook, monkeypatch, mh.WM_XBUTTONDOWN, button=mh.XBUTTON1) == 0


def test_injected_returning_event_is_also_liveness_without_inline_app_work(recovery, monkeypatch):
    s = recovery
    waiting(s)
    assert fire(s.hook, monkeypatch, mh.WM_XBUTTONDOWN, flags=mh.LLMHF_INJECTED) == 0
    assert s.recovered == []
    s.hook.dispatch_pending()
    assert s.recovered == [True]


def test_late_real_callback_overrides_dead_probe_verdict(recovery, monkeypatch):
    s = recovery
    s.hook._begin_recovery('lost')

    def delayed():
        fire(s.hook, monkeypatch)
        return 'dead'

    monkeypatch.setattr(s.hook, '_self_test', delayed)
    s.hook.watchdog_tick()
    assert s.recovered == [True] and not s.installs


def test_waiting_listener_is_replaced_at_deadline_if_no_events_arrive(recovery):
    s = recovery
    waiting(s)
    s.clock.sleep(300)
    s.hook.watchdog_tick()
    assert len(s.installs) == 1 and s.hook.installed
    assert s.hook.recovering and not s.hook.suppress_buttons


@pytest.mark.parametrize('available,idle', [(False, 0), (True, mh.INPUT_IDLE_S)])
def test_idle_or_other_desktop_pauses_loss_and_due_retries(recovery, monkeypatch, available, idle):
    s = recovery
    activity(s, monkeypatch, available=available, idle=idle)
    probe = MagicMock(return_value='dead')
    monkeypatch.setattr(s.hook, '_self_test', probe)
    monkeypatch.setattr(mh, '_cursor_pos', lambda: (999, 999))
    for _ in range(10):
        s.hook.watchdog_tick()
        s.clock.sleep(1)
    assert not s.hook.recovering and not s.installs and not s.notices
    waiting(s)
    s.clock.sleep(301)
    s.hook.watchdog_tick()
    assert not s.installs
    probe.assert_not_called()


@pytest.mark.parametrize('available,idle', [(False, 0), (True, 90)])
def test_return_from_idle_or_other_desktop_bypasses_remaining_backoff(
        recovery, monkeypatch, available, idle):
    s = recovery
    waiting(s)
    activity(s, monkeypatch, available=available, idle=idle)
    s.hook.watchdog_tick()
    activity(s, monkeypatch, tick=101)
    s.hook.watchdog_tick()
    assert s.installs == [s.clock.now]
    assert s.hook._retry_due is None and s.hook.recovering


@pytest.mark.parametrize('verdict', ['alive', 'dead', 'inconclusive'])
def test_return_rechecks_previously_healthy_hook_without_waiting_for_movement_window(
        recovery, monkeypatch, verdict):
    s = recovery
    activity(s, monkeypatch, idle=90)
    s.hook.watchdog_tick()
    probe = MagicMock(return_value=verdict)
    monkeypatch.setattr(s.hook, '_self_test', probe)
    s.hook._backoff_step = 99  # returning user bypasses stale escalation too
    activity(s, monkeypatch, tick=101)
    s.hook.watchdog_tick()
    probe.assert_called_once()
    assert len(s.installs) == (1 if verdict == 'dead' else 0)
    assert s.hook._retry_due is None


def test_first_input_after_outage_bypasses_backoff_even_without_idle(recovery, monkeypatch):
    s = recovery
    activity(s, monkeypatch, tick=100)
    s.hook.watchdog_tick()
    waiting(s)
    activity(s, monkeypatch, tick=101)
    s.hook.watchdog_tick()
    assert s.installs == [s.clock.now]
    # Persistent input cannot force a tight reinstall loop after that one wake.
    s.hook._schedule_retry('still silent')
    activity(s, monkeypatch, tick=102)
    s.hook.watchdog_tick()
    assert len(s.installs) == 1
    assert s.hook._retry_due == s.clock.now + 30


def test_own_probe_does_not_consume_first_real_input_retry(recovery, monkeypatch):
    s = recovery
    activity(s, monkeypatch, tick=100)
    s.hook.watchdog_tick()

    def inject():
        activity(s, monkeypatch, tick=101)  # SendInput updates the session tick
        return True

    monkeypatch.setattr(mh, '_send_zero_move', inject)
    monkeypatch.setattr(s.hook, '_pump_responsive', lambda: True)
    assert s.hook._self_test() == 'dead'
    waiting(s)
    s.hook.watchdog_tick()
    assert not s.installs and not s.hook._input_retry_used
    activity(s, monkeypatch, tick=102)
    s.hook.watchdog_tick()
    assert len(s.installs) == 1


def test_own_probes_cannot_keep_an_idle_session_active(recovery, monkeypatch):
    s = recovery
    activity(s, monkeypatch, tick=100)
    s.hook.watchdog_tick()
    s.hook._probe_input_tick = 101
    activity(s, monkeypatch, tick=101, idle=0)  # own injection just reset OS idle
    s.clock.sleep(mh.INPUT_IDLE_S)
    s.hook.watchdog_tick()
    assert s.hook._input_paused and not s.hook._activity_active


@pytest.mark.parametrize('install_failed', [False, True])
def test_active_retry_cap_applies_to_loss_and_install_failure(recovery, monkeypatch, install_failed):
    s = recovery
    activity(s, monkeypatch)
    s.hook.watchdog_tick()
    s.hook._backoff_step = 99
    s.hook._schedule_retry('outage', install_failed=install_failed)
    assert s.hook._retry_due == s.clock.now + 30


def test_already_active_user_does_not_wait_five_minutes(recovery, monkeypatch):
    s = recovery
    waiting(s)
    activity(s, monkeypatch)
    s.hook._input_retry_used = True
    s.hook.watchdog_tick()
    assert s.hook._retry_due == s.clock.now + 30
    s.clock.sleep(30)
    s.hook.watchdog_tick()
    assert s.installs == [s.clock.now]


def test_real_event_still_recovers_when_desktop_sample_is_unavailable(recovery, monkeypatch):
    s = recovery
    waiting(s)
    activity(s, monkeypatch, available=False)
    fire(s.hook, monkeypatch)
    s.hook.watchdog_tick()
    assert s.recovered == [True] and s.hook._retry_due is None


def test_missing_input_sample_preserves_conservative_existing_probe_rules(recovery, monkeypatch):
    s = recovery
    s.hook._thread_id = 123
    monkeypatch.setattr(mh, '_input_status', lambda: None)
    waiting(s)
    s.hook.watchdog_tick()
    assert s.hook._retry_due == s.clock.now + 300
    assert not s.installs


def test_no_cursor_movement_is_not_a_loss(recovery, monkeypatch):
    s = recovery
    activity(s, monkeypatch)
    probe = MagicMock()
    monkeypatch.setattr(s.hook, '_self_test', probe)
    for _ in range(70):
        s.hook.watchdog_tick()
        s.clock.sleep(1)
    probe.assert_not_called()
    assert not s.installs and not s.notices


def test_uipi_refused_probe_is_inconclusive(recovery, monkeypatch, caplog):
    monkeypatch.setattr(mh, '_send_zero_move', lambda: False)
    with caplog.at_level(logging.INFO, logger=mh.logger.name):
        assert recovery.hook._self_test() == 'inconclusive'
    assert 'accepted=False' in caplog.text and 'callbacks=0' in caplog.text


def test_tagged_probe_records_flags_injection_timing_and_pump_state(recovery, monkeypatch, caplog):
    s = recovery
    s.hook._loop_state = 'GetMessageW'

    def send():
        s.clock.sleep(.01)
        fire(s.hook, monkeypatch, flags=mh.LLMHF_INJECTED, tag=mh.PROBE_TAG)
        return True

    monkeypatch.setattr(mh, '_send_zero_move', send)
    with caplog.at_level(logging.INFO, logger=mh.logger.name):
        assert s.hook._self_test() == 'alive'
    for text in ('tag_seen=True', 'probe_flags=1', 'probe_injected=True',
                 'probe_after_ms=10.0', 'loop=GetMessageW', 'callbacks=1'):
        assert text in caplog.text


def test_real_event_can_answer_probe_without_matching_tag(recovery, monkeypatch, caplog):
    s = recovery
    monkeypatch.setattr(mh, '_send_zero_move', lambda: fire(s.hook, monkeypatch) == 0)
    with caplog.at_level(logging.INFO, logger=mh.logger.name):
        assert s.hook._self_test() == 'alive'
    assert 'tag_seen=False' in caplog.text and 'last_flags=0' in caplog.text


@pytest.mark.parametrize('pump,verdict', [(True, 'dead'), (False, 'inconclusive')])
def test_missed_probe_logs_pump_result_and_no_received_events(recovery, monkeypatch, caplog, pump, verdict):
    s = recovery
    s.hook._thread_id = 123
    s.hook._loop_state = 'GetMessageW'
    monkeypatch.setattr(mh, '_send_zero_move', lambda: True)

    def post(_thread, _message, sequence, _param):
        if pump:
            s.hook._pump_ack = sequence
        return 1

    monkeypatch.setattr(mh._user32, 'PostThreadMessageW', post)
    with caplog.at_level(logging.INFO, logger=mh.logger.name):
        assert s.hook._self_test() == verdict
    assert f'pump_ok={pump}' in caplog.text and 'callbacks=0' in caplog.text
    assert 'tag_seen=False' in caplog.text


def test_send_input_has_diagnostic_tag_without_moving_cursor(monkeypatch):
    def send(count, pointer, size):
        event = ctypes.cast(pointer, ctypes.POINTER(mh._INPUT)).contents
        assert count == 1 and size == ctypes.sizeof(mh._INPUT)
        assert (event.mi.dx, event.mi.dy, event.mi.dwFlags) == (0, 0, mh.MOUSEEVENTF_MOVE)
        assert event.mi.dwExtraInfo == mh.PROBE_TAG
        return 1

    monkeypatch.setattr(mh._user32, 'SendInput', send)
    assert mh._send_zero_move()


def test_manual_reenable_forces_retry_without_waiting_for_deadline(
        fallback_app, recovery, monkeypatch, notice_clock):
    app, s = fallback_app, recovery
    app._mouse_hook = s.hook
    waiting(s)
    installed = []
    monkeypatch.setattr(app, '_install_mouse_listener', lambda: installed.append(True))
    app.reenable_mouse_hotkey()
    assert installed == [True] and s.hook._stopping.is_set()
    assert s.hook._retry_due is None


def test_deliberate_release_is_never_rearmed_by_input(recovery, monkeypatch):
    s = recovery
    waiting(s)
    s.hook.release('test deliberate release')
    activity(s, monkeypatch, tick=999)
    s.hook.watchdog_tick()
    assert not s.installs and not s.recovered and s.hook._retry_due is None


@pytest.mark.parametrize('input_name,own_name,opened,expected', [
    ('Default', 'Default', True, True),
    ('Winlogon', 'Default', True, False),
    ('Default', 'Default', False, False),
])
def test_native_input_status_compares_desktops_and_closes_handle(
        monkeypatch, input_name, own_name, opened, expected):
    handles = iter([0x1_0000_0001, 0x1_0000_0002])
    input_handle, own_handle = next(handles), next(handles)

    def last_input(pointer):
        # LASTINPUTINFO consists of two UINT32 members.
        ctypes.cast(pointer, ctypes.POINTER(ctypes.wintypes.DWORD))[1] = 0xFFFFFF00
        return 1

    def name(handle, _index, buffer, _size, _needed):
        buffer.value = input_name if handle == input_handle else own_name
        return 1

    functions = {
        'OpenInputDesktop': MagicMock(return_value=input_handle if opened else 0),
        'GetThreadDesktop': MagicMock(return_value=own_handle),
        'GetUserObjectInformationW': MagicMock(side_effect=name),
        'CloseDesktop': MagicMock(return_value=1),
    }
    dll = MagicMock()
    dll.GetLastInputInfo.side_effect = last_input
    dll.__getitem__.side_effect = functions.__getitem__
    monkeypatch.setattr(mh, '_user32', dll)
    monkeypatch.setattr(mh, '_kernel32', SimpleNamespace(
        GetTickCount=lambda: 0x100, GetCurrentThreadId=lambda: 123))
    assert mh._input_status() == (expected, .512, 0xFFFFFF00)
    if opened:
        functions['CloseDesktop'].assert_called_once_with(input_handle)
    else:
        functions['CloseDesktop'].assert_not_called()


def test_failed_native_input_sample_is_unknown(monkeypatch):
    dll = MagicMock()
    dll.GetLastInputInfo.return_value = 0
    monkeypatch.setattr(mh, '_user32', dll)
    assert mh._input_status() is None
    dll.__getitem__.assert_not_called()
