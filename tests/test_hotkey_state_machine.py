"""28: the main-hotkey state machine, one table over both sources.

Before, `hotkey_pressed` meant both "a key is physically down" and "the
mouse button is physically down"; either path could leave it True with no
matching release and the other path then ignored every press in silence.
Now the physical state is `_hold_down_key` / `_hold_down_mouse` (one per
source), toggle state is `toggle_active` alone, and the mouse path never
reads or writes `hotkey_pressed`.

The table runs {mode: hold, toggle} x {source: key, mouse} x {sequence:
press-release, press-press, press-without-release, interleaved key-then-
mouse, mouse-then-key}. After every step the (recording, toggle_active,
hold_down_key, hold_down_mouse) tuple is checked against a small reference
model, and after every sequence the invariant that would have caught the
bug: a FRESH press on the main source is never ignored.

Same _App-stub style as tests/test_main_hotkey_toggle.py: the real
DictationApp methods are bound onto a stand-in; dictation is imported inside
the helper (never at module level) -- run with the dev app closed.
"""

import ctypes
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_BOUND = (
    'on_key_press', 'on_key_release', '_on_mouse_button', '_on_command_button',
    '_on_main_hotkey_mouse', '_main_hotkey_toggle_off', '_mouse_guard',
    'parse_hotkey', 'check_hotkey_state', 'get_key_name',
    '_hotkey_state_text', '_other_hotkey_held', '_start_recording_declined', '_install_mouse_listener',
    '_mouse_hook_bindings',
)

KEY = 'f8'            # keyboard main hotkey: parse_hotkey -> {'f8'}
MOUSE = 'mouse4'      # mouse main hotkey
UNDO = 'ctrl+alt+z'   # a non-main keyboard hotkey, for the mouse-source interleave


class _App:
    def __init__(self, mode, source):
        import dictation

        for name in _BOUND:
            setattr(self, name, getattr(dictation.DictationApp, name).__get__(self))
        self.config = {
            'hotkey': MOUSE if source == 'mouse' else KEY,
            'mode': mode,
            'undo_hotkey': UNDO,
            'command_mode': {'enabled': False, 'button': 'rctrl'},
        }
        self.snoozed = False
        self.recording = False
        self._streaming_session = None
        self._stop_in_flight = False
        self.hotkey_pressed = False
        self.toggle_active = False
        self.continuous_active = False
        self.command_mode_active = False
        self.command_mode_recording = False
        self._memo_recording = False
        self._hotkey_recording = False
        self.current_keys = set()
        self.key_press_times = {}
        self._main_hotkey_source = 'key'
        self._hold_down_key = False
        self._hold_down_mouse = False
        self._mouse_hook = None
        self.calls = []

    def start_recording(self, streaming=False, play_earcon=True):
        self.calls.append(('start', streaming))
        self.recording = True
        self._hotkey_recording = True

    def stop_recording(self):
        self.calls.append(('stop',))
        self.recording = False

    def toggle_continuous_mode(self):
        self.calls.append(('continuous',))

    def undo_last_dictation(self):
        self.calls.append(('undo',))

    def _check_command_mode_key(self, key, pressed):
        pass

    def _hands_free_dictation_commit_available(self):
        return False

    def play_sound(self, *a, **k):
        pass


@pytest.fixture
def keyboard(monkeypatch):
    """The OS key state the app polls (_raw_key_pressed): a mutable set of
    key names; a combo is held when all its keys are in the set."""
    import dictation

    held = set()
    monkeypatch.setattr(dictation, '_raw_key_pressed', lambda key: key in held)
    monkeypatch.setattr(dictation.flight_recorder, 'record', MagicMock())
    return held


@pytest.fixture
def spawned(monkeypatch):
    import dictation

    jobs = []
    monkeypatch.setattr(
        dictation.thread_registry, 'spawn',
        lambda name, target, daemon=True, **kw: jobs.append((name, target)),
    )
    return jobs


def _drain(spawned):
    """Run every deferred stop the app spawned."""
    while spawned:
        _name, job = spawned.pop(0)
        job()


# ---------------------------------------------------------------------------
# Driving one device
# ---------------------------------------------------------------------------

