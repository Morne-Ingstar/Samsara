"""1052e: no native hook, audio device, app bootstrap or live configuration."""

import ctypes
import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import hold_backstop as hb, hotkeys, mouse_hook as mh


class App(hotkeys.HotkeyCluster):
    def __init__(self, binding='mouse5', mode='hold'):
        self.config = {'hotkey': binding, 'mode': mode, 'command_mode': {'enabled': False}}
        self._running = True
        self.recording = self.snoozed = self._stop_in_flight = False
        self.hotkey_pressed = self.toggle_active = self.continuous_active = False
        self.command_mode_recording = self._memo_recording = self._hotkey_recording = False
        self._hold_down_key = self._hold_down_mouse = False
        self._main_hotkey_source = 'key'
        self._streaming_session = None
        self.current_keys = set()
        self.key_press_times = {}
        self.calls = []
        self._mouse_hook = mh.MouseHook(self._on_mouse_button,
                                        on_hook_interrupted=self._on_mouse_hook_failed)
        self._mouse_hook._hook_id = 123  # no native hook is ever installed
        self._show_outcome_chip = Mock()
        self._check_command_mode_key = Mock()
        self._hands_free_dictation_commit_available = lambda: False

    def start_recording(self, **kwargs):
        self.calls.append('start')
        self.recording = self._hotkey_recording = True

    def stop_recording(self):
        self.calls.append('stop')
        self.recording = False


@pytest.fixture
def env(monkeypatch):
    from samsara import decode_recovery, speech_readiness
    workers = []
    real_spawn = hb.thread_registry.spawn

    def spawn(name, target, args=(), **kwargs):
        if name == 'hold-release-backstop':
            workers.append(target)
        else:
            target(*args)
        return SimpleNamespace(is_alive=lambda: False)

    monkeypatch.setattr(hb.thread_registry, 'spawn', spawn)
    monkeypatch.setattr(hotkeys, 'notice_for', lambda *args: SimpleNamespace(
        interrupted=lambda *args: None, recovered=lambda: None))
    monkeypatch.setattr(hotkeys.flight_recorder, 'record', Mock())
    monkeypatch.setattr(speech_readiness, 'status', lambda app: '')
    monkeypatch.setattr(speech_readiness, 'show_for_keyboard_hotkey', lambda app: None)
    monkeypatch.setattr(decode_recovery, 'supersede_final', lambda app: None)
    monkeypatch.setattr(mh, '_call_next_hook_ex', lambda *args: 0)
    down = {'value': True}
    sample = Mock(side_effect=lambda binding: down['value'])
    monkeypatch.setattr(hb, 'physical_down', sample)
    monkeypatch.setattr(hotkeys, '_raw_key_pressed', lambda name: down['value'] if name == 'f8' else False)
    return SimpleNamespace(workers=workers, down=down, sample=sample, real_spawn=real_spawn)


def start(env, binding='mouse5', mode='hold'):
    app = App(binding, mode)
    if binding.startswith('mouse'):
        app._on_mouse_button(binding, True)
    else:
        app.on_key_press(SimpleNamespace(name=binding))
    return app


def slow(app):
    app._mouse_hook.slow_callbacks += 1
    app._mouse_hook.last_callback_ms = 12.0


def fire(app, message=mh.WM_XBUTTONUP, *, injected=False):
    info = mh.MSLLHOOKSTRUCT()
    info.mouseData = mh.XBUTTON2 << 16
    info.flags = mh.LLMHF_INJECTED if injected else 0
    app._mouse_hook._hook_callback(0, message, ctypes.pointer(info))
    app._mouse_hook.dispatch_pending()


@pytest.mark.parametrize('binding', ['mouse4', 'mouse5', 'f8'])
def test_stall_hold_survives_until_two_consecutive_up_samples(env, binding, caplog):
    app = start(env, binding)
    monitor = app._hold_backstop
    slow(app)
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            monitor.tick()
        assert app.calls == ['start'] and app.recording
        env.down['value'] = False
        monitor.tick()
        assert app.recording
        env.down['value'] = True
        monitor.tick()  # bounce resets the up count
        env.down['value'] = False
        monitor.tick()
        assert app.recording
        monitor.tick()
        if binding == 'mouse5':
            # 1164: two quick polls cannot end Mouse 5; a delivered hook up can.
            assert app.recording
            app._on_mouse_button(binding, False)
    assert app.calls == ['start', 'stop'] and not app.recording
    monitor.tick()
    assert app.calls.count('stop') == 1
    assert caplog.text.count('hold ended by backstop poll, hook stalled') == (binding != 'mouse5')
    assert all(call.args == (binding,) for call in env.sample.call_args_list)


