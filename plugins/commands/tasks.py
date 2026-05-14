"""Tasks plugin — add items to The Arcana's task list by voice.

"Jarvis, add to list car needs a new battery"
"Jarvis, todo buy groceries"
"Jarvis, add to tasks test reverse TTS"
"""

import json
import logging
import urllib.request
import urllib.error

from samsara.plugin_commands import command

logger = logging.getLogger(__name__)

ARCANA_API = "https://morneis.com/api/add"


def _post_task(text):
    """POST a new task to the Arcana's /api/add endpoint."""
    payload = json.dumps({
        "text": text.strip(),
        "section": "capture",
        "tags": ["voice", "samsara"],
    }).encode('utf-8')

    req = urllib.request.Request(
        ARCANA_API,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode('utf-8'))


@command("add to list", aliases=["add to tasks", "todo", "add to the list"], pack="tasks")
def handle_add_to_list(app, remainder):
    """Add an item to the task list on The Arcana."""
    if not remainder or not remainder.strip():
        print("[TASKS] No text provided")
        return True

    text = remainder.strip()
    try:
        result = _post_task(text)
        if result.get("success"):
            print(f"[TASKS] Added: {text}")
        else:
            print(f"[TASKS] Server error: {result}")
    except Exception as e:
        logger.error(f"Tasks plugin error: {e}")
        print(f"[TASKS] Failed — couldn't reach The Arcana: {e}")

    return True
