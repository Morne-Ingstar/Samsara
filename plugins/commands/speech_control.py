"""Speech playback controls; independent of the session's stop/cancel action."""
from samsara.plugin_commands import command
from samsara.tts.speech_control import stop_speaking


@command("stop speaking", aliases=["quiet", "stop talking", "that's enough"],
         pack="core", risk_class="safe", reversible=True, side_effects=["audio"])
def handle_stop_speaking(app, remainder):
    """Stops Samsara's spoken audio, keeping drafts and pending questions."""  # PLACEHOLDER COPY (1149)
    if remainder.strip():
        return False
    from samsara.audio_engine.speech_stop import dictation_owns_words
    if dictation_owns_words(app):
        return False
    return stop_speaking(app)
