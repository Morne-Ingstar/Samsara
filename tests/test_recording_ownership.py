"""Regression coverage for recording ownership leaking across early returns."""

from types import SimpleNamespace
import threading
import time

import numpy as np
import pytest

import dictation


class _Consumer:
    def __init__(self, audio):
        self.audio = audio

    def drain(self):
        return self.audio

    def drain_after_release(self, **_kwargs):
        return self.audio


def _app(*, mode=None, ghost=False, audio=None):
    app = object.__new__(dictation.DictationApp)
    app.recording = True
    app.hotkey_pressed = False
    app._hotkey_recording = True
    app._ace_dictation_active = True
    app._dictation_consumer = _Consumer(
        np.ones(16_000, dtype=np.float32) if audio is None else audio
    )
    app.command_mode_recording = mode == "command"
    app.ava_mode_recording = mode == "ava"
    app._command_mode_ghost_tap = bool(ghost and mode == "command")
    app._ava_mode_ghost_tap = bool(ghost and mode == "ava")
    app.config = {
        "mode": "hold",
        "recording_tail_ms": 0,
        "recording_tail_silence_ms": 300,
        "recording_tail_max_ms": 1200,
        "recording_tail_speech_threshold": 0.008,
    }
    app.set_app_state = lambda **kw: setattr(app, "recording", kw["recording"])
    app.play_sound = lambda *_args, **_kwargs: None
    app._release_icon_chase = lambda *_args: None
    app._update_tray_tooltip = lambda: None
    return app


def _assert_ownership_clear(app):
    assert app.command_mode_recording is False
    assert app.ava_mode_recording is False
    assert app._command_mode_ghost_tap is False
    assert app._ava_mode_ghost_tap is False


@pytest.mark.parametrize("mode", ["ava", "command"])
def test_gated_ghost_tap_cannot_discard_next_ordinary_recording(monkeypatch, mode):
    """A ghost recording can return before Whisper without leaking ownership."""
    spawned = []
    monkeypatch.setattr(
        dictation.thread_registry,
        "spawn",
        lambda name, target, daemon: spawned.append((name, target, daemon)),
    )

    app = _app(mode=mode, ghost=True)
    app._stop_recording_impl()

    _assert_ownership_clear(app)
    assert spawned == []  # discarded before gate/model work

    # The next Ctrl+Shift recording uses the same app object. It must get a
    # clean ordinary context and reach the worker instead of inheriting the
    # prior Ava/command ghost flag.
    app.recording = True
    app._ace_dictation_active = True
    app._dictation_consumer = _Consumer(np.ones(16_000, dtype=np.float32))
    app._stop_recording_impl()

    _assert_ownership_clear(app)
    assert [item[0] for item in spawned] == ["dictation.transcribe"]


@pytest.mark.parametrize("mode", ["ava", "command"])
def test_no_audio_early_return_still_clears_recording_ownership(monkeypatch, mode):
    spawned = []
    monkeypatch.setattr(
        dictation.thread_registry,
        "spawn",
        lambda *args, **kwargs: spawned.append((args, kwargs)),
    )
    app = _app(mode=mode, ghost=True, audio=SimpleNamespace())
    app._dictation_consumer = _Consumer(None)

    app._stop_recording_impl()

    _assert_ownership_clear(app)
    assert spawned == []


def _mode_race_app(mode):
    app = object.__new__(dictation.DictationApp)
    app.recording = False
    app.command_mode_active = mode == "command"
    app.ava_mode_active = mode == "ava"
    app.command_mode_recording = False
    app.ava_mode_recording = False
    app._command_mode_ghost_tap = False
    app._ava_mode_ghost_tap = False
    app._command_mode_lock = threading.Lock()
    app._ava_mode_lock = threading.Lock()
    app._command_mode_session_start = time.monotonic()
    app._ava_mode_session_start = time.monotonic()
    app._session_mode_manager = None
    app.config = {
        "command_mode": {
            "mode": "hold",
            "enter_debounce_ms": 0,
            "exit_earcon": False,
        },
    }
    app._cancel_command_mode_inactivity_timer = lambda: None
    app.play_sound = lambda *_args, **_kwargs: None
    app._started = 0
    app._stopped = 0

    def start_recording(**_kwargs):
        app._started += 1
        app.recording = True

    def stop_recording():
        app._stopped += 1
        app.recording = False

    app.start_recording = start_recording
    app.stop_recording = stop_recording
    return app


def _exit_mode(app, mode):
    if mode == "ava":
        app.exit_ava_mode()
    else:
        app.exit_command_mode()


def _enter_worker(app, mode):
    if mode == "ava":
        app._do_enter_ava_mode()
    else:
        app._do_enter_command_mode()


@pytest.mark.parametrize("mode", ["ava", "command"])
def test_hold_exit_before_enter_worker_cannot_start_orphan_recording(mode):
    app = _mode_race_app(mode)

    _exit_mode(app, mode)
    _enter_worker(app, mode)

    assert app._started == 0
    assert app.recording is False


@pytest.mark.parametrize("mode", ["ava", "command"])
def test_hold_enter_worker_claim_is_atomic_with_exit(monkeypatch, mode):
    app = _mode_race_app(mode)
    start_entered = threading.Event()
    release_start = threading.Event()
    exit_attempted = threading.Event()
    exit_done = threading.Event()

    def blocked_start(**_kwargs):
        start_entered.set()
        assert release_start.wait(1.0)
        app._started += 1
        app.recording = True

    app.start_recording = blocked_start
    worker = threading.Thread(target=_enter_worker, args=(app, mode))
    worker.start()
    assert start_entered.wait(1.0)

    def exit_worker():
        exit_attempted.set()
        _exit_mode(app, mode)
        exit_done.set()

    exiting = threading.Thread(target=exit_worker)
    exiting.start()
    assert exit_attempted.wait(1.0)
    assert not exit_done.wait(0.05)

    release_start.set()
    worker.join(1.0)
    exiting.join(1.0)

    assert not worker.is_alive()
    assert not exiting.is_alive()
    assert app._started == 1
    assert app._stopped == 1
    assert app.recording is False
