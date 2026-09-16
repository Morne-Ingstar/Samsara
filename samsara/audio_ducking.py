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
    # 73: the level last confirmed written to the app. `applied_volume` is
    # bookkeeping and moves before the write; when a session vanishes this is
    # what the app is really left at, and what a later session of it is
    # matched against.
    written_volume: float | None = None


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


# --- Duck journal (52) -------------------------------------------------------
#
# A duck only exists in this process's memory, and atexit does not run when
# Windows kills the process (WER after an access violation, Task Manager,
# os._exit). The other apps then stay ducked, and the next capture read that
# ducked level as the "original" to restore to -- a self-perpetuating 1%.
#
# So every shared-session change is written ahead of the volume change to a
# small JSON file beside logs/session.running. The write is a whole-file
# os.replace: once it returns, the bytes belong to the OS and survive the
# process being terminated (no fsync -- a power loss is not the failure
# being fixed, and the engage path is latency-sensitive). A clean release
# removes the entry; whatever is still listed at the next start is a duck
# nobody restored, and recover_leftover_ducks() puts it back -- but only
# where the session is still at the level we applied, so a user change made
# since the crash wins, exactly as stop()'s conditional restore does.
#
# The journal also keeps the last plausible original per process name
# ("known levels"). _vet_original() uses it to refuse an original that is at
# or below what a capture duck would have produced from that remembered
# level: that reading is a leftover duck (a crash from before this journal,
# or a lost journal), not the user's volume.
#
# 73 -- stranding WITHOUT a crash. When an app closes its audio stream while
# ducked, the ducking host drops the session and release() cannot read it
# ("unknown sid"), so the restore is skipped. Windows keeps the app's volume
# at the ducked level, and the app's next session opens there. Before 73 the
# entry then sat in memory until the next launch while a sweep adopted the
# new session at the ducked level as its "original" -- 0.8 x 0.8 x ... for as
# long as the idle duck stayed on. Now such an entry goes back to the pending
# queue at once, and every start() and sweep retries it against the sessions
# it just listed: a session of that app still at exactly the level we left
# it at is put back. An entry stays queued while the app is not running (its
# relaunch opens at the ducked level); it is dropped once the app is seen at
# any other level (healed, or the user changed it).

DUCK_JOURNAL_NAME = "duck_state.json"

# An original at or below this is never trusted or remembered: no one keeps
# an app they want to hear at 2%, and it is where two crashed capture ducks
# (0.12 * 0.12 = 1.4%) leave a full-volume app.
IMPLAUSIBLE_ORIGINAL_FLOOR = 0.02

# Deep-duck threshold, not a factor: an original at or below remembered * this
# is treated as a leftover duck whatever produced it (the capture duck, 0.15,
# and everything composed under it). A shallower duck (the idle duck, 0.8)
# is recognised by exact match against the factors really in use instead --
# see _vet_original().
NEAR_DUCK_FACTOR = 0.15

# Relative tolerance for "this level is exactly remembered * a duck factor".
# The app holds float32(original * factor): at most one float32 ULP (about
# 6e-8 relative) from the double we compute. 1e-4 is far above that and far
# below a 1% volume-mixer step at any audible level.
_FACTOR_MATCH_REL = 1e-4

# Pending entries kept while their app is not running are capped, oldest out.
_PENDING_CAP = 64

_JOURNAL_LOCK = threading.RLock()
_RECOVERY_LOCK = threading.Lock()
_journal_path: Path | None = None
_journal_active: dict[str, dict] = {}
_journal_pending: list[dict] = []
_known_levels: dict[str, float] = {}
# 73: every duck factor a SessionDucker has been built with (persisted), so
# _vet_original() checks the multipliers actually applied on each path.
_duck_factors: set[float] = set()
# 73: process name -> level a shallow match rejected this run. Seeing the same
# level again means the user set it (a leftover is restored or journaled).
_shallow_rejected: dict[str, float] = {}
_notice_sink: Any = None
_pending_notices: list[str] = []


def _journal_key(handle: Any) -> str:
    instance = getattr(handle, "instance_id", None)
    if instance:
        return f"instance:{instance}"
    return f"pid:{getattr(handle, 'pid', 0)}:{getattr(handle, 'process_name', None) or ''}"


def _write_journal_locked() -> None:
    if _journal_path is None:
        return
    payload = {
        "version": 1,
        "ducked": list(_journal_pending) + list(_journal_active.values()),
        "known_levels": dict(_known_levels),
        "duck_factors": sorted(_duck_factors),
    }
    tmp = _journal_path.with_name(_journal_path.name + ".tmp")
    try:
        _journal_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, _journal_path)
    except Exception as exc:
        logger.debug("[DUCK] duck journal write failed: %s", exc)


