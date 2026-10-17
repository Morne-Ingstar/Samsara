"""Bound the one-command wake exception to the numbered overlay's lifetime."""
from time import monotonic

from samsara.runtime import thread_registry


def keep_numbers_window(app):
    from plugins.commands.show_numbers import is_overlay_active

    if not is_overlay_active():
        return False
    token = object()
    app._numbers_followup_token = token
    deadline = monotonic() + 30.0
    app.wake_word_triggered = True
    app._start_wake_timeout(30.0)

    def check():
        if (getattr(app, '_numbers_followup_token', None) is not token
                or app.app_state != 'command_window' or not app.wake_word_triggered):
            return
        if is_overlay_active() and monotonic() < deadline:
            thread_registry.timer('numbers.followup', 0.2, check, daemon=True)
            return
        app._numbers_followup_token = None
        app.wake_word_triggered = False
        app.app_state = 'asleep'
        if getattr(app, 'wake_word_timer', None):
            app.wake_word_timer.cancel()
        app._indicator_reset()

    thread_registry.timer('numbers.followup', 0.2, check, daemon=True)
    return True
