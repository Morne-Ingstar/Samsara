"""946: real hotkey callbacks, with capture startup controlled by another thread.

No dictation bootstrap, hardware, or owner profile is needed. Production
start_recording waits for activation before returning; the stub preserves that
contract while a separate thread publishes recording=True after a delay.
"""
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from samsara import hotkeys


class App(hotkeys.HotkeyCluster):
    def __init__(self, source='mouse', mode='hold'):
        self.config = {'hotkey': 'mouse5' if source == 'mouse' else 'f8', 'mode': mode}
        self.snoozed = self.recording = self.hotkey_pressed = False
        self._stop_in_flight = self.toggle_active = self.continuous_active = False
        self.command_mode_active = self.command_mode_recording = False
        self.ava_mode_recording = self._memo_recording = self._hotkey_recording = False
        self._hold_down_key = self._hold_down_mouse = False
        self._main_hotkey_source = 'key'
        self._streaming_session = None
        self._session_mode_manager = None
        self.current_keys = set()
        self.key_press_times = {}
        self.calls = []
        self.start_entered = threading.Event()
        self.allow_start = threading.Event()
        self.allow_start.set()
        self.stop_entered = threading.Event()
        self.allow_stop = threading.Event()
        self.allow_stop.set()

    def start_recording(self, streaming=False):
        self.calls.append('start')
        self.start_entered.set()

        def activate():
            assert self.allow_start.wait(3)
            time.sleep(.02)
            self.recording = self._hotkey_recording = True

        worker = threading.Thread(target=activate)
        worker.start()
        worker.join(4)
        assert not worker.is_alive()

    def stop_recording(self):
        self.calls.append('stop')
        self.stop_entered.set()
        assert self.allow_stop.wait(3)
        self.recording = self._hotkey_recording = False

    def check_hotkey_state(self, combo):
        return self.parse_hotkey(combo).issubset(self.current_keys) if combo else False

    def _check_command_mode_key(self, *args, **kwargs):
        pass

    def toggle_continuous_mode(self):
        self.calls.append('continuous')

    def event(self, source, pressed):
        if source == 'mouse':
            self._on_main_hotkey_mouse(pressed)
        else:
            key = SimpleNamespace(name='f8')
            (self.on_key_press if pressed else self.on_key_release)(key)


@pytest.fixture
def jobs(monkeypatch):
    pending = []
    monkeypatch.setattr(hotkeys.thread_registry, 'spawn',
                        lambda name, target, **kw: pending.append(target))
    monkeypatch.setattr(hotkeys.flight_recorder, 'record', lambda *a, **kw: None)
    return pending


def drain(jobs):
    while jobs:
        jobs.pop(0)()


def join(worker, errors):
    worker.join(4)
    assert not worker.is_alive()
    assert not errors


def launch(action):
    errors = []

    def run():
        try:
            action()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    return worker, errors


@pytest.mark.parametrize('source', ['mouse', 'key'])
@pytest.mark.parametrize('replay', [True, False])
def test_release_during_delayed_start_ends_capture(jobs, source, replay):
    app = App(source)
    if replay:
        app.event(source, True)
        app.event(source, False)
        # Physical re-press during a stop, as in the supplied 49 ms sequence.
        app.event(source, True)
        assert app._release_tail_repress[0] == source
        app.start_entered.clear()
    app.allow_start.clear()
    action = jobs.pop(0) if replay else lambda: app.event(source, True)
    starter, errors = launch(action)
    assert app.start_entered.wait(2)
    released = threading.Event()

    def release():
        app.event(source, False)
        released.set()

    releaser, release_errors = launch(release)
    try:
        # Baseline consumes the release here before recording becomes True.
        released.wait(.05)
    finally:
        app.allow_start.set()
        join(starter, errors)
        join(releaser, release_errors)
    drain(jobs)
    assert not app.recording
    assert not app._hold_down_mouse and not app._hold_down_key
    assert not app.hotkey_pressed and not app._stop_in_flight
    assert app._main_hotkey_source == 'key'
    assert app.calls == ['start', 'stop'] * (2 if replay else 1)


@pytest.mark.parametrize('owner', ['mouse', 'key'])
def test_stale_mouse_hold_recovers_in_one_press_release(jobs, caplog, owner):
    app = App()
    app.recording = app._hotkey_recording = True
    app._main_hotkey_source = owner  # 'key' is the corrupted state in the log.
    with caplog.at_level(logging.WARNING, logger='Samsara'):
        app.event('mouse', True)
        app.event('mouse', False)
        drain(jobs)
    assert not app.recording
    assert app.calls == ['stop']
    assert '[HOTKEY] stale hold recovered' in caplog.text


@pytest.mark.parametrize('special', ['keyboard', 'command', 'ava', 'memo', 'wake'])
def test_mouse_cannot_recover_another_capture(jobs, special):
    app = App()
    app.recording = True
    app._hotkey_recording = special != 'wake'
    if special == 'keyboard':
        app._hold_down_key = True
    elif special != 'wake':
        setattr(app, {'command': 'command_mode_recording', 'ava': 'ava_mode_recording',
                      'memo': '_memo_recording'}[special], True)
    app.event('mouse', True)
    app.event('mouse', False)
    drain(jobs)
    assert app.recording
    assert not app.calls


@pytest.mark.parametrize('source', ['mouse', 'key'])
@pytest.mark.parametrize('fast', [False, True])
def test_ordinary_and_fast_repress_have_one_pair_per_hold(jobs, source, fast):
    app = App(source)
    app.event(source, True)
    app.event(source, False)
    if fast:
        app.allow_stop.clear()
        worker, errors = launch(jobs.pop(0))
        assert app.stop_entered.wait(2)
        started = time.monotonic()
        app.event(source, True)
        assert time.monotonic() - started < .06
        app.allow_stop.set()
        join(worker, errors)
    else:
        drain(jobs)
        app.event(source, True)
    app.event(source, False)
    drain(jobs)
    assert app.calls == ['start', 'stop', 'start', 'stop']
    assert not app.recording and not app._stop_in_flight


@pytest.mark.parametrize('mode', ['toggle', 'continuous'])
def test_recovery_does_not_apply_to_other_modes(jobs, mode):
    app = App(mode=mode)
    app.recording = app._hotkey_recording = True
    app.event('mouse', True)
    app.event('mouse', False)
    drain(jobs)
    assert app.recording and not app.calls


def test_declined_mouse_start_cannot_claim_later_wake_capture(jobs):
    app = App()
    app.start_recording = lambda **kwargs: None
    app.event('mouse', True)
    app.event('mouse', False)
    assert app._main_hotkey_source == 'key'
    app.recording = True  # A wake capture starts after the declined hold.
    app.event('mouse', True)
    app.event('mouse', False)
    drain(jobs)
    assert app.recording and not app.calls
