"""Main record hotkey on the KEYBOARD in toggle mode (14, 2026-09-13).

Before: on_key_press applied the "another recording owns capture" guard
before the toggle branch, so the second press of the hotkey never reached
`toggle_active -> stop_recording()`; a toggle session could only end by
voice, tray or timeout. The mouse path (_on_main_hotkey_mouse) already did
it right. Now both share _main_hotkey_toggle_off() and the keyboard checks
toggle-off FIRST -- for a toggle it started itself -- and only then guards
STARTING against a streaming / wake / command / mouse-owned capture.

Same _App-stub style as tests/test_mouse_hotkey.py: the real DictationApp
methods are bound onto a small stand-in; dictation is imported inside the
helper (never at module level) -- run with the dev app closed.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_BOUND = (
    'on_key_press', 'on_key_release', '_on_main_hotkey_mouse', '_main_hotkey_toggle_off',
    'parse_hotkey', 'check_hotkey_state', 'get_key_name',
    '_hotkey_state_text', '_other_hotkey_held', '_start_recording_declined', '_mouse_guard',
)

KEY = 'f8'          # a single named key: parse_hotkey -> {'f8'}, no modifiers


class _App:
    def __init__(self, mode='toggle'):
        import dictation

        for name in _BOUND:
            setattr(self, name, getattr(dictation.DictationApp, name).__get__(self))
        self.config = {
            'hotkey': KEY,
            'mode': mode,
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
        self.current_keys = set()
        self.key_press_times = {}
        self._main_hotkey_source = 'key'
        self._hold_down_key = False
        self._hold_down_mouse = False
        self._hotkey_recording = False
        self.calls = []

    # -- what the real app would do -------------------------------------
    def start_recording(self, streaming=False):
        self.calls.append(('start', streaming))
        self.recording = True

    def stop_recording(self):
        self.calls.append(('stop',))
        self.recording = False

    def toggle_continuous_mode(self):
        self.calls.append(('continuous',))

    def _check_command_mode_key(self, key, pressed):
        pass

    def _hands_free_dictation_commit_available(self):
        return False

    def play_sound(self, *a, **k):
        pass


@pytest.fixture
def keyboard(monkeypatch):
    """The OS key state the app polls (_raw_key_pressed): a mutable set."""
    import dictation

    held = set()
    monkeypatch.setattr(dictation, '_raw_key_pressed', lambda key: key in held)
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


def _press(app, held):
    held.add(KEY)
    app.on_key_press(SimpleNamespace(name=KEY))


def _release(app, held):
    held.discard(KEY)
    app.on_key_release(SimpleNamespace(name=KEY))


def _tap(app, held):
    _press(app, held)
    _release(app, held)


class TestToggleFromTheKeyboard:
    def test_first_press_starts_a_toggle_recording(self, keyboard, spawned):
        app = _App(mode='toggle')
        _press(app, keyboard)
        assert app.calls == [('start', False)]
        assert app.toggle_active is True and app.recording is True
        assert app._main_hotkey_source == 'key'
        _release(app, keyboard)
        assert app.hotkey_pressed is False and app.recording is True     # toggle: release never stops
        assert spawned == []

    def test_second_press_stops_it(self, keyboard, spawned):
        app = _App(mode='toggle')
        _tap(app, keyboard)
        _tap(app, keyboard)
        assert app.calls == [('start', False), ('stop',)]
        assert app.toggle_active is False and app.recording is False
        assert app.hotkey_pressed is False
        assert spawned == []                                              # stop is direct, not deferred

    def test_third_press_starts_again(self, keyboard):
        app = _App(mode='toggle')
        for _ in range(3):
            _tap(app, keyboard)
        assert app.calls == [('start', False), ('stop',), ('start', False)]
        assert app.toggle_active is True

    def test_press_while_stop_in_flight_is_ignored(self, keyboard):
        app = _App(mode='toggle')
        _tap(app, keyboard)
        app._stop_in_flight = True
        _tap(app, keyboard)
        assert app.calls == [('start', False)]
        assert app.toggle_active is True and app.recording is True

    def test_press_while_a_streaming_session_owns_capture_is_ignored(self, keyboard):
        app = _App(mode='toggle')
        app.toggle_active = True
        app.recording = True
        app._streaming_session = object()
        _tap(app, keyboard)
        assert app.calls == []
        assert app.toggle_active is True, "the guard must not clear a toggle it did not stop"
        assert app.recording is True

    def test_press_stops_a_mouse_started_toggle_too(self, keyboard):
        """28: toggle_active is only ever set by the main hotkey, so a main
        hotkey press ends it from either path. Before, the owner check left
        the keyboard press dead until the mouse ended the toggle."""
        app = _App(mode='toggle')
        app._on_main_hotkey_mouse(True)          # mouse starts the toggle
        app._on_main_hotkey_mouse(False)
        assert app.calls == [('start', False)] and app._main_hotkey_source == 'mouse'
        _tap(app, keyboard)
        assert app.calls == [('start', False), ('stop',)]
        assert app.toggle_active is False and app.recording is False
        assert app.hotkey_pressed is False and app._hold_down_key is False

    def test_press_does_not_stop_a_wake_or_command_session_recording(self, keyboard):
        """Capture owned by something else (wake session, command session):
        recording is on but toggle_active is off -- still protected."""
        app = _App(mode='toggle')
        app.recording = True                     # e.g. a wake-word capture in progress
        _tap(app, keyboard)
        assert app.calls == []
        assert app.recording is True and app.toggle_active is False

    def test_mouse_path_still_stops_a_keyboard_started_toggle(self, keyboard):
        """Unchanged mouse behaviour, now through the shared helper."""
        app = _App(mode='toggle')
        _tap(app, keyboard)
        app._on_main_hotkey_mouse(True)
        assert app.calls == [('start', False), ('stop',)]
        assert app.toggle_active is False and app._main_hotkey_source == 'mouse'


class TestOtherModesUnchanged:
    def test_hold_mode_press_starts_and_release_stops(self, keyboard, spawned):
        app = _App(mode='hold')
        _press(app, keyboard)
        assert app.calls == [('start', False)] and app.hotkey_pressed is True
        _release(app, keyboard)
        assert [name for name, _ in spawned] == ['stop-rec']              # deferred stop, as before

    def test_hold_mode_press_while_recording_is_still_guarded(self, keyboard, spawned):
        app = _App(mode='hold')
        app.recording = True
        _tap(app, keyboard)
        assert app.calls == []

    def test_continuous_mode_press_toggles_continuous_listening(self, keyboard, spawned):
        app = _App(mode='continuous')
        _tap(app, keyboard)
        assert app.calls == [('continuous',)]
        assert app.toggle_active is False and spawned == []

    def test_toggle_off_helper_is_the_single_body(self):
        app = _App(mode='toggle')
        app.toggle_active = True
        app.recording = True
        app._main_hotkey_toggle_off('mouse')
        assert app.calls == [('stop',)]
        assert app.toggle_active is False
        # 28: the helper touches toggle state only; the physical-press flags
        # belong to the caller, so neither path can strand the other.
        assert app.hotkey_pressed is False and app._hold_down_key is False
        assert app._hotkey_recording is False
        assert app._main_hotkey_source == 'mouse'