def _journal_record(session_id: str, handle: Any, original: float, applied: float) -> None:
    with _JOURNAL_LOCK:
        entry = _journal_active.get(session_id)
        if entry is None:
            entry = {
                "key": _journal_key(handle),
                "pid": int(getattr(handle, "pid", 0) or 0),
                "process_name": getattr(handle, "process_name", None),
                "original": float(original),
            }
            _journal_active[session_id] = entry
        # The original is written once, when the shared state is created;
        # later leases only move `applied`. See test_engage_while_engaged_*.
        entry["applied"] = float(applied)
        _write_journal_locked()


def _journal_forget(session_id: str) -> None:
    with _JOURNAL_LOCK:
        if _journal_active.pop(session_id, None) is not None:
            _write_journal_locked()


def _journal_orphan(session_id: str, left_at: float | None) -> None:
    """A release could not verify or restore this session (it vanished, or
    the write failed): hand its entry back to the pending queue so the next
    start() or sweep retries it (73), instead of waiting for a relaunch."""
    with _JOURNAL_LOCK:
        entry = _journal_active.pop(session_id, None)
        if entry is None:
            return
        if left_at is not None:
            entry["applied"] = float(left_at)
        _journal_pending.append(entry)
        if len(_journal_pending) > _PENDING_CAP:
            dropped = _journal_pending[: len(_journal_pending) - _PENDING_CAP]
            del _journal_pending[: len(dropped)]
            logger.warning(
                "[DUCK] duck journal over %d pending entries, dropped the oldest: %s",
                _PENDING_CAP, ", ".join(str(e.get("process_name")) for e in dropped),
            )
        _write_journal_locked()
    logger.info(
        "[DUCK] %s could not be restored now; queued to restore when it is seen again",
        entry.get("process_name") or session_id,
    )
    flight_recorder.record(
        "ducker_restore_queued", session_id=session_id,
        process_name=entry.get("process_name"), left_at=left_at,
    )


def _register_duck_factor(level: float) -> None:
    if 0.0 < level < 1.0:
        with _JOURNAL_LOCK:
            _duck_factors.add(round(float(level), 6))


def _duck_products() -> list[float]:
    """Every multiplier a duck can leave an app at: each factor in use and
    each composition of up to three (idle x capture x hotkey)."""
    with _JOURNAL_LOCK:
        factors = sorted(_duck_factors)
    products: set[float] = set()
    for size in (1, 2, 3):
        for combo in itertools.combinations(factors, size):
            products.add(_multiply_factors(combo))
    return sorted(products)


def set_notice_sink(sink: Any) -> None:
    """Register `sink(message)` to show the user something ducking could not
    fix by itself. Notices raised before a sink exists are delivered on
    registration. Each is delivered once."""
    global _notice_sink
    with _JOURNAL_LOCK:
        _notice_sink = sink
        queued, _pending_notices[:] = list(_pending_notices), []
    for message in queued:
        _deliver_notice(sink, message)


def _deliver_notice(sink: Any, message: str) -> None:
    try:
        sink(message)
    except Exception as exc:
        logger.warning("[DUCK] could not show ducking notice: %s", exc)


def _surface_once(message: str) -> None:
    logger.warning("[DUCK] USER NOTICE: %s", message)
    flight_recorder.record("ducker_user_notice", message=message)
    with _JOURNAL_LOCK:
        sink = _notice_sink
        if sink is None:
            _pending_notices.append(message)
            return
    _deliver_notice(sink, message)


def _remember_level(process_name: str | None, level: float) -> None:
    if not process_name or level <= IMPLAUSIBLE_ORIGINAL_FLOOR:
        return
    with _JOURNAL_LOCK:
        if _known_levels.get(process_name) != level:
            _known_levels[process_name] = float(level)
            _write_journal_locked()


