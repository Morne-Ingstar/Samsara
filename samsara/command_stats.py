import json
import os
import threading

_STATS_PATH = os.path.join(os.path.expanduser('~'), '.samsara', 'command_stats.json')
_stats = {}
_lock = threading.Lock()          # protects _stats and _pending_timer
_pending_timer = None             # one-shot debounce timer for disk writes
_FLUSH_DELAY = 5.0                # seconds to coalesce before writing


def _load():
    global _stats
    try:
        with open(_STATS_PATH, 'r', encoding='utf-8') as f:
            _stats = json.load(f)
    except Exception:
        _stats = {}


def _save():
    """Snapshot _stats under lock, then write atomically.

    Called from the timer callback and from flush().  Safe from any thread.
    """
    with _lock:
        snapshot = dict(_stats)
    os.makedirs(os.path.dirname(_STATS_PATH), exist_ok=True)
    tmp = _STATS_PATH + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp, _STATS_PATH)
    except Exception as e:
        print(f"[STATS] Save failed: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def _schedule_flush():
    """Cancel any pending flush and start a fresh debounce timer.

    Caller MUST hold _lock.
    """
    global _pending_timer
    if _pending_timer is not None:
        _pending_timer.cancel()
    _pending_timer = threading.Timer(_FLUSH_DELAY, _save)
    _pending_timer.daemon = True   # never block process shutdown
    _pending_timer.start()


def increment_command_count(name: str):
    """Increment the usage counter for *name* and schedule a deferred write.

    The in-memory increment is synchronous and visible immediately to
    callers of get_count / get_top_commands.  The disk write is coalesced:
    successive calls within _FLUSH_DELAY seconds cancel and restart the
    timer, so a burst of commands produces at most one write.
    """
    with _lock:
        _stats[name] = _stats.get(name, 0) + 1
        _schedule_flush()


def flush():
    """Cancel any pending timer and write immediately.

    Wire into the app's clean-shutdown path so counters inside the debounce
    window aren't lost.
    """
    global _pending_timer
    with _lock:
        if _pending_timer is not None:
            _pending_timer.cancel()
            _pending_timer = None
    _save()


def get_top_commands(n: int = 10):
    with _lock:
        return sorted(_stats.items(), key=lambda x: x[1], reverse=True)[:n]


def get_count(name: str) -> int:
    with _lock:
        return _stats.get(name, 0)


_load()
