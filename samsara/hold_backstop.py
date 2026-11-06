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
MOUSE5_UP_S = 0.300
MOUSE5_UP_SAMPLES = 4
MOUSE5_SILENCE_S = 2.0
MOUSE5_RELEASE_S = 0.150
_now = time.perf_counter
_now_ns = time.perf_counter_ns
_stop_context = threading.local()

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
        self.started_at = _now()
        self.down_seen = False
        self.up_since = None
        self.last_sample_at = None

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
                self.unhealthy_since = _now()
            if fault != self.seen:
                self.up = 0
            self.seen = fault
            self.healthy_after = healthy
            self.stall_ms = max(self.stall_ms, duration)
        return self.unhealthy_since is not None

    def tick(self):
        """Called only by the non-audio worker, under the hotkey ownership lock."""
        faulted = self.observe()
        mouse5 = self.source == 'mouse' and self.binding == 'mouse5'
        if not self.current() or (not faulted and not mouse5):
            return
        now = _now()
        try:
            down = physical_down(self.binding)
        except (OSError, AttributeError):
            down = None
        self.up = self.up + 1 if down is False else 0
        confirmed = self.up >= UP_SAMPLES
        if mouse5:
            # Calibrate on this hold, off the hook thread. Some drivers or
            # suppressed bindings never expose a down bit: zero is not proof
            # of release until this same button has actually reported down.
            if down is True:
                self.down_seen = True
            if self.last_sample_at is not None and now - self.last_sample_at > POLL_S * 3:
                self.up = 1 if down is False else 0
                self.up_since = None
            self.last_sample_at = now
            if down is False:
                if self.up_since is None:
                    self.up_since = now
            else:
                self.up_since = None
            last_hook = getattr(self.hook, 'last_callback_at', None) or self.started_at
            confirmed = (self.up >= MOUSE5_UP_SAMPLES and faulted and self.down_seen
                         and not getattr(self.app, '_mouse5_poll_unreliable', False)
                         and now - self.up_since >= MOUSE5_UP_S
                         and now - max(last_hook, self.started_at, self.unhealthy_since) >= MOUSE5_SILENCE_S)
        if confirmed:
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
            if mouse5:
                self.app._mouse5_polled_release_at = now
            elapsed = max(self.stall_ms, (_now() - self.unhealthy_since) * 1000)
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
        if faulted and down is True and snapshot is not None and not snapshot[1] and snapshot[2] > self.healthy_after:
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
        if app.config.get('hotkey') == 'mouse5':
            # 1164: a real hook release outranks an unreliable poll. The
            # hold ends immediately; a later press may resume its audio.
            return False
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


class _Mouse5Release:
    """A released hold whose undrained audio can resume for 150 ms."""

    def __init__(self, app):
        self.app = app
        self.at = _now()
        self.at_ns = _now_ns()
        self.hook = getattr(app, '_mouse_hook', None)
        self.session = getattr(app, '_streaming_session', None)
        self.consumer = app._dictation_consumer
        self.provenance = getattr(app, '_capture_provenance', None)
        self.changed = threading.Event()
        self.resumed = self.sealed = False

    def current(self):
        app = self.app
        return (self.owns_capture() and getattr(app, '_running', True)
                and app.config.get('hotkey') == 'mouse5'
                and app.config.get('mode', 'hold') == 'hold'
                and not getattr(app, 'snoozed', False)
                and not getattr(app, '_hold_down_key', False)
                and not any(getattr(app, flag, False) for flag in
                            ('command_mode_recording', 'ava_mode_recording', '_memo_recording'))
                and getattr(app, '_mouse_hook', None) is self.hook)

    def owns_capture(self):
        app = self.app
        return (getattr(app, '_mouse5_release', None) is self
                and getattr(app, '_streaming_session', None) is self.session
                and getattr(app, '_dictation_consumer', None) is self.consumer
                and getattr(app, '_capture_provenance', None) is self.provenance
                and (getattr(app, '_ace_dictation_active', False)
                     or getattr(app, '_ace_streaming_active', False))
                and not (self.session is not None and self.session.cancel_event.is_set()))


