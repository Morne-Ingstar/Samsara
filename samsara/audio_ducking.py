"""Session audio ducking engine for WASAPI session control.

ARCHITECTURE (tribunal arc_20260803_163412, tier-2): the ducking ALGORITHM
lives here -- lease composition, generations, owner tokens, conditional
restore -- but not one line of COM. Every Core Audio call happens in a
child process (samsara/ducking_host.py) reached through
DuckingHostTransport below, so a wedged COM call is killable instead of
poisoning the process that owns the user's hotkeys and UI.

The seam is deliberately narrow: `_iter_audio_sessions()` and the
get/set_master_volume methods of the handles it returns. Everything
SessionDucker does with those handles is unchanged from the in-process
implementation, which is why the engine's test suite exercises the same
assertions against a fake child.
"""

from __future__ import annotations

import atexit
import itertools
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from samsara.runtime import thread_registry
from samsara import flight_recorder

logger = logging.getLogger("Samsara")


# 2026-07-24 amendment: cut from 2.0s so a session that appears mid-duck
# (e.g. a newly launched Electron media player) gets caught with less
# audible delay. If 500ms still proves insufficient for slow-to-register
# Electron-based players, the real fix is switching from this poll sweep
# to event-driven session discovery (IAudioSessionNotification /
# IAudioSessionManager2::RegisterSessionNotification) -- tracked as a
# follow-up, not attempted here.
SWEEP_INTERVAL_SECONDS = 0.5

# Float tolerance for "is the current volume still what we last set it
# to" comparisons (relative-attenuation conditional restore, see stop()).
# WASAPI volumes are float32 under the hood; a few ULPs of round-trip
# drift through get/set is normal and must not look like a user change.
_VOLUME_EPSILON = 1e-3


def _floats_close(a: float, b: float, epsilon: float = _VOLUME_EPSILON) -> bool:
    return abs(a - b) <= epsilon


class _SessionHandle(Protocol):
    session_id: str
    pid: int

    def get_master_volume(self) -> float: ...

    def set_master_volume(self, level: float) -> None: ...

    def close(self) -> None: ...


@dataclass
class _TrackedSession:
    session_id: str
    pid: int
    handle: Any
    original_volume: float
    applied_volume: float


@dataclass
class _SharedSessionState:
    """Shared per-session ducking authority used by all SessionDucker instances."""

    session_id: str
    pid: int
    handle: _SessionHandle
    original_volume: float
    lease_factors: dict[int, float]
    applied_volume: float


_DUCKER_LOCK = threading.Lock()
_LEASE_ID_SOURCE = itertools.count(1)
_SHARED_SESSIONS: dict[str, _SharedSessionState] = {}


def _multiply_factors(factors: Iterable[float]) -> float:
    factor = 1.0
    for value in factors:
        factor *= value
    return factor


def _clamp_volume(level: float) -> float:
    return max(0.0, min(1.0, float(level)))


