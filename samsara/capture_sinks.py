"""Delivery sinks for recordings that share the memo capture lane."""
import logging

logger = logging.getLogger("Samsara")


def deliver_task(app, text) -> bool:
    """Deliver a task capture, preserving non-empty text if the plugin fails."""
    try:
        from plugins.commands.tasks import capture_spoken_task
        return bool(capture_spoken_task(app, text))
    except Exception:
        logger.exception("[TASKS] Capture delivery failed; saving raw task")
        if text and text.strip():
            from samsara import tasks_store
            tasks_store.add_task(text.strip())
            return True
        return False
