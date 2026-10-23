"""Explicit voice entry to the existing latched hands-free session."""
from samsara.command_registry import DispatchResult, DispatchState
from samsara.plugin_commands import command
from samsara.session_modes import HANDS_FREE_ENTRY_PHRASES


def _notice(app, text):
    app._show_outcome_chip(text, "accent", 3500)
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is not None:
        coordinator.speak(text, category="command")


@command("hands free", aliases=["hands-free", "start hands free", "hands free mode"],
         pack="core", risk_class="safe", ai_composable=False,
         side_effects=["audio", "ui"])
def handle_hands_free(app, remainder=""):
    """Starts hands-free dictation: talk in whole thoughts, say 'finish' to send."""
    if remainder.strip():
        return False
    detail = {"chain_feedback_sent": True}
    if app.config.get("command_mode", {}).get("mode", "hold") != "toggle":
        _notice(app, "Choose Toggle for the session button to start hands-free.")
        return DispatchResult(DispatchState.REJECTED, detail=detail)
    if getattr(app, "ava_mode_active", False):
        _notice(app, "Release the Ava button before starting hands-free.")
        return DispatchResult(DispatchState.REJECTED, detail=detail)
    if app.command_mode_active:
        _notice(app, "Already hands-free.")
        return DispatchResult(DispatchState.COMPLETED, detail=detail)

    def enter():
        # Same serialized transition and entry function as the session key.
        app.enter_command_mode()
        if app.command_mode_active:
            _notice(app, "Hands-free started.")

    app.wake_word_triggered = False
    timer = getattr(app, "wake_word_timer", None)
    if timer is not None:
        timer.cancel()
    app._dispatch_session_transition(enter)
    return DispatchResult(DispatchState.QUEUED, detail=detail)