class SessionDucker:
    """Duck and restore non-excluded WASAPI sessions with a context manager.

    `current_duck(pid)` returns the currently applied duck level for a PID, so
    callers can compose levels with `min(self.current_duck(pid), own_level)` and
    restore in reverse order when nested behavior is needed.

    RELATIVE ATTENUATION (2026-07-24 amendment): `duck_level` is a
    multiplicative FACTOR applied to each session's CURRENT volume at
    start() time (`applied = current * duck_level`), not an absolute
    level to jam every session to. This is what makes LIFO-layering two
    independent SessionDucker instances behave as a true nest (e.g. an
    "idle" ducker at 0.8 followed by a "capture" ducker at 0.15 while the
    idle one is still active correctly compounds to 0.12 of the true
    original, and releasing the capture ducker lands back at the idle
    ducker's own level, not full volume) -- see dictation.py's hands-free
    ducking for the concrete two-stage caller.

    RESTORE IS CONDITIONAL: stop() only restores a session to its
    pre-duck volume if that session's CURRENT volume still equals the
    value THIS instance applied. If the user (or the app itself) changed
    that session's volume while ducked, their change wins -- we leave it
    alone rather than clobbering it with a stale snapshot. See stop() and
    _floats_close().
    """

    def __init__(
        self,
        duck_level: float = 0.15,
        exclude_pids: set[int] | None = None,
    ) -> None:
        self.duck_level = max(0.0, min(1.0, float(duck_level)))
        self._base_excludes = set(exclude_pids or [])
        self._base_excludes.add(os.getpid())

        self._lock = _DUCKER_LOCK
        self._lease_id = next(_LEASE_ID_SOURCE)
        self._active = False
        self._sweep_generation = 0
        self._tracked_by_id: dict[str, _TrackedSession] = {}
        self._tracking_order: list[str] = []
        self._pid_counts: dict[int, int] = {}
        self._sweep_timer: threading.Timer | None = None
        self._atexit_registered = False
        self._work_inflight = 0
        self._work_inflight_cv = threading.Condition(self._lock)
        self.sessions_seen = 0
        self.sessions_ducked = 0
        self.sessions_failed = 0
        self.last_error: str | None = None

    def __enter__(self) -> "SessionDucker":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any | None,
    ) -> None:
        self.stop()

    def current_duck(self, pid: int) -> float | None:
        """Return active duck level for `pid` or None when this instance is not ducking it."""
        with self._lock:
            return self.duck_level if self._pid_counts.get(int(pid), 0) else None

    def _begin_work(self) -> None:
        with self._lock:
            self._work_inflight += 1

    def _end_work(self) -> None:
        with self._lock:
            if self._work_inflight > 0:
                self._work_inflight -= 1
            if self._work_inflight <= 0:
                self._work_inflight_cv.notify_all()

    def _set_last_error(self, message: str | Exception | None) -> None:
        self.last_error = None if message is None else str(message)

    def _increment_counter(self, *, seen: bool = False, ducked: bool = False, failed: bool = False) -> None:
        if not (seen or ducked or failed):
            return
        with self._lock:
            if seen:
                self.sessions_seen += 1
            if ducked:
                self.sessions_ducked += 1
            if failed:
                self.sessions_failed += 1

    def _reset_start_counters(self) -> None:
        self.sessions_seen = 0
        self.sessions_ducked = 0
        self.sessions_failed = 0
        self.last_error = None

    def _is_generation_active(self, generation: int) -> bool:
        return self._active and self._sweep_generation == generation

    def _target_for_factors(self, state: _SharedSessionState) -> float:
        return _clamp_volume(state.original_volume * _multiply_factors(state.lease_factors.values()))

    def _clear_tracking_locked(self) -> None:
        self._tracking_order = []
        self._tracked_by_id = {}
        self._pid_counts = {}

    def _track_session_locked(self, state: _SharedSessionState) -> None:
        self._tracked_by_id[state.session_id] = _TrackedSession(
            session_id=state.session_id,
            pid=state.pid,
            handle=state.handle,
            original_volume=state.original_volume,
            applied_volume=state.applied_volume,
        )
        self._tracking_order.append(state.session_id)
        self._pid_counts[state.pid] = self._pid_counts.get(state.pid, 0) + 1

    def _untrack_session_locked(self, session_id: str) -> bool:
        entry = self._tracked_by_id.pop(session_id, None)
        if entry is None:
            return False
        try:
            self._tracking_order.remove(session_id)
        except ValueError:
            pass
        pid = entry.pid
        count = self._pid_counts.get(pid, 0) - 1
        if count <= 0:
            self._pid_counts.pop(pid, None)
        else:
            self._pid_counts[pid] = count
        return True

    def _release_session(
        self,
        session_id: str,
        *,
        rollback_volume: float | None = None,
        conditional_restore: bool = False,
    ) -> None:
        with self._lock:
            self._untrack_session_locked(session_id)
            state = _SHARED_SESSIONS.get(session_id)
            if state is None or self._lease_id not in state.lease_factors:
                return

            expected_volume = state.applied_volume
            state.lease_factors.pop(self._lease_id, None)
            handle = state.handle
            close_handle = False
            restore_volume = None

            if state.lease_factors:
                state.applied_volume = self._target_for_factors(state)
                restore_volume = state.applied_volume
            else:
                _SHARED_SESSIONS.pop(session_id, None)
                restore_volume = (
                    rollback_volume if rollback_volume is not None else state.original_volume
                )
                close_handle = True

        if conditional_restore:
            try:
                current = float(handle.get_master_volume())
            except Exception as exc:
                # CANNOT VERIFY -> DO NOT RESTORE (child-process amendment).
                # Previously a failed read fell through to restoring the
                # pre-duck snapshot anyway. With COM in a child that is
                # killed and respawned, a read failure most often means the
                # sid belonged to a dead child or the session vanished --
                # in neither case is the snapshot known to still be the
                # right answer, and writing it would clobber whatever the
                # user or the app has since set. Recorded so an incident
                # bundle shows exactly which sessions were skipped.
                logger.warning(
                    "SessionDucker.release(): failed to read current volume "
                    "for session %s: %s -- skipping restore (cannot verify)",
                    session_id,
                    exc,
                )
                flight_recorder.record(
                    "ducker_restore_skipped",
                    session_id=session_id,
                    reason=str(exc),
                )
                if close_handle:
                    self._close_session(handle)
                return
            if not _floats_close(current, expected_volume):
                if close_handle:
                    self._close_session(handle)
                return

        if restore_volume is None:
            return
        try:
            handle.set_master_volume(restore_volume)
        except Exception as exc:
            logger.warning(
                "SessionDucker.release(): failed to restore session %s: %s",
                session_id,
                exc,
            )
        if close_handle:
            self._close_session(handle)

    def _add_session_if_needed(
        self,
        session: _SessionHandle,
        generation: int,
    ) -> tuple[str, float] | None:
        session_id = session.session_id
        state: _SharedSessionState | None = None
        previous_volume: float | None = None
        target_volume: float | None = None
        should_add_lease = False
        needs_master_volume = False
        created_new_state = False
        close_now = False

        with self._lock:
            if not self._is_generation_active(generation) or session_id in self._tracked_by_id:
                close_now = True
            else:
                state = _SHARED_SESSIONS.get(session_id)
                if state is None:
                    needs_master_volume = True
                elif self._lease_id in state.lease_factors:
                    close_now = True
                else:
                    previous_volume = state.applied_volume
                    state.lease_factors[self._lease_id] = self.duck_level
                    state.applied_volume = self._target_for_factors(state)
                    target_volume = state.applied_volume
                    should_add_lease = True

        if close_now:
            self._close_session(session)
            return None

        if needs_master_volume:
            try:
                original_volume = float(session.get_master_volume())
            except Exception:
                self._increment_counter(failed=True)
                self._set_last_error("ISimpleAudioVolume.GetMasterVolume failed")
                self._close_session(session)
                return None

            with self._lock:
                if not self._is_generation_active(generation) or session_id in self._tracked_by_id:
                    close_now = True
                else:
                    state = _SHARED_SESSIONS.get(session_id)
                    if state is None:
                        state = _SharedSessionState(
                            session_id=session_id,
                            pid=session.pid,
                            handle=session,
                            original_volume=original_volume,
                            lease_factors={self._lease_id: self.duck_level},
                            applied_volume=_clamp_volume(original_volume * self.duck_level),
                        )
                        _SHARED_SESSIONS[session_id] = state
                        previous_volume = original_volume
                        target_volume = state.applied_volume
                        created_new_state = True
                        should_add_lease = True
                    elif self._lease_id in state.lease_factors:
                        close_now = True
                    else:
                        previous_volume = state.applied_volume
                        state.lease_factors[self._lease_id] = self.duck_level
                        state.applied_volume = self._target_for_factors(state)
                        target_volume = state.applied_volume
                        should_add_lease = True
            if close_now:
                self._close_session(session)
                return None

        if (
            state is None
            or target_volume is None
            or previous_volume is None
            or not should_add_lease
        ):
            self._close_session(session)
            return None

        try:
            state.handle.set_master_volume(target_volume)
        except Exception:
            self._increment_counter(failed=True)
            self._set_last_error("set_master_volume failed")
            self._release_session(session_id, rollback_volume=previous_volume)
            return None

        tracked_now = False
        with self._lock:
            state = _SHARED_SESSIONS.get(session_id)
            if (
                state is not None
                and self._is_generation_active(generation)
                and self._lease_id in state.lease_factors
                and session_id not in self._tracked_by_id
            ):
                self._track_session_locked(state)
                tracked_now = True

        if not tracked_now:
            self._increment_counter(failed=True)
            self._release_session(session_id, rollback_volume=previous_volume)
            # If this was a freshly-created state, the handle belongs to us;
            # ensure any duplicate COM reference used for enumeration is closed.
            if not created_new_state:
                self._close_session(session)
            return None

        self._increment_counter(ducked=True)

        return session_id, previous_volume

    def start(self) -> None:
        """Enumerate sessions and duck every non-excluded PID by `duck_level`
        (a multiplicative factor against each session's OWN current volume
        -- see the class docstring's "RELATIVE ATTENUATION").

        A lightweight re-sweep is scheduled every SWEEP_INTERVAL_SECONDS to
        catch sessions that appear while active. A no-op while already
        active (idempotent -- safe to call repeatedly, e.g. from a debounce
        path that reuses an already-ducking instance).
        """
        with self._lock:
            if self._active:
                return
            self._reset_start_counters()
            self._active = True
            self._sweep_generation += 1
            generation = self._sweep_generation

        self._begin_work()
        applied: list[tuple[str, float]] = []
        stale_start = False
        try:
            discovered = list(_iter_audio_sessions())
            for session in discovered:
                self._increment_counter(seen=True)
                if session.pid in self._base_excludes:
                    self._close_session(session)
                    continue

                added = self._add_session_if_needed(session, generation)
                if added is not None:
                    applied.append(added)

            with self._lock:
                if self._is_generation_active(generation):
                    self._schedule_sweep_locked()
                    self._register_atexit_locked()
                else:
                    stale_start = True
            if stale_start:
                return
        except Exception as exc:
            for session_id, previous_volume in reversed(applied):
                self._release_session(
                    session_id,
                    rollback_volume=previous_volume,
                )
            self._increment_counter(failed=True)
            self._set_last_error(exc)
            stale_start = True
            logger.warning("SessionDucker.start(): ducking disabled: %s", exc)
        finally:
            self._end_work()

        if stale_start:
            for session_id, previous_volume in reversed(applied):
                self._release_session(
                    session_id,
                    rollback_volume=previous_volume,
                )
            with self._lock:
                if self._sweep_generation == generation:
                    self._active = False
                    self._clear_tracking_locked()

    def stop(self) -> None:
        """Restore stored volumes in reverse acquisition order -- CONDITIONALLY:
        a session is only restored if its CURRENT volume still equals the
        value THIS instance applied (see class docstring). If the user or
        the app itself changed that session's volume while ducked, their
        change wins; we leave it alone instead of overwriting it with a
        stale pre-duck snapshot.
        """
        with self._lock:
            if not self._active:
                return

            self._active = False
            self._sweep_generation += 1
            tracked_order = list(self._tracking_order)

            timer = self._sweep_timer
            self._sweep_timer = None
            self._clear_tracking_locked()
            while self._work_inflight > 0:
                self._work_inflight_cv.wait()

        if timer is not None:
            timer.cancel()

        for session_id in reversed(tracked_order):
            self._release_session(session_id, conditional_restore=True)

    def _run_sweep(self) -> None:
        applied: list[tuple[str, float]] = []
        generation = -1
        sessions: list[_SessionHandle] = []
        tracked_ids: set[str] = set()
        self._begin_work()
        try:
            with self._lock:
                if not self._active:
                    return
                generation = self._sweep_generation
                tracked_ids = set(self._tracked_by_id)

            sessions = list(_iter_audio_sessions())
            for session in sessions:
                if session.pid in self._base_excludes:
                    self._close_session(session)
                    continue
                if session.session_id in tracked_ids:
                    self._close_session(session)
                    continue
                self._increment_counter(seen=True)
                added = self._add_session_if_needed(session, generation)
                if added is not None:
                    applied.append(added)

        except Exception as exc:
            logger.warning(
                "SessionDucker.sweep(): ducking sweep failed, unchanged: %s", exc
            )
            self._increment_counter(failed=True)
            self._set_last_error(exc)
            for session_id, previous_volume in reversed(applied):
                self._release_session(session_id, rollback_volume=previous_volume)
        finally:
            self._end_work()
            with self._lock:
                if not self._is_generation_active(generation):
                    return
                self._schedule_sweep_locked()

    def _schedule_sweep_locked(self) -> None:
        if self._sweep_timer is not None:
            self._sweep_timer.cancel()
        timer = thread_registry.timer(
            f"session-ducker.{self._lease_id}.sweep",
            SWEEP_INTERVAL_SECONDS,
            self._run_sweep,
            daemon=True,
        )
        self._sweep_timer = timer

    def _register_atexit_locked(self) -> None:
        if self._atexit_registered:
            return
        atexit.register(self.stop)
        self._atexit_registered = True

    @staticmethod
    def _close_session(session: _SessionHandle) -> None:
        try:
            session.close()
        except Exception:
            logger.debug("SessionDucker: failed to close a session handle")


