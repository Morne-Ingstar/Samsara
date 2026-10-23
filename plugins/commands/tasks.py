"""Tasks plugin — local task list with voice management.

Tasks are stored locally in samsara/tasks_store.py first. Todoist delivery is
strictly opt-in: when both its Settings toggle and personal token are present,
the spoken task text is sent to Todoist's servers in a background thread.

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
READBACK_ITEM_LIMIT = 5


def _get_overlay():
    global _overlay
    if _overlay is None:
        from samsara.ui.task_overlay import TaskOverlay
        _overlay = TaskOverlay()
    return _overlay


def _refresh_overlay():
    if _overlay is not None and _overlay._window is not None:
        _overlay.refresh(tasks_store.get_all())


def _speak(app, text, *, category="agent_response"):
    if hasattr(app, "audio_coordinator") and app.audio_coordinator:
        app.audio_coordinator.speak(text, category=category, interruptible=False)
    elif hasattr(app, "tts_engine") and app.tts_engine:
        app.tts_engine.speak(text)
    else:
        print(f"[TASKS] {text}")


def _parse_position(remainder):
    """Extract a 1-based position number from a remainder string."""
    if not remainder:
        return None
    from samsara.spoken_numbers import parse_spoken_number
    return parse_spoken_number(remainder.strip().rstrip('.!?'))


def _todoist_token(app):
    todoist = ((getattr(app, "config", {}) or {}).get("tasks", {}) or {}).get("todoist", {}) or {}
    token = str(todoist.get("api_token", "")).strip()
    return token if todoist.get("enabled") and token else ""


def _deliver(app, task):
    from samsara import todoist_client
    try:
        tasks_store.set_todoist_result(task["id"], todoist_id=todoist_client.quick_add(_todoist_token(app), task["text"]))
        _speak(app, f"Added to Todoist: {task['text']}.")
    except Exception:
        tasks_store.set_todoist_result(task["id"], pending=True)
        _speak(app, "Saved locally; Todoist didn't take it.")


@command(
    "add to list",
    aliases=["add to tasks", "todo", "add to the list", "add task", "new task"],
    pack="tasks",
    risk_class="write", param_schema={"remainder": {"type": "str", "required": False}},
)
def handle_add_to_list(app, remainder="", **kwargs):
    """Adds what you say next to your task list."""
    if not remainder or not remainder.strip():
        _speak(app, "What should I add to your list?")
        return True
    text = remainder.strip()
    task = tasks_store.add_task(text)
    _refresh_overlay()
    if token := _todoist_token(app):
        from samsara.runtime import thread_registry
        thread_registry.spawn("tasks-todoist-add", _deliver, args=(app, task), daemon=True)
    else:
        _speak(app, f"Added: {text}.")
    return True


def capture_spoken_task(app, text) -> bool:
    """Save a hold-key task through the existing raw capture path."""
    if not text or not text.strip():
        _speak(app, "I didn't catch a task.")
        return False
    config = getattr(app, "config", {}) or {}
    # The provider wrapper adds conversation/personal context even with a
    # custom system prompt. Read the switch, but keep it inert until that
    # wrapper offers an explicit stateless, utterance-only request seam.
    _ava_parse = ((config.get("tasks") or {}).get("capture") or {}).get("ava_parse") is True
    return handle_add_to_list(app, text)


@command("sync tasks", aliases=["sync my tasks"], pack="tasks", risk_class="write")
def handle_sync_tasks(app, remainder="", **kwargs):
    """Sends pending tasks from your local list to Todoist when the connection is enabled."""  # PLACEHOLDER COPY (531)
    token = _todoist_token(app)
    if not token:
        _speak(app, "Todoist is off.")
        return True
    pending = tasks_store.get_todoist_pending()
    def _sync():
        from samsara import todoist_client
        sent = 0
        for task in pending:
            try:
                tasks_store.set_todoist_result(task["id"], todoist_id=todoist_client.quick_add(token, task["text"]))
                sent += 1
            except Exception:
                pass
        _speak(app, f"Synced {sent} task{'s' if sent != 1 else ''} to Todoist.")
    from samsara.runtime import thread_registry
    thread_registry.spawn("tasks-todoist-sync", _sync, daemon=True)
    return True


@command("what's on today", aliases=["today's tasks", "read today"], pack="tasks", risk_class="read")
def handle_today_tasks(app, remainder="", **kwargs):
    """Reads the tasks due today from your connected Todoist account."""  # PLACEHOLDER COPY (531)
    token = _todoist_token(app)
    if not token:
        _speak(app, "Todoist is off.", category="readback")
        return True
    def _read():
        from samsara import todoist_client
        try:
            items = todoist_client.today(token)
            if not items: _speak(app, "Nothing is due today.", category="readback")
            else: _speak(app, "Today: " + ". ".join(items[:READBACK_ITEM_LIMIT]) + ".", category="readback")
        except Exception:
            _speak(app, "I couldn't read Todoist today.", category="readback")
    from samsara.runtime import thread_registry
    thread_registry.spawn("tasks-todoist-today", _read, daemon=True)
    return True


@command(
    "show tasks",
    aliases=["task list", "show my tasks", "open tasks", "show task list"],
    pack="tasks",
    risk_class="ui",
)
def handle_show_tasks(app, remainder="", **kwargs):
    """Puts your task list on the screen."""
    _get_overlay().show(tasks_store.get_all())
    return True


@command(
    "hide tasks",
    aliases=["close tasks", "close task list"],
    pack="tasks",
    risk_class="ui",
)
def handle_hide_tasks(app, remainder="", **kwargs):
    """Takes the task list off the screen."""
    if _overlay is not None:
        _overlay.hide()
    return True


@command(
    "complete task",
    aliases=["finish task", "done task", "check task", "task complete"],
    pack="tasks",
    risk_class="write", param_schema={"task_number": {"type": "int", "required": False}},
)
def handle_complete_task(app, remainder="", **kwargs):
    """Marks the task you name as done."""
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
    risk_class="destructive", param_schema={"task_number": {"type": "int", "required": False}},
)
def handle_remove_task(app, remainder="", **kwargs):
    """Deletes the task you name from the list."""
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
    risk_class="destructive",
)
def handle_clear_completed(app, remainder="", **kwargs):
    """Deletes every task already marked as done."""
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
    risk_class="read",
)
def handle_read_tasks(app, remainder="", **kwargs):
    """Reads out the tasks you have not finished."""
    active = tasks_store.get_active()
    if not active:
        _speak(app, "No active tasks.", category="readback")
        return True
    n = len(active)
    parts = [f"You have {n} task{'s' if n != 1 else ''}."]
    for i, t in enumerate(active[:READBACK_ITEM_LIMIT], 1):
        parts.append(f"{i}: {t['text']}.")
    if n > READBACK_ITEM_LIMIT:
        parts.append(
            f"This is a shortened list; {n - READBACK_ITEM_LIMIT} more tasks were not read."
        )
    _speak(app, " ".join(parts), category="readback")
    return True
