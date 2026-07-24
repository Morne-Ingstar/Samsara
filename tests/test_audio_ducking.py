"""Tests for the session ducking engine API.

2026-07-24 amendment: RELATIVE (multiplicative) attenuation, not absolute
level-setting -- duck_level is a factor applied to each session's OWN
current volume (applied = current * duck_level), and stop() only restores
a session if its live volume still equals what THIS instance applied (a
user/app change mid-duck wins over a stale snapshot). See
samsara/audio_ducking.py's SessionDucker docstring.
"""

from __future__ import annotations

import pytest

from samsara import audio_ducking as ad


class FakeSession:
    def __init__(self, session_id: str, pid: int, volume: float = 1.0):
        self.session_id = session_id
        self.pid = pid
        self.volume = volume
        self.closed = False
        self.volume_calls: list[float] = []

    def get_master_volume(self) -> float:
        return self.volume

    def set_master_volume(self, level: float) -> None:
        self.volume = float(level)
        self.volume_calls.append(float(level))

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def no_threads(monkeypatch):
    class NoopTimer:
        def __init__(self, *_args, **_kwargs):
            self.cancelled = False

        def start(self):
            return None

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(ad.threading, "Timer", NoopTimer)


def test_start_excludes_own_pid_and_excludes_set(monkeypatch):
    sessions = [
        FakeSession("own", 111, 0.8),
        FakeSession("excluded", 222, 0.6),
        FakeSession("other", 333, 0.9),
    ]
    monkeypatch.setattr(ad.os, "getpid", lambda: 111)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: sessions)

    ducker = ad.SessionDucker(exclude_pids={222})
    ducker.start()

    assert sessions[0].volume == pytest.approx(0.8)   # excluded (own pid) -- untouched
    assert sessions[1].volume == pytest.approx(0.6)   # excluded (explicit) -- untouched
    assert sessions[2].volume == pytest.approx(0.9 * 0.15)  # relative: 0.9 * default duck_level
    assert ducker.current_duck(333) == pytest.approx(0.15)


def test_restore_is_exact_and_idempotent(monkeypatch):
    session = FakeSession("restore", 333, 0.9)
    sessions = [session]
    monkeypatch.setattr(ad.os, "getpid", lambda: 111)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: sessions)

    ducker = ad.SessionDucker(duck_level=0.2)
    ducker.start()
    applied = pytest.approx(0.9 * 0.2)
    assert session.volume == applied

    ducker.stop()
    assert session.volume == pytest.approx(0.9)
    assert session.volume_calls == [pytest.approx(0.9 * 0.2), pytest.approx(0.9)]

    ducker.stop()
    assert session.volume == pytest.approx(0.9)
    assert len(session.volume_calls) == 2


def test_restore_occurs_in_reverse_order(monkeypatch):
    first = FakeSession("first", 333, 0.7)
    second = FakeSession("second", 444, 0.8)
    all_calls: list[tuple[str, float]] = []

    old_first = first.set_master_volume
    old_second = second.set_master_volume

    def first_set(level: float) -> None:
        all_calls.append(("first", level))
        old_first(level)

    def second_set(level: float) -> None:
        all_calls.append(("second", level))
        old_second(level)

    first.set_master_volume = first_set  # type: ignore[method-assign]
    second.set_master_volume = second_set  # type: ignore[method-assign]

    monkeypatch.setattr(ad.os, "getpid", lambda: 111)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [first, second])
    ducker = ad.SessionDucker(duck_level=0.1)

    ducker.start()
    ducker.stop()

    assert all_calls == [
        ("first", pytest.approx(0.7 * 0.1)),
        ("second", pytest.approx(0.8 * 0.1)),
        ("second", pytest.approx(0.8)),
        ("first", pytest.approx(0.7)),
    ]


def test_context_manager_restores_on_exception(monkeypatch):
    session = FakeSession("ctx", 333, 0.9)
    monkeypatch.setattr(ad.os, "getpid", lambda: 111)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [session])

    with pytest.raises(RuntimeError, match="boom"):
        with ad.SessionDucker() as ducker:
            ducker.start()
            raise RuntimeError("boom")

    assert session.volume == pytest.approx(0.9)