def _vet_original(handle: Any, current: float) -> tuple[float, bool]:
    """(original to use, whether `current` is a plausible user level).

    Implausible:
      * at or below IMPLAUSIBLE_ORIGINAL_FLOOR;
      * at or below remembered * NEAR_DUCK_FACTOR (a deep duck, whatever
        composition produced it);
      * exactly remembered * a factor or composition really in use (73: the
        idle duck's 0.8 was never caught by the 0.15 rule). A mixer step
        cannot be told from a duck by value alone when remembered * factor
        is a whole percent, so this rule fires once per process name and
        level per run: seeing the same level again after it was put back
        means the user chose it, and it is accepted.
    Then the remembered level (when there is one and it is louder) is what
    the session returns to; with nothing remembered the current level is
    used but never remembered, because restoring to a guessed 100% is the
    louder bug.
    """
    name = getattr(handle, "process_name", None)
    with _JOURNAL_LOCK:
        remembered = _known_levels.get(name) if name else None
    near_target = current <= IMPLAUSIBLE_ORIGINAL_FLOOR or (
        remembered is not None and current <= remembered * NEAR_DUCK_FACTOR + _VOLUME_EPSILON
    )
    if not near_target and remembered is not None and current < remembered:
        factor = next(
            (p for p in _duck_products()
             if abs(current - remembered * p) <= _FACTOR_MATCH_REL * remembered * p),
            None,
        )
        if factor is not None:
            with _JOURNAL_LOCK:
                previous = _shallow_rejected.pop(name, None)
                repeat = previous is not None and abs(previous - current) <= _FACTOR_MATCH_REL * current
                if not repeat:
                    _shallow_rejected[name] = current
            if repeat:
                logger.info(
                    "[DUCK] %s is back at %.3f after being restored from it -- "
                    "accepting it as the user's own level", name, current,
                )
                return current, True
            logger.warning(
                "[DUCK] %s is at %.3f, exactly x%.3f of its remembered %.3f -- "
                "treating it as a leftover duck and restoring to %.3f",
                name, current, factor, remembered, remembered,
            )
            flight_recorder.record(
                "ducker_original_rejected", process_name=name, current=current,
                used=remembered, factor=factor,
            )
            return remembered, False
    if not near_target:
        return current, True
    if remembered is not None and remembered > current:
        logger.warning(
            "[DUCK] %s is at %.3f, at the duck target of its remembered %.3f -- "
            "treating it as a leftover duck and restoring to %.3f",
            name, current, remembered, remembered,
        )
        flight_recorder.record(
            "ducker_original_rejected", process_name=name, current=current, used=remembered,
        )
        return remembered, False
    logger.warning(
        "[DUCK] %s is at %.3f, too low to trust as its normal volume and nothing "
        "better is remembered -- ducking from it but not remembering it",
        name, current,
    )
    return current, False


def configure_duck_journal(path: str | os.PathLike, *, previous_unclean: bool = False) -> int:
    """Point the engine at its journal and queue any duck a previous run left
    behind. Returns the number of leftover entries queued. Recovery runs on a
    background thread and, at the latest, before the next SessionDucker.start().

    `previous_unclean` is boot.begin_session()'s verdict (logs/session.running
    was still there). It explains the leftover in the log; the journal entries
    themselves are the authority on what is still ducked, because quit_app's
    os._exit can also skip a restore and clears the marker first."""
    global _journal_path
    path = Path(path)
    loaded: dict = {}
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("[DUCK] duck journal unreadable, ignoring it: %s", exc)
        loaded = {}
    scrubbed_levels: list[str] = []
    retargeted: list[str] = []
    abandoned: list[dict] = []
    with _JOURNAL_LOCK:
        _journal_path = path
        _known_levels.clear()
        for name, level in (loaded.get("known_levels") or {}).items():
            try:
                level = float(level)
            except (TypeError, ValueError):
                continue
            if level > IMPLAUSIBLE_ORIGINAL_FLOOR:
                _known_levels[str(name)] = level
            else:
                scrubbed_levels.append(f"{name}={level:.4f}")
        for factor in loaded.get("duck_factors") or []:
            try:
                _register_duck_factor(float(factor))
            except (TypeError, ValueError):
                continue
        # 73: an entry whose original is itself implausible would put the app
        # back at a leftover duck. With a trusted level remembered for that app,
        # restore to that instead (still only if the app is at exactly the level
        # we left it at). With nothing trusted there is no evidence of the right
        # level: stop tracking it and tell the user once, rather than guess.
        entries: list[dict] = []
        for entry in loaded.get("ducked") or []:
            if not isinstance(entry, dict):
                continue
            try:
                original = float(entry.get("original", 0.0))
            except (TypeError, ValueError):
                original = 0.0
            if original > IMPLAUSIBLE_ORIGINAL_FLOOR:
                entries.append(entry)
                continue
            name = entry.get("process_name")
            remembered = _known_levels.get(name) if name else None
            if remembered is not None:
                retargeted.append(f"{name}: {original:.4f} -> {remembered:.3f}")
                entries.append(dict(entry, original=remembered))
            else:
                abandoned.append(entry)
        _journal_pending[:] = entries[-_PENDING_CAP:]
        pending = len(_journal_pending)
        if scrubbed_levels or retargeted or abandoned:
            _write_journal_locked()
    if scrubbed_levels or retargeted or abandoned:
        logger.warning(
            "[DUCK] startup scrub: dropped implausible remembered level(s) [%s]; "
            "restore target raised to the remembered level [%s]; stopped tracking [%s]",
            ", ".join(scrubbed_levels),
            ", ".join(retargeted),
            ", ".join(f"{e.get('process_name')}={float(e.get('original', 0.0)):.4f}" for e in abandoned),
        )
        flight_recorder.record(
            "ducker_journal_scrubbed", known_levels=scrubbed_levels,
            retargeted=retargeted, abandoned=[e.get("process_name") for e in abandoned],
        )
    for entry in abandoned:
        _surface_once(
            f"{entry.get('process_name') or 'An app'} may still be very quiet: an earlier "
            f"Samsara run left it at {float(entry.get('applied', 0.0)) * 100:.1f}% and "
            "Samsara has no record of its normal volume, so it was left as it is."
        )
    if pending:
        logger.warning(
            "[DUCK] %d app session(s) were left ducked by the previous run (%s) -- restoring",
            pending,
            "it did not shut down cleanly" if previous_unclean else "it exited without restoring",
        )
        thread_registry.spawn("duck-journal.recover", recover_leftover_ducks, daemon=True)
    return pending


