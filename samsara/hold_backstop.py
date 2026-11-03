"""1052e: release-only hardware confirmation while a main hold's hook stalls."""

import ctypes
import threading
import time
from types import SimpleNamespace

from samsara import hold_key_rules
from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)
POLL_S = 0.050
UP_SAMPLES = 2

_VKS = {name: (vk,) for vk, name in hold_key_rules.VK_NAMES.items()}
_VKS.update(ctrl=(0x11, 0xA2, 0xA3), shift=(0x10, 0xA0, 0xA1),
            alt=(0x12, 0xA4, 0xA5), win=(0x5B, 0x5C),
            mouse4=(0x05,), mouse5=(0x06,), numpad_enter=(0x0D,))


def physical_down(binding):
    """True/False/unknown, without installing another hook or reading app state."""
    from samsara.mouse_hook import _input_status
    keys = hold_key_rules.parts(binding)
    if not keys or any(key not in _VKS for key in keys):
        return None
    status = _input_status()
    # GetAsyncKeyState returns zero on an inaccessible input desktop too.
    if status is None or not status[0]:
        return None
    get_state = ctypes.windll.user32['GetAsyncKeyState']
    get_state.argtypes = (ctypes.c_int,)
    get_state.restype = ctypes.c_short
    return all(any(get_state(vk) & 0x8000 for vk in _VKS[key]) for key in keys)


def health(app):
    hook = getattr(app, '_mouse_hook', None)
    read = getattr(hook, 'hold_health', None)
    return read() if callable(read) else None


def active_source(app):
    if (app.config.get('mode', 'hold') != 'hold'
            or not getattr(app, 'recording', False)
            or getattr(app, '_stop_in_flight', False)
            or any(getattr(app, name, False) for name in
                   ('command_mode_recording', 'ava_mode_recording', '_memo_recording'))):
        return None
    source = getattr(app, '_main_hotkey_source', 'key')
    flag = '_hold_down_mouse' if source == 'mouse' else '_hold_down_key'
    return source if getattr(app, flag, False) else None


class HoldBackstop:
    def __init__(self, app, before):
        self.app = app
        self.source = active_source(app)
        self.hook = app._mouse_hook
        self.binding = (app.config['hotkey'] if self.source == 'mouse' else
                        getattr(app, '_main_hotkey_override', None) or app.config['hotkey'])
        self.seen = before[0]
        self.unhealthy_since = None
        self.healthy_after = 0.0
        self.up = 0
        self.stall_ms = 0.0
        self.releasing = False
        self.stop = threading.Event()

    def current(self):
        return (not self.stop.is_set()
                and getattr(self.app, '_running', True)
                and getattr(self.app, '_hold_backstop', None) is self
                and getattr(self.app, '_mouse_hook', None) is self.hook
                and active_source(self.app) == self.source)

    def observe(self):
        snapshot = health(self.app)
        if snapshot is None or not self.current():
            return False
        fault, stalled, healthy, duration = snapshot
        if fault != self.seen or stalled:
            if self.unhealthy_since is None:
                self.unhealthy_since = time.perf_counter()
            if fault != self.seen:
                self.up = 0
            self.seen = fault
            self.healthy_after = healthy
            self.stall_ms = max(self.stall_ms, duration)
        return self.unhealthy_since is not None

    def tick(self):
        """Called only by the non-audio worker, under the hotkey ownership lock."""
        if not self.observe():
            return
        try:
            down = physical_down(self.binding)
        except (OSError, AttributeError):
            down = None
        self.up = self.up + 1 if down is False else 0
        if self.up >= UP_SAMPLES:
            released = None
            if self.source == 'key':
                try:
                    released = next((key for key in hold_key_rules.parts(self.binding)
                                     if physical_down(key) is False), None)
                except (OSError, AttributeError):
                    released = None
                if released is None:
                    self.up = 0
                    return  # a re-press between confirmation and delivery
            self.releasing = True
            elapsed = max(self.stall_ms, (time.perf_counter() - self.unhealthy_since) * 1000)
            logger.info('hold ended by backstop poll, hook stalled %.0f ms', elapsed)
            try:
                if self.source == 'mouse':
                    self.app._on_mouse_button(self.binding, False)
                else:
                    listener = getattr(self.app, '_hold_key_listener', None)
                    if listener is not None:
                        listener.event_down.difference_update(
                            key for key in tuple(listener.event_down)
                            if key == released or hold_key_rules.base_modifier(key) == released)
                    self.app.on_key_release(SimpleNamespace(name=released))
            finally:
                self.releasing = False
                self.stop.set()
            return
        snapshot = health(self.app)
        if down is True and snapshot is not None and not snapshot[1] and snapshot[2] > self.healthy_after:
            # A fresh, fast hardware callback proves recovery. An injected or
            # button-up callback cannot turn off the guard protecting that up.
            self.unhealthy_since = None
            self.up = 0
            self.stall_ms = 0.0

    def run(self):
        from samsara.hotkeys import _main_hotkey_lock
        while not self.stop.wait(POLL_S):
            with _main_hotkey_lock(self.app):
                if not self.current():
                    return
                self.tick()


def sync(app, before=None):
    monitor = getattr(app, '_hold_backstop', None)
    if monitor is not None and not monitor.current():
        monitor.stop.set()
        app._hold_backstop = None
        monitor = None
    snapshot = health(app)
    if (monitor is None and active_source(app) is not None and snapshot is not None
            and getattr(app, '_running', False)):
        monitor = app._hold_backstop = HoldBackstop(app, before or snapshot)
        thread_registry.spawn('hold-release-backstop', monitor.run, daemon=True)


def protects(app, source):
    monitor = getattr(app, '_hold_backstop', None)
    return bool(monitor is not None and monitor.source == source
                and not monitor.releasing and monitor.observe())


def ignores_release(app, callback_name, args):
    if callback_name == '_on_main_hotkey_mouse' and args and not args[0]:
        return protects(app, 'mouse')
    if callback_name == 'on_key_release' and args and protects(app, 'key'):
        monitor = app._hold_backstop
        name = app.get_key_name(args[0])
        if name in app.parse_hotkey(monitor.binding):
            listener = getattr(app, '_hold_key_listener', None)
            if listener is not None:
                listener.event_down.add(getattr(args[0], 'hold_name', name))
            return True
    return False