def test_sweep_catches_new_session(monkeypatch):
    sessions = [FakeSession("first", 333, 0.9)]
    monkeypatch.setattr(ad.os, "getpid", lambda: 111)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: sessions)

    ducker = ad.SessionDucker(duck_level=0.2)
    ducker.start()
    assert sessions[0].volume == pytest.approx(0.9 * 0.2)

    sessions.append(FakeSession("new", 333, 0.3))
    ducker._run_sweep()
    assert sessions[1].volume == pytest.approx(0.3 * 0.2)

    ducker.stop()
    assert sessions[0].volume == pytest.approx(0.9)
    assert sessions[1].volume == pytest.approx(0.3)


def test_com_error_is_noop(monkeypatch):
    def boom():
        raise OSError("simulated enumeration failure")

    monkeypatch.setattr(ad, "_iter_audio_sessions", boom)
    ducker = ad.SessionDucker()
    ducker.start()

    assert ducker.current_duck(123) is None
    ducker.stop()


def test_current_duck_min_composition(monkeypatch):
    session = FakeSession("compose", 333, 1.0)
    monkeypatch.setattr(ad.os, "getpid", lambda: 111)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [session])

    ducker = ad.SessionDucker(duck_level=0.2)
    assert ducker.current_duck(333) is None

    ducker.start()
    assert ducker.current_duck(333) == pytest.approx(0.2)
    assert min(ducker.current_duck(333) or 1.0, 0.07) == pytest.approx(0.07)

    ducker.stop()
    assert ducker.current_duck(333) is None


def test_stop_without_start_is_noop() -> None:
    ad.SessionDucker().stop()


class TestConditionalRestore:
    """2026-07-24 amendment: a user/app volume change mid-duck must win
    over stop()'s stale pre-duck snapshot."""

    def test_restores_when_volume_unchanged_since_duck(self, monkeypatch):
        session = FakeSession("unchanged", 333, 0.9)
        monkeypatch.setattr(ad.os, "getpid", lambda: 111)
        monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [session])

        ducker = ad.SessionDucker(duck_level=0.2)
        ducker.start()
        ducker.stop()

        assert session.volume == pytest.approx(0.9)

    def test_skips_restore_when_user_changed_volume_mid_duck(self, monkeypatch):
        session = FakeSession("user-changed", 333, 0.9)
        monkeypatch.setattr(ad.os, "getpid", lambda: 111)
        monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [session])

        ducker = ad.SessionDucker(duck_level=0.2)
        ducker.start()
        assert session.volume == pytest.approx(0.9 * 0.2)

        # User (or the app itself) changes the volume WHILE ducked --
        # not through the ducker, e.g. the user dragged Spotify's volume
        # slider.
        session.set_master_volume(0.5)

        ducker.stop()

        # Must NOT be clobbered back to 0.9 -- the user's 0.5 wins.
        assert session.volume == pytest.approx(0.5)

    def test_skips_restore_within_float_epsilon_still_counts_as_unchanged(self, monkeypatch):
        """A few ULPs of get/set round-trip drift must not look like a
        user change."""
        session = FakeSession("epsilon", 333, 0.9)
        monkeypatch.setattr(ad.os, "getpid", lambda: 111)
        monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [session])

        ducker = ad.SessionDucker(duck_level=0.2)
        ducker.start()
        applied = session.volume
        # Simulate negligible float round-trip drift, not a real change.
        session.volume = applied + 1e-5

        ducker.stop()

        assert session.volume == pytest.approx(0.9)


class TestStartIdempotent:
    """Spark's recommended engine test: start() while already active is a
    no-op -- callers (e.g. a debounced capture-window reuse path) must be
    able to call start() repeatedly without re-ducking or losing the
    original pre-duck snapshot."""

    def test_start_while_active_does_not_reapply_duck(self, monkeypatch):
        session = FakeSession("idempotent", 333, 0.9)
        monkeypatch.setattr(ad.os, "getpid", lambda: 111)
        monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [session])

        ducker = ad.SessionDucker(duck_level=0.2)
        ducker.start()
        calls_after_first_start = list(session.volume_calls)

        ducker.start()  # must be a no-op

        assert session.volume_calls == calls_after_first_start

    def test_start_while_active_preserves_original_snapshot_for_stop(self, monkeypatch):
        session = FakeSession("idempotent-restore", 333, 0.9)
        monkeypatch.setattr(ad.os, "getpid", lambda: 111)
        monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [session])

        ducker = ad.SessionDucker(duck_level=0.2)
        ducker.start()
        ducker.start()  # no-op, must not overwrite the original-volume snapshot

        ducker.stop()

        assert session.volume == pytest.approx(0.9)
