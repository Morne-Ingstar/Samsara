"""Shared routing for explicit voice entry to a hands-free session."""

HANDS_FREE_ENTRY_PHRASES = frozenset({
    "hands free", "hands-free", "start hands free", "hands free mode",
    "dictate mode", "dictation mode", "dictate",
})


def route_wake_hands_free_entry(app, text):
    """Consume a wake command that explicitly starts hands-free mode."""
    from samsara.session_modes import match_whole_utterance
    if not match_whole_utterance(text, HANDS_FREE_ENTRY_PHRASES):
        return False
    from plugins.commands.hands_free_voice import handle_hands_free
    handle_hands_free(app)
    return True


def dispatch_dictate_hands_free_entry(manager, text, signals):
    """Dispatch the hands-free entry command only from a safe DICTATE utterance."""
    from samsara.session_modes import (
        SessionMode, match_whole_utterance, passes_switch_anti_hallucination_gate,
    )
    if (manager.mode is not SessionMode.DICTATE
            or not match_whole_utterance(text, HANDS_FREE_ENTRY_PHRASES)
            or not passes_switch_anti_hallucination_gate(signals)):
        return None
    entry = manager._dispatch_command("hands free", allow_suggestion=False)
    return None if entry.kind == "command_miss" else entry
