"""Tasks plugin — local task list with voice management.

Tasks are stored locally only in samsara/tasks_store.py. This plugin contains
no network, account-sync, or background-delivery path; see CHANGELOG.md for
the v0.21.1 removal of the obsolete Arcana forwarding code.

Voice commands:
  "Jarvis, add to list buy groceries"
  "Jarvis, show tasks"
  "Jarvis, complete task 2"
  "Jarvis, remove task 1"
  "Jarvis, read tasks"
  "Jarvis, clear completed"
  "Jarvis, hide tasks"
"""

import re

from samsara import tasks_store
from samsara.plugin_commands import command


_overlay = None


def _get_overlay():
    global _overlay
    if _overlay is None:
        from samsara.ui.task_overlay import TaskOverlay
        _overlay = TaskOverlay()
    return _overlay


def _refresh_overlay():
    if _overlay is not None and _overlay._window is not None:
        _overlay.refresh(tasks_store.get_all())


def _speak(app, text):
    if hasattr(app, "audio_coordinator") and app.audio_coordinator:
        app.audio_coordinator.speak(text, category="agent_response", interruptible=False)
    elif hasattr(app, "tts_engine") and app.tts_engine:
        app.tts_engine.speak(text)
    else:
        print(f"[TASKS] {text}")


def _parse_position(remainder):
    """Extract a 1-based position number from a remainder string."""
    if not remainder:
        return None
    m = re.search(r"\d+", remainder)
    return int(m.group()) if m else None


@command(
    "add to list",
    aliases=["add to tasks", "todo", "add to the list", "add task", "new task"],
    pack="tasks",
)
def handle_add_to_list(app, remainder="", **kwargs):
    if not remainder or not remainder.strip():
        _speak(app, "What should I add to your list?")
        return True
    text = remainder.strip()
    tasks_store.add_task(text)
    _refresh_overlay()
    _speak(app, f"Added: {text}.")
    return True


@command(
    "show tasks",
    aliases=["task list", "show my tasks", "open tasks", "show task list"],
    pack="tasks",
)
def handle_show_tasks(app, remainder="", **kwargs):
    _get_overlay().show(tasks_store.get_all())
    return True


@command(
    "hide tasks",
    aliases=["close tasks", "close task list"],
    pack="tasks",
)
def handle_hide_tasks(app, remainder="", **kwargs):
    if _overlay is not None:
        _overlay.hide()
    return True


@command(
    "complete task",
    aliases=["finish task", "done task", "check task", "task complete"],
    pack="tasks",
)
def handle_complete_task(app, remainder="", **kwargs):
    pos = _parse_position(remainder)
    if pos is None:
        _speak(app, "Which task number?")
        return True
    active = tasks_store.get_active()
    if pos < 1 or pos > len(active):
        _speak(app, f"No task {pos} in the active list.")
        return True
    task = active[pos - 1]
    tasks_store.complete_task(task["id"])
    _refresh_overlay()
    _speak(app, f"Completed: {task['text']}.")
    return True


@command(
    "remove task",
    aliases=["delete task"],
    pack="tasks",
)
def handle_remove_task(app, remainder="", **kwargs):
    pos = _parse_position(remainder)
    if pos is None:
        _speak(app, "Which task number?")
        return True
    active = tasks_store.get_active()
    if pos < 1 or pos > len(active):
        _speak(app, f"No task {pos} in the active list.")
        return True
    task = active[pos - 1]
    tasks_store.remove_task(task["id"])
    _refresh_overlay()
    _speak(app, f"Removed: {task['text']}.")
    return True


@command(
    "clear completed",
    aliases=["clear done tasks", "remove completed"],
    pack="tasks",
)
def handle_clear_completed(app, remainder="", **kwargs):
    count = tasks_store.clear_completed()
    _refresh_overlay()
    if count == 0:
        _speak(app, "No completed tasks to clear.")
    else:
        _speak(app, f"Cleared {count} completed task{'s' if count != 1 else ''}.")
    return True


@command(
    "read tasks",
    aliases=["read my tasks", "what are my tasks", "list tasks"],
    pack="tasks",
)
def handle_read_tasks(app, remainder="", **kwargs):
    active = tasks_store.get_active()
    if not active:
        _speak(app, "No active tasks.")
        return True
    n = len(active)
    parts = [f"You have {n} task{'s' if n != 1 else ''}."]
    for i, t in enumerate(active, 1):
        parts.append(f"{i}: {t['text']}.")
    _speak(app, " ".join(parts))
    return True
