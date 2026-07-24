"""Tests for the session ducking engine API."""

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

    assert sessions[0].volume == pytest.approx(0.8)
    assert sessions[1].volume == pytest.approx(0.6)
    assert sessions[2].volume == pytest.approx(0.15)
    assert ducker.current_duck(333) == pytest.approx(0.15)


def test_restore_is_exact_and_idempotent(monkeypatch):
    session = FakeSession("restore", 333, 0.9)
    sessions = [session]
    monkeypatch.setattr(ad.os, "getpid", lambda: 111)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: sessions)

    ducker = ad.SessionDucker(duck_level=0.2)
    ducker.start()
    assert session.volume == pytest.approx(0.2)

    ducker.stop()
    assert session.volume == pytest.approx(0.9)
    assert session.volume_calls == [0.2, 0.9]

    ducker.stop()
    assert session.volume == pytest.approx(0.9)
    assert session.volume_calls == [0.2, 0.9]


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
        ("first", 0.1),
        ("second", 0.1),
        ("second", 0.8),
        ("first", 0.7),
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
    assert sessions[0].volume == pytest.approx(0.2)

    sessions.append(FakeSession("new", 333, 0.3))
    ducker._run_sweep()
    assert sessions[1].volume == pytest.approx(0.2)

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