@pytest.mark.parametrize('injected', [False, True])
def test_hook_up_cannot_end_stalled_hold_even_before_first_poll(env, injected):
    app = start(env)
    monitor = app._hold_backstop
    slow(app)
    fire(app, injected=injected)
    # 1164: delivered hook release wins immediately, even during a short stall.
    assert not app.recording and not app._hold_down_mouse and app.calls == ['start', 'stop']
    env.sample.assert_not_called()  # callback path never reads the native state
    env.down['value'] = False
    monitor.tick()
    fire(app, injected=injected)  # first up sample still needs confirmation
    assert not app.recording
    monitor.tick()
    assert app.calls == ['start', 'stop']


def test_reinstall_fallback_does_not_stop_hold_and_injected_up_does_not_disarm(env):
    app = start(env)
    monitor = app._hold_backstop
    app._mouse_hook._begin_recovery('hook stalled')
    assert app.recording and app._main_hotkey_source == 'mouse'
    assert app._main_hotkey_override  # ordinary fallback remains available
    fire(app, injected=True)  # 1032 recovery happens before delivering the up
    assert not app._mouse_hook.recovering
    monitor.tick()
    # 1164: hook up settles the take; stale polls cannot deliver another stop.
    assert not app.recording and monitor.stop.is_set()
    env.down['value'] = False
    monitor.tick()
    monitor.tick()
    assert app.calls == ['start', 'stop']


def test_keyboard_synthetic_up_restores_listener_state_until_confirmed(env):
    app = start(env, 'f8')
    app._hold_key_listener = SimpleNamespace(event_down=set())  # listener consumed synthetic up
    slow(app)
    app.on_key_release(SimpleNamespace(name='f8', hold_name='f8'))
    assert app.recording and app.current_keys == {'f8'}
    assert app._hold_key_listener.event_down == {'f8'}
    env.down['value'] = False
    monitor = app._hold_backstop
    monitor.tick()
    assert app.recording
    monitor.tick()
    assert app.calls == ['start', 'stop'] and not app.current_keys
    assert not app._hold_key_listener.event_down


def test_healthy_hardware_callback_stops_polling_and_normal_up_stops_capture(env):
    app = start(env)
    monitor = app._hold_backstop
    slow(app)
    monitor.tick()
    fire(app, 0x0200)  # fresh, fast, non-injected movement proves health
    monitor.tick()
    assert monitor.unhealthy_since is None
    env.sample.reset_mock()
    monitor.tick()
    env.sample.assert_called_once_with('mouse5')  # 1164: calibrate the held button off-hook.
    fire(app)
    assert app.calls == ['start', 'stop']


def test_running_callback_stall_is_detected_without_waiting_for_watchdog(env):
    app = start(env)
    app._mouse_hook._callback_started_at = mh._perf() - 0.020
    app._hold_backstop.tick()
    assert app._hold_backstop.unhealthy_since is not None
    assert app._hold_backstop.stall_ms >= 20
    assert app.recording


def test_completed_slow_callback_arms_backstop_from_real_timing(env, monkeypatch):
    app = start(env)
    clock = [1000.0]
    monkeypatch.setattr(mh, '_perf', lambda: clock[0])
    def downstream(*args):
        clock[0] += 0.012
        return 0
    monkeypatch.setattr(mh, '_call_next_hook_ex', downstream)
    info = mh.MSLLHOOKSTRUCT()
    app._mouse_hook._hook_callback(0, 0x0200, ctypes.pointer(info))
    assert app._mouse_hook.slow_callbacks == 1
    app._mouse_hook._report_counters()  # reporting must not consume the fault signal
    app._hold_backstop.tick()
    assert app.recording and app._hold_backstop.stall_ms == pytest.approx(12)


def test_keyboard_chord_release_uses_hardware_not_stale_listener_keys(env, monkeypatch):
    app = App('ctrl+shift')
    app.recording = app.hotkey_pressed = app._hold_down_key = True
    app.current_keys = {'ctrl', 'shift'}
    app._hold_key_listener = SimpleNamespace(event_down={'left_ctrl', 'right_shift'})
    hb.sync(app)
    monitor = app._hold_backstop
    slow(app)
    app.on_key_release(SimpleNamespace(name='ctrl', hold_name='left_ctrl'))
    assert app.recording
    monkeypatch.setattr(hb, 'physical_down', lambda binding: binding == 'ctrl')
    monitor.tick()
    assert app.recording
    monitor.tick()
    assert app.calls == ['stop'] and not app.recording
    assert app._hold_key_listener.event_down == {'left_ctrl'}