# --- Child-process transport (tribunal arc_20260803_163412, tier-2) --------
#
# Every Core Audio COM call now happens in samsara/ducking_host.py, a
# separate process. The tribunal rejected in-process COM timeouts as fake
# isolation: a wedged COM call cannot be abandoned from inside the process
# -- the thread stays stuck in the RPC runtime still holding its locks, so
# "timing out" only stops the caller waiting while the damage is already
# done and permanent. A wedged CHILD, by contrast, is killed and respawned
# and the parent never notices.
#
# NOTE that this module no longer imports ctypes/ole32 at all: the parent
# process does not so much as load the COM machinery any more. Everything
# below is pipes and bookkeeping.

# Per-command deadline. Generous relative to a healthy call (sub-millisecond
# round trip locally) and short relative to a human noticing.
_CHILD_DEADLINE_S = 2.0

# Shutdown is best-effort by design: app exit must never wait on the child
# longer than this before killing it outright.
_CHILD_SHUTDOWN_DEADLINE_S = 1.0

# Mirrors samsara/ducking_host.py's HOST_ENV_SENTINEL. Deliberately a
# literal rather than an import: importing ducking_host here would pull
# ole32 and the whole COM surface back into the parent process, which is
# precisely what this design exists to prevent.
_HOST_ENV_SENTINEL = "SAMSARA_DUCKING_HOST"


