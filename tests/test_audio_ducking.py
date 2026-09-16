"""Tests for the session ducking engine API.

2026-07-24 amendment: RELATIVE (multiplicative) attenuation, not absolute
level-setting -- duck_level is a factor applied to each session's OWN
current volume (applied = current * duck_level), and stop() only restores
a session if its live volume still equals what THIS instance applied (a
user/app change mid-duck wins over a stale snapshot). See
samsara/audio_ducking.py's SessionDucker docstring.
"""

from __future__ import annotations

import json
import logging
import struct
import threading
import ctypes
import inspect
import ast

import pytest

from samsara import audio_ducking as ad
from samsara import ducking_host as dh


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


# --- Fake ducking child ----------------------------------------------------
# Every COM call now crosses a process boundary (samsara/ducking_host.py).
# These tests exercise the PARENT algorithm, so they speak the real wire
# protocol to an in-process stand-in instead of a real child. The assertions
# below are unchanged from the in-process implementation -- the whole point
# is that moving COM out of the process did not change ducking behavior.

class FakeHostTransport:
    """In-process DuckingHostTransport stand-in speaking the ducking_host
    protocol (ping/list_sessions/get_volume/set_volume/set_many/shutdown),
    backed by FakeSession objects.

    Serializes requests under a lock with the same timeout-acquire
    semantics as the real transport, so tests see production's concurrency
    behavior (one child, one command at a time) rather than a more
    permissive fiction.
    """

    def __init__(self) -> None:
        self.sessions_source = []
        self.fail_list_with = None
        self.generation = 1
        self.ops: list[str] = []
        self._by_sid: dict[str, FakeSession] = {}
        self._lock = threading.Lock()

    def _current(self) -> list:
        source = self.sessions_source
        return list(source() if callable(source) else source)

    def request(self, op: str, deadline: float = 2.0, **fields) -> dict:
        if not self._lock.acquire(timeout=deadline):
            return {"ok": False, "err": "ducking host busy", "elapsed_ms": 0}
        try:
            self.ops.append(op)
            return self._handle(op, fields)
        finally:
            self._lock.release()

    def _handle(self, op: str, fields: dict) -> dict:
        if op == "ping":
            return {"ok": True, "elapsed_ms": 0}

        if op == "list_sessions":
            if self.fail_list_with is not None:
                return {"ok": False, "err": self.fail_list_with, "elapsed_ms": 0}
            listed = []
            for session in self._current():
                self._by_sid[session.session_id] = session
                listed.append(
                    {
                        "sid": session.session_id,
                        "pid": session.pid,
                        "process_name": getattr(session, "process_name", None),
                        "instance": getattr(session, "instance_id", None),
                    }
                )
            return {"ok": True, "sessions": listed, "elapsed_ms": 0}

        if op in ("get_volume", "set_volume"):
            session = self._by_sid.get(fields.get("sid"))
            if session is None:
                return {"ok": False, "err": "unknown sid", "elapsed_ms": 0}
            try:
                previous = session.get_master_volume()
                if op == "get_volume":
                    return {"ok": True, "level": previous, "elapsed_ms": 0}
                session.set_master_volume(fields["level"])
                return {"ok": True, "prev_level": previous, "elapsed_ms": 0}
            except Exception as exc:
                return {"ok": False, "err": str(exc), "elapsed_ms": 0}

        if op == "set_many":
            results = []
            for item in fields.get("items") or []:
                results.append(self._handle("set_volume", item) | {"sid": item["sid"]})
            return {"ok": True, "results": results, "elapsed_ms": 0}

        if op == "shutdown":
            return {"ok": True, "elapsed_ms": 0}

        return {"ok": False, "err": f"unknown op {op!r}", "elapsed_ms": 0}


_FAKE_HOST: FakeHostTransport | None = None


def _fake() -> FakeHostTransport:
    assert _FAKE_HOST is not None, "fake ducking host not installed"
    return _FAKE_HOST


def use_sessions(sessions) -> None:
    """Point the fake child at `sessions` (a list, or a callable returning
    one). Replaces the old `monkeypatch.setattr(ad, "_iter_audio_sessions",
    ...)`: enumeration is now a child command, not a local function."""
    _fake().sessions_source = sessions


