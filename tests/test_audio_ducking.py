"""Tests for the session ducking engine API.

2026-07-24 amendment: RELATIVE (multiplicative) attenuation, not absolute
level-setting -- duck_level is a factor applied to each session's OWN
current volume (applied = current * duck_level), and stop() only restores
a session if its live volume still equals what THIS instance applied (a
user/app change mid-duck wins over a stale snapshot). See
samsara/audio_ducking.py's SessionDucker docstring.
"""

from __future__ import annotations

import threading
import ctypes
import inspect
import ast

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
    ad._SHARED_SESSIONS.clear()

    class NoopTimer:
        def __init__(self, *_args, **_kwargs):
            self.cancelled = False

        def start(self):
            return None

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(
        ad.thread_registry,
        "timer",
        lambda *_args, **_kwargs: NoopTimer(),
    )
    yield
    ad._SHARED_SESSIONS.clear()


def test_capture_first_idle_second_discovery_restores_full_volume(monkeypatch):
    session = FakeSession("shared", 333, 1.0)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [session])

    capture_ducker = ad.SessionDucker(duck_level=0.15)
    idle_ducker = ad.SessionDucker(duck_level=0.8)

    capture_ducker.start()
    assert session.volume == pytest.approx(1.0 * 0.15)

    idle_ducker.start()
    assert session.volume == pytest.approx(1.0 * 0.12)

    capture_ducker.stop()
    idle_ducker.stop()

    assert session.volume == pytest.approx(1.0)


def test_idle_first_capture_second_discovery_restores_full_volume(monkeypatch):
    session = FakeSession("shared", 333, 1.0)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [session])

    idle_ducker = ad.SessionDucker(duck_level=0.8)
    capture_ducker = ad.SessionDucker(duck_level=0.15)

    idle_ducker.start()
    assert session.volume == pytest.approx(1.0 * 0.8)

    capture_ducker.start()
    assert session.volume == pytest.approx(1.0 * 0.12)

    capture_ducker.stop()
    idle_ducker.stop()

    assert session.volume == pytest.approx(1.0)


def test_stop_interleaved_with_blocked_sweep_restores_nothing(monkeypatch):
    first_session = FakeSession("tracked", 333, 1.0)
    blocked_session = FakeSession("blocked", 333, 1.0)
    started = threading.Event()
    allow_set = threading.Event()

    original_set = blocked_session.set_master_volume

    def blocking_set(level: float) -> None:
        started.set()
        allow_set.wait(timeout=1)
        original_set(level)

    blocked_session.set_master_volume = blocking_set  # type: ignore[method-assign]
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [first_session])

    ducker = ad.SessionDucker(duck_level=0.2)
    ducker.start()
    assert first_session.volume == pytest.approx(0.2)

    blocked_session.set_master_volume = blocking_set  # type: ignore[method-assign]
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [blocked_session])

    sweep_thread = threading.Thread(target=ducker._run_sweep)
    sweep_thread.start()

    assert started.wait(timeout=1)
    ducker.stop()
    assert first_session.volume == pytest.approx(1.0)
    assert ducker.current_duck(333) is None
    assert ducker._tracked_by_id == {}

    allow_set.set()
    sweep_thread.join(timeout=1)
    assert blocked_session.volume == pytest.approx(1.0)
    assert ad._SHARED_SESSIONS == {}
    assert blocked_session.volume_calls[-1] == pytest.approx(1.0)


def test_repeated_start_stop_is_idempotent(monkeypatch):
    session = FakeSession("idempotent", 333, 0.9)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [session])

    ducker = ad.SessionDucker(duck_level=0.2)
    ducker.start()
    ducker.start()
    assert session.volume == pytest.approx(0.9 * 0.2)

    ducker.stop()
    ducker.stop()
    assert session.volume == pytest.approx(0.9)


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


def test_start_interleaved_with_stop_waits_for_inflight_and_no_stale_write(monkeypatch):
    session = FakeSession("interleaved", 333, 1.0)
    blocked = FakeSession("blocked", 333, 1.0)

    start_started = threading.Event()
    allow_set = threading.Event()

    original_set = blocked.set_master_volume

    def blocking_set(level: float) -> None:
        start_started.set()
        allow_set.wait(timeout=1.0)
        original_set(level)

    blocked.set_master_volume = blocking_set  # type: ignore[method-assign]

    monkeypatch.setattr(ad.os, "getpid", lambda: 111)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [session, blocked])

    ducker = ad.SessionDucker(duck_level=0.2)

    start_thread = threading.Thread(target=ducker.start)
    start_thread.start()
    assert start_started.wait(timeout=1.0)

    stop_done = threading.Event()
    stop_thread = threading.Thread(target=lambda: (ducker.stop(), stop_done.set()))
    stop_thread.start()

    assert stop_done.wait(timeout=0.05) is False

    allow_set.set()

    stop_thread.join(timeout=1.0)
    start_thread.join(timeout=1.0)
    assert not start_thread.is_alive()
    assert not stop_thread.is_alive()
    assert stop_done.is_set()

    assert session.volume == pytest.approx(1.0)
    assert blocked.volume == pytest.approx(1.0)
    assert ducker.current_duck(333) is None
    assert ducker._tracked_by_id == {}
    assert ad._SHARED_SESSIONS == {}


