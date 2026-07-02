"""Unified session (COMMAND <-> DICTATE) recovery command.

"retype that" -- COMMAND-mode only. Retries the most recent DICTATE chunk
that focus-lock suppressed (foreground window changed before injection),
re-checking focus-lock against that chunk's original target process. See
samsara/session_modes.py SessionModeManager.retype_last_suppressed().
"""
from samsara.plugin_commands import command


@command("retype that", aliases=["retype it", "retype last"], pack="session", ai_visible=False)
def handle_retype_that(app, remainder="", **kwargs):
    manager = getattr(app, "_session_mode_manager", None)
    if manager is None:
        return False
    ok = manager.retype_last_suppressed()
    if hasattr(app, "play_sound"):
        app.play_sound("scratch_success" if ok else "scratch_refuse")
    return ok