class _EOF:
    """Sentinel the reader thread pushes when the child's stdout closes."""


def _kill_process_tree(proc) -> None:
    """Kill the child and anything it spawned. Never raises.

    Tree-kill rather than a plain kill(): a wedged COM call can be blocked
    inside an RPC to another process, and leaving descendants behind is how
    "restarted cleanly" quietly becomes "two hosts fighting over volumes".
    """
    pid = getattr(proc, "pid", None)
    if pid is not None and sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=1)
    except Exception:
        pass


class DuckingHostTransport:
    """Owns the ducking child process and its request/reply pipe.

    One command is in flight at a time (the child is sequential), so replies
    match requests by construction; the `id` echo is checked anyway so a
    late reply from a killed generation can never be mistaken for the
    current one.

    FAILURE MODEL -- every path returns a dict, none raise:
      * deadline exceeded, or the pipe is dead  -> tree-kill the child,
        record `ducker_child_restart` with the pending op, return
        {"ok": False, ...}. The next request lazily respawns.
      * lock held past the deadline (another thread is already inside a
        hanging op) -> return failure immediately rather than stacking a
        second full deadline on top of the first. This is what keeps a
        caller's worst case at ~one deadline instead of N.

    GENERATION: bumped on every spawn. Session ids issued by a dead child
    are meaningless to its replacement, so handles carry the generation
    they were minted in and refuse to act once it moves (see
    _ProxySessionHandle).
    """

    def __init__(self, spawn_fn=None) -> None:
        self._lock = threading.Lock()
        self._proc = None
        self._replies: "queue.Queue" = queue.Queue()
        self._ids = itertools.count(1)
        self._generation = 0
        self._spawn_fn = spawn_fn  # tests inject a fake child here

    @property
    def generation(self) -> int:
        return self._generation

    def _default_spawn(self):
        env = dict(os.environ)
        env[_HOST_ENV_SENTINEL] = "1"
        if getattr(sys, "frozen", False):
            # A frozen build has no separate interpreter to hand `-m` to, so
            # it re-executes its own exe; the entry point checks the
            # sentinel and diverts into ducking_host.main().
            argv = [sys.executable]
        else:
            argv = [sys.executable, "-u", "-m", "samsara.ducking_host"]
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def _spawn_locked(self) -> None:
        spawn = self._spawn_fn or self._default_spawn
        proc = spawn()
        self._proc = proc
        self._generation += 1
        self._replies = queue.Queue()
        replies = self._replies

        def _reader_loop() -> None:
            try:
                for raw in proc.stdout:
                    replies.put(raw)
            except Exception:
                pass
            finally:
                replies.put(_EOF)

        thread_registry.spawn(
            f"ducking-host.reader.{self._generation}", _reader_loop, daemon=True,
        )

    def _restart_locked(self, pending_op: str, reason: str) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            _kill_process_tree(proc)
        flight_recorder.record(
            "ducker_child_restart",
            pending_op=pending_op,
            reason=reason,
            generation=self._generation,
        )

    def request(self, op: str, deadline: float = _CHILD_DEADLINE_S, **fields) -> dict:
        """Send one command and await its reply. Never raises."""
        if not self._lock.acquire(timeout=deadline):
            # Someone else is inside a hanging op and owns the restart.
            flight_recorder.record("ducker_child_busy", pending_op=op)
            return {"ok": False, "err": "ducking host busy"}
        try:
            return self._request_locked(op, deadline, fields)
        finally:
            self._lock.release()

    def _request_locked(self, op: str, deadline: float, fields: dict) -> dict:
        if self._proc is None:
            try:
                self._spawn_locked()
            except Exception as exc:
                flight_recorder.record(
                    "ducker_child_restart", pending_op=op, reason=f"spawn failed: {exc}",
                )
                return {"ok": False, "err": f"ducking host spawn failed: {exc}"}

        request_id = next(self._ids)
        payload = {"id": request_id, "op": op, **fields}
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except Exception as exc:
            self._restart_locked(op, reason=f"write failed: {exc}")
            return {"ok": False, "err": f"ducking host write failed: {exc}"}

        expires_at = time.monotonic() + deadline
        while True:
            remaining = expires_at - time.monotonic()
            if remaining <= 0:
                self._restart_locked(op, reason="deadline")
                return {"ok": False, "err": f"ducking host timeout after {deadline}s"}
            try:
                raw = self._replies.get(timeout=remaining)
            except queue.Empty:
                continue  # loop re-checks the deadline
            if raw is _EOF:
                self._restart_locked(op, reason="pipe closed")
                return {"ok": False, "err": "ducking host died"}
            try:
                reply = json.loads(raw)
            except Exception:
                continue  # garbage line; keep waiting within the deadline
            if not isinstance(reply, dict) or reply.get("id") != request_id:
                continue  # stale reply from an earlier op
            return reply

    def shutdown(self) -> None:
        """Ask the child to exit, then kill it. Never waits past
        _CHILD_SHUTDOWN_DEADLINE_S -- app exit must not hang on audio."""
        if not self._lock.acquire(timeout=_CHILD_SHUTDOWN_DEADLINE_S):
            proc, self._proc = self._proc, None
            if proc is not None:
                _kill_process_tree(proc)
            return
        try:
            if self._proc is None:
                return
            try:
                self._request_locked(
                    "shutdown", _CHILD_SHUTDOWN_DEADLINE_S, {},
                )
            except Exception:
                pass
            proc, self._proc = self._proc, None
            if proc is not None:
                _kill_process_tree(proc)
        finally:
            self._lock.release()


