"""Fake WinRT operations exercise the real adapter and recording owner."""
from types import SimpleNamespace
import asyncio
import time

import pytest

from samsara import media_pause


class _Operation:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def complete():
            return self.value
        return complete().__await__()


class _Session:
    source_app_user_model_id = "fake.music"

    def __init__(self, inline=True, app_id="fake.music", refuse=False, after_pause=None):
        self.state = "Playing"
        self.source_app_user_model_id = app_id
        self.refuse = refuse
        self.after_pause = after_pause
        self.pause_calls = 0
        self.inline = inline
        self.observers = []
        self.resumes = 0

    def get_playback_info(self):
        return SimpleNamespace(playback_status=self.state)

    def add_playback_info_changed(self, callback):
        self.observers.append(callback)
        return callback

    def remove_playback_info_changed(self, token):
        self.observers.remove(token)

    def emit(self, state="Paused"):
        self.state = state
        for callback in list(self.observers):
            callback(self, None)

    def try_pause_async(self):
        self.pause_calls += 1
        if self.refuse:
            return _Operation(False)
        self.state = "Paused"
        if self.inline:
            self.emit()
        if self.after_pause:
            self.after_pause()
        return _Operation(True)

    def try_play_async(self):
        self.resumes += 1
        self.emit("Playing")
        return _Operation(True)


@pytest.fixture(autouse=True)
def socket_free_immediate_operations(monkeypatch):
    # These fake awaitables complete from the ready queue, without I/O.
    # Retain real asyncio.run while avoiding the gate's forbidden wake socket.
    class LocalLoop(asyncio.SelectorEventLoop):
        def __init__(self):
            super().__init__()
            self._selector.select = lambda timeout=None: []

        def _make_self_pipe(self):
            pass

        def _close_self_pipe(self):
            pass

    monkeypatch.setattr(asyncio.events, "new_event_loop", LocalLoop)


def _hold(monkeypatch, inline=True, other=None, short=False, wrappers=False):
    clock = [100.0]
    monkeypatch.setattr(media_pause, "monotonic", lambda: clock[0])
    music = _Session(inline)
    sessions = [music]
    if other is not None:
        sessions.append(other)
    # No winsdk import or real media: only the real adapter's methods run.
    layer = media_pause.WinRTMediaSessionLayer.__new__(media_pause.WinRTMediaSessionLayer)
    class Wrapper:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

    layer._manager = SimpleNamespace(
        get_sessions=lambda: [Wrapper(s) for s in sessions] if wrappers else sessions,
    )
    pauser = media_pause.RecordingMediaPauser(layer_factory=lambda: layer)
    if short:
        music.after_pause = pauser.finish
    pauser.start()
    deadline = time.monotonic() + 2
    while not pauser._paused and time.monotonic() < deadline:
        time.sleep(0.001)
    assert pauser._paused
    if other is not None and other.state == "Playing" and not short:
        while not other.pause_calls and time.monotonic() < deadline:
            time.sleep(0.001)
        assert other.pause_calls
    return pauser, music, sessions, clock


def _finish(pauser, music, resumes):
    pauser.finish()
    pauser.wait()
    assert not pauser._worker.is_alive()
    assert music.resumes == resumes
    assert not music.observers


@pytest.mark.parametrize("inline", [True, False], ids=["inside_pause", "after_pause_returns"])
def test_own_pause_notification_resumes(monkeypatch, inline):
    pauser, music, _, clock = _hold(monkeypatch, inline)
    try:
        if not inline:
            clock[0] += 0.5
            music.emit()
    finally:
        _finish(pauser, music, 1)


@pytest.mark.parametrize("inline", [True, False])
def test_duplicate_own_paused_notifications_resume(monkeypatch, inline):
    pauser, music, _, _ = _hold(monkeypatch, inline=inline)
    try:
        music.emit()
        music.emit()
    finally:
        _finish(pauser, music, 1)


def test_first_pause_event_after_window_is_conservative(monkeypatch):
    pauser, music, _, clock = _hold(monkeypatch, inline=False)
    try:
        clock[0] += 1.001
        music.emit()
    finally:
        _finish(pauser, music, 0)


def test_user_play_then_pause_before_delayed_acknowledgement_wins(monkeypatch):
    pauser, music, _, _ = _hold(monkeypatch, inline=False)
    try:
        music.emit("Playing")
        music.emit("Paused")
    finally:
        _finish(pauser, music, 0)


@pytest.mark.parametrize("wrappers", [False, True], ids=["same_objects", "fresh_wrappers"])
def test_different_session_starting_wins(monkeypatch, wrappers):
    pauser, music, sessions, _ = _hold(monkeypatch, wrappers=wrappers)
    try:
        other = _Session(app_id="fake.other")
        sessions.append(other)
    finally:
        _finish(pauser, music, 0)
    assert other.resumes == 0
    assert other.state == "Playing"


def test_existing_other_session_starting_wins(monkeypatch):
    other = _Session(app_id="fake.other")
    other.state = "Paused"
    pauser, music, _, _ = _hold(monkeypatch, other=other)
    try:
        other.emit("Playing")
    finally:
        _finish(pauser, music, 0)


def test_no_event_still_resumes_confirmed_pause(monkeypatch):
    pauser, music, _, clock = _hold(monkeypatch, inline=False)
    clock[0] += 20
    _finish(pauser, music, 1)


def test_failed_other_session_check_suppresses_resume(monkeypatch):
    pauser, music, _, _ = _hold(monkeypatch)
    def fail():
        raise RuntimeError("fake manager unavailable")
    pauser._layer_factory()._manager.get_sessions = fail
    _finish(pauser, music, 0)


@pytest.mark.parametrize("wrappers", [False, True], ids=["same_objects", "fresh_wrappers"])
def test_refused_player_running_at_start_does_not_block_resume(monkeypatch, wrappers):
    other = _Session(app_id="fake.other", refuse=True)
    pauser, music, _, _ = _hold(monkeypatch, other=other, wrappers=wrappers)
    _finish(pauser, music, 1)
    assert other.state == "Playing"
    assert other.resumes == 0


@pytest.mark.parametrize("wrappers", [False, True], ids=["same_objects", "fresh_wrappers"])
def test_short_hold_unreached_player_does_not_block_resume(monkeypatch, wrappers):
    other = _Session(app_id="fake.other")
    # Run synchronously: finish inside A's pause, before the loop reaches B.
    clock = [100.0]
    monkeypatch.setattr(media_pause, "monotonic", lambda: clock[0])
    music = _Session()
    sessions = [music, other]
    class Wrapper:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

    layer = media_pause.WinRTMediaSessionLayer.__new__(media_pause.WinRTMediaSessionLayer)
    layer._manager = SimpleNamespace(
        get_sessions=lambda: [Wrapper(s) for s in sessions] if wrappers else sessions,
    )
    pauser = media_pause.RecordingMediaPauser(layer_factory=lambda: layer)
    music.after_pause = pauser.finish
    pauser._run()
    assert music.resumes == 1
    assert not music.observers
    assert other.pause_calls == 0
    assert other.state == "Playing"


@pytest.mark.parametrize("state", ["Playing", "Changing"])
def test_non_paused_notification_inside_window_opts_out(monkeypatch, state):
    pauser, music, _, _ = _hold(monkeypatch)
    try:
        music.emit(state)
        music.emit("Paused")
    finally:
        _finish(pauser, music, 0)


def test_genuine_user_pause_after_window_wins(monkeypatch):
    pauser, music, _, clock = _hold(monkeypatch)
    try:
        clock[0] += 1.001
        music.emit()
    finally:
        _finish(pauser, music, 0)
