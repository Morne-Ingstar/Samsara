"""Exercise the native suppression decision and queued hotkey dispatch together."""
from types import SimpleNamespace

import pytest

from samsara import hold_key_rules, hotkeys, speech_readiness


class FakeListener:
    _WM_PROCESS = 123
    _PRESS_MESSAGES = (0x100, 0x104)

    class Suppressed(Exception):
        pass

    def __init__(self, on_press, on_release, win32_event_filter):
        self.on_press = on_press
        self.on_release = on_release
        self.filter = win32_event_filter
        self.queue = []
        self._message_loop = SimpleNamespace(post=lambda *args: self.queue.append(args))

    def suppress_event(self):
        raise self.Suppressed


class App(hotkeys.HotkeyCluster):
    def __init__(self):
        self.config = {'hotkey': 'f9'}
        self.current_keys = set()
        self.key_press_times = {}
        self.snoozed = self.recording = self.hotkey_pressed = False
        self.command_mode_active = False
        self._session_mode_manager = None
        self.sounds = []
        self.starts = []

    def _check_command_mode_key(self, key, pressed):
        pass

    def play_sound(self, name, **kwargs):
        self.sounds.append(name)

    def start_recording(self, **kwargs):
        self.starts.append(self._capture_sink)
        self.recording = True


@pytest.fixture
def input_hook(monkeypatch):
    class InputHook:
        def __init__(self):
            self.app = App()
            self.physical = set()
            self.listener = hold_key_rules.hold_listener(
                self.app, SimpleNamespace(Listener=FakeListener),
                self.app.on_key_press, self.app.on_key_release,
            )

        def send(self, vk, pressed=True):
            data = SimpleNamespace(vkCode=vk, scanCode=0, flags=0)
            try:
                self.listener.filter(0x100 if pressed else 0x101, data)
            except FakeListener.Suppressed:
                return True
            # Windows changes the async state only after an unsuppressed event.
            if pressed:
                self.physical.add(vk)
            else:
                self.physical.discard(vk)
            return False

        def dispatch(self):
            for _, message, packed in self.listener.queue:
                self.listener._process(message, packed)
            self.listener.queue.clear()

    hook = InputHook()
    # The low bit alone reports an earlier press, never a currently held key.
    user32 = SimpleNamespace(GetAsyncKeyState=lambda vk: 0x8000 if vk in hook.physical else 1)
    monkeypatch.setattr(hold_key_rules.ctypes, 'windll', SimpleNamespace(user32=user32), raising=False)
    monkeypatch.setattr(speech_readiness, 'show_for_keyboard_hotkey', lambda app: None)
    monkeypatch.setattr(hotkeys.thread_registry, 'spawn', lambda *args, **kwargs: None)
    return hook


@pytest.mark.parametrize('missed_releases', [{162}, {164}, {162, 164}])
def test_stale_task_modifiers_leave_t_alone_and_clear_tracking(input_hook, caplog, missed_releases):
    hook = input_hook
    caplog.set_level('INFO', logger='Samsara')
    assert not hook.send(162)
    assert not hook.send(164)
    hook.dispatch()
    assert hook.listener.event_down == {'left_ctrl', 'left_alt'}

    hook.physical.difference_update(missed_releases)
    assert not hook.send(ord('T'))
    assert not hook.send(ord('T'), False)
    hook.dispatch()

    expected = {hold_key_rules.VK_NAMES[vk] for vk in hook.physical}
    assert hook.listener.event_down == expected
    assert hook.app.current_keys == {hold_key_rules.base_modifier(name) for name in expected}
    assert hook.app.key_press_times.keys() & {'ctrl', 'alt'} == hook.app.current_keys
    assert hook.app.starts == hook.app.sounds == []
    assert caplog.text.count('[HOTKEY_STALE_MODS]') == 1
    assert '[HOTKEY_FIRE]' not in caplog.text

    # Later physical state alone must not resurrect the missed keydowns.
    hook.physical.update(missed_releases)
    assert not hook.send(ord('T'))
    hook.dispatch()
    assert hook.app.starts == []