_transport: "DuckingHostTransport | None" = None
_transport_lock = threading.Lock()


def _get_transport() -> DuckingHostTransport:
    """Lazily create the transport. NOTE the child is not spawned here --
    only on the first actual command -- which is what makes
    `ducking.enabled=false` (nothing ever calls a duck op) mean the child
    process is never created at all."""
    global _transport
    with _transport_lock:
        if _transport is None:
            _transport = DuckingHostTransport()
            atexit.register(shutdown_host)
        return _transport


def shutdown_host() -> None:
    """Stop the ducking child if one is running. Safe to call repeatedly."""
    global _transport
    with _transport_lock:
        transport, _transport = _transport, None
    if transport is not None:
        transport.shutdown()


class _ProxySessionHandle:
    """A `_SessionHandle` whose volume calls cross the process boundary.

    Deliberately mirrors the old _CtypesSessionHandle surface exactly, so
    SessionDucker's algorithm above is untouched by the move to a child
    process: it still holds "session handles" and calls
    get_master_volume/set_master_volume/close on them.
    """

    def __init__(self, sid: str, pid: int, generation: int, process_name=None) -> None:
        self.session_id = sid
        self.pid = pid
        self.process_name = process_name
        self._generation = generation

    def _transport_checked(self) -> DuckingHostTransport:
        transport = _get_transport()
        if transport.generation != self._generation:
            # The child that issued this sid is gone; its ids mean nothing
            # to the replacement. Fail rather than address a random session.
            raise OSError("ducking host restarted; session id is stale")
        return transport

    def get_master_volume(self) -> float:
        reply = self._transport_checked().request("get_volume", sid=self.session_id)
        if not reply.get("ok"):
            raise OSError(f"get_volume failed: {reply.get('err')}")
        return float(reply["level"])

    def set_master_volume(self, level: float) -> None:
        reply = self._transport_checked().request(
            "set_volume", sid=self.session_id, level=_clamp_volume(level),
        )
        if not reply.get("ok"):
            raise OSError(f"set_volume failed: {reply.get('err')}")

    def close(self) -> None:
        """No-op: the child owns COM lifetime.

        There is deliberately no `release` command in the protocol. The
        child releases a session's interface when it disappears from an
        enumeration or at shutdown, on the one thread that created it --
        which is what satisfies COM's same-apartment-release rule. A
        per-handle release command would add chatter and a second way for
        the parent to be wrong about child state.
        """


