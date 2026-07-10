"""Central registry for every thread the app spawns.

Pure observability infrastructure: it gives us one place to see what threads
are running, log stragglers at shutdown, and detect leaked/zombie threads.
Wrapping is transparent -- it does not change target behavior, daemon flags,
or startup ordering. All app code should spawn threads via spawn() below
rather than constructing threading.Thread directly (enforced by
tools/check_thread_discipline.py); code that must construct the Thread itself
(subclassed Thread, deferred/conditional start) should call register()
immediately after start() instead.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback

from samsara.log import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()
_threads: dict[int, "_Entry"] = {}      # id(thread) -> entry, currently tracked
_finished: list["_Entry"] = []          # most-recently-finished, oldest first
_MAX_FINISHED = 50
_name_counts: dict[str, int] = {}       # base name -> times it's been used


class _Entry:
    __slots__ = ("thread", "name", "started_at", "finished_at")

    def __init__(self, thread: threading.Thread, name: str) -> None:
        self.thread = thread
        self.name = name
        self.started_at = time.time()
        self.finished_at: float | None = None


def _unique_name(name: str) -> str:
    """First use of a name is returned as-is; repeats get a -2, -3, ... suffix."""
    with _lock:
        count = _name_counts.get(name, 0) + 1
        _name_counts[name] = count
        return name if count == 1 else f"{name}-{count}"


def _to_dict(entry: "_Entry") -> dict:
    return {
        "name": entry.name,
        "ident": entry.thread.ident,
        "daemon": entry.thread.daemon,
        "alive": not _is_done(entry),
        "started_at": entry.started_at,
        "finished_at": entry.finished_at,
    }


def _retire(entry: "_Entry") -> None:
    """Move a no-longer-alive entry from _threads to the bounded _finished list."""
    with _lock:
        _threads.pop(id(entry.thread), None)
        if entry.finished_at is None:
            entry.finished_at = time.time()
        _finished.append(entry)
        if len(_finished) > _MAX_FINISHED:
            del _finished[: len(_finished) - _MAX_FINISHED]


def _is_done(entry: "_Entry") -> bool:
    """True once a thread has actually gone quiet. For a threading.Timer,
    cancel() only sets its internal `finished` Event -- the OS thread may
    take a moment longer to unwind out of `finished.wait(interval)` -- so a
    cancelled timer is treated as done immediately rather than waiting on
    is_alive() to catch up."""
    thread = entry.thread
    if not thread.is_alive():
        return True
    timer_finished = getattr(thread, "finished", None)
    return isinstance(timer_finished, threading.Event) and timer_finished.is_set()


def spawn(
    name: str,
    target,
    args: tuple = (),
    kwargs: dict | None = None,
    daemon: bool = True,
) -> threading.Thread:
    """Create, register, and start a thread. Name is mandatory; duplicates get
    a numeric suffix. The target is wrapped so the registry always learns
    when the thread finishes (return or raise) -- exceptions are logged
    (fail-loud) and then re-raised so existing crash/propagation behavior is
    unchanged."""
    kwargs = kwargs or {}
    unique = _unique_name(name)

    def _wrapped() -> None:
        try:
            target(*args, **kwargs)
        except Exception:
            logger.exception(f"[ThreadRegistry] Unhandled exception in thread '{unique}'")
            raise
        finally:
            _retire(entry)

    thread = threading.Thread(target=_wrapped, name=unique, daemon=daemon)
    entry = _Entry(thread, unique)
    with _lock:
        _threads[id(thread)] = entry
    thread.start()
    return thread


def timer(
    name: str,
    delay: float,
    fn,
    args: tuple = (),
    kwargs: dict | None = None,
    daemon: bool | None = None,
) -> threading.Timer:
    """Create, register, and start a threading.Timer. Name dedup and
    fail-loud exception logging/re-raise match spawn(). `daemon` is left
    unset (Timer's own default of False) unless explicitly passed, matching
    the daemon flag callers set on the raw Timer today.

    If the timer is cancelled before firing, `fn` never runs, so this
    wrapper's finally-block never fires either -- that's fine, because
    _is_done() treats a cancelled timer as finished as soon as its `finished`
    Event is set, and snapshot()/dump()/shutdown() lazily retire it the next
    time they observe it that way (the same lazy-prune path register() relies
    on for threads we don't own the run() call for)."""
    kwargs = kwargs or {}
    unique = _unique_name(name)

    def _wrapped(*a, **kw) -> None:
        try:
            fn(*a, **kw)
        except Exception:
            logger.exception(f"[ThreadRegistry] Unhandled exception in timer '{unique}'")
            raise
        finally:
            _retire(entry)

    thread = threading.Timer(delay, _wrapped, args=args, kwargs=kwargs)
    thread.name = unique
    if daemon is not None:
        thread.daemon = daemon
    entry = _Entry(thread, unique)
    with _lock:
        _threads[id(thread)] = entry
    thread.start()
    return thread


def register(thread: threading.Thread, name: str | None = None) -> None:
    """Register a thread this module didn't construct (e.g. a subclassed
    Thread, or one started conditionally elsewhere). Call it right after
    start(). Duplicate names get the same numeric-suffix treatment as
    spawn(). Since we don't own the run() call here, finish detection is
    lazy -- snapshot()/dump()/shutdown() notice and retire it the next time
    they observe it as no longer alive."""
    unique = _unique_name(name or thread.name)
    entry = _Entry(thread, unique)
    with _lock:
        _threads[id(thread)] = entry


def snapshot() -> list[dict]:
    """Full diagnostic view: every currently-tracked thread plus the last
    _MAX_FINISHED finished ones. Also lazily retires any tracked entry that
    has gone quiet without us catching it via spawn()'s wrapper (i.e. one
    registered via register())."""
    with _lock:
        live = list(_threads.values())
        done = list(_finished)

    still_live = []
    newly_done = []
    for entry in live:
        if _is_done(entry):
            newly_done.append(entry)
        else:
            still_live.append(entry)

    for entry in newly_done:
        _retire(entry)

    return [_to_dict(e) for e in still_live + newly_done + done]


def dump(logger) -> None:
    """Log one line per currently-alive registered thread."""
    with _lock:
        entries = list(_threads.values())
    for entry in entries:
        if _is_done(entry):
            continue
        logger.info(
            f"[ThreadRegistry] name={entry.name} ident={entry.thread.ident} "
            f"daemon={entry.thread.daemon} started_at={entry.started_at}"
        )


def shutdown(timeout: float = 5.0) -> None:
    """Best-effort join of tracked NON-daemon threads within a shared
    `timeout`-second deadline (not `timeout` seconds EACH -- one hung
    thread eating the whole budget still leaves the rest a fair,
    shrinking remainder to join in). Anything still alive when the
    deadline passes is logged (name + current stack) as a straggler, but
    never force-killed.

    Daemon threads are intentionally NOT joined here at all -- nearly
    every spawn() site in this app passes daemon=True, so in practice this
    function joins very few threads; daemon threads are simply reaped by
    the interpreter at process exit. This registry is observability/
    diagnostics infrastructure, not a hard cleanup guarantee -- do not
    rely on shutdown() returning to mean "every thread has exited."

    Registered threading.Timer instances are cancelled instead of joined,
    regardless of their daemon flag -- a pending non-daemon Timer would
    otherwise block this whole function for up to its remaining delay.
    cancel() is a no-op-safe call even if the timer already fired."""
    with _lock:
        all_entries = list(_threads.values())

    for entry in all_entries:
        if isinstance(entry.thread, threading.Timer) and entry.thread.is_alive():
            entry.thread.cancel()

    with _lock:
        entries = [
            e for e in _threads.values()
            if not isinstance(e.thread, threading.Timer)
            and not e.thread.daemon and e.thread.is_alive()
        ]

    if not entries:
        return

    deadline = time.monotonic() + timeout
    for entry in entries:
        remaining = deadline - time.monotonic()
        entry.thread.join(max(0.0, remaining))

    stragglers = [e for e in entries if e.thread.is_alive()]
    if not stragglers:
        logger.info(
            f"[ThreadRegistry] shutdown: all {len(entries)} non-daemon "
            f"thread(s) joined cleanly"
        )
        return

    frames = sys._current_frames()
    for entry in stragglers:
        stack = frames.get(entry.thread.ident)
        stack_text = "".join(traceback.format_stack(stack)) if stack else "<no frame available>"
        logger.error(
            f"[ThreadRegistry] Thread '{entry.name}' (ident={entry.thread.ident}) "
            f"did not exit within {timeout}s shutdown timeout:\n{stack_text}"
        )
