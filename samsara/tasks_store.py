"""Local persistent task storage for Samsara.

Tasks are stored in ~/.samsara/tasks.json.
All public functions are thread-safe.
"""

import json
import os
import threading
from datetime import datetime, timezone

_TASKS_PATH = os.path.join(os.path.expanduser("~"), ".samsara", "tasks.json")
_lock = threading.Lock()
_data = {"tasks": [], "next_id": 1}


def _load():
    global _data
    try:
        with open(_TASKS_PATH, "r", encoding="utf-8") as f:
            _data = json.load(f)
    except FileNotFoundError:
        _data = {"tasks": [], "next_id": 1}
    except Exception as e:
        print(f"[TASKS] Could not load tasks: {e}")
        _data = {"tasks": [], "next_id": 1}


def _save():
    os.makedirs(os.path.dirname(_TASKS_PATH), exist_ok=True)
    tmp = _TASKS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data, f, indent=2)
    os.replace(tmp, _TASKS_PATH)


def add_task(text: str) -> dict:
    with _lock:
        task = {
            "id": _data["next_id"],
            "text": text.strip(),
            "completed": False,
            "created": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "completed_at": None,
        }
        _data["tasks"].append(task)
        _data["next_id"] += 1
        _save()
        return dict(task)


def complete_task(task_id: int) -> bool:
    with _lock:
        for t in _data["tasks"]:
            if t["id"] == task_id:
                t["completed"] = True
                t["completed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                _save()
                return True
        return False


def uncomplete_task(task_id: int) -> bool:
    with _lock:
        for t in _data["tasks"]:
            if t["id"] == task_id:
                t["completed"] = False
                t["completed_at"] = None
                _save()
                return True
        return False


def remove_task(task_id: int) -> bool:
    with _lock:
        before = len(_data["tasks"])
        _data["tasks"] = [t for t in _data["tasks"] if t["id"] != task_id]
        if len(_data["tasks"]) < before:
            _save()
            return True
        return False


def clear_completed() -> int:
    with _lock:
        before = len(_data["tasks"])
        _data["tasks"] = [t for t in _data["tasks"] if not t["completed"]]
        removed = before - len(_data["tasks"])
        if removed:
            _save()
        return removed


def get_all() -> list:
    with _lock:
        return [dict(t) for t in _data["tasks"]]


def get_active() -> list:
    with _lock:
        return [dict(t) for t in _data["tasks"] if not t["completed"]]


def get_completed() -> list:
    with _lock:
        return [dict(t) for t in _data["tasks"] if t["completed"]]


_load()