def recover_leftover_ducks(sessions: list | None = None) -> int:
    """Restore sessions a previous run -- or a release that could not verify
    its session -- left ducked. `sessions` is an enumeration the caller just
    made (start() and sweeps pass theirs); without it one is made here.
    Idempotent; returns the number of sessions restored. Never raises."""
    with _RECOVERY_LOCK:
        with _JOURNAL_LOCK:
            pending = list(_journal_pending)
        if not pending:
            return 0
        if sessions is None:
            try:
                sessions = list(_iter_audio_sessions())
            except Exception as exc:
                logger.warning("[DUCK] leftover duck recovery could not list sessions: %s", exc)
                return 0  # entries stay queued; the next start() retries
        with _DUCKER_LOCK:
            owned = set(_SHARED_SESSIONS)

        by_key: dict[str, Any] = {}
        by_name: dict[str, list] = {}
        for s in sessions:
            by_key[_journal_key(s)] = s
            name = getattr(s, "process_name", None)
            if name:
                by_name.setdefault(name, []).append(s)

        restored = 0
        resolved: list[dict] = []
        for entry in pending:
            original = float(entry.get("original", 0.0))
            applied = float(entry.get("applied", -1.0))
            name = entry.get("process_name")
            candidates = [by_key[entry["key"]]] if entry.get("key") in by_key else list(by_name.get(name, []))
            # A session a live duck holds is that duck's to restore, and says
            # nothing about this entry either way.
            candidates = [s for s in candidates if getattr(s, "session_id", None) not in owned]
            if not candidates:
                # The app is not running (or only under a live duck). Keep the
                # entry: Windows keeps a per-app volume, so it opens at the
                # ducked level when it comes back.
                continue
            for session in candidates:
                # Only a session still at exactly what we left it at is ours
                # to put back; any other level is a heal or a user change.
                try:
                    current = float(session.get_master_volume())
                except Exception:
                    continue
                if not _floats_close(current, applied):
                    continue
                if original <= IMPLAUSIBLE_ORIGINAL_FLOOR:
                    break
                try:
                    session.set_master_volume(original)
                    restored += 1
                    if session in by_name.get(name, []):
                        by_name[name].remove(session)
                except Exception as exc:
                    logger.warning("[DUCK] leftover duck restore failed for %s: %s", name, exc)
                break
            resolved.append(entry)
            # Restored or not, the journal's original is the best evidence of
            # this app's real level: it guards the next capture of it.
            if name and original > IMPLAUSIBLE_ORIGINAL_FLOOR:
                with _JOURNAL_LOCK:
                    _known_levels[name] = original

        if not resolved:
            return 0
        resolved_ids = {id(e) for e in resolved}
        with _JOURNAL_LOCK:
            _journal_pending[:] = [e for e in _journal_pending if id(e) not in resolved_ids]
            waiting = len(_journal_pending)
            _write_journal_locked()
        logger.info(
            "[DUCK] leftover duck recovery: %d of %d session(s) restored, %d waiting for their app",
            restored, len(resolved), waiting,
        )
        flight_recorder.record(
            "ducker_leftover_recovered", restored=restored, pending=len(resolved), waiting=waiting,
        )
        return restored


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
        _register_duck_factor(self.duck_level)
        self._base_excludes = set(exclude_pids or [])
        self._base_excludes.add(os.getpid())
        # pid 0 is the Windows "System Sounds" session (psutil names it
        # "System Idle Process"). It carries notification sounds, not media
        # that masks speech, and it is shared by all of Windows -- ducking it
        # bought nothing and stranded it like any app (73).
        self._base_excludes.add(0)

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
            left_at = state.written_volume if state.written_volume is not None else expected_volume
            state.lease_factors.pop(self._lease_id, None)
            handle = state.handle
            close_handle = False
            restore_volume = None

            if state.lease_factors:
                state.applied_volume = self._target_for_factors(state)
                restore_volume = state.applied_volume
                _journal_record(session_id, handle, state.original_volume, restore_volume)
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
                    # 73: queued for the next start() or sweep, which put it
                    # back if the app is still at the level we left it at.
                    _journal_orphan(session_id, left_at)
                    self._close_session(handle)
                else:
                    # Still leased: the journal must name what the app is
                    # really at, not the target this release never wrote.
                    with self._lock:
                        if _SHARED_SESSIONS.get(session_id) is state:
                            _journal_record(session_id, handle, state.original_volume, left_at)
                return
            if not _floats_close(current, expected_volume):
                if close_handle:
                    _journal_forget(session_id)  # the user's change wins
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
                _journal_orphan(session_id, left_at)  # still ducked: retried (73)
                self._close_session(handle)
            return
        if close_handle:
            _journal_forget(session_id)
            self._close_session(handle)
        else:
            with self._lock:
                if _SHARED_SESSIONS.get(session_id) is state:
                    state.written_volume = restore_volume

    def _sibling_original(self, session: _SessionHandle, live: float) -> float | None:
        """The original of another session of the same app that a duck holds
        at exactly `live`, if any (73). A new session opening at the level we
        wrote to its app is that app's ducked volume -- Windows keeps one per
        app -- typically because the ducked session just closed its stream
        and this is its replacement. Adopting `live` as the original is how
        an app compounded 0.8 x 0.8 x ... under a long idle duck."""
        name = getattr(session, "process_name", None)
        if not name:
            return None
        with self._lock:
            for session_id, state in _SHARED_SESSIONS.items():
                written = state.written_volume
                if (
                    session_id == session.session_id
                    or written is None
                    or getattr(state.handle, "process_name", None) != name
                    or abs(written - live) > max(1e-6, _FACTOR_MATCH_REL * written)
                    or state.original_volume <= live + _VOLUME_EPSILON
                ):
                    continue
                original = state.original_volume
                break
            else:
                return None
        logger.info(
            "[DUCK] new %s session opened at %.3f, the level a duck holds it at -- "
            "restoring it to %.3f with the rest of the app",
            name, live, original,
        )
        flight_recorder.record(
            "ducker_original_inherited", process_name=name, current=live, used=original,
        )
        return original

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
                    _journal_record(session_id, state.handle, state.original_volume, target_volume)

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
            live_volume = original_volume
            inherited = self._sibling_original(session, live_volume)
            if inherited is not None:
                original_volume, plausible = inherited, False
            else:
                original_volume, plausible = _vet_original(session, live_volume)

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
                        # A failed engage rolls back to what was really
                        # playing, not to a remembered level.
                        previous_volume = live_volume
                        target_volume = state.applied_volume
                        created_new_state = True
                        should_add_lease = True
                        _journal_record(session_id, session, original_volume, target_volume)
                        if plausible:
                            _remember_level(getattr(session, "process_name", None), original_volume)
                    elif self._lease_id in state.lease_factors:
                        close_now = True
                    else:
                        previous_volume = state.applied_volume
                        state.lease_factors[self._lease_id] = self.duck_level
                        state.applied_volume = self._target_for_factors(state)
                        target_volume = state.applied_volume
                        should_add_lease = True
                        _journal_record(session_id, state.handle, state.original_volume, target_volume)
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
            if _SHARED_SESSIONS.get(session_id) is state:
                state.written_volume = target_volume
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
            if _journal_pending:
                # A duck left by a crashed run, or by a release that could not
                # reach its session, goes back before this one reads
                # "original" levels (52, 73).
                recover_leftover_ducks(discovered)
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
            if _journal_pending:
                recover_leftover_ducks(sessions)  # before adoption reads originals (73)
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

    def __init__(self, sid: str, pid: int, generation: int, process_name=None, instance_id=None) -> None:
        self.session_id = sid
        self.pid = pid
        self.process_name = process_name
        self.instance_id = instance_id
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
            entry.get("instance"),
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
