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


def _hold(monkeypatch, inline=True, other=None, wrappers=False):
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
    pauser.start()
    deadline = time.monotonic() + 2
    while not pauser._paused and time.monotonic() < deadline:
        time.sleep(0.001)
    assert pauser._paused
    if other is not None and other.state == "Playing":
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


def test_duplicate_paused_event_after_window_opts_out(monkeypatch):
    pauser, music, _, clock = _hold(monkeypatch)
    try:
        clock[0] += 1.001
        music.emit()
    finally:
        _finish(pauser, music, 0)


class _UnreadableSession(_Session):
    def __init__(self, *args, persistent=False, recovered_state="Stopped", **kwargs):
        super().__init__(*args, **kwargs)
        self.persistent = persistent
        self.recovered_state = recovered_state
        self.reads = 0

    def get_playback_info(self):
        self.reads += 1
        if self.persistent or self.reads == 1:
            self.state = self.recovered_state
            raise RuntimeError("fake playback unavailable")
        return super().get_playback_info()


@pytest.mark.parametrize("bad_first", [True, False])
@pytest.mark.parametrize("persistent,recovered_state", [(False, "Stopped"), (False, "Playing"), (True, "Playing")])
@pytest.mark.parametrize("wrappers", [False, True], ids=["same_objects", "fresh_wrappers"])
def test_unreadable_snapshot_session_respects_release_state(monkeypatch, caplog, bad_first, persistent, recovered_state, wrappers):
    music = _Session()
    bad = _UnreadableSession(app_id="fake.unreadable", persistent=persistent, recovered_state=recovered_state)
    sessions = [bad, music] if bad_first else [music, bad]
    layer = media_pause.WinRTMediaSessionLayer.__new__(media_pause.WinRTMediaSessionLayer)
    class Wrapper:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

    layer._manager = SimpleNamespace(
        get_sessions=lambda: [Wrapper(s) for s in sessions] if wrappers else sessions,
    )
    fallback = []
    pauser = media_pause.RecordingMediaPauser(
        layer_factory=lambda: layer, fallback=lambda: fallback.append(True),
    )
    music.after_pause = pauser.finish
    pauser._run()
    assert music.pause_calls == 1
    assert music.resumes == (0 if not persistent and recovered_state == "Playing" else 1)
    assert not music.observers
    assert bad.pause_calls == bad.resumes == 0
    assert bad.source_app_user_model_id not in pauser._playing_at_start
    assert not fallback
    warnings = [r.message for r in caplog.records if "Hold-start session unreadable" in r.message]
    assert warnings == ["[MEDIA] Hold-start session unreadable; skipping session"]
    assert "fake.unreadable" not in caplog.text


def test_all_snapshot_sessions_unreadable_uses_duck_fallback(caplog):
    sessions = [_UnreadableSession(app_id=f"fake.unreadable.{i}", persistent=True) for i in range(2)]
    layer = media_pause.WinRTMediaSessionLayer.__new__(media_pause.WinRTMediaSessionLayer)
    layer._manager = SimpleNamespace(get_sessions=lambda: sessions)
    fallback, failures = [], []
    pauser = media_pause.RecordingMediaPauser(
        layer_factory=lambda: layer, fallback=lambda: fallback.append(True),
        on_failure=lambda step, error: failures.append((step, error)),
    )
    pauser._run()
    assert fallback == [True]
    assert failures == [("media snapshot", "no readable media sessions")]
    assert not pauser._playing_at_start
    assert not pauser._paused
    assert all(s.pause_calls == s.resumes == 0 and not s.observers for s in sessions)
    warnings = [r.message for r in caplog.records if "Hold-start session unreadable" in r.message]
    assert len(warnings) == len(sessions)
    assert "fake.unreadable" not in caplog.text


@pytest.mark.parametrize("bad_first", [True, False])
@pytest.mark.parametrize("another_playing", [False, True])
def test_release_unreadable_session_does_not_block_other_resume(monkeypatch, bad_first, another_playing):
    pauser, music, sessions, _ = _hold(monkeypatch)
    bad = _UnreadableSession(app_id="fake.release", persistent=True)
    sessions.insert(0 if bad_first else len(sessions), bad)
    if another_playing:
        sessions.append(_Session(app_id="fake.chosen"))
    _finish(pauser, music, 0 if another_playing else 1)
    assert bad.pause_calls == bad.resumes == 0


@pytest.mark.parametrize("bad_first", [True, False])
@pytest.mark.parametrize("wrappers", [False, True], ids=["same_objects", "fresh_wrappers"])
def test_start_unreadable_session_user_starts_during_hold_wins(monkeypatch, bad_first, wrappers):
    monkeypatch.setattr(media_pause, "monotonic", lambda: 100.0)
    music = _Session()
    other = _UnreadableSession(app_id="fake.chosen", recovered_state="Stopped")
    other.state = "Stopped"
    sessions = [other, music] if bad_first else [music, other]

    class Wrapper:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

    layer = media_pause.WinRTMediaSessionLayer.__new__(media_pause.WinRTMediaSessionLayer)
    layer._manager = SimpleNamespace(
        get_sessions=lambda: [Wrapper(s) for s in sessions] if wrappers else sessions,
    )
    fallback = []
    pauser = media_pause.RecordingMediaPauser(
        layer_factory=lambda: layer, fallback=lambda: fallback.append(True),
    )

    def user_starts_other():
        assert other.state == "Stopped"
        other.emit("Playing")
        pauser.finish()

    music.after_pause = user_starts_other
    pauser._run()
    assert music.pause_calls == 1
    assert music.resumes == 0
    assert music.state == "Paused"
    assert not music.observers
    assert other.reads == 2
    assert other.pause_calls == other.resumes == 0
    assert other.state == "Playing"
    assert not fallback