class _Driver:
    """Delivers press/release events for the main source, the other main
    path, or the undo hotkey, the way the OS would."""

    def __init__(self, app, held, spawned, source):
        self.app, self.held, self.spawned, self.source = app, held, spawned, source

    def key(self, name, pressed):
        if pressed:
            self.held.add(name)
            self.app.on_key_press(SimpleNamespace(name=name))
        else:
            self.held.discard(name)
            self.app.on_key_release(SimpleNamespace(name=name))

    def combo(self, combo, pressed):
        keys = combo.split('+')
        for k in keys:
            if pressed:
                self.held.add(k)
            else:
                self.held.discard(k)
        last = keys[-1]
        if pressed:
            self.app.current_keys.update(keys)
            self.app.on_key_press(SimpleNamespace(name=last))
        else:
            self.app.on_key_release(SimpleNamespace(name=last))
            self.app.current_keys.difference_update(keys)

    def main(self, pressed):
        if self.source == 'mouse':
            self.app._on_mouse_button(MOUSE, pressed)
        else:
            self.key(KEY, pressed)

    def other(self, pressed):
        """The other path: the mouse handler when the key is main; the undo
        hotkey (which owns hotkey_pressed while held) when the mouse is."""
        if self.source == 'mouse':
            self.combo(UNDO, pressed)
        else:
            self.app._on_main_hotkey_mouse(pressed)


# ---------------------------------------------------------------------------
# Reference model
# ---------------------------------------------------------------------------

class _Model:
    def __init__(self, mode, source):
        self.mode, self.source = mode, source
        self.recording = False
        self.toggle_active = False
        self.key_down = False
        self.mouse_down = False
        self.undo_down = False
        self.owner = None     # 'key' | 'mouse'

    def _dev(self, which):
        if which == 'main':
            return self.source
        return 'undo' if self.source == 'mouse' else 'mouse'

    def press(self, which):
        dev = self._dev(which)
        if dev == 'undo':
            self.undo_down = True
            return
        if dev == 'mouse':
            if self.mouse_down:
                return                                 # auto-repeat: edge-triggered
            self.mouse_down = True                     # the button IS down, acted on or not
        elif self.key_down:
            return
        acted = False
        if self.mode == 'hold':
            if not self.recording:
                self.recording, self.owner, acted = True, dev, True
        else:
            if self.toggle_active:
                # A toggle belongs to the main hotkey: either path ends it.
                self.toggle_active = self.recording = False
                self.owner, acted = dev, True
            elif not self.recording:
                self.toggle_active = self.recording = True
                self.owner, acted = dev, True
        if dev == 'key' and acted:
            # _hold_down_key means "this key-down owns an in-progress action";
            # an ignored press (capture owned elsewhere) marks nothing.
            self.key_down = True

    def release(self, which):
        dev = self._dev(which)
        if dev == 'undo':
            self.undo_down = False
            return
        down = 'key_down' if dev == 'key' else 'mouse_down'
        if not getattr(self, down):
            return
        setattr(self, down, False)
        if self.mode == 'hold' and self.recording and self.owner == dev:
            self.recording = False

    @property
    def state(self):
        return (self.recording, self.toggle_active, self.key_down, self.mouse_down)


def _observed(app):
    return (app.recording, app.toggle_active, app._hold_down_key, app._hold_down_mouse)


SEQUENCES = {
    'press-release':        [('main', True), ('main', False)],
    'press-press':          [('main', True), ('main', True), ('main', False)],
    'press-without-release': [('main', True)],
    'key-then-mouse':       [('main', True), ('other', True), ('main', False), ('other', False)],
    'mouse-then-key':       [('other', True), ('main', True), ('other', False), ('main', False)],
}


def _fresh_press_counts(app, drv, spawned, mode, source):
    """The invariant: a fresh press on the main source always does something,
    and its release leaves none of that source's flags behind."""
    before = len(app.calls)
    toggle_before = app.toggle_active
    drv.main(True)
    assert len(app.calls) == before + 1, (
        f"fresh press ignored | {app._hotkey_state_text()} | calls={app.calls}")
    drv.main(False)
    _drain(spawned)
    if mode == 'hold':
        assert app.calls[before] == ('start', False)
        assert app.recording is False
    else:
        assert app.toggle_active is (not toggle_before)
        assert app.recording is app.toggle_active
    if source == 'key':
        assert app._hold_down_key is False and app.hotkey_pressed is False, app._hotkey_state_text()
    else:
        assert app._hold_down_mouse is False, app._hotkey_state_text()


@pytest.mark.parametrize('mode', ['hold', 'toggle'])
@pytest.mark.parametrize('source', ['key', 'mouse'])
@pytest.mark.parametrize('sequence', list(SEQUENCES))
def test_state_table(keyboard, spawned, mode, source, sequence):
    app = _App(mode, source)
    drv = _Driver(app, keyboard, spawned, source)
    model = _Model(mode, source)
    for step, (which, pressed) in enumerate(SEQUENCES[sequence]):
        if pressed:
            drv.main(True) if which == 'main' else drv.other(True)
            model.press(which)
        else:
            drv.main(False) if which == 'main' else drv.other(False)
            model.release(which)
        _drain(spawned)
        assert _observed(app) == model.state, (
            f"{mode}/{source}/{sequence} step {step} ({which} {'press' if pressed else 'release'}):"
            f" observed {_observed(app)} != model {model.state} | {app._hotkey_state_text()}")
        # The keyboard flag belongs to the keyboard alone.
        assert app.hotkey_pressed is (model.key_down or model.undo_down), app._hotkey_state_text()

    # Whatever is still down goes up, then a fresh press must count.
    for which in ('other', 'main'):
        drv.main(False) if which == 'main' else drv.other(False)
    _drain(spawned)
    _fresh_press_counts(app, drv, spawned, mode, source)
    _fresh_press_counts(app, drv, spawned, mode, source)     # and again: no one-shot recovery