def test_shutdown_and_new_hold_cannot_reuse_old_poll_confirmation(env):
    app = start(env)
    old = app._hold_backstop
    slow(app)
    env.down['value'] = False
    old.tick()
    assert old.up == 1
    app._running = False
    old.tick()
    assert app.recording
    hb.sync(app)
    app._running = True
    app.recording = app._hold_down_mouse = False
    env.down['value'] = True
    app._on_mouse_button('mouse5', True)
    assert app._hold_backstop is not old
    old.tick()
    assert app.calls == ['start', 'start']


def test_no_native_poll_in_idle_toggle_healthy_hold_or_after_cancellation(env):
    idle = App()
    slow(idle)
    hb.sync(idle)
    assert not env.workers
    toggle = start(env, mode='toggle')
    slow(toggle)
    hb.sync(toggle)
    assert not env.workers
    hold = start(env)
    assert len(env.workers) == 1
    monitor = hold._hold_backstop
    monitor.tick()
    env.sample.assert_called_once_with('mouse5')  # 1164: verify down before trusting up.
    env.sample.reset_mock()
    hold.recording = False
    slow(hold)
    monitor.tick()
    hb.sync(hold)
    assert monitor.stop.is_set() and hold._hold_backstop is None
    env.sample.assert_not_called()


@pytest.mark.parametrize('unknown', [None, OSError('read unavailable')])
def test_unknown_state_resets_up_confirmation(env, unknown):
    app = start(env)
    slow(app)
    monitor = app._hold_backstop
    env.sample.side_effect = [False, unknown, False, True]
    for _ in range(4):
        monitor.tick()
    assert app.recording and monitor.up == 0


def test_keyboard_final_recheck_error_keeps_worker_and_hold_active(env):
    app = start(env, 'f8')
    monitor = app._hold_backstop
    slow(app)
    env.sample.side_effect = [False, False, OSError('desktop changed')]
    monitor.tick()
    monitor.tick()
    assert app.recording and monitor.up == 0 and not monitor.stop.is_set()
    env.sample.side_effect = lambda binding: False
    monitor.tick()
    monitor.tick()
    assert app.calls == ['start', 'stop']


def test_idle_recovery_and_explicit_panic_release_keep_existing_behavior(env):
    app = App()
    app._mouse_hook._begin_recovery('idle interruption')
    assert app._main_hotkey_override and not env.workers and not app.calls
    app = start(env)
    app._mouse_hook = None  # tray Release mouse buttons removes the hook first
    app._mouse_fallback_to_keyboard('released by user')
    assert app.calls == ['start', 'stop']
    env.sample.assert_not_called()


def test_poll_runs_on_separate_worker_at_50ms(env, monkeypatch):
    app = start(env)
    monitor = app._hold_backstop
    slow(app)
    sampled = []
    def sample(binding):
        sampled.append(threading.current_thread().ident)
        monitor.stop.set()
        return True
    monkeypatch.setattr(hb, 'physical_down', sample)
    worker = env.real_spawn('test-hold-backstop-1052e', env.workers[0], daemon=True)
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert sampled and sampled[0] != threading.current_thread().ident
    assert hb.POLL_S == 0.050 and hb.UP_SAMPLES == 2


@pytest.mark.parametrize('binding,vks', [('mouse4', {5}), ('mouse5', {6}),
                                        ('ctrl+shift', {0xA2, 0xA1}), ('right_alt', {0xA5}),
                                        ('numpad_enter', {0x0D})])
def test_native_high_bit_and_mapped_keyboard_chords(env, monkeypatch, binding, vks):
    monkeypatch.setattr(mh, '_input_status', lambda: (True, 0, 1))
    fn = Mock(side_effect=lambda vk: 0x8000 if vk in vks else 1)
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(user32={'GetAsyncKeyState': fn}))
    # Call the production function saved before fixture replacement below.
    assert NATIVE_DOWN(binding) is True
    fn.side_effect = lambda vk: 1  # low bit means "pressed since query", not held
    assert NATIVE_DOWN(binding) is False


NATIVE_DOWN = hb.physical_down


@pytest.mark.parametrize('status', [None, (False, 0, 1)])
def test_inaccessible_desktop_is_unknown_without_querying_keys(env, monkeypatch, status):
    monkeypatch.setattr(mh, '_input_status', lambda: status)
    fn = Mock()
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(user32={'GetAsyncKeyState': fn}))
    assert NATIVE_DOWN('mouse5') is None
    assert NATIVE_DOWN('unknown binding') is None
    fn.assert_not_called()
