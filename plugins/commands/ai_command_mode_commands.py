"""AI Command Mode -- voice command registrations.

Provides:
  - Wake-phrase entry:  "command mode" + aliases activate AI command mode.
  - Cancel commands:    "cancel that" / "scratch that" clear the queue from
                        any mode (useful for recovery; within AI command mode
                        stop-words are also checked inline in the utterance
                        handler before enqueue).

All entries: ai_visible=False so the AI resolver cannot pick these
meta-commands as actions in a generated plan.
"""
from samsara.plugin_commands import command


@command(
    "command mode",
    aliases=[
        "ai command mode",
        "enter command mode",
        "activate command mode",
        "start command mode",
    ],
    pack="ai",
    ai_visible=False,
)
def handle_enter_ai_command_mode(app, remainder="", **kwargs):
    """Activate AI command mode via voice (same as pressing the toggle key)."""
    if hasattr(app, "enter_ai_command_mode"):
        app.enter_ai_command_mode()
    return True


@command(
    "cancel that",
    aliases=["scratch that", "abort plan", "clear command queue"],
    pack="ai",
    ai_visible=False,
)
def handle_ai_command_cancel(app, remainder="", **kwargs):
    """Clear the AI command queue and discard any pending unsafe-plan confirmation."""
    try:
        from samsara.ai_command_mode import cancel_queue, reset_cancel  # noqa: PLC0415
        cancel_queue()
        reset_cancel()
    except ImportError:
        pass
    return True