# ---------------------------------------------------------------------------
# The stranding sequences, named
# ---------------------------------------------------------------------------

class TestNoPathStrandsTheOther:
    def test_stranded_hotkey_pressed_never_mutes_the_mouse(self, keyboard, spawned):
        """Before: _on_main_hotkey_mouse returned on `self.hotkey_pressed`
        BEFORE its toggle-off branch, so a keyboard flag left True by a lost
        release (or a held undo hotkey) made every Mouse 4 press a silent
        no-op, in both hold and toggle mode."""
        for mode in ('hold', 'toggle'):
            app = _App(mode, 'mouse')
            app.hotkey_pressed = True                  # stranded by the keyboard path
            drv = _Driver(app, keyboard, spawned, 'mouse')
            _fresh_press_counts(app, drv, spawned, mode, 'mouse')
            assert app.hotkey_pressed is True, "the mouse path does not touch the keyboard flag"

    def test_lost_mouse_release_never_mutes_the_keyboard(self, keyboard, spawned):
        """Before: the mouse press set hotkey_pressed and only the mouse
        release cleared it; with that release lost, on_key_press's
        `not self.hotkey_pressed` guard was dead for good."""
        app = _App('hold', 'key')
        drv = _Driver(app, keyboard, spawned, 'key')
        app._on_main_hotkey_mouse(True)                # mouse press; its release is never delivered
        _drain(spawned)
        assert app.recording and app._hold_down_mouse
        app.recording = False                          # the capture ended some other way
        _fresh_press_counts(app, drv, spawned, 'hold', 'key')
        assert app._hold_down_mouse is True, "only the mouse's own release clears its flag"

    def test_stranded_keyboard_flag_self_heals_on_a_fresh_press(self, keyboard, spawned, caplog):
        """hotkey_pressed True with no key down owning it (no _hold_down_key,
        no other combo held) is a stranded flag: the next main press clears
        it, logs a WARNING naming the state, and counts."""
        app = _App('hold', 'key')
        app.hotkey_pressed = True
        drv = _Driver(app, keyboard, spawned, 'key')
        with caplog.at_level(logging.WARNING):
            _fresh_press_counts(app, drv, spawned, 'hold', 'key')
        assert any('stranded' in r.getMessage() for r in caplog.records)

    def test_auto_repeat_is_still_ignored_while_the_key_owns_the_press(self, keyboard, spawned):
        app = _App('toggle', 'key')
        drv = _Driver(app, keyboard, spawned, 'key')
        drv.key(KEY, True)
        app.on_key_press(SimpleNamespace(name=KEY))    # auto-repeat while held
        app.on_key_press(SimpleNamespace(name=KEY))
        assert app.calls == [('start', False)] and app.toggle_active
        drv.key(KEY, False)
        drv.key(KEY, True)                             # toggle-off press
        app.on_key_press(SimpleNamespace(name=KEY))    # auto-repeat of the toggle-off press
        assert app.calls == [('start', False), ('stop',)], "auto-repeat must not restart the toggle"
        assert app.toggle_active is False

    def test_held_undo_hotkey_owns_hotkey_pressed_without_stranding(self, keyboard, spawned):
        app = _App('hold', 'key')
        drv = _Driver(app, keyboard, spawned, 'key')
        drv.combo(UNDO, True)
        assert app.hotkey_pressed is True and app.calls == []   # undo is spawned, not called
        drv.key(KEY, True)                             # main press while undo is held: auto-repeat rule
        assert app.calls == []
        drv.key(KEY, False)
        drv.combo(UNDO, False)
        assert app.hotkey_pressed is False
        _fresh_press_counts(app, drv, spawned, 'hold', 'key')

    def test_stale_os_key_state_cannot_strand_the_release(self, keyboard, spawned):
        """GetAsyncKeyState still reports the key down after pynput delivered
        the key-up (the documented stale-state case): the event-tracked
        current_keys says released, so the press flags clear and the hold
        recording stops."""
        app = _App('hold', 'key')
        drv = _Driver(app, keyboard, spawned, 'key')
        drv.key(KEY, True)
        assert app.recording and app._hold_down_key
        # key-up event arrives but the OS bit is stale: leave KEY in `held`
        app.on_key_release(SimpleNamespace(name=KEY))
        _drain(spawned)
        assert app.recording is False and app._hold_down_key is False and app.hotkey_pressed is False
        keyboard.discard(KEY)
        _fresh_press_counts(app, drv, spawned, 'hold', 'key')

    def test_toggle_off_helper_touches_toggle_state_only(self, keyboard, spawned):
        app = _App('toggle', 'key')
        app.toggle_active = app.recording = app._hotkey_recording = True
        app._main_hotkey_toggle_off('key')
        assert app.calls == [('stop',)]
        assert app.toggle_active is False and app.recording is False
        assert app.hotkey_pressed is False and app._hold_down_key is False and app._hold_down_mouse is False
        assert app._hotkey_recording is False, "a stale True keeps the wake listener deaf"

    def test_keyboard_toggle_off_clears_hotkey_recording(self, keyboard, spawned):
        """stop_recording keeps _hotkey_recording while hotkey_pressed is True,
        which for a toggle-off press is always; the helper clears it."""
        app = _App('toggle', 'key')
        drv = _Driver(app, keyboard, spawned, 'key')
        drv.key(KEY, True); drv.key(KEY, False)
        assert app._hotkey_recording is True
        drv.key(KEY, True)
        assert app.toggle_active is False and app._hotkey_recording is False
        drv.key(KEY, False)
        assert app.hotkey_pressed is False and app._hold_down_key is False

    def test_declined_toggle_start_does_not_arm_a_phantom_toggle(self, keyboard, spawned):
        for source in ('key', 'mouse'):
            app = _App('toggle', source)
            app.model_loaded = False                   # the reason start_recording declines
            app.start_recording = lambda streaming=False, play_earcon=True: app.calls.append(('declined',))
            drv = _Driver(app, keyboard, spawned, source)
            drv.main(True); drv.main(False)
            assert app.toggle_active is False and app.recording is False
            app.start_recording = _App.start_recording.__get__(app)
            app.model_loaded = True
            _fresh_press_counts(app, drv, spawned, 'toggle', source)


