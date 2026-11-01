"""Voice entry points for microphone accuracy comparisons."""
from samsara.plugin_commands import command


@command("test my mic", aliases=["mic score", "which mic is best"], pack="core",
         risk_class="safe", ai_composable=True, side_effects=["ui"])
def test_my_mic(app, remainder=""):
    """Opens a five-sentence microphone comparison."""
    from samsara.ui.mic_score_qt import open_mic_score
    open_mic_score(app)
    return True


@command("how's my mic doing", aliases=["how is my mic doing", "hows my mic doing"],
         pack="core", risk_class="safe", ai_composable=True, side_effects=["audio", "ui"])
def how_is_my_mic(app, remainder=""):
    """Reports microphone accuracy, recognition estimates and command matching results."""
    from samsara.mic_score import current_estimate, current_read_aloud
    estimate = current_estimate(app) or {}
    recognition, matching = estimate.get("recognition"), estimate.get("command_matching")
    measured = current_read_aloud(app)
    accuracy = (f"Read-aloud accuracy measured {measured['accuracy']:.0%}. "
                if measured else "Read-aloud accuracy not measured yet. ")
    audio = (f"Recognition estimate {recognition['score']} out of 10. "
             if recognition else "Not enough data for a recognition estimate. ")
    router = (f"Command matching {matching['match_rate']:.0%}; router, not the mic."
              if matching else "No recent command matching data; this is about the router, not the mic.")
    text = accuracy + audio + router
    show = getattr(app, "_show_outcome_chip", None)
    if callable(show):
        show(text, "success", 3500)
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is not None:
        coordinator.speak(text, category="agent_response")
    return True
