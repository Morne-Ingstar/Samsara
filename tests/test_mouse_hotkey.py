"""Main record hotkey bound to Mouse 4/5 (config['hotkey'] = 'mouse4'/'mouse5').

Same _App-stub style as test_mouse_hook.py::TestOnCommandButton: the real
DictationApp methods are bound onto a small stand-in. dictation is imported
inside the helper (never at module level) -- run with the dev app closed.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_BOUND = (
    '_on_mouse_button', '_on_command_button', '_on_main_hotkey_mouse', '_main_hotkey_toggle_off',
    '_mouse_hook_bindings', '_install_mouse_listener', 'refresh_mouse_hook',
    'parse_hotkey', 'check_hotkey_state', 'on_key_release', 'get_key_name',
    '_hotkey_state_text', '_other_hotkey_held', '_start_recording_declined', '_mouse_guard',
    '_on_mouse_hook_failed', 'release_mouse_buttons', '_mouse_fallback_to_keyboard',
)


class _App:
    def __init__(self, hotkey='mouse4', mode='hold', command_mode=None):
        import dictation

        for name in _BOUND:
            setattr(self, name, getattr(dictation.DictationApp, name).__get__(self))
        self.config = {
            'hotkey': hotkey,
            'mode': mode,
            'command_mode': command_mode or {'enabled': False, 'button': 'rctrl'},
        }
        self.snoozed = False
        self.recording = False
        self._streaming_session = None
        self._stop_in_flight = False
        self.hotkey_pressed = False
        self.toggle_active = False
        self.command_mode_active = False
        self.command_mode_recording = False
        self._memo_recording = False
        self.current_keys = set()
        self._main_hotkey_source = 'key'
        self._hold_down_key = False
        self._hold_down_mouse = False
        self._hotkey_recording = False
        self._mouse_hook = None
        self.calls = []

    def start_recording(self, streaming=False):
        self.calls.append(('start', streaming))
        self.recording = True

    def stop_recording(self):
        self.calls.append(('stop',))
        self.recording = False

    def toggle_continuous_mode(self):
        self.calls.append(('continuous',))

    def _show_outcome_chip(self, label, kind, ttl_ms="default"):
        self.calls.append(('chip', label, kind))

    def enter_command_mode(self):
        self.command_mode_active = True
        self.calls.append(('cmd_enter',))

    def exit_command_mode(self):
        self.command_mode_active = False
        self.calls.append(('cmd_exit',))

    def _check_command_mode_key(self, key, pressed):
        pass


@pytest.fixture
def spawned(monkeypatch):
    import dictation

    jobs = []
    monkeypatch.setattr(
        dictation.thread_registry, 'spawn',
        lambda name, target, daemon=True, **kw: jobs.append((name, target)),
    )
    monkeypatch.setattr(dictation.flight_recorder, 'record', MagicMock())
    return jobs


class TestHold:
    def test_press_starts_and_release_stops_via_spawn(self, spawned):
        app = _App(mode='hold')
        app._on_mouse_button('mouse4', True)
        assert app.calls == [('start', False)]
        assert app._hold_down_mouse and app._main_hotkey_source == 'mouse'
        assert app.hotkey_pressed is False       # 28: the mouse never touches the keyboard flag

        app._on_mouse_button('mouse4', False)
        assert [name for name, _ in spawned] == ['stop-rec']
        assert app._stop_in_flight is True
        assert app._hold_down_mouse is False

        spawned[0][1]()   # the stop worker
        assert app.calls == [('start', False), ('stop',)]
        assert app._stop_in_flight is False

    def test_auto_repeat_press_is_edge_triggered(self, spawned):
        app = _App(mode='hold')
        app._on_mouse_button('mouse4', True)
        app._on_mouse_button('mouse4', True)
        assert app.calls == [('start', False)]

    def test_press_ignored_while_recording(self, spawned):
        app = _App(mode='hold')
        app.recording = True
        app._on_mouse_button('mouse4', True)
        app._on_mouse_button('mouse4', False)
        assert app.calls == [] and spawned == []

    def test_press_ignored_while_streaming_session(self, spawned):
        app = _App(mode='hold')
        app._streaming_session = object()
        app._on_mouse_button('mouse4', True)
        assert app.calls == []

    def test_press_ignored_while_stop_in_flight(self, spawned):
        app = _App(mode='hold')
        app._stop_in_flight = True
        app._on_mouse_button('mouse4', True)
        app._on_mouse_button('mouse4', False)
        assert app.calls == [] and spawned == []

    def test_press_ignored_while_snoozed(self, spawned):
        app = _App(mode='hold')
        app.snoozed = True
        app._on_mouse_button('mouse4', True)
        assert app.calls == []

    def test_other_button_does_nothing(self, spawned):
        app = _App(hotkey='mouse4', mode='hold')
        app._on_mouse_button('mouse5', True)
        assert app.calls == []


class TestToggleAndContinuous:
    def test_toggle_flips_on_then_off(self, spawned):
        app = _App(mode='toggle')
        app._on_mouse_button('mouse5', True)   # hotkey is mouse4: no effect
        assert app.calls == []

        app._on_mouse_button('mouse4', True)
        app._on_mouse_button('mouse4', False)
        assert app.calls == [('start', False)] and app.toggle_active is True
        assert spawned == []   # toggle release never stops
        assert app._main_hotkey_source == 'mouse'   # owner stays until toggle-off (14)

        app._on_mouse_button('mouse4', True)
        app._on_mouse_button('mouse4', False)
        assert app.calls == [('start', False), ('stop',)]
        assert app.toggle_active is False and app.hotkey_pressed is False
        assert app._main_hotkey_source == 'key'

    def test_continuous_press_toggles_continuous_mode(self, spawned):
        app = _App(mode='continuous')
        app._on_mouse_button('mouse4', True)
        app._on_mouse_button('mouse4', False)
        assert app.calls == [('continuous',)]
        assert app.hotkey_pressed is False


class TestSharedHookRouting:
    def test_command_mode_on_mouse5_and_hotkey_on_mouse4_share_one_callback(self, spawned):
        app = _App(hotkey='mouse4', mode='hold',
                   command_mode={'enabled': True, 'button': 'mouse5', 'mode': 'hold'})
        app._on_mouse_button('mouse5', True)
        app._on_mouse_button('mouse4', True)
        app._on_mouse_button('mouse5', False)
        app._on_mouse_button('mouse4', False)
        assert app.calls == [('cmd_enter',), ('start', False), ('cmd_exit',)]
        assert [name for name, _ in spawned] == ['stop-rec']

    def test_hook_installed_when_only_the_hotkey_is_a_mouse_button(self, monkeypatch):
        import samsara.mouse_hook as mouse_hook

        created = []

        class _FakeHook:
            def __init__(self, on_button_event, suppress_buttons=None, **kw):
                self.on_button_event = on_button_event
                self.suppress_buttons = frozenset(suppress_buttons or ())
                self.started = self.stopped = False
                self.kw = kw
                created.append(self)

            def start(self):
                self.started = True

            def stop(self):
                self.stopped = True

        monkeypatch.setattr(mouse_hook, 'MouseHook', _FakeHook)
        app = _App(hotkey='mouse4', command_mode={'enabled': True, 'button': 'rctrl'})
        app._install_mouse_listener()
        assert len(created) == 1 and created[0].started
        assert created[0].suppress_buttons == frozenset({'mouse4'})
        assert created[0].on_button_event == app._on_mouse_button
        assert created[0].kw.get('on_hook_failed') == app._on_mouse_hook_failed

    def test_suppress_set_honours_command_mode_flag_and_always_suppresses_hotkey(self):
        app = _App(hotkey='mouse4',
                   command_mode={'enabled': True, 'button': 'mouse5', 'suppress_button': False})
        assert app._mouse_hook_bindings() == (frozenset({'mouse4', 'mouse5'}), frozenset({'mouse4'}))

    def test_no_hook_when_nothing_is_mouse_bound(self, monkeypatch):
        import samsara.mouse_hook as mouse_hook

        monkeypatch.setattr(mouse_hook, 'MouseHook', MagicMock(side_effect=AssertionError("no hook")))
        app = _App(hotkey='ctrl+shift')
        app._install_mouse_listener()
        assert app._mouse_hook is None

    def test_refresh_reinstalls_when_hotkey_moves_between_mouse_and_keyboard(self, monkeypatch):
        import samsara.mouse_hook as mouse_hook

        created = []

        class _FakeHook:
            def __init__(self, on_button_event, suppress_buttons=None, **kw):
                self.suppress_buttons = frozenset(suppress_buttons or ())
                self.stopped = False
                created.append(self)

            def start(self):
                pass

            def stop(self):
                self.stopped = True

        monkeypatch.setattr(mouse_hook, 'MouseHook', _FakeHook)
        app = _App(hotkey='ctrl+shift')
        app.refresh_mouse_hook()
        assert created == []

        app.config['hotkey'] = 'mouse5'
        app.refresh_mouse_hook()
        assert len(created) == 1 and app._mouse_hook is created[0]

        app.refresh_mouse_hook()   # unchanged bindings: no churn
        assert len(created) == 1

        app.config['hotkey'] = 'ctrl+shift'
        app.refresh_mouse_hook()
        assert created[0].stopped and app._mouse_hook is None


class TestKeyboardBranchInert:
    def test_parse_and_check_never_touch_the_keyboard(self, monkeypatch):
        import dictation

        monkeypatch.setattr(dictation, '_raw_key_pressed',
                            MagicMock(side_effect=AssertionError("keyboard queried")))
        app = _App(hotkey='mouse4')
        assert app.parse_hotkey('mouse4') == set()
        assert app.parse_hotkey('Mouse5') == set()
        assert app.check_hotkey_state('mouse4') is False

    def test_key_release_never_stops_a_mouse_held_recording(self, monkeypatch, spawned):
        import dictation

        monkeypatch.setattr(dictation, '_raw_key_pressed', lambda key: False)
        app = _App(hotkey='mouse4', mode='hold')
        app._on_mouse_button('mouse4', True)
        assert app.recording

        app.on_key_release(MagicMock(char='a'))   # any key released, no combo held

        assert spawned == [] and app.recording and app._hold_down_mouse
        app._on_mouse_button('mouse4', False)
        assert [name for name, _ in spawned] == ['stop-rec']


# ---------------------------------------------------------------------------
# 32: panic release and the watchdog's keyboard fallback
# ---------------------------------------------------------------------------

class _ReleasableHook:
    def __init__(self, on_button_event=None, suppress_buttons=None, **kw):
        self.suppress_buttons = frozenset(suppress_buttons or ())
        self.released = []
        self.started = False
        _ReleasableHook.created.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.released.append('stop')

    def release(self, why=''):
        self.released.append(('release', why))


@pytest.fixture
def releasable(monkeypatch):
    import samsara.mouse_hook as mouse_hook

    _ReleasableHook.created = []
    monkeypatch.setattr(mouse_hook, 'MouseHook', _ReleasableHook)
    return _ReleasableHook


class TestPanicRelease:
    def test_release_unhooks_now_and_falls_back_to_the_keyboard_hotkey(self, releasable, spawned):
        app = _App(hotkey='mouse4')
        app._install_mouse_listener()
        hook = app._mouse_hook
        app.release_mouse_buttons()
        assert app._mouse_hook is None
        assert [r[0] if isinstance(r, tuple) else r for r in hook.released] == ['release']
        assert app.config['hotkey'] == 'ctrl+shift'            # in memory only
        assert any(c[0] == 'chip' and 'released' in c[1] and c[2] == 'warning' for c in app.calls)

    def test_release_stays_released_until_bindings_change(self, releasable, spawned):
        app = _App(hotkey='mouse5', command_mode={'enabled': True, 'button': 'mouse4'})
        app._install_mouse_listener()
        app.release_mouse_buttons()
        app.refresh_mouse_hook()                               # e.g. a config reload: no reinstall
        assert len(releasable.created) == 1 and app._mouse_hook is None
        app.config['command_mode'] = {'enabled': True, 'button': 'mouse5'}   # a real settings change
        app.refresh_mouse_hook()
        assert len(releasable.created) == 2 and app._mouse_hook is releasable.created[1]

    def test_release_during_a_mouse_hold_stops_the_recording(self, releasable, spawned):
        app = _App(hotkey='mouse4', mode='hold')
        app._install_mouse_listener()
        app._on_mouse_button('mouse4', True)
        assert app.recording
        app.release_mouse_buttons()
        assert [name for name, _ in spawned] == ['stop-rec'] and not app._hold_down_mouse

    def test_release_without_a_hook_is_harmless(self, spawned):
        app = _App(hotkey='ctrl+shift')
        app.release_mouse_buttons()
        assert app.config['hotkey'] == 'ctrl+shift' and app._mouse_hook is None

    def test_watchdog_give_up_falls_back_and_says_so(self, releasable, spawned):
        app = _App(hotkey='mouse4')
        app._install_mouse_listener()
        app._on_mouse_hook_failed("lost 4 times within 60s")
        assert app._mouse_hook is None and app.config['hotkey'] == 'ctrl+shift'
        chips = [c for c in app.calls if c[0] == 'chip']
        assert chips and 'hands-free mouse control disabled' in chips[-1][1] and chips[-1][2] == 'warning'
        app.refresh_mouse_hook()
        assert len(releasable.created) == 1                      # not silently re-armed


class TestHookRunsHandlerOffTheHookThread:
    def test_real_hook_delivers_to_on_mouse_button_on_the_dispatcher_thread(self, spawned):
        import ctypes
        import threading
        import time
        import samsara.mouse_hook as mouse_hook

        threads = []
        app = _App(hotkey='mouse4', mode='hold')
        real = app._on_mouse_button

        def _spy(button, pressed):
            threads.append(threading.current_thread().name)
            time.sleep(0.3)                                      # a slow start_recording
            real(button, pressed)

        hook = mouse_hook.MouseHook(on_button_event=_spy, suppress_buttons={'mouse4'})
        hook._hook_id = 999
        # `spawned` stubs thread_registry.spawn (shared with mouse_hook): a raw test thread.
        hook._dispatcher = threading.Thread(target=hook._dispatch_loop, name='test-dispatch', daemon=True)
        hook._dispatcher.start()
        info = mouse_hook.MSLLHOOKSTRUCT()
        info.mouseData = mouse_hook.XBUTTON1 << 16
        try:
            t0 = time.perf_counter()
            assert hook._hook_callback(0, mouse_hook.WM_XBUTTONDOWN, ctypes.pointer(info)) == 1
            assert (time.perf_counter() - t0) < 0.05
            deadline = time.monotonic() + 3
            while not app.recording and time.monotonic() < deadline:
                time.sleep(0.02)
            assert app.recording and threads == ['test-dispatch']
        finally:
            hook._stopping.set()
            hook._events.put(mouse_hook._STOP)
            hook._dispatcher.join(timeout=3)