def _iter_audio_sessions() -> Iterable[_SessionHandle]:
    """Enumerate live audio sessions via the child process.

    Returns a list (not a generator) because the underlying COM enumeration
    now completes entirely inside one child command; there is no enumerator
    to hold open across iteration any more.
    """
    transport = _get_transport()
    reply = transport.request("list_sessions")
    if not reply.get("ok"):
        raise OSError(f"list_sessions failed: {reply.get('err')}")
    generation = transport.generation
    return [
        _ProxySessionHandle(
            entry["sid"],
            int(entry.get("pid", 0)),
            generation,
            entry.get("process_name"),
        )
        for entry in reply.get("sessions", [])
    ]


# --- Module-level compatibility shims (hotfix 2026-07-24) -------------------
# dictation.py's TTS/recording duck path calls audio_ducking.duck(level) /
# audio_ducking.restore() (module functions), but this engine only exposed
# SessionDucker. Every hotkey press died in on_key_press with
# AttributeError, killing dictation entirely. These shims back the old
# call surface with a module singleton. Idempotent; COM-failure-safe via
# SessionDucker's own guards.
_shim_ducker: "SessionDucker | None" = None


def duck(level: float = 0.2) -> None:
    global _shim_ducker
    if _shim_ducker is not None:
        return  # already active; keep first duck, restore() releases it
    _t0 = time.monotonic()
    d = SessionDucker(duck_level=level)
    d.start()
    _shim_ducker = d
    flight_recorder.record(
        'ducker.op', op='start', duck_level=level,
        sessions_seen=d.sessions_seen, sessions_ducked=d.sessions_ducked,
        sessions_failed=d.sessions_failed, last_error=d.last_error,
        elapsed_ms=int((time.monotonic() - _t0) * 1000),
    )


def restore() -> None:
    global _shim_ducker
    d, _shim_ducker = _shim_ducker, None
    if d is not None:
        _t0 = time.monotonic()
        d.stop()
        flight_recorder.record(
            'ducker.op', op='stop',
            sessions_seen=d.sessions_seen, sessions_ducked=d.sessions_ducked,
            sessions_failed=d.sessions_failed, last_error=d.last_error,
            elapsed_ms=int((time.monotonic() - _t0) * 1000),
        )