@pytest.fixture(autouse=True)
def no_threads(monkeypatch):
    global _FAKE_HOST
    _simulate_new_process()

    # One fake child per test, shared by every enumeration in that test --
    # session ids must stay stable across sweeps exactly as a real child's
    # do, so re-pointing use_sessions() must NOT mint a new transport.
    _FAKE_HOST = FakeHostTransport()
    monkeypatch.setattr(ad, "_get_transport", _fake)

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
    _FAKE_HOST = None
    _simulate_new_process()


def _simulate_new_process() -> None:
    """Drop every piece of engine state that lives in memory. What is left
    is exactly what survives a hard kill: the files on disk and the other
    apps' volumes."""
    ad._SHARED_SESSIONS.clear()
    ad._journal_active.clear()
    ad._journal_pending.clear()
    ad._known_levels.clear()
    ad._duck_factors.clear()
    ad._shallow_rejected.clear()
    ad._pending_notices.clear()
    ad._notice_sink = None
    ad._journal_path = None


def test_capture_first_idle_second_discovery_restores_full_volume(monkeypatch):
    session = FakeSession("shared", 333, 1.0)
    use_sessions([session])

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
    use_sessions([session])

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
    use_sessions([first_session])

    ducker = ad.SessionDucker(duck_level=0.2)
    ducker.start()
    assert first_session.volume == pytest.approx(0.2)

    blocked_session.set_master_volume = blocking_set  # type: ignore[method-assign]
    use_sessions([blocked_session])

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
    use_sessions([session])

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
    use_sessions(sessions)

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
    use_sessions(sessions)

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
    use_sessions([first, second])
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
    use_sessions([session])

    with pytest.raises(RuntimeError, match="boom"):
        with ad.SessionDucker() as ducker:
            ducker.start()
            raise RuntimeError("boom")

    assert session.volume == pytest.approx(0.9)


def test_sweep_catches_new_session(monkeypatch):
    sessions = [FakeSession("first", 333, 0.9)]
    monkeypatch.setattr(ad.os, "getpid", lambda: 111)
    use_sessions(sessions)

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
    # The child reports enumeration failure as {"ok": false, "err": ...};
    # _iter_audio_sessions turns that into the OSError the engine already
    # handled, so this stays the same test it always was.
    use_sessions([])
    _fake().fail_list_with = "simulated enumeration failure"
    ducker = ad.SessionDucker()
    ducker.start()

    assert ducker.current_duck(123) is None
    ducker.stop()


def test_current_duck_min_composition(monkeypatch):
    session = FakeSession("compose", 333, 1.0)
    monkeypatch.setattr(ad.os, "getpid", lambda: 111)
    use_sessions([session])

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
        use_sessions([session])

        ducker = ad.SessionDucker(duck_level=0.2)
        ducker.start()
        ducker.stop()

        assert session.volume == pytest.approx(0.9)

    def test_skips_restore_when_user_changed_volume_mid_duck(self, monkeypatch):
        session = FakeSession("user-changed", 333, 0.9)
        monkeypatch.setattr(ad.os, "getpid", lambda: 111)
        use_sessions([session])

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
        use_sessions([session])

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
        use_sessions([session])

        ducker = ad.SessionDucker(duck_level=0.2)
        ducker.start()
        calls_after_first_start = list(session.volume_calls)

        ducker.start()  # must be a no-op

        assert session.volume_calls == calls_after_first_start

    def test_start_while_active_preserves_original_snapshot_for_stop(self, monkeypatch):
        session = FakeSession("idempotent-restore", 333, 0.9)
        monkeypatch.setattr(ad.os, "getpid", lambda: 111)
        use_sessions([session])

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
    use_sessions([session, blocked])

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
    use_sessions([session])

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
    source = inspect.getsource(dh)
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
        ("_CtypesSessionHandle.get_master_volume", dh.ISimpleAudioVolumeVtbl, "GetMasterVolume"),
        ("_CtypesSessionHandle.set_master_volume", dh.ISimpleAudioVolumeVtbl, "SetMasterVolume"),
        ("_query_interface", dh.IUnknownVtbl, "QueryInterface"),
        ("_release", dh.IUnknownVtbl, "Release"),
        ("_get_session_enumerator", dh.IMMDeviceEnumeratorVtbl, "GetDefaultAudioEndpoint"),
        ("_get_session_enumerator", dh.IMMDeviceVtbl, "Activate"),
        ("_get_session_enumerator", dh.IAudioSessionManager2Vtbl, "GetSessionEnumerator"),
        ("_iter_audio_sessions", dh.IAudioSessionEnumeratorVtbl, "GetCount"),
        ("_iter_audio_sessions", dh.IAudioSessionEnumeratorVtbl, "GetSession"),
        ("_iter_audio_sessions", dh.IAudioSessionControl2Vtbl, "GetProcessId"),
    )

    for qualname, vtbl, method in required_calls:
        index, documented_argcount = _index_and_arity(vtbl, method)
        call = call_sites.get((qualname, index))
        assert call is not None, f"missing _vtbl call site: {qualname}::{method}"
        assert len(call.args) >= 3

        restype = eval(
            compile(ast.Expression(call.args[2]), "<audio-ducking-call>", "eval"),
            vars(dh),
        )
        argtypes = tuple(
            eval(
                compile(ast.Expression(argtype), "<audio-ducking-call>", "eval"),
                vars(dh),
            )
            for argtype in call.args[3:]
        )
        prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        assert len(prototype._argtypes_) == 1 + documented_argcount