def debounce_mouse5(app, pressed):
    """Resume only an actual re-press; never swallow a delivered release."""
    monitor = getattr(app, '_hold_backstop', None)
    pending = getattr(app, '_mouse5_release', None)
    if monitor is not None and monitor.releasing:
        return False
    polled = getattr(app, '_mouse5_polled_release_at', None)
    if app.config.get('hotkey') == 'mouse5' and polled is not None:
        if pressed:
            app._mouse5_polled_release_at = None
        elif monitor is None or not monitor.releasing:
            _reject_mouse5_poll(app)
    if (pressed and pending is not None and pending.current()
            and not pending.resumed and not pending.sealed
            and 0 <= _now() - pending.at < MOUSE5_RELEASE_S):
        pending.resumed = True
        app._hold_down_mouse = True
        app._main_hotkey_source = 'mouse'
        app._stop_in_flight = False
        app.set_app_state(recording=True)
        pending.changed.set()
        logger.info('[MOUSE] chatter merged gap_ms=%.0f', (_now() - pending.at) * 1000)
        return True
    if not pressed and app.config.get('hotkey') == 'mouse5' and monitor is not None:
        if monitor.up_since is not None and _now() - monitor.up_since >= MOUSE5_UP_S:
            # A later hook up contradicts the earlier sustained poll up. Keep
            # that button hook-owned for the rest of this app's lifetime.
            _reject_mouse5_poll(app)
    return False


def begin_mouse5_release(app):
    """End logical capture now, preserving only a live ACE accumulator."""
    monitor = getattr(app, '_hold_backstop', None)
    if (app.config.get('hotkey') != 'mouse5'
            or (monitor is not None and monitor.releasing)
            or not callable(getattr(app, 'set_app_state', None))
            or getattr(app, '_dictation_consumer', None) is None
            or not (getattr(app, '_ace_dictation_active', False)
                    or getattr(app, '_ace_streaming_active', False))):
        return None
    pending = app._mouse5_release = _Mouse5Release(app)
    app.set_app_state(recording=False)
    return pending


def stop_mouse5(app, pending):
    """Bind the stop worker to its release, including rapid repeated chatter."""
    previous = getattr(_stop_context, 'release', None)
    _stop_context.release = pending
    try:
        app.stop_recording()
    finally:
        _stop_context.release = previous


def finish_mouse5_stop(app, pending):
    """An older resumed worker cannot retire a newer release's stop latch."""
    if pending is None:
        return True
    if getattr(app, '_mouse5_release', None) is not pending:
        return False
    app._mouse5_release = None
    return True


def _mouse5_stop(app):
    pending = getattr(_stop_context, 'release', None)
    return pending if pending is not None and pending.app is app else None


def resume_mouse5_release(app):
    """Stop entry: wait off the hook thread before consuming audio/ownership.

    True skips destructive stop work after a resume or a retired capture.
    Logical recording and the physical hold already ended on the release.
    """
    from samsara.hotkeys import _main_hotkey_lock
    pending = _mouse5_stop(app)
    if pending is None:
        return False
    while True:
        with _main_hotkey_lock(app):
            if pending.resumed or not pending.owns_capture():
                return True
            remaining = MOUSE5_RELEASE_S - (_now() - pending.at)
            if remaining <= 0:
                pending.sealed = True
                return False
        pending.changed.wait(remaining)


def final_mouse5_release(app):
    pending = _mouse5_stop(app)
    return pending is not None and pending.sealed and pending.owns_capture()


def capture_end_ns(app):
    pending = _mouse5_stop(app)
    return pending.at_ns if pending is not None and pending.sealed else _now_ns()


def _reject_mouse5_poll(app):
    app._mouse5_polled_release_at = None
    if not getattr(app, '_mouse5_poll_unreliable', False):
        app._mouse5_poll_unreliable = True
        logger.warning('[MOUSE] poll disagreed with hook; using hook releases')
