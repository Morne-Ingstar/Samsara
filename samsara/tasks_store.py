"""Local persistent task storage for Samsara.

Tasks are stored in ~/.samsara/tasks.json.
All public functions are thread-safe.
"""

import json
import os
import threading
from datetime import datetime, timezone

from samsara.paths import samsara_home_dir

_TASKS_PATH = str(samsara_home_dir() / "tasks.json")
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


def add_task(text: str, *, due=None, time=None, heard=None, parsed=False) -> dict:
    with _lock:
        task = {
            "id": _data["next_id"],
            "text": text.strip(),
            "completed": False,
            "created": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "completed_at": None,
        }
        if parsed:
            task.update(parsed=True,
                        due=due if isinstance(due, str) else "",
                        time=time if isinstance(time, str) else "")
            if heard is not None:
                task["heard"] = heard
        _data["tasks"].append(task)
        _data["next_id"] += 1
        _save()
        return dict(task)


def set_todoist_result(task_id: int, *, todoist_id: str | None = None, pending: bool = False) -> bool:
    """Record delivery state without ever replacing the durable local task."""
    with _lock:
        for task in _data["tasks"]:
            if task["id"] == task_id:
                if todoist_id is not None:
                    task["todoist_id"] = str(todoist_id)
                task["todoist_pending"] = bool(pending)
                _save()
                return True
        return False


def get_todoist_pending() -> list:
    with _lock:
        return [dict(task) for task in _data["tasks"] if task.get("todoist_pending")]


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