def test_real_task_chord_uses_hook_snapshot_for_one_fire(input_hook, caplog):
    hook = input_hook
    caplog.set_level('INFO', logger='Samsara')
    assert not hook.send(163)
    assert not hook.send(165)
    assert hook.send(ord('T'))
    assert hook.send(ord('T'))  # autorepeat
    assert hook.send(ord('T'), False)
    # Dispatch may run after the physically held keys have already changed.
    assert not hook.send(165, False)
    assert not hook.send(163, False)
    hook.dispatch()

    assert hook.app.starts == ['task']
    assert hook.app.sounds == ['capture_started']
    assert not hook.listener.event_down
    assert caplog.text.count('[HOTKEY_FIRE]') == 1
    assert '[HOTKEY_FIRE] name=task_hotkey mods=0xa physical=0xa suppressed=True' in caplog.text
    assert '[HOTKEY_STALE_MODS]' not in caplog.text


def test_modifier_pressed_last_can_complete_task_chord(input_hook, caplog):
    hook = input_hook
    caplog.set_level('INFO', logger='Samsara')
    assert not hook.send(ord('T'))
    assert not hook.send(162)
    assert not hook.send(164)
    hook.dispatch()

    assert hook.app.starts == ['task']
    assert '[HOTKEY_FIRE] name=task_hotkey mods=0x5 physical=0x5 suppressed=False' in caplog.text


def test_legacy_win_shift_t_does_not_fire_when_shift_has_been_released(input_hook, caplog):
    hook = input_hook
    hook.app.config['task_hotkey'] = 'win+shift+t'
    caplog.set_level('INFO', logger='Samsara')
    assert not hook.send(92)
    assert not hook.send(161)
    hook.physical.remove(161)
    assert not hook.send(ord('T'))
    hook.dispatch()

    assert hook.listener.event_down == {'win', 't'}
    assert hook.app.starts == []
    assert caplog.text.count('[HOTKEY_STALE_MODS]') == 1
    assert '[HOTKEY_FIRE]' not in caplog.text


def test_other_ctrl_side_does_not_verify_a_side_specific_binding(input_hook):
    hook = input_hook
    hook.app.config['task_hotkey'] = 'right_ctrl+alt+t'
    assert not hook.send(163)
    assert not hook.send(162)
    assert not hook.send(164)
    hook.physical.remove(163)
    assert not hook.send(ord('T'))
    hook.dispatch()

    assert hook.listener.event_down == {'left_ctrl', 'left_alt', 't'}
    assert 'ctrl' in hook.app.current_keys
    assert hook.app.starts == []


def test_swallowed_modifier_preserves_its_standalone_binding(input_hook):
    hook = input_hook
    hook.app.config['command_mode'] = {'enabled': True, 'button': 'rctrl'}
    assert hook.send(163)
    assert not hook.send(164)
    assert not hook.send(ord('T'))
    hook.dispatch()

    assert hook.app.check_hotkey_state('right_ctrl')
    assert hook.app.starts == []
    assert hook.send(163, False)
    hook.dispatch()
    assert not hook.app.check_hotkey_state('right_ctrl')


@pytest.mark.parametrize('repeat', [False, True])
def test_prior_task_suppression_does_not_swallow_a_new_plain_t(input_hook, repeat):
    hook = input_hook
    assert not hook.send(162)
    assert not hook.send(164)
    assert hook.send(ord('T'))
    hook.dispatch()
    assert hook.app.starts == ['task']

    hook.physical.clear()
    assert not hook.send(ord('A'))  # reconciliation can precede the next T
    if repeat:
        assert not hook.send(ord('T'))
    assert hook.send(ord('T'), False) is not repeat
    hook.dispatch()
    assert hook.app.starts == ['task']