def test_counters_reflect_zero_ducked_sessions(monkeypatch):
    monkeypatch.setattr(ad.os, "getpid", lambda: 111)
    use_sessions([])

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

    use_sessions(sequence)
    ducker = ad.SessionDucker()
    ducker.start()

    assert ducker.sessions_seen == 1
    assert ducker.sessions_ducked == 1

    ducker._run_sweep()

    assert ducker.sessions_seen == 2
    assert ducker.sessions_ducked == 2
    ducker.stop()


# --- 52: a crash while ducked -------------------------------------------------

class NamedSession(FakeSession):
    def __init__(self, session_id, pid, volume, process_name, instance_id=None):
        super().__init__(session_id, pid, volume)
        self.process_name = process_name
        self.instance_id = instance_id


@pytest.fixture
def journal(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(ad.thread_registry, "spawn", lambda *a, **k: spawned.append(a))
    return tmp_path / ad.DUCK_JOURNAL_NAME


def _hard_kill_and_restart(journal_path, sessions, *, unclean=True):
    """The process dies without stop() or atexit; a new one starts. The fake
    child is replaced too, so session ids from the dead run mean nothing."""
    global _FAKE_HOST
    _simulate_new_process()
    _FAKE_HOST = FakeHostTransport()
    use_sessions(sessions)
    return ad.configure_duck_journal(journal_path, previous_unclean=unclean)


def test_hard_kill_while_ducked_restores_pre_duck_levels_on_next_start(journal):
    spotify = NamedSession("s1", 100, 0.3, "Spotify.exe", "inst-spotify")
    chrome = NamedSession("s2", 200, 1.0, "chrome.exe", "inst-chrome")
    use_sessions([spotify, chrome])
    ad.configure_duck_journal(journal)

    idle = ad.SessionDucker(duck_level=0.8)
    capture = ad.SessionDucker(duck_level=0.15)
    idle.start()
    capture.start()
    assert spotify.volume == pytest.approx(0.3 * 0.12)
    assert chrome.volume == pytest.approx(0.12)

    # Killed here. The apps stay ducked; the new run gets fresh sid strings.
    spotify.session_id, chrome.session_id = "new-1", "new-2"
    assert _hard_kill_and_restart(journal, [spotify, chrome]) == 2
    assert ad.recover_leftover_ducks() == 2

    assert spotify.volume == pytest.approx(0.3)
    assert chrome.volume == pytest.approx(1.0)
    assert json.loads(journal.read_text(encoding="utf-8"))["ducked"] == []

    # And the next capture ducks from the real level, and restores to it.
    ducker = ad.SessionDucker(duck_level=0.15)
    ducker.start()
    assert spotify.volume == pytest.approx(0.3 * 0.15)
    ducker.stop()
    assert spotify.volume == pytest.approx(0.3)


def test_recovery_runs_before_the_next_duck_reads_originals(journal):
    app = NamedSession("s1", 100, 0.5, "vlc.exe", "inst-vlc")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    ad.SessionDucker(duck_level=0.15).start()

    app.session_id = "new-1"
    _hard_kill_and_restart(journal, [app])
    ducker = ad.SessionDucker(duck_level=0.15)
    ducker.start()  # recovery has not run on its thread yet
    assert app.volume == pytest.approx(0.5 * 0.15)
    ducker.stop()
    assert app.volume == pytest.approx(0.5)


def test_recovery_leaves_a_session_the_user_changed_after_the_crash(journal):
    app = NamedSession("s1", 100, 0.6, "vlc.exe", "inst-vlc")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    ad.SessionDucker(duck_level=0.15).start()

    app.volume = 0.25  # the user turned it back up themselves
    _hard_kill_and_restart(journal, [app])
    assert ad.recover_leftover_ducks() == 0
    assert app.volume == pytest.approx(0.25)


def test_clean_run_leaves_nothing_to_recover(journal):
    app = NamedSession("s1", 100, 0.6, "vlc.exe", "inst-vlc")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    ducker = ad.SessionDucker(duck_level=0.15)
    ducker.start()
    ducker.stop()
    assert _hard_kill_and_restart(journal, [app], unclean=False) == 0


def test_original_at_the_duck_target_is_rejected_for_the_remembered_level(journal):
    app = NamedSession("s1", 100, 0.3, "Spotify.exe", "inst-a")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    ducker = ad.SessionDucker(duck_level=0.15)
    ducker.start()
    ducker.stop()  # a clean run remembers 0.3
    assert json.loads(journal.read_text(encoding="utf-8"))["known_levels"] == {"Spotify.exe": 0.3}

    # A pre-journal crash left it ducked and the journal lost the entry:
    # the next run sees only 0.045, the duck target of 0.3.
    app.volume = 0.3 * 0.15
    app.session_id, app.instance_id = "new-1", "inst-b"
    _hard_kill_and_restart(journal, [app])
    ducker = ad.SessionDucker(duck_level=0.15)
    ducker.start()
    assert ad._SHARED_SESSIONS["new-1"].original_volume == pytest.approx(0.3)
    ducker.stop()
    assert app.volume == pytest.approx(0.3)
    assert json.loads(journal.read_text(encoding="utf-8"))["known_levels"] == {"Spotify.exe": 0.3}


def test_implausible_original_with_nothing_remembered_is_not_remembered(journal):
    app = NamedSession("s1", 100, 0.01, "Spotify.exe", "inst-a")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    ducker = ad.SessionDucker(duck_level=0.15)
    ducker.start()
    ducker.stop()
    assert app.volume == pytest.approx(0.01)  # never guessed up to 100%
    assert json.loads(journal.read_text(encoding="utf-8"))["known_levels"] == {}


def test_deliberate_lower_level_above_the_duck_target_is_accepted(journal):
    app = NamedSession("s1", 100, 1.0, "chrome.exe", "inst-a")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    first = ad.SessionDucker(duck_level=0.15)
    first.start()
    first.stop()

    app.volume = 0.3  # the user turns it down on purpose
    second = ad.SessionDucker(duck_level=0.15)
    second.start()
    second.stop()
    assert app.volume == pytest.approx(0.3)
    assert json.loads(journal.read_text(encoding="utf-8"))["known_levels"] == {"chrome.exe": 0.3}


def test_engage_while_engaged_does_not_mutate_stored_originals(journal):
    app = NamedSession("s1", 100, 0.3, "Spotify.exe", "inst-a")
    use_sessions([app])
    ad.configure_duck_journal(journal)

    def stored():
        return [e["original"] for e in json.loads(journal.read_text(encoding="utf-8"))["ducked"]]

    capture = ad.SessionDucker(duck_level=0.15)
    capture.start()
    assert stored() == [pytest.approx(0.3)]

    capture.start()          # the documented no-op
    capture._run_sweep()     # a sweep re-sees the ducked session
    idle = ad.SessionDucker(duck_level=0.8)
    idle.start()             # a second lease on the same session
    idle.stop()
    assert stored() == [pytest.approx(0.3)]
    assert ad._SHARED_SESSIONS["s1"].original_volume == pytest.approx(0.3)

    capture.stop()
    assert app.volume == pytest.approx(0.3)
    assert stored() == []


def test_user_set_30_percent_comes_back_as_30_not_100(journal):
    app = NamedSession("s1", 100, 0.3, "Spotify.exe", "inst-a")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    ad.SessionDucker(duck_level=0.8).start()
    ad.SessionDucker(duck_level=0.15).start()

    app.session_id = "new-1"
    _hard_kill_and_restart(journal, [app])
    ad.recover_leftover_ducks()
    assert app.volume == pytest.approx(0.3)
    assert app.volume_calls[-1] != pytest.approx(1.0)


# --- 73: float noise, poisoned journals, real duck factors, no-crash strands --

def _f32(level: float) -> float:
    return struct.unpack("f", struct.pack("f", float(level)))[0]


class Float32Session(NamedSession):
    """Stores what Core Audio stores: a 32-bit float. Every read is the
    float32 nearest the double that was written, never the double itself."""

    def __init__(self, session_id, pid, volume, process_name, instance_id=None):
        super().__init__(session_id, pid, _f32(volume), process_name, instance_id)

    def set_master_volume(self, level: float) -> None:
        super().set_master_volume(_f32(level))


def _vanish(sessions: list, session) -> None:
    """The app closes its stream: Windows drops the session and the host
    forgets its sid, so reads of it fail with "unknown sid"."""
    sessions.remove(session)
    _fake()._by_sid.pop(session.session_id, None)


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def duck_log():
    handler = _Records()
    ad.logger.addHandler(handler)
    yield handler.messages
    ad.logger.removeHandler(handler)


STREMIO = 0.1802240014076233
STEAM = 0.0508433


def test_float_equality_trap_float32_readback_noise_still_restores(journal):
    # Bug 1 (tribunal): a ducked level read back as float32 must not look like
    # a user change -- on a live release or in crash recovery.
    app = Float32Session("s1", 100, STREMIO, "stremio.exe", "inst-a")
    other = Float32Session("s2", 200, STEAM, "steam.exe", "inst-b")
    sessions = [app, other]
    use_sessions(sessions)
    ad.configure_duck_journal(journal)

    idle = ad.SessionDucker(duck_level=0.8)
    capture = ad.SessionDucker(duck_level=0.15)
    idle.start()
    capture.start()
    # The noise is real: what the app reports is not the double we applied.
    assert app.volume != ad._SHARED_SESSIONS["s1"].applied_volume
    assert other.volume != ad._SHARED_SESSIONS["s2"].applied_volume
    capture.stop()
    idle.stop()
    assert app.volume == pytest.approx(STREMIO, abs=1e-7)
    assert other.volume == pytest.approx(STEAM, abs=1e-7)
    assert json.loads(journal.read_text(encoding="utf-8"))["ducked"] == []

    # Same trap on the recovery path: the journal holds the double, the app
    # holds its float32.
    ad.SessionDucker(duck_level=0.8).start()
    app.session_id, other.session_id = "new-1", "new-2"
    _hard_kill_and_restart(journal, sessions)
    assert ad.recover_leftover_ducks() == 2
    assert app.volume == pytest.approx(STREMIO, abs=1e-7)
    assert other.volume == pytest.approx(STEAM, abs=1e-7)


def test_startup_scrubs_sub_floor_journal_and_known_levels_and_logs_it(journal, duck_log):
    spotify = NamedSession("s1", 100, 0.0015462, "Spotify.exe", "inst-spotify")
    steam = NamedSession("s2", 200, 0.008, "steam.exe", "inst-steam")
    journal.write_text(json.dumps({
        "version": 1,
        "ducked": [
            {"key": "instance:inst-spotify", "pid": 100, "process_name": "Spotify.exe",
             "original": 0.0019327, "applied": 0.0015462},
            {"key": "instance:inst-steam", "pid": 200, "process_name": "steam.exe",
             "original": 0.01, "applied": 0.008},
        ],
        "known_levels": {"Spotify.exe": 0.0019327, "steam.exe": 1.0, "VoiceAccess.exe": 1.0},
    }), encoding="utf-8")
    shown: list[str] = []

    assert _hard_kill_and_restart(journal, [spotify, steam]) == 1
    on_disk = json.loads(journal.read_text(encoding="utf-8"))
    assert on_disk["known_levels"] == {"steam.exe": 1.0, "VoiceAccess.exe": 1.0}
    assert [e["process_name"] for e in on_disk["ducked"]] == ["steam.exe"]
    scrub = [m for m in duck_log if "startup scrub" in m]
    assert len(scrub) == 1
    assert "Spotify.exe=0.0019" in scrub[0]          # remembered level dropped
    assert "steam.exe: 0.0100 -> 1.000" in scrub[0]  # retargeted to the trusted level
    assert "stopped tracking [Spotify.exe=0.0019]" in scrub[0]

    # The unrecoverable one is shown once, when the app registers a sink.
    ad.set_notice_sink(shown.append)
    ad.set_notice_sink(shown.append)
    assert len(shown) == 1 and "Spotify.exe" in shown[0]

    assert ad.recover_leftover_ducks() == 1
    assert steam.volume == pytest.approx(1.0)
    assert spotify.volume_calls == []  # left alone, not guessed up to 100%

    # The next start has nothing left to scrub or say.
    duck_log.clear()
    _hard_kill_and_restart(journal, [spotify, steam], unclean=False)
    ad.set_notice_sink(shown.append)
    assert len(shown) == 1
    assert not [m for m in duck_log if "startup scrub" in m]


@pytest.mark.parametrize(
    "path, factor",
    [
        ("idle duck (ducking.hands_free_idle_level)", 0.8),
        ("capture duck (ducking.hands_free_level)", 0.15),
        ("idle + capture", 0.8 * 0.15),
        ("hotkey duck, live config (ducking.level)", 0.1),
        ("hotkey duck, code default (ducking.level)", 0.2),
        ("idle + hotkey default", 0.8 * 0.2),
    ],
)
def test_vet_original_rejects_a_leftover_from_each_real_duck_path(journal, path, factor):
    ad.configure_duck_journal(journal)
    for level in (0.8, 0.15, 0.1, 0.2):  # every factor dictation.py builds duckers with
        ad.SessionDucker(duck_level=level)
    remembered = _f32(0.6)
    ad._known_levels["vlc.exe"] = remembered
    handle = NamedSession("s1", 100, 0.0, "vlc.exe")

    assert ad._vet_original(handle, _f32(remembered * factor)) == (remembered, False), path


def test_duck_factors_in_use_survive_a_restart(journal):
    use_sessions([NamedSession("s1", 100, 1.0, "vlc.exe", "inst-a")])
    ad.configure_duck_journal(journal)
    ducker = ad.SessionDucker(duck_level=0.8)
    ducker.start()
    ducker.stop()
    _hard_kill_and_restart(journal, [])
    assert ad._duck_products() == [pytest.approx(0.8)]


def test_user_set_30_percent_is_preserved_not_treated_as_leftover(journal):
    app = NamedSession("s1", 100, 1.0, "Spotify.exe", "inst-a")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    for level in (0.8, 0.15, 0.1, 0.2):
        ad.SessionDucker(duck_level=level)
    first = ad.SessionDucker(duck_level=0.8)
    first.start()
    first.stop()  # remembers 1.0

    app.volume = 0.3  # the user turns Spotify down
    idle = ad.SessionDucker(duck_level=0.8)
    capture = ad.SessionDucker(duck_level=0.15)
    idle.start()
    capture.start()
    assert ad._SHARED_SESSIONS["s1"].original_volume == pytest.approx(0.3)
    capture.stop()
    idle.stop()
    assert app.volume == pytest.approx(0.3)
    assert json.loads(journal.read_text(encoding="utf-8"))["known_levels"] == {"Spotify.exe": 0.3}


def test_user_set_level_equal_to_an_idle_duck_is_accepted_after_one_restore(journal):
    # 80% of a remembered 100% cannot be told from a leftover idle duck by
    # value. It is restored once; seeing it again means the user chose it.
    app = NamedSession("s1", 100, 1.0, "firefox.exe", "inst-a")
    use_sessions([app])
    ad.configure_duck_journal(journal)
    ducker = ad.SessionDucker(duck_level=0.8)
    ducker.start()
    ducker.stop()

    app.volume = _f32(0.8)
    ducker.start()
    ducker.stop()
    assert app.volume == pytest.approx(1.0)  # treated as a leftover once

    app.volume = _f32(0.8)  # the user sets it again
    ducker.start()
    ducker.stop()
    assert app.volume == pytest.approx(0.8)
    assert json.loads(journal.read_text(encoding="utf-8"))["known_levels"] == {"firefox.exe": pytest.approx(0.8)}


def test_session_vanishing_while_ducked_does_not_compound(journal):
    # The no-crash strand in samsara.log ("unknown sid -- skipping restore"):
    # the app closes its stream while the idle duck holds it, Windows keeps
    # the app at the ducked level, and its next session opens there. Before
    # 73 that session was adopted at 0.8 as its "original".
    sessions = [NamedSession("s1", 100, 1.0, "brave.exe", "inst-1")]
    use_sessions(sessions)
    ad.configure_duck_journal(journal)
    idle = ad.SessionDucker(duck_level=0.8)
    idle.start()
    assert sessions[0].volume == pytest.approx(0.8)

    for cycle in range(3):
        old = sessions[0]
        _vanish(sessions, old)
        sessions.append(NamedSession(f"s{cycle + 2}", 100, old.volume, "brave.exe", f"inst-{cycle + 2}"))
        idle._run_sweep()
        assert sessions[0].volume == pytest.approx(0.8), cycle
        assert ad._SHARED_SESSIONS[sessions[0].session_id].original_volume == pytest.approx(1.0), cycle
        idle.stop()  # the vanished session cannot be read: queued, not lost
        assert sessions[0].volume == pytest.approx(1.0), cycle
        idle.start()
        assert sessions[0].volume == pytest.approx(0.8), cycle

    idle.stop()
    assert sessions[0].volume == pytest.approx(1.0)
    on_disk = json.loads(journal.read_text(encoding="utf-8"))
    assert on_disk["known_levels"] == {"brave.exe": 1.0}
    assert on_disk["ducked"] == []


def test_session_vanished_at_release_is_restored_when_the_app_comes_back(journal):
    app = NamedSession("s1", 100, 0.5, "Spotify.exe", "inst-a")
    sessions = [app]
    use_sessions(sessions)
    ad.configure_duck_journal(journal)
    idle = ad.SessionDucker(duck_level=0.8)
    capture = ad.SessionDucker(duck_level=0.15)
    idle.start()
    capture.start()
    assert app.volume == pytest.approx(0.06)

    _vanish(sessions, app)
    capture.stop()  # cannot read it; idle still holds it
    idle.stop()     # cannot read it; queued at the level really left (0.06)
    queued = json.loads(journal.read_text(encoding="utf-8"))["ducked"]
    assert [(e["process_name"], e["applied"]) for e in queued] == [("Spotify.exe", pytest.approx(0.06))]

    # A duck while Spotify is closed keeps the entry waiting.
    other = ad.SessionDucker(duck_level=0.15)
    other.start()
    other.stop()
    assert len(ad._journal_pending) == 1

    # Spotify relaunches at the level Windows kept; the next duck heals it
    # before reading its original, and releases to the real 0.5.
    back = NamedSession("s9", 300, 0.06, "Spotify.exe", "inst-z")
    sessions.append(back)
    other.start()
    assert back.volume == pytest.approx(0.5 * 0.15)
    other.stop()
    assert back.volume == pytest.approx(0.5)
    assert json.loads(journal.read_text(encoding="utf-8"))["ducked"] == []


def test_system_sounds_session_is_not_ducked(journal):
    system_sounds = NamedSession("sys", 0, 0.8, "System Idle Process", "inst-sys")
    app = NamedSession("s1", 100, 1.0, "vlc.exe", "inst-a")
    use_sessions([system_sounds, app])
    ducker = ad.SessionDucker(duck_level=0.15)
    ducker.start()
    assert system_sounds.volume_calls == []
    assert app.volume == pytest.approx(0.15)
    ducker.stop()
