import json
import os
import threading

_STATS_PATH = os.path.join(os.path.expanduser('~'), '.samsara', 'command_stats.json')
_stats = {}
_lock = threading.Lock()


def _load():
    global _stats
    try:
        with open(_STATS_PATH, 'r', encoding='utf-8') as f:
            _stats = json.load(f)
    except Exception:
        _stats = {}


def _save():
    os.makedirs(os.path.dirname(_STATS_PATH), exist_ok=True)
    tmp = _STATS_PATH + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_stats, f, indent=2)
        os.replace(tmp, _STATS_PATH)
    except Exception as e:
        print(f"[STATS] Save failed: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def increment_command_count(name: str):
    with _lock:
        _stats[name] = _stats.get(name, 0) + 1
        if _stats[name] % 5 == 0:
            _save()


def get_top_commands(n: int = 10):
    with _lock:
        return sorted(_stats.items(), key=lambda x: x[1], reverse=True)[:n]


def get_count(name: str) -> int:
    with _lock:
        return _stats.get(name, 0)


_load()