def test_original_volume_07_is_preserved_for_composed_ducking(monkeypatch):
    session = FakeSession("compose", 333, 0.7)
    monkeypatch.setattr(ad.os, "getpid", lambda: 111)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [session])

    capture = ad.SessionDucker(duck_level=0.15)
    capture.start()
    assert session.volume == pytest.approx(0.105)

    idle = ad.SessionDucker(duck_level=0.8)
    idle.start()
    assert session.volume == pytest.approx(0.084)

    idle.stop()
    assert session.volume == pytest.approx(0.105)

    capture.stop()
    assert session.volume == pytest.approx(0.7)


def _collect_vtbl_calls() -> dict[tuple[str, int], ast.Call]:
    source = inspect.getsource(ad)
    tree = ast.parse(source)
    calls: dict[tuple[str, int], ast.Call] = {}

    class CallCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.calls: dict[tuple[str, int], ast.Call] = {}

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "_vtbl"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, int)
            ):
                self.calls[".".join(self.scope), int(node.args[1].value)] = node
            self.generic_visit(node)

    collector = CallCollector()
    collector.visit(tree)
    calls = collector.calls

    return calls

def _index_and_arity(vtbl: type, method: str) -> tuple[int, int]:
    for index, (name, func) in enumerate(vtbl._fields_):
        if name == method:
            return index, len(func._argtypes_) - 1
    raise AssertionError(f"method {method} not in {vtbl.__name__}")


def test_vtbl_arity_matches_documented_interface_signatures() -> None:
    call_sites = _collect_vtbl_calls()
    required_calls = (
        ("_CtypesSessionHandle.get_master_volume", ad.ISimpleAudioVolumeVtbl, "GetMasterVolume"),
        ("_CtypesSessionHandle.set_master_volume", ad.ISimpleAudioVolumeVtbl, "SetMasterVolume"),
        ("_query_interface", ad.IUnknownVtbl, "QueryInterface"),
        ("_release", ad.IUnknownVtbl, "Release"),
        ("_get_session_enumerator", ad.IMMDeviceEnumeratorVtbl, "GetDefaultAudioEndpoint"),
        ("_get_session_enumerator", ad.IMMDeviceVtbl, "Activate"),
        ("_get_session_enumerator", ad.IAudioSessionManager2Vtbl, "GetSessionEnumerator"),
        ("_iter_audio_sessions", ad.IAudioSessionEnumeratorVtbl, "GetCount"),
        ("_iter_audio_sessions", ad.IAudioSessionEnumeratorVtbl, "GetSession"),
        ("_iter_audio_sessions", ad.IAudioSessionControl2Vtbl, "GetProcessId"),
    )

    for qualname, vtbl, method in required_calls:
        index, documented_argcount = _index_and_arity(vtbl, method)
        call = call_sites.get((qualname, index))
        assert call is not None, f"missing _vtbl call site: {qualname}::{method}"
        assert len(call.args) >= 3

        restype = eval(
            compile(ast.Expression(call.args[2]), "<audio-ducking-call>", "eval"),
            vars(ad),
        )
        argtypes = tuple(
            eval(
                compile(ast.Expression(argtype), "<audio-ducking-call>", "eval"),
                vars(ad),
            )
            for argtype in call.args[3:]
        )
        prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        assert len(prototype._argtypes_) == 1 + documented_argcount


def test_counters_reflect_zero_ducked_sessions(monkeypatch):
    monkeypatch.setattr(ad.os, "getpid", lambda: 111)
    monkeypatch.setattr(ad, "_iter_audio_sessions", lambda: [])

    ducker = ad.SessionDucker()
    ducker.start()

    assert ducker.sessions_seen == 0
    assert ducker.sessions_ducked == 0
    assert ducker.sessions_failed == 0
    assert ducker.last_error is None


def test_counters_increment_on_sweep(monkeypatch):
    start_session = FakeSession("tracked", 333, 1.0)

    def sequence() -> list[FakeSession]:
        if not sequence.calls:
            sequence.calls.append(True)
            return [start_session]
        return [start_session, FakeSession("new", 333, 0.6)]

    sequence.calls = []  # type: ignore[attr-defined]

    monkeypatch.setattr(ad, "_iter_audio_sessions", sequence)
    ducker = ad.SessionDucker()
    ducker.start()

    assert ducker.sessions_seen == 1
    assert ducker.sessions_ducked == 1

    ducker._run_sweep()

    assert ducker.sessions_seen == 2
    assert ducker.sessions_ducked == 2
    ducker.stop()