# ---------------------------------------------------------------------------
# The hook that never installed (the probe's finding)
# ---------------------------------------------------------------------------

class TestHookInstallsBesidePynput:
    def test_foreign_argtypes_get_a_fresh_pointer(self, monkeypatch):
        """pynput declares its own HOOKPROC in SetWindowsHookExW.argtypes on
        the shared ctypes.windll.user32; our WINFUNCTYPE is then rejected
        and the hook silently never installs. The selector must hand back a
        pointer whose contract is ours."""
        import samsara.mouse_hook as mh

        # A signature ctypes' WINFUNCTYPE cache cannot fold into ours (c_int32
        # IS c_long on Windows, so that would be the very same class).
        foreign_proc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.wintypes.WPARAM,
                                          ctypes.wintypes.LPARAM)
        shared = ctypes.windll.user32.SetWindowsHookExW
        saved = getattr(shared, 'argtypes', None)
        try:
            shared.argtypes = (ctypes.c_int, foreign_proc, ctypes.wintypes.HINSTANCE, ctypes.wintypes.DWORD)
            fn = mh._set_windows_hook_ex()
            assert fn is not shared
            assert fn.argtypes[1] is mh.LowLevelMouseProc and fn.restype is ctypes.c_void_p
        finally:
            shared.argtypes = saved

    def test_matching_or_absent_argtypes_keep_the_shared_pointer(self):
        import samsara.mouse_hook as mh

        shared = ctypes.windll.user32.SetWindowsHookExW
        saved = getattr(shared, 'argtypes', None)
        try:
            shared.argtypes = None
            assert mh._set_windows_hook_ex() is shared
            shared.argtypes = (ctypes.c_int, mh.LowLevelMouseProc, ctypes.c_void_p, ctypes.wintypes.DWORD)
            assert mh._set_windows_hook_ex() is shared
        finally:
            shared.argtypes = saved

    def test_app_logs_an_error_and_keeps_no_hook_when_install_fails(self, monkeypatch, caplog):
        import samsara.mouse_hook as mouse_hook

        class _NeverInstalls:
            def __init__(self, on_button_event, suppress_buttons=None, **kw):
                self.installed = False
                self.install_error = 'SetWindowsHookExW raised: argument 2: TypeError'
                self.suppress_buttons = frozenset(suppress_buttons or ())

            def start(self):
                pass

        monkeypatch.setattr(mouse_hook, 'MouseHook', _NeverInstalls)
        app = _App('hold', 'mouse')
        with caplog.at_level(logging.ERROR):
            app._install_mouse_listener()
        assert app._mouse_hook is None
        assert any('did NOT install' in r.getMessage() for r in caplog.records)
